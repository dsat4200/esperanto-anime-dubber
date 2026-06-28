import argparse
import logging
import mimetypes
import os
import re
import subprocess as sp
import sys
import tempfile
import threading
import traceback
import webbrowser
from pathlib import Path

from flask import Flask, request, jsonify, Response, render_template

_log = logging.getLogger("anidub.gui.server")

from anidub.project import AnimeProject, Project, ClipStatus
from anidub.config import get_ffmpeg_location

app = Flask(__name__)
_anime: AnimeProject | None = None
_progress: dict = {}
_jobs: dict = {}
# Live OmniVoice backends, keyed by job name. Allows /api/gpu/clear?force=1
# and /api/cleanup to actually drop model weights from VRAM.
_backends: dict = {}
# Long-lived backend reused by manual single-clip clones so model weights
# aren't reloaded (and stacked) on every click. Lazily created and registered
# under the "__shared__" key in `_backends` so it's visible in the GPU panel.
_shared_backend = None
_shared_backend_lock = threading.Lock()


def _free_vram(ipc: bool = True):
    """Best-effort: synchronize, GC, IPC-collect, empty the allocator cache."""
    import gc
    import torch
    if not torch.cuda.is_available():
        return
    torch.cuda.synchronize()
    gc.collect()
    torch.cuda.synchronize()
    torch.cuda.empty_cache()
    if ipc:
        torch.cuda.ipc_collect()
    torch.cuda.empty_cache()


def _flush_vram():
    """Lightweight: sync + empty_cache only. Safe between clips when the
    shared backend is kept hot (no GC, no ipc_collect). Drops the allocator's
    reserved-but-unallocated pool back to the driver without touching alive
    model weights — prevents the Windows CUDA Unified Memory fallback from
    kicking in.
    """
    import torch
    if not torch.cuda.is_available():
        return
    torch.cuda.synchronize()
    torch.cuda.empty_cache()


def _get_or_create_shared_backend(whisper_model: str = "openai/whisper-tiny"):
    """Return the long-lived OmniVoice backend used by manual clones.

    Lazily created on first call, then reused. Thread-safe so concurrent
    single-clip requests don't stack two backends. Registered in `_backends`
    under `"__shared__"` so the GPU panel and Force-Unload button can see it.
    """
    global _shared_backend
    with _shared_backend_lock:
        existing = _shared_backend
        if existing is not None and getattr(existing, "_model", None) is not None:
            return existing
        from anidub.tts.omnivoice import OmniVoiceTTSBackend
        _shared_backend = OmniVoiceTTSBackend(whisper_model=whisper_model)
        _backends["__shared__"] = _shared_backend
        return _shared_backend


def _unload_shared_backend():
    """Drop the long-lived single-clip backend and reclaim its VRAM."""
    global _shared_backend
    with _shared_backend_lock:
        backend = _shared_backend
        _shared_backend = None
        _backends.pop("__shared__", None)
    if backend is not None:
        try:
            backend.unload()
        except Exception:
            pass
        del backend
        _free_vram()


def _unload_all_backends():
    """Tear down any registered OmniVoice backends + the Whisper cache."""
    global _shared_backend
    # Drop the long-lived shared backend (single-clip path) first.
    if _shared_backend is not None:
        _unload_shared_backend()
    if _backends:
        for key in list(_backends.keys()):
            backend = _backends.pop(key, None)
            if backend is not None:
                try:
                    backend.unload()
                except Exception:
                    pass
    from anidub.asr import clear_whisper_cache
    clear_whisper_cache()
    _free_vram()


def _ensure_gpu_memory():
    """Proactively reclaim reserved VRAM before launching a GPU-bound task.

    Uses ``memory_reserved`` (the caching allocator's held pool) rather than
    ``memory_allocated`` (live tensors) — the reserved pool is what
    ``empty_cache`` can actually give back. Sync + GC first so finalized
    tensors are reclaimed.
    """
    import gc
    import torch
    if not torch.cuda.is_available():
        return
    total = torch.cuda.get_device_properties(0).total_memory
    reserved = torch.cuda.memory_reserved()
    torch.cuda.synchronize()
    gc.collect()
    torch.cuda.synchronize()
    torch.cuda.empty_cache()
    if reserved / total > 0.5:
        torch.cuda.ipc_collect()
        torch.cuda.empty_cache()


@app.route("/api/gpu")
def api_gpu():
    import torch
    if not torch.cuda.is_available():
        return jsonify({"available": False})
    total = torch.cuda.get_device_properties(0).total_memory / 1024**2
    allocated = torch.cuda.memory_allocated() / 1024**2
    reserved = torch.cuda.memory_reserved() / 1024**2
    # Reserved is what the driver sees as "held by this process"; use it for
    # the indicator so freeing the cache actually shows a drop.
    return jsonify({
        "available": True,
        "device": torch.cuda.get_device_name(0),
        "total_mb": round(total, 1),
        "allocated_mb": round(allocated, 1),
        "reserved_mb": round(reserved, 1),
        "pct_used": round(reserved / total * 100, 1),
        "pct_allocated": round(allocated / total * 100, 1),
        "live_backends": list(_backends.keys()),
    })


@app.route("/api/gpu/clear", methods=["POST"])
def api_gpu_clear():
    import torch
    force = request.args.get("force", "").lower() in ("1", "true", "yes")
    if force:
        _unload_all_backends()
    elif torch.cuda.is_available():
        _free_vram()
    return jsonify({
        "ok": True,
        "force": force,
        "live_backends": list(_backends.keys()),
    })


def _require_anime():
    global _anime
    if _anime is None:
        return jsonify({"error": "No project loaded. Open from anime folder or load existing project."}), 400
    return None


def _require_project():
    global _anime
    err = _require_anime()
    if err: return err
    proj = _anime.get_active_project()
    if proj is None:
        return jsonify({"error": "No episode selected."}), 400
    return None


# ── Page ──────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html", project_path="")


# ── Discovery ─────────────────────────────────

@app.route("/api/projects")
def api_projects():
    return jsonify(AnimeProject.discover())


# ── Project lifecycle ─────────────────────────

@app.route("/api/open", methods=["POST"])
def api_open():
    global _anime
    data = request.get_json(silent=True) or {}
    anime_name = data.get("anime")
    proj_dir = data.get("project_dir")

    if anime_name:
        from anidub.config import ANIME_ROOT
        anime_dir = ANIME_ROOT / anime_name
        if not anime_dir.is_dir():
            return jsonify({"error": f"Anime folder not found: {anime_dir}"}), 404
        _anime = AnimeProject.create(anime_name, anime_dir)
    elif proj_dir:
        _anime = AnimeProject.load(Path(proj_dir))
    else:
        return jsonify({"error": "Need either 'anime' (folder name under anime/) or 'project_dir'"}), 400
    return jsonify({"path": str(_anime.path), "anime_name": _anime.state.get("anime_name", "")})


@app.route("/api/save", methods=["POST"])
def api_save():
    err = _require_anime()
    if err: return err
    _anime.save()
    proj = _anime.get_active_project()
    if proj:
        proj.save()
    return jsonify({"ok": True})


# ── Episodes ──────────────────────────────────

@app.route("/api/episodes")
def api_episodes():
    err = _require_anime()
    if err: return err
    eps = _anime.get_episodes()
    active = _anime._active_stem
    return jsonify({"episodes": eps, "active_stem": active, "anime_name": _anime.state.get("anime_name", "")})


@app.route("/api/episodes/select", methods=["POST"])
def api_episode_select():
    err = _require_anime()
    if err: return err
    data = request.get_json(silent=True) or {}
    stem = data.get("stem", "")
    if not stem:
        return jsonify({"error": "Need 'stem'"}), 400
    try:
        _anime.select_episode(stem)
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    return jsonify({"ok": True})


@app.route("/api/episodes/<stem>/title", methods=["POST"])
def api_episode_title(stem):
    err = _require_anime()
    if err: return err
    data = request.get_json(silent=True) or {}
    _anime.set_episode_title(stem, data.get("title"))
    return jsonify({"ok": True})


@app.route("/api/episodes/<stem>/complete", methods=["POST"])
def api_episode_complete(stem):
    err = _require_anime()
    if err: return err
    data = request.get_json(silent=True) or {}
    _anime.mark_episode_complete(stem, data.get("completed", True))
    return jsonify({"ok": True})


@app.route("/api/episodes/batch-translate", methods=["POST"])
def api_batch_translate():
    err = _require_anime()
    if err: return err
    data = request.get_json(silent=True) or {}
    stems = data.get("stems", [])
    audio_idx = data.get("audio_idx", 0)
    sub_idx = data.get("sub_idx", 0)
    key = "batch-translate"

    info = _anime.process_batch_episodes(stems, audio_idx, sub_idx, key, None)
    valid = info["valid"]
    skipped = info["skipped"]

    def _run():
        _jobs[key] = {"running": True, "cancel": False, "type": "batch-translate", "message": "Starting..."}
        total_translated = 0
        failed = []
        _progress[key] = {
            "done": False, "phase": "init",
            "episode_current": 0, "episode_total": len(valid),
            "episode_name": "", "clip_current": 0, "clip_total": 0,
            "clip_text": "", "total_translated": 0,
            "message": "Initializing...", "skipped": skipped, "failed": [],
        }
        try:
            for ep_idx, stem in enumerate(valid):
                if _jobs[key].get("cancel"):
                    break
                try:
                    _progress[key]["phase"] = "init"
                    _progress[key]["episode_current"] = ep_idx + 1
                    _progress[key]["episode_name"] = stem
                    _progress[key]["message"] = f"Loading {stem}..."
                    _jobs[key]["message"] = f"Loading {stem}..."

                    proj = _anime.select_episode(stem)
                    proj._init_timeline(force=False)
                    proj.select_audio_track(audio_idx)
                    proj.select_subtitle_track(sub_idx)

                    if not proj.state.get("demucs_done"):
                        _progress[key]["phase"] = "demucs"
                        _progress[key]["message"] = f"Demucs: {stem}..."
                        _jobs[key]["message"] = f"Demucs: {stem}..."
                        proj.run_demucs()

                    order = proj.state.get("order", [])
                    pending = []
                    for cid in order:
                        clip = proj.get_clip(cid)
                        if clip and clip.status in (ClipStatus.PENDING, ClipStatus.REJECTED):
                            pending.append(cid)

                    _progress[key]["phase"] = "translating"
                    _progress[key]["clip_total"] = len(pending)
                    _progress[key]["clip_current"] = 0
                    _jobs[key]["message"] = f"{stem} ({ep_idx+1}/{len(valid)})"

                    for cl_idx, cid in enumerate(pending):
                        if _jobs[key].get("cancel"):
                            break
                        clip = proj.get_clip(cid)
                        _progress[key]["clip_current"] = cl_idx + 1
                        _progress[key]["clip_text"] = (clip.original_text or "")[:80]
                        _progress[key]["message"] = f"{stem}: {cl_idx+1}/{len(pending)}"
                        _jobs[key]["message"] = f"{stem}: {cl_idx+1}/{len(pending)}"
                        try:
                            proj.translate_clip(cid)
                            total_translated += 1
                        except Exception:
                            pass
                        _progress[key]["total_translated"] = total_translated

                except Exception as e:
                    failed.append({"stem": stem, "error": str(e)})
                _progress[key]["failed"] = failed

            _progress[key]["done"] = True
            _progress[key]["phase"] = "done"
            _progress[key]["message"] = f"Done: {total_translated} lines in {len(valid)} episodes"
        finally:
            _jobs[key]["running"] = False
            _jobs[key]["message"] = "Done"

    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"started": True, "total": len(valid), "skipped": skipped})


@app.route("/api/episodes/batch-clone", methods=["POST"])
def api_batch_clone():
    err = _require_anime()
    if err: return err
    data = request.get_json(silent=True) or {}
    stems = data.get("stems", [])
    audio_idx = data.get("audio_idx", 0)
    sub_idx = data.get("sub_idx", 0)
    key = "batch-clone"

    info = _anime.process_batch_episodes(stems, audio_idx, sub_idx, key, None)
    valid = info["valid"]
    skipped = info["skipped"]

    def _run():
        from anidub.tts.omnivoice import OmniVoiceTTSBackend
        import torch
        whisper_model = "openai/whisper-tiny"
        backend = None
        processed = 0
        failed = []
        _jobs[key] = {"running": True, "cancel": False, "type": "batch-clone", "message": "Loading TTS model..."}
        try:
            backend = OmniVoiceTTSBackend(whisper_model=whisper_model)
            _backends[key] = backend
            _jobs[key]["message"] = "Cloning..."
            _progress[key] = {"current": 0, "total": len(valid), "done": False, "message": "Cloning..."}
            _ensure_gpu_memory()
            for stem in valid:
                if _jobs[key].get("cancel"):
                    break
                try:
                    proj = _anime.select_episode(stem)
                    proj._init_timeline(force=False)
                    proj.select_audio_track(audio_idx)
                    proj.select_subtitle_track(sub_idx)
                    if not proj.state.get("demucs_done"):
                        proj.run_demucs()
                    proj.clone_range(backend=backend)
                    processed += 1
                except Exception as e:
                    failed.append({"stem": stem, "error": str(e)})
                _progress[key]["current"] = processed
                _progress[key]["message"] = f"Cloned {processed}/{len(valid)} episodes"
                _jobs[key]["message"] = f"Cloned {processed}/{len(valid)} episodes"
                if failed:
                    _progress[key]["failed"] = failed
                _free_vram(ipc=False)
            _progress[key]["done"] = True
            _progress[key]["message"] = f"Cloned {processed}/{len(valid)} episodes"
        finally:
            _jobs[key]["running"] = False
            _jobs[key]["message"] = "Done"
            backend = _backends.pop(key, None)
            if backend is not None:
                try:
                    backend.unload()
                except Exception:
                    pass
                del backend
            _free_vram()

    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"started": True, "total": len(valid), "skipped": skipped})


@app.route("/api/episodes/batch-translate/progress")
def api_batch_translate_progress():
    return jsonify(_progress.get("batch-translate", {"current": 0, "total": 0, "done": True, "message": "", "skipped": []}))


@app.route("/api/episodes/batch-clone/progress")
def api_batch_clone_progress():
    return jsonify(_progress.get("batch-clone", {"current": 0, "total": 0, "done": True, "message": "", "skipped": []}))


@app.route("/api/batch-jobs")
def api_batch_jobs():
    result = {}
    for key in ("batch-translate", "batch-clone"):
        job = _jobs.get(key)
        if job and job.get("running"):
            result[key] = {
                "running": True,
                "type": job.get("type", key),
                "message": job.get("message", ""),
                "progress": _progress.get(key, {}),
            }
    return jsonify(result)


# ── Setup (per episode) ───────────────────────

@app.route("/api/tracks")
def api_tracks():
    err = _require_project()
    if err: return err
    proj = _anime.get_active_project()
    source = proj.state.get("source", {})
    subtitle_tracks = proj.get_subtitle_tracks()
    return jsonify({
        "audio": proj.get_audio_tracks(),
        "subtitle": subtitle_tracks,
        "subtitle_none": len(subtitle_tracks) == 0,
        "selected_audio_idx": source.get("audio_track_rel", -1),
        "selected_sub_idx": source.get("subtitle_track_rel", -1),
        "demucs_done": proj.state.get("demucs_done", False),
        "tracks_confirmed": proj.state.get("tracks_confirmed", False),
    })


@app.route("/api/audio-track", methods=["POST"])
def api_audio_track():
    err = _require_project()
    if err: return err
    data = request.get_json(silent=True) or {}
    _anime.get_active_project().select_audio_track(data.get("index", 0))
    return jsonify({"ok": True})


@app.route("/api/sub-track", methods=["POST"])
def api_sub_track():
    err = _require_project()
    if err: return err
    data = request.get_json(silent=True) or {}
    _anime.get_active_project().select_subtitle_track(data.get("index", 0))
    return jsonify({"ok": True})


@app.route("/api/tracks/confirm", methods=["POST"])
def api_tracks_confirm():
    err = _require_project()
    if err: return err
    data = request.get_json(silent=True) or {}
    proj = _anime.get_active_project()
    audio_idx = data.get("audio_idx")
    sub_idx = data.get("sub_idx")
    if audio_idx is not None:
        proj.select_audio_track(int(audio_idx))
    if sub_idx is not None:
        proj.select_subtitle_track(int(sub_idx))
    proj.state["tracks_confirmed"] = True
    proj.save()
    return jsonify({"ok": True, "tracks_confirmed": True})


@app.route("/api/transcribe", methods=["POST"])
def api_transcribe():
    err = _require_project()
    if err: return err
    data = request.get_json(silent=True) or {}
    proj = _anime.get_active_project()
    audio_idx = data.get("audio_idx", 0)
    model_name = data.get("model", "openai/whisper-large-v3-turbo")
    language = data.get("language") or None

    key = "transcribe"
    _progress[key] = {"done": False, "message": "Starting..."}

    def _run():
        try:
            _progress[key]["message"] = "Transcribing..."
            audio_tracks = proj.get_audio_tracks()
            if audio_idx >= len(audio_tracks):
                raise IndexError(f"Audio index {audio_idx} out of range")
            audio_path = proj._abs(audio_tracks[audio_idx]["path"])
            out_ass = proj.path / "subtitle_track_generated.ass"

            from anidub.transcribe import transcribe_full_audio
            seg_count = transcribe_full_audio(
                audio_path=audio_path,
                out_ass_path=out_ass,
                model_name=model_name,
                language=language,
            )
            lang_tag = language or "auto"
            proj.select_audio_track(int(audio_idx))
            proj.add_subtitle_track(out_ass, language=lang_tag, title=f"Whisper ({model_name.split('/')[-1]})")
            _progress[key] = {"done": True, "message": f"Done: {seg_count} segments"}
        except Exception as e:
            _log.exception("Transcription failed")
            _progress[key] = {"done": True, "message": f"Failed: {e}"}

    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"started": True})


@app.route("/api/transcribe/progress")
def api_transcribe_progress():
    p = _progress.get("transcribe", {"done": True, "message": ""})
    return jsonify(p)


@app.route("/api/demucs", methods=["POST"])
def api_demucs():
    err = _require_project()
    if err: return err
    nv, v = _anime.get_active_project().run_demucs()
    return jsonify({"ok": True, "no_vocals": str(nv), "vocals": str(v)})


# ── Timeline ──────────────────────────────────

@app.route("/api/timeline")
def api_timeline():
    err = _require_project()
    if err: return err
    proj = _anime.get_active_project()
    clips = proj.get_timeline_clips()
    return jsonify([
        {
            "clip_id": c["clip_id"],
            "start_sec": c["start_sec"],
            "end_sec": c["end_sec"],
            "duration": c["end_sec"] - c["start_sec"],
            "audio_offset_ms": c.get("audio_offset_ms", 0.0),
            "status": c.get("status", "pending"),
            "original_text": c["original_text"],
            "translated_text": c.get("translated_text"),
            "character": c.get("character"),
        }
        for c in clips
    ])


# ── Clip CRUD ─────────────────────────────────

def _clip_to_dict(clip):
    proj = _anime.get_active_project()
    d = {
        "clip_id": clip.clip_id,
        "start_sec": clip.start_sec,
        "end_sec": clip.end_sec,
        "original_text": clip.original_text,
        "translated_text": clip.translated_text,
        "audio_offset_ms": clip.audio_offset_ms,
        "character": clip.character,
        "character_mood": clip.character_mood,
        "ref_source": clip.ref_source.value,
        "ref_clip": clip.ref_clip,
        "status": clip.status.value,
        "clone_path": clip.clone_path,
        "clone_ms": clip.clone_ms,
        "attempts": clip.attempts,
        "instruct_extra": clip.instruct_extra,
        "speed_factor": clip.speed_factor,
        "pronunciation_override": clip.pronunciation_override,
        "can_clone": clip.status not in (ClipStatus.NON_DUB, ClipStatus.SIGN),
    }
    d["needs_processing"] = proj.needs_processing(clip.clip_id)
    return d


@app.route("/api/clips/<clip_id>")
def api_get_clip(clip_id):
    err = _require_project()
    if err: return err
    proj = _anime.get_active_project()
    clip = proj.get_clip(clip_id)
    if not clip:
        return jsonify({"error": f"Clip {clip_id} not found"}), 404
    return jsonify(_clip_to_dict(clip))


@app.route("/api/clips/current")
def api_current_clip():
    err = _require_project()
    if err: return err
    proj = _anime.get_active_project()
    clip = proj.get_current_clip()
    if not clip:
        order = proj.state.get("order", [])
        if order:
            clip = proj.get_clip(order[0])
    if not clip:
        return jsonify({"error": "No clips"}), 404
    return jsonify(_clip_to_dict(clip))


@app.route("/api/clips/<clip_id>/translate", methods=["POST"])
def api_translate(clip_id):
    err = _require_project()
    if err: return err
    data = request.get_json(silent=True) or {}
    override = data.get("text_override")
    proj = _anime.get_active_project()
    text = proj.translate_clip(clip_id, text_override=override if override else None)
    return jsonify({"translated_text": text})


@app.route("/api/clips/<clip_id>/clone", methods=["POST"])
def api_clone(clip_id):
    err = _require_project()
    if err: return err
    data = request.get_json(silent=True) or {}
    character = data.get("character") or None
    mood = data.get("mood", "normal")
    proj = _anime.get_active_project()

    import torch

    # If VRAM is already above 75% (e.g. another process filled it, or a
    # previous clip leaked intermediates), drop the shared backend before we
    # (re)create one so we don't overflow into Windows shared RAM.
    if torch.cuda.is_available():
        total = torch.cuda.get_device_properties(0).total_memory
        reserved = torch.cuda.memory_reserved()
        if total > 0 and reserved / total > 0.75:
            _unload_shared_backend()

    backend = _get_or_create_shared_backend()
    try:
        result = proj.clone_clip(clip_id, character=character,
                                 mood=mood, backend=backend)
    except Exception as e:
        # A timed-out generation leaves the CUDA stream dirty and the model
        # potentially wedged — drop it so the next click rebuilds clean.
        if "timed out" in str(e):
            _unload_shared_backend()
        raise
    finally:
        # Light cleanup: drop the allocator's free pool back to the driver
        # without GC-ing the still-hot shared model.
        _flush_vram()

    return jsonify({
        "inference_ms": result.get("inference_ms"),
        "output_duration": result.get("output_duration"),
    })


@app.route("/api/clips/<clip_id>/preview", methods=["POST"])
def api_preview(clip_id):
    err = _require_project()
    if err: return err
    _anime.get_active_project().preview_clip(clip_id)
    return jsonify({"url": f"/preview/{clip_id}.mp4"})


@app.route("/api/clips/<clip_id>/audio-offset", methods=["POST"])
def api_audio_offset(clip_id):
    err = _require_project()
    if err: return err
    data = request.get_json(silent=True) or {}
    _anime.get_active_project().set_audio_offset(clip_id, data.get("offset_ms", 0.0))
    return jsonify({"ok": True})


@app.route("/api/clips/<clip_id>/character", methods=["POST"])
def api_character(clip_id):
    err = _require_project()
    if err: return err
    data = request.get_json(silent=True) or {}
    char = data.get("character") or None
    mood = data.get("mood", "normal")
    _anime.get_active_project().set_clip_character(clip_id, char, mood)
    return jsonify({"ok": True})


@app.route("/api/clips/<clip_id>/instruct", methods=["POST"])
def api_instruct(clip_id):
    err = _require_project()
    if err: return err
    data = request.get_json(silent=True) or {}
    extra = data.get("instruct_extra") or None
    _anime.get_active_project().set_instruct_extra(clip_id, extra)
    return jsonify({"ok": True})


@app.route("/api/clips/<clip_id>/process", methods=["POST"])
def api_process(clip_id):
    err = _require_project()
    if err: return err
    data = request.get_json(silent=True) or {}
    character = data.get("character") or None
    mood = data.get("mood", "normal")
    proj = _anime.get_active_project()
    result = proj.process_clip(clip_id, character=character, mood=mood)
    return jsonify(result)


@app.route("/api/clips/<clip_id>/speed", methods=["POST"])
def api_speed(clip_id):
    err = _require_project()
    if err: return err
    data = request.get_json(silent=True) or {}
    _anime.get_active_project().set_clip_speed(clip_id, data.get("speed_factor", 1.0))
    return jsonify({"ok": True})


@app.route("/api/clips/<clip_id>/pronunciation", methods=["POST"])
def api_pronunciation(clip_id):
    err = _require_project()
    if err: return err
    data = request.get_json(silent=True) or {}
    _anime.get_active_project().set_clip_pronunciation(clip_id, data.get("pronunciation_override") or None)
    return jsonify({"ok": True})


@app.route("/api/clips/<clip_id>/accept", methods=["POST"])
def api_accept(clip_id):
    err = _require_project()
    if err: return err
    proj = _anime.get_active_project()
    proj.accept_clip(clip_id)
    return jsonify({"ok": True})


@app.route("/api/clips/<clip_id>/reject", methods=["POST"])
def api_reject(clip_id):
    err = _require_project()
    if err: return err
    _anime.get_active_project().reject_clip(clip_id)
    return jsonify({"ok": True})


@app.route("/api/clips/<clip_id>/reset", methods=["POST"])
def api_reset(clip_id):
    err = _require_project()
    if err: return err
    _anime.get_active_project().reset_clip(clip_id)
    return jsonify({"ok": True})


# ── Timeline editing ──────────────────────────

@app.route("/api/clips/<clip_id>/resize", methods=["POST"])
def api_resize(clip_id):
    err = _require_project()
    if err: return err
    data = request.get_json(silent=True) or {}
    proj = _anime.get_active_project()
    proj.resize_clip(clip_id, data.get("start_sec", 0.0), data.get("end_sec", 0.0))
    return jsonify({"ok": True})


@app.route("/api/clips/create", methods=["POST"])
def api_create_clip():
    err = _require_project()
    if err: return err
    data = request.get_json(silent=True) or {}
    proj = _anime.get_active_project()
    cid = proj.create_clip(
        float(data.get("start_sec", 0)),
        float(data.get("end_sec", 0)),
        data.get("text", ""),
    )
    return jsonify({"clip_id": cid})


@app.route("/api/clips/<clip_id>/split", methods=["POST"])
def api_split_clip(clip_id):
    err = _require_project()
    if err: return err
    data = request.get_json(silent=True) or {}
    proj = _anime.get_active_project()
    try:
        new_id = proj.split_clip(clip_id, float(data.get("split_time", 0)))
        return jsonify({"clip_id": new_id})
    except ValueError as e:
        return jsonify({"error": str(e)}), 400


@app.route("/api/clips/restore", methods=["POST"])
def api_restore_clip():
    err = _require_project()
    if err: return err
    data = request.get_json(silent=True) or {}
    entry = data.get("entry")
    if not entry or not entry.get("clip_id"):
        return jsonify({"error": "Missing clip entry"}), 400
    _anime.get_active_project().restore_clip(entry)
    return jsonify({"ok": True})


@app.route("/api/clips/<clip_id>/original-text", methods=["POST"])
def api_original_text(clip_id):
    err = _require_project()
    if err: return err
    data = request.get_json(silent=True) or {}
    text = data.get("text", "")
    _anime.get_active_project().set_original_text(clip_id, text)
    return jsonify({"ok": True})


@app.route("/api/clips/<clip_id>/delete", methods=["POST"])
def api_delete(clip_id):
    err = _require_project()
    if err: return err
    _anime.get_active_project().delete_clip(clip_id)
    return jsonify({"ok": True})


@app.route("/api/clips/<clip_id>/status", methods=["POST"])
def api_clip_status(clip_id):
    err = _require_project()
    if err: return err
    data = request.get_json(silent=True) or {}
    _anime.get_active_project().set_clip_status(clip_id, data.get("status", "pending"))
    return jsonify({"ok": True})


# ── Bulk ──────────────────────────────────────

@app.route("/api/translate-all", methods=["POST"])
def api_translate_all():
    err = _require_project()
    if err: return err
    proj = _anime.get_active_project()
    order = list(proj.state.get("order", []))
    total = len(order)
    key = "translate-all"

    def _run():
        _jobs[key] = {"running": True, "cancel": False, "type": "translate-all", "message": "Translating..."}
        try:
            processed = 0
            _progress[key] = {"current": 0, "total": total, "done": False, "message": "Translating..."}
            for idx, cid in enumerate(order):
                if _jobs[key].get("cancel"):
                    break
                clip = proj.get_clip(cid)
                if clip and clip.status in (ClipStatus.PENDING, ClipStatus.REJECTED):
                    try:
                        proj.translate_clip(cid)
                        processed += 1
                    except Exception:
                        pass
                _progress[key]["current"] = idx + 1
                _progress[key]["message"] = f"Translating clip {idx + 1}/{total}"
                _jobs[key]["message"] = f"Translating clip {idx + 1}/{total}"
            _progress[key]["done"] = True
            _progress[key]["message"] = f"Translated {processed}/{total}"
        finally:
            _jobs[key]["running"] = False
            _jobs[key]["message"] = "Done"

    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"started": True, "total": total})


@app.route("/api/translate-all/progress")
def api_translate_progress():
    return jsonify(_progress.get("translate-all", {"current": 0, "total": 0, "done": True, "message": ""}))


@app.route("/api/clone-all", methods=["POST"])
def api_clone_all():
    err = _require_project()
    if err: return err
    proj = _anime.get_active_project()
    order = list(proj.state.get("order", []))
    total = len(order)
    key = "clone-all"

    def _run():
        from anidub.tts.omnivoice import OmniVoiceTTSBackend
        import torch
        import time

        whisper_model = "openai/whisper-tiny"
        backend = None
        _jobs[key] = {"running": True, "cancel": False, "type": "clone-all", "message": "Loading TTS model..."}
        try:
            backend = OmniVoiceTTSBackend(whisper_model=whisper_model)
            _backends[key] = backend
            processed = 0
            _progress[key] = {"current": 0, "total": total, "done": False, "message": "Cloning..."}
            _jobs[key]["message"] = "Cloning..."
            _ensure_gpu_memory()
            for idx, cid in enumerate(order):
                if _jobs[key].get("cancel"):
                    break
                clip = proj.get_clip(cid)
                if clip and clip.status in (ClipStatus.TRANSLATED, ClipStatus.CLONED, ClipStatus.REJECTED):
                    if clip.status == ClipStatus.REJECTED and not clip.translated_text:
                        pass
                    else:
                        try:
                            proj.clone_clip(cid, character=clip.character,
                                            mood=clip.character_mood or "normal", backend=backend)
                            processed += 1
                        except Exception as e:
                            if "timed out" in str(e):
                                _progress[key]["message"] = f"Timeout on clip {cid} — recreating TTS backend..."
                                time.sleep(30)
                                old = _backends.pop(key, None)
                                if old is not None:
                                    try:
                                        old.unload()
                                    except Exception:
                                        pass
                                    del old
                                _free_vram()
                                backend = OmniVoiceTTSBackend(whisper_model=whisper_model)
                                _backends[key] = backend
                _progress[key]["current"] = idx + 1
                _progress[key]["message"] = f"Cloning clip {idx + 1}/{total}"
                _jobs[key]["message"] = f"Cloning clip {idx + 1}/{total}"
                _free_vram(ipc=False)
            _progress[key]["done"] = True
            _progress[key]["message"] = f"Cloned {processed}/{total}"
        finally:
            _jobs[key]["running"] = False
            _jobs[key]["message"] = "Done"
            backend = _backends.pop(key, None)
            if backend is not None:
                try:
                    backend.unload()
                except Exception:
                    pass
                del backend
            _free_vram()

    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"started": True, "total": total})


@app.route("/api/clone-all/progress")
def api_clone_progress():
    return jsonify(_progress.get("clone-all", {"current": 0, "total": 0, "done": True, "message": ""}))


# ── Characters (shared across episodes) ───────

@app.route("/api/characters")
def api_characters():
    err = _require_anime()
    if err: return err
    result = {}
    for name, moods in _anime.get_all_character_clips().items():
        result[name] = {m: str(p) for m, p in moods.items()}
    return jsonify(result)


@app.route("/api/characters", methods=["POST"])
def api_characters_save():
    err = _require_project()
    if err: return err
    data = request.get_json(silent=True) or {}
    name = data.get("name", "")
    mood = data.get("mood", "normal")
    clip_id = data.get("clip_id")
    if not clip_id:
        return jsonify({"error": "clip_id required"}), 400
    proj = _anime.get_active_project()
    src = proj.path / "lines" / clip_id / "ref.wav"
    if not src.exists():
        return jsonify({"error": f"ref.wav not found for clip {clip_id}"}), 404
    dst = _anime.save_character_clip(name, src, mood)
    return jsonify({"path": str(dst)})


@app.route("/api/characters/<name>/<mood>", methods=["DELETE"])
def api_characters_delete(name, mood):
    err = _require_anime()
    if err: return err
    _anime.delete_character_clip(name, mood)
    return jsonify({"ok": True})


# ── Preview ───────────────────────────────────

def _send_file_range(path: str, mimetype: str):
    file_size = os.path.getsize(path)
    range_header = request.headers.get("Range")
    if range_header:
        m = re.match(r"bytes=(\d+)-(\d*)", range_header)
        if m:
            start = int(m.group(1))
            end = int(m.group(2)) if m.group(2) else file_size - 1
            end = min(end, file_size - 1)
            length = end - start + 1
            with open(path, "rb") as f:
                f.seek(start)
                data = f.read(length)
            return Response(
                data, 206, mimetype=mimetype,
                headers={
                    "Content-Range": f"bytes {start}-{end}/{file_size}",
                    "Accept-Ranges": "bytes",
                    "Content-Length": str(length),
                    "Cache-Control": "no-cache",
                },
                direct_passthrough=True,
            )
    with open(path, "rb") as f:
        return Response(f.read(), 200, mimetype=mimetype,
                        headers={"Accept-Ranges": "bytes",
                                 "Content-Length": str(file_size),
                                 "Cache-Control": "no-cache"})


@app.route("/api/preview-sample", methods=["POST"])
def api_preview_sample():
    err = _require_project()
    if err: return err
    data = request.get_json(silent=True) or {}
    sample_type = data.get("type", "audio")
    index = data.get("index", 0)
    proj = _anime.get_active_project()
    ffmpeg = str(Path(get_ffmpeg_location()) / "ffmpeg.exe")

    if sample_type == "audio":
        tracks = proj.get_audio_tracks()
        if index >= len(tracks):
            return jsonify({"error": f"Audio track {index} not found"}), 404
        src = proj._abs(tracks[index]["path"])
        if not src.exists():
            return jsonify({"error": "Audio file not found"}), 404
        tmp = tempfile.NamedTemporaryFile(suffix=".mp3", delete=False)
        tmp.close()
        sp.run([ffmpeg, "-y", "-loglevel", "error",
                "-ss", "30", "-t", "5", "-i", str(src),
                "-c:a", "libmp3lame", "-q:a", "5", tmp.name], check=True)
        return _send_file_range(tmp.name, "audio/mpeg")

    elif sample_type == "sub":
        tracks = proj.get_subtitle_tracks()
        if index >= len(tracks):
            return jsonify({"error": f"Sub track {index} not found"}), 404
        src = proj._abs(tracks[index]["path"])
        if not src.exists():
            return jsonify({"error": "Subtitle file not found"}), 404
        from anidub.ass import parse_ass
        events = parse_ass(src)
        dialogue = [e for e in events if e.get("style", "").lower() not in ("op", "ed")]
        if not dialogue:
            return jsonify({"error": "No dialogue lines"}), 404
        first = dialogue[0]
        start = first["start_sec"]
        dur = min(first["end_sec"] - start, 10.0)
        tmp = tempfile.NamedTemporaryFile(suffix=".mp4", delete=False)
        tmp.close()
        video_src = proj._abs(proj.state.get("video_only", "video_only.mkv"))
        ass_safe = str(src).replace("\\", "/")
        sp.run([ffmpeg, "-y", "-loglevel", "error",
                "-ss", f"{start:.3f}", "-t", f"{dur:.3f}",
                "-i", str(video_src),
                "-filter_complex", f"[0:v]ass={ass_safe}[subbed]",
                "-map", "[subbed]", "-an",
                "-c:v", "libx264", "-preset", "ultrafast", "-crf", "23",
                "-movflags", "+faststart", tmp.name], check=True)
        return _send_file_range(tmp.name, "video/mp4")

    return jsonify({"error": f"Unknown type: {sample_type}"}), 400


# ── Auto-play (CPU-only draft playback) ────────

@app.route("/api/playback/plan")
def api_playback_plan():
    err = _require_project()
    if err: return err
    from anidub.playback import get_playback_plan
    return jsonify(get_playback_plan(_anime.get_active_project()))


@app.route("/api/playback/segment")
def api_playback_segment():
    err = _require_project()
    if err: return err
    proj = _anime.get_active_project()
    clip_id = request.args.get("clip_id")
    start = request.args.get("start")
    end = request.args.get("end")

    # ── OP / ED placeholders ──
    if clip_id in ("__op__", "__ed__"):
        pb = proj.path / "_playback" / (clip_id[2:] + ".mp4")
        if pb.exists() and pb.stat().st_size > 1024:
            return _send_file_range(str(pb), "video/mp4")
        range_key = "op_range" if clip_id == "__op__" else "ed_range"
        op_ed_start, op_ed_end = proj.state.get(range_key, (0.0, 0.0))
        if op_ed_end > op_ed_start:
            start = str(op_ed_start)
            end = str(op_ed_end)

    # ── cloned clip with existing playback.mp4 ──
    if clip_id:
        playback_mp4 = proj.path / "lines" / clip_id / "playback.mp4"
        if playback_mp4.exists() and playback_mp4.stat().st_size > 1024:
            return _send_file_range(str(playback_mp4), "video/mp4")

    # ── known time range → slice video_only + original audio ──
    clip_start = start
    clip_end = end
    if clip_id:
        clip = proj.get_clip(clip_id)
        if clip:
            clip_start = clip.start_sec
            clip_end = clip.end_sec

    if clip_start is None or clip_end is None:
        return jsonify({"error": "Need clip_id or start+end"}), 400

    start_s = float(clip_start)
    end_s = float(clip_end)
    dur = end_s - start_s
    ffmpeg = str(Path(get_ffmpeg_location()) / "ffmpeg.exe")
    video_src = proj._abs(proj.state.get("video_only", "video_only.mkv"))
    audio = proj._audio_path()

    tmp = tempfile.NamedTemporaryFile(suffix=".mp4", delete=False)
    tmp.close()

    try:
        if audio and audio.exists():
            sp.run([ffmpeg, "-y", "-loglevel", "error",
                    "-ss", f"{start_s:.3f}", "-t", f"{dur:.3f}",
                    "-i", str(video_src),
                    "-ss", f"{start_s:.3f}", "-t", f"{dur:.3f}",
                    "-i", str(audio),
                    "-c:v", "libx264", "-preset", "ultrafast", "-crf", "23",
                    "-c:a", "aac", "-b:a", "128k",
                    "-movflags", "+faststart", "-shortest", tmp.name], check=True)
        else:
            sp.run([ffmpeg, "-y", "-loglevel", "error",
                    "-ss", f"{start_s:.3f}", "-t", f"{dur:.3f}",
                    "-i", str(video_src),
                    "-c:v", "libx264", "-preset", "ultrafast", "-crf", "23",
                    "-movflags", "+faststart", tmp.name], check=True)
        return _send_file_range(tmp.name, "video/mp4")
    except Exception as e:
        _log.error("playback render failed: %s", e)
        return jsonify({"error": str(e)}), 500


@app.route("/api/playback/generate-previews", methods=["POST"])
def api_playback_generate_previews():
    err = _require_project()
    if err: return err
    proj = _anime.get_active_project()
    order = list(proj.state.get("order", []))
    op_s, op_e = proj.state.get("op_range", (0.0, 0.0))
    ed_s, ed_e = proj.state.get("ed_range", (0.0, 0.0))

    # Count OP/ED as extra items
    extra = 0
    if op_e > op_s:
        extra += 1
    if ed_e > ed_s:
        extra += 1
    total = len(order) + extra
    key = "playback-previews"

    def _run():
        processed = 0
        failed: list[dict] = []
        try:
            _jobs[key] = {"running": True, "cancel": False, "type": "playback-previews",
                          "message": "Generating previews..."}
            _progress[key] = {"current": 0, "total": total, "done": False,
                              "message": "Generating...", "failed": []}

            # ── OP as one full segment ──
            if op_e > op_s:
                _progress[key]["message"] = "OP..."
                _jobs[key]["message"] = "OP..."
                try:
                    op_dir = proj.path / "_playback"
                    op_dir.mkdir(parents=True, exist_ok=True)
                    from anidub.assembler import _slice_audio
                    from anidub.extract import extract_video_clip
                    from anidub.assembler import _mux_playback
                    video_src = proj._abs(proj.state.get("video_only", "video_only.mkv"))
                    op_dur = op_e - op_s
                    audio_src = proj._audio_path()
                    audio_clip = op_dir / "_op_audio.wav"
                    _slice_audio(audio_src, op_s, op_dur, audio_clip)
                    op_video = op_dir / "_op_video.mkv"
                    extract_video_clip(video_src, op_s, op_e, op_video)
                    op_out = op_dir / "op.mp4"
                    _mux_playback(op_video, audio_clip, op_out)
                    processed += 1
                except Exception as e:
                    failed.append({"clip_id": "__op__", "error": str(e)})
                _progress[key]["current"] = 1

            # ── ED as one full segment ──
            if ed_e > ed_s:
                _progress[key]["message"] = "ED..."
                _jobs[key]["message"] = "ED..."
                try:
                    ed_dir = proj.path / "_playback"
                    ed_dir.mkdir(parents=True, exist_ok=True)
                    from anidub.assembler import _slice_audio
                    from anidub.extract import extract_video_clip
                    from anidub.assembler import _mux_playback
                    video_src = proj._abs(proj.state.get("video_only", "video_only.mkv"))
                    ed_dur = ed_e - ed_s
                    audio_src = proj._audio_path()
                    ed_audio = ed_dir / "_ed_audio.wav"
                    _slice_audio(audio_src, ed_s, ed_dur, ed_audio)
                    ed_video = ed_dir / "_ed_video.mkv"
                    extract_video_clip(video_src, ed_s, ed_e, ed_video)
                    ed_out = ed_dir / "ed.mp4"
                    _mux_playback(ed_video, ed_audio, ed_out)
                    processed += 1
                except Exception as e:
                    failed.append({"clip_id": "__ed__", "error": str(e)})
                _progress[key]["current"] = (1 if op_e > op_s else 0) + 1

            # ── Per-clip playback previews ──
            for i, cid in enumerate(order):
                if _jobs[key].get("cancel"):
                    break
                clip = proj.get_clip(cid)
                idx = extra + i + 1
                if not clip or not clip.clone_path:
                    _progress[key]["current"] = idx
                    continue
                if clip.status in (ClipStatus.NON_DUB, ClipStatus.SIGN):
                    _progress[key]["current"] = idx
                    continue
                playback_mp4 = proj.path / "lines" / cid / "playback.mp4"
                if playback_mp4.exists() and playback_mp4.stat().st_size > 1024:
                    _progress[key]["current"] = idx
                    processed += 1
                    continue
                try:
                    proj.preview_playback_clip(cid)
                    processed += 1
                except Exception as e:
                    failed.append({"clip_id": cid, "error": str(e)})
                _progress[key]["current"] = idx
                _progress[key]["message"] = f"{processed}/{total}"
                _jobs[key]["message"] = _progress[key]["message"]
            _progress[key]["done"] = True
            _progress[key]["failed"] = failed
            _progress[key]["message"] = f"Generated {processed} previews"
        finally:
            _jobs[key]["running"] = False
            _jobs[key]["message"] = "Done"

    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"started": True, "total": total})


@app.route("/api/playback/generate-previews/progress")
def api_playback_generate_previews_progress():
    return jsonify(_progress.get("playback-previews",
                                 {"current": 0, "total": 0, "done": True, "message": ""}))


@app.route("/api/playback/delete-previews", methods=["POST"])
def api_playback_delete_previews():
    err = _require_project()
    if err: return err
    proj = _anime.get_active_project()
    count = proj.delete_playback_previews()
    return jsonify({"deleted": count})


@app.route("/api/preview-raw", methods=["POST"])
def api_preview_raw():
    err = _require_project()
    if err: return err
    data = request.get_json(silent=True) or {}
    start_sec = data.get("start_sec", 0)
    end_sec = data.get("end_sec", start_sec + 5)
    dur = end_sec - start_sec
    proj = _anime.get_active_project()
    ffmpeg = str(Path(get_ffmpeg_location()) / "ffmpeg.exe")
    video_src = proj._abs(proj.state.get("video_only", "video_only.mkv"))
    audio = proj._audio_path()

    tmp = tempfile.NamedTemporaryFile(suffix=".mp4", delete=False)
    tmp.close()

    if audio and audio.exists():
        sp.run([ffmpeg, "-y", "-loglevel", "error",
                "-ss", f"{start_sec:.3f}", "-t", f"{dur:.3f}",
                "-i", str(video_src),
                "-ss", f"{start_sec:.3f}", "-t", f"{dur:.3f}",
                "-i", str(audio),
                "-c:v", "libx264", "-preset", "ultrafast", "-crf", "23",
                "-c:a", "aac", "-b:a", "128k",
                "-movflags", "+faststart", "-shortest", tmp.name], check=True)
    else:
        sp.run([ffmpeg, "-y", "-loglevel", "error",
                "-ss", f"{start_sec:.3f}", "-t", f"{dur:.3f}",
                "-i", str(video_src),
                "-c:v", "libx264", "-preset", "ultrafast", "-crf", "23",
                "-movflags", "+faststart", tmp.name], check=True)

    return _send_file_range(tmp.name, "video/mp4")


@app.route("/api/assemble", methods=["POST"])
def api_assemble():
    err = _require_project()
    if err: return err
    _log.info("/api/assemble: starting")
    try:
        final = _anime.get_active_project().assemble_full()
        _log.info("/api/assemble: OK -> %s", final)
        return jsonify({"final_path": str(final)})
    except Exception as e:
        _log.error("/api/assemble: FAILED %s", type(e).__name__)
        _log.error("%s", traceback.format_exc())
        return jsonify({"error": str(e), "trace": traceback.format_exc()}), 500


@app.route("/api/stats")
def api_stats():
    err = _require_project()
    if err: return err
    return jsonify(_anime.get_active_project().get_stats())


# ── Static preview files ──────────────────────

@app.route("/preview/<clip_id>.mp4")
def serve_preview(clip_id):
    err = _require_project()
    if err: return err
    proj = _anime.get_active_project()
    preview = proj.path / "lines" / clip_id / "preview.mp4"
    if not preview.exists():
        return jsonify({"error": "Preview not found — clone and preview first"}), 404

    file_size = preview.stat().st_size
    range_header = request.headers.get("Range")

    if range_header:
        m = re.match(r"bytes=(\d+)-(\d*)", range_header)
        if m:
            start = int(m.group(1))
            end = int(m.group(2)) if m.group(2) else file_size - 1
            end = min(end, file_size - 1)
            length = end - start + 1
            with open(preview, "rb") as f:
                f.seek(start)
                data = f.read(length)
            return Response(
                data, 206, mimetype="video/mp4",
                headers={
                    "Content-Range": f"bytes {start}-{end}/{file_size}",
                    "Accept-Ranges": "bytes",
                    "Content-Length": str(length),
                    "Cache-Control": "no-cache",
                },
                direct_passthrough=True,
            )

    return Response(
        open(preview, "rb").read(), 200,
        mimetype="video/mp4",
        headers={
            "Accept-Ranges": "bytes",
            "Content-Length": str(file_size),
            "Cache-Control": "no-cache",
        },
    )


# ── Jobs ─────────────────────────────────────

@app.route("/api/jobs")
def api_jobs():
    return jsonify(_jobs)


@app.route("/api/jobs/<key>/cancel", methods=["POST"])
def api_job_cancel(key):
    if key in _jobs:
        _jobs[key]["cancel"] = True
    return jsonify({"ok": True})


@app.route("/api/cleanup", methods=["POST"])
def api_cleanup():
    force = request.args.get("force", "").lower() in ("1", "true", "yes")
    for key in list(_jobs.keys()):
        _jobs[key]["cancel"] = True
    proj = _anime.get_active_project() if _anime else None
    if proj:
        proj.cleanup_running()
    if force:
        _unload_all_backends()
    else:
        _free_vram()
    return jsonify({"ok": True, "force": force, "live_backends": list(_backends.keys())})


# ── CLI ───────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(prog="anidub-edit", description="Subtitle review & voice clone GUI")
    ap.add_argument("--port", type=int, default=5000, help="server port (default 5000)")
    ap.add_argument("--host", default="127.0.0.1", help="bind address")
    ap.add_argument("--project", type=Path, default=None, help="load existing anime project")
    args = ap.parse_args()

    if args.project:
        global _anime
        _anime = AnimeProject.load(args.project)

    url = f"http://{args.host}:{args.port}/"
    print(f"Starting anidub-edit at {url}")
    webbrowser.open(url)
    app.run(host=args.host, port=args.port, debug=False)


if __name__ == "__main__":
    main()
