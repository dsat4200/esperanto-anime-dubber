import argparse
import mimetypes
import os
import re
import sys
import webbrowser
from pathlib import Path

from flask import Flask, request, jsonify, Response, render_template

from anidub.project import Project, ClipStatus

app = Flask(__name__)
_project: Project | None = None


def _require_project():
    global _project
    if _project is None:
        return jsonify({"error": "No project loaded. POST /api/open first."}), 400
    return None


# ── Page ──────────────────────────────────────

@app.route("/")
def index():
    proj = request.args.get("project", "")
    return render_template("index.html", project_path=proj)


# ── Project lifecycle ─────────────────────────

@app.route("/api/open", methods=["POST"])
def api_open():
    global _project
    data = request.get_json(silent=True) or {}
    mkv = data.get("mkv_path")
    proj_dir = data.get("project_dir")
    name = data.get("project_name") or None
    if mkv:
        _project = Project.create(Path(mkv), name=name)
    elif proj_dir:
        _project = Project.load(Path(proj_dir))
    else:
        return jsonify({"error": "Need mkv_path or project_dir"}), 400
    return jsonify({"path": str(_project.path)})


@app.route("/api/save", methods=["POST"])
def api_save():
    err = _require_project()
    if err: return err
    _project.save()
    return jsonify({"ok": True})


# ── Setup ─────────────────────────────────────

@app.route("/api/tracks")
def api_tracks():
    err = _require_project()
    if err: return err
    return jsonify({
        "audio": _project.get_audio_tracks(),
        "subtitle": _project.get_subtitle_tracks(),
        "demucs_done": _project.state.get("demucs_done", False),
    })


@app.route("/api/audio-track", methods=["POST"])
def api_audio_track():
    err = _require_project()
    if err: return err
    data = request.get_json(silent=True) or {}
    _project.select_audio_track(data.get("index", 0))
    return jsonify({"ok": True})


@app.route("/api/sub-track", methods=["POST"])
def api_sub_track():
    err = _require_project()
    if err: return err
    data = request.get_json(silent=True) or {}
    _project.select_subtitle_track(data.get("index", 0))
    return jsonify({"ok": True})


@app.route("/api/demucs", methods=["POST"])
def api_demucs():
    err = _require_project()
    if err: return err
    nv, v = _project.run_demucs()
    return jsonify({"ok": True, "no_vocals": str(nv), "vocals": str(v)})


# ── Timeline ──────────────────────────────────

@app.route("/api/timeline")
def api_timeline():
    err = _require_project()
    if err: return err
    regions = _project.get_timeline_regions()
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

@app.route("/api/clips/<int:n>")
def api_get_clip(n):
    err = _require_project()
    if err: return err
    clip = _project.get_clip(n)
    if not clip:
        return jsonify({"error": f"Clip {n} not found"}), 404
    return jsonify(_clip_to_dict(clip))


@app.route("/api/clips/current")
def api_current_clip():
    err = _require_project()
    if err: return err
    clip = _project.get_current_clip()
    if not clip:
        return jsonify({"error": "No current clip"}), 404
    return jsonify(_clip_to_dict(clip))


def _clip_to_dict(clip):
    return {
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
    }


@app.route("/api/clips/<int:n>/translate", methods=["POST"])
def api_translate(n):
    err = _require_project()
    if err: return err
    data = request.get_json(silent=True) or {}
    override = data.get("text_override")
    text = _project.translate_clip(n, text_override=override if override else None)
    return jsonify({"translated_text": text})


@app.route("/api/clips/<int:n>/clone", methods=["POST"])
def api_clone(n):
    err = _require_project()
    if err: return err
    data = request.get_json(silent=True) or {}
    character = data.get("character") or None
    mood = data.get("mood", "normal")
    result = _project.clone_clip(n, character=character, mood=mood)
    return jsonify({
        "inference_ms": result.get("inference_ms"),
        "output_duration": result.get("output_duration"),
    })


@app.route("/api/clips/<int:n>/preview", methods=["POST"])
def api_preview(n):
    err = _require_project()
    if err: return err
    _project.preview_clip(n)
    return jsonify({"url": f"/preview/{n:03d}.mp4"})


@app.route("/api/clips/<int:n>/offset", methods=["POST"])
def api_offset(n):
    err = _require_project()
    if err: return err
    data = request.get_json(silent=True) or {}
    _project.set_clip_offset(n, data.get("offset_ms", 0.0))
    return jsonify({"ok": True})


@app.route("/api/clips/<int:n>/character", methods=["POST"])
def api_character(n):
    err = _require_project()
    if err: return err
    data = request.get_json(silent=True) or {}
    char = data.get("character") or None
    mood = data.get("mood", "normal")
    _project.set_clip_character(n, char, mood)
    return jsonify({"ok": True})


@app.route("/api/clips/<int:n>/accept", methods=["POST"])
def api_accept(n):
    err = _require_project()
    if err: return err
    _project.accept_clip(n)
    nxt = _project.get_next_clip()
    if nxt and nxt.status != ClipStatus.ACCEPTED:
        return jsonify({"next_index": nxt.index, "done": False})
    for i in range(1, _project.get_clip_count() + 1):
        c = _project.get_clip(i)
        if c and c.status != ClipStatus.ACCEPTED:
            return jsonify({"next_index": i, "done": False})
    return jsonify({"next_index": None, "done": True})


@app.route("/api/clips/<int:n>/reject", methods=["POST"])
def api_reject(n):
    err = _require_project()
    if err: return err
    _project.reject_clip(n)
    return jsonify({"ok": True})


@app.route("/api/clips/<int:n>/reset", methods=["POST"])
def api_reset(n):
    err = _require_project()
    if err: return err
    _project.reset_clip(n)
    return jsonify({"ok": True})


# ── Bulk ──────────────────────────────────────

@app.route("/api/translate-all", methods=["POST"])
def api_translate_all():
    err = _require_project()
    if err: return err
    total = _project.get_clip_count()
    _project.translate_range(1, total)
    stats = _project.get_stats()
    return jsonify({"processed": stats.get("translated", 0), "total": total})


@app.route("/api/clone-all", methods=["POST"])
def api_clone_all():
    err = _require_project()
    if err: return err
    total = _project.get_clip_count()
    _project.clone_range(1, total)
    stats = _project.get_stats()
    return jsonify({"processed": stats.get("cloned", 0), "total": total})


# ── Characters ────────────────────────────────

@app.route("/api/characters")
def api_characters():
    err = _require_project()
    if err: return err
    return jsonify(_project.get_all_character_clips())


@app.route("/api/characters", methods=["POST"])
def api_characters_save():
    err = _require_project()
    if err: return err
    data = request.get_json(silent=True) or {}
    name = data.get("name", "")
    mood = data.get("mood", "normal")
    clip_index = data.get("clip_index")
    if clip_index is not None:
        src = _project.path / "lines" / f"{clip_index:03d}" / "ref.wav"
    else:
        return jsonify({"error": "clip_index required"}), 400
    if not src.exists():
        return jsonify({"error": f"ref.wav not found for clip {clip_index}"}), 404
    dst = _project.save_character_clip(name, src, mood)
    return jsonify({"path": str(dst)})


@app.route("/api/characters/<name>/<mood>", methods=["DELETE"])
def api_characters_delete(name, mood):
    err = _require_project()
    if err: return err
    _project.delete_character_clip(name, mood)
    return jsonify({"ok": True})


# ── Final ─────────────────────────────────────

@app.route("/api/assemble", methods=["POST"])
def api_assemble():
    err = _require_project()
    if err: return err
    final = _project.assemble_full()
    return jsonify({"final_path": str(final)})


@app.route("/api/stats")
def api_stats():
    err = _require_project()
    if err: return err
    return jsonify(_project.get_stats())


# ── Static preview files ──────────────────────

@app.route("/preview/<filename>")
def serve_preview(filename):
    err = _require_project()
    if err: return err
    parts = filename.replace(".mp4", "").lstrip("0")
    idx = int(parts) if parts else 1
    line_dir = _project.path / "lines" / f"{idx:03d}"
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
    ap.add_argument("--project", type=Path, default=None, help="load existing project")
    args = ap.parse_args()

    if args.project:
        global _project
        _project = Project.load(args.project)

    url = f"http://{args.host}:{args.port}/"
    if args.project:
        url += f"?project={args.project}"

    print(f"Starting anidub-edit at {url}")
    webbrowser.open(url)
    app.run(host=args.host, port=args.port, debug=False)


if __name__ == "__main__":
    main()
