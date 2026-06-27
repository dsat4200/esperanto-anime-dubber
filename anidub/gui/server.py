import argparse
import mimetypes
import os
import re
import subprocess as sp
import sys
import tempfile
import webbrowser
from pathlib import Path

from flask import Flask, request, jsonify, Response, render_template

from anidub.project import AnimeProject, Project, ClipStatus
from anidub.config import get_ffmpeg_location

app = Flask(__name__)
_anime: AnimeProject | None = None


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
    return jsonify({"episodes": eps, "active_stem": active})


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
    regions = proj.get_timeline_regions()
    return jsonify([
        {
            "start_sec": r.start_sec,
            "end_sec": r.end_sec,
            "kind": r.kind,
            "clip_index": r.clip_index,
            "status": r.status.value if r.status else None,
            "duration": r.end_sec - r.start_sec,
        }
        for r in regions
    ])


# ── Clip CRUD ─────────────────────────────────

def _clip_to_dict(clip):
    d = {
        "index": clip.index,
        "start_sec": clip.start_sec,
        "end_sec": clip.end_sec,
        "original_text": clip.original_text,
        "translated_text": clip.translated_text,
        "offset_ms": clip.offset_ms,
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
        "can_clone": clip.status != ClipStatus.NON_DUB,
    }
    proj = _anime.get_active_project()
    d["needs_processing"] = proj.needs_processing(clip.index)
    return d


@app.route("/api/clips/<int:n>")
def api_get_clip(n):
    err = _require_project()
    if err: return err
    proj = _anime.get_active_project()
    clip = proj.get_clip(n)
    if not clip:
        return jsonify({"error": f"Clip {n} not found"}), 404
    return jsonify(_clip_to_dict(clip))


@app.route("/api/clips/current")
def api_current_clip():
    err = _require_project()
    if err: return err
    proj = _anime.get_active_project()
    clip = proj.get_current_clip()
    if not clip:
        return jsonify({"error": "No current clip"}), 404
    return jsonify(_clip_to_dict(clip))


@app.route("/api/clips/<int:n>/translate", methods=["POST"])
def api_translate(n):
    err = _require_project()
    if err: return err
    data = request.get_json(silent=True) or {}
    override = data.get("text_override")
    proj = _anime.get_active_project()
    text = proj.translate_clip(n, text_override=override if override else None)
    return jsonify({"translated_text": text})


@app.route("/api/clips/<int:n>/clone", methods=["POST"])
def api_clone(n):
    err = _require_project()
    if err: return err
    data = request.get_json(silent=True) or {}
    character = data.get("character") or None
    mood = data.get("mood", "normal")
    proj = _anime.get_active_project()
    result = proj.clone_clip(n, character=character, mood=mood)
    return jsonify({
        "inference_ms": result.get("inference_ms"),
        "output_duration": result.get("output_duration"),
    })


@app.route("/api/clips/<int:n>/preview", methods=["POST"])
def api_preview(n):
    err = _require_project()
    if err: return err
    _anime.get_active_project().preview_clip(n)
    return jsonify({"url": f"/preview/{n:03d}.mp4"})


@app.route("/api/clips/<int:n>/offset", methods=["POST"])
def api_offset(n):
    err = _require_project()
    if err: return err
    data = request.get_json(silent=True) or {}
    _anime.get_active_project().set_clip_offset(n, data.get("offset_ms", 0.0))
    return jsonify({"ok": True})


@app.route("/api/clips/<int:n>/character", methods=["POST"])
def api_character(n):
    err = _require_project()
    if err: return err
    data = request.get_json(silent=True) or {}
    char = data.get("character") or None
    mood = data.get("mood", "normal")
    _anime.get_active_project().set_clip_character(n, char, mood)
    return jsonify({"ok": True})


@app.route("/api/clips/<int:n>/instruct", methods=["POST"])
def api_instruct(n):
    err = _require_project()
    if err: return err
    data = request.get_json(silent=True) or {}
    extra = data.get("instruct_extra") or None
    _anime.get_active_project().set_instruct_extra(n, extra)
    return jsonify({"ok": True})


@app.route("/api/clips/<int:n>/process", methods=["POST"])
def api_process(n):
    err = _require_project()
    if err: return err
    data = request.get_json(silent=True) or {}
    character = data.get("character") or None
    mood = data.get("mood", "normal")
    proj = _anime.get_active_project()
    result = proj.process_clip(n, character=character, mood=mood)
    return jsonify(result)


@app.route("/api/clips/<int:n>/speed", methods=["POST"])
def api_speed(n):
    err = _require_project()
    if err: return err
    data = request.get_json(silent=True) or {}
    _anime.get_active_project().set_clip_speed(n, data.get("speed_factor", 1.0))
    return jsonify({"ok": True})


@app.route("/api/clips/<int:n>/accept", methods=["POST"])
def api_accept(n):
    err = _require_project()
    if err: return err
    proj = _anime.get_active_project()
    proj.accept_clip(n)
    nxt = proj.get_next_clip()
    if nxt and nxt.status != ClipStatus.ACCEPTED:
        return jsonify({"next_index": nxt.index, "done": False})
    for i in range(1, proj.get_clip_count() + 1):
        c = proj.get_clip(i)
        if c and c.status != ClipStatus.ACCEPTED:
            return jsonify({"next_index": i, "done": False})
    return jsonify({"next_index": None, "done": True})


@app.route("/api/clips/<int:n>/reject", methods=["POST"])
def api_reject(n):
    err = _require_project()
    if err: return err
    _anime.get_active_project().reject_clip(n)
    return jsonify({"ok": True})


@app.route("/api/clips/<int:n>/reset", methods=["POST"])
def api_reset(n):
    err = _require_project()
    if err: return err
    _anime.get_active_project().reset_clip(n)
    return jsonify({"ok": True})


# ── Bulk ──────────────────────────────────────

@app.route("/api/translate-all", methods=["POST"])
def api_translate_all():
    err = _require_project()
    if err: return err
    proj = _anime.get_active_project()
    total = proj.get_clip_count()
    proj.translate_range(1, total)
    stats = proj.get_stats()
    return jsonify({"processed": stats.get("translated", 0), "total": total})


@app.route("/api/clone-all", methods=["POST"])
def api_clone_all():
    err = _require_project()
    if err: return err
    proj = _anime.get_active_project()
    total = proj.get_clip_count()
    proj.clone_range(1, total)
    stats = proj.get_stats()
    return jsonify({"processed": stats.get("cloned", 0), "total": total})


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
    clip_index = data.get("clip_index")
    if not clip_index:
        return jsonify({"error": "clip_index required"}), 400
    proj = _anime.get_active_project()
    src = proj.path / "lines" / f"{clip_index:03d}" / "ref.wav"
    if not src.exists():
        return jsonify({"error": f"ref.wav not found for clip {clip_index}"}), 404
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

@app.route("/preview/<filename>")
def serve_preview(filename):
    err = _require_project()
    if err: return err
    parts = filename.replace(".mp4", "").lstrip("0")
    idx = int(parts) if parts else 1
    proj = _anime.get_active_project()
    line_dir = proj.path / "lines" / f"{idx:03d}"
    preview = line_dir / "preview.mp4"
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
                data, 206,
                mimetype="video/mp4",
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
