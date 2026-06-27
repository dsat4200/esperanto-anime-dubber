import argparse
import mimetypes
import os
import re
import subprocess as sp
import sys
import tempfile
import threading
import webbrowser
from pathlib import Path

from flask import Flask, request, jsonify, Response, render_template

from anidub.project import AnimeProject, Project, ClipStatus
from anidub.config import get_ffmpeg_location

app = Flask(__name__)
_anime: AnimeProject | None = None
_progress: dict = {}
_jobs: dict = {}


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
        _jobs[key] = {"running": True, "cancel": False, "type": "batch-translate", "message": "Translating..."}
        try:
            processed = 0
            _progress[key] = {"current": 0, "total": len(valid), "done": False, "message": "Translating..."}
            for stem in valid:
                if _jobs[key].get("cancel"):
                    break
                try:
                    proj = _anime.select_episode(stem)
                    proj.select_audio_track(audio_idx)
                    proj.select_subtitle_track(sub_idx)
                    if not proj.state.get("demucs_done"):
                        proj.run_demucs()
                    proj.translate_all()
                    processed += 1
                except Exception:
                    pass
                _progress[key]["current"] = processed
                _progress[key]["message"] = f"Episode {processed}/{len(valid)}"
                _jobs[key]["message"] = f"Translated {processed}/{len(valid)} episodes"
            _progress[key] = {"current": processed, "total": len(valid),
                              "done": True, "message": f"Translated {processed}/{len(valid)} episodes",
                              "skipped": skipped}
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
        _jobs[key] = {"running": True, "cancel": False, "type": "batch-clone", "message": "Loading TTS model..."}
        try:
            backend = OmniVoiceTTSBackend(whisper_model=whisper_model)
            _jobs[key]["message"] = "Cloning..."
            _progress[key] = {"current": 0, "total": len(valid), "done": False, "message": "Cloning..."}
            for stem in valid:
                if _jobs[key].get("cancel"):
                    break
                try:
                    proj = _anime.select_episode(stem)
                    proj.select_audio_track(audio_idx)
                    proj.select_subtitle_track(sub_idx)
                    if not proj.state.get("demucs_done"):
                        proj.run_demucs()
                    proj.clone_range(backend=backend)
                    processed += 1
                except Exception:
                    pass
                _progress[key]["current"] = processed
                _progress[key]["message"] = f"Cloned {processed}/{len(valid)} episodes"
                _jobs[key]["message"] = f"Cloned {processed}/{len(valid)} episodes"
                if torch.cuda.is_available():
                    torch.cuda.empty_cache()
            _progress[key]["done"] = True
            _progress[key]["message"] = f"Cloned {processed}/{len(valid)} episodes"
        finally:
            _jobs[key]["running"] = False
            _jobs[key]["message"] = "Done"
            if backend is not None:
                del backend
            if torch.cuda.is_available():
                torch.cuda.empty_cache()

    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"started": True, "total": len(valid), "skipped": skipped})


@app.route("/api/episodes/batch-translate/progress")
def api_batch_translate_progress():
    return jsonify(_progress.get("batch-translate", {"current": 0, "total": 0, "done": True, "message": "", "skipped": []}))


@app.route("/api/episodes/batch-clone/progress")
def api_batch_clone_progress():
    return jsonify(_progress.get("batch-clone", {"current": 0, "total": 0, "done": True, "message": "", "skipped": []}))


# ── Setup (per episode) ───────────────────────

@app.route("/api/tracks")
def api_tracks():
    err = _require_project()
    if err: return err
    proj = _anime.get_active_project()
    return jsonify({
        "audio": proj.get_audio_tracks(),
        "subtitle": proj.get_subtitle_tracks(),
        "demucs_done": proj.state.get("demucs_done", False),
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
    result = proj.clone_clip(clip_id, character=character, mood=mood)
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
            processed = 0
            _progress[key] = {"current": 0, "total": total, "done": False, "message": "Cloning..."}
            _jobs[key]["message"] = "Cloning..."
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
                                del backend
                                if torch.cuda.is_available():
                                    torch.cuda.empty_cache()
                                backend = OmniVoiceTTSBackend(whisper_model=whisper_model)
                _progress[key]["current"] = idx + 1
                _progress[key]["message"] = f"Cloning clip {idx + 1}/{total}"
                _jobs[key]["message"] = f"Cloning clip {idx + 1}/{total}"
                if torch.cuda.is_available():
                    torch.cuda.empty_cache()
            _progress[key]["done"] = True
            _progress[key]["message"] = f"Cloned {processed}/{total}"
        finally:
            _jobs[key]["running"] = False
            _jobs[key]["message"] = "Done"
            if backend is not None:
                del backend
            if torch.cuda.is_available():
                torch.cuda.empty_cache()

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
    final = _anime.get_active_project().assemble_full()
    return jsonify({"final_path": str(final)})


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
