"""Auto-play backend: CPU-only ffmpeg slicing/muxing for seamless draft playback.

This module never touches GPU/VRAM. It produces small H.264/aac mp4 segments
under ``<project>/_playback/`` covering every clip and the gaps between them:

  * Cloned clips  → dubbed audio (no_vocals + TTS, mixed at the clip's offset)
  * Un-cloned clips & gaps → original audio track as-is

The frontend renders the Esperanto subtitle as an HTML/CSS overlay over these
slices, so editing a translation updates live without re-rendering anything.
"""
import json
import subprocess
from pathlib import Path

from anidub.assembler import _slice_audio, _mix_background_voice, _ffmpeg_bin
from anidub.project import ClipStatus

PLAYBACK_DIR = "_playback"


def _pb_dir(proj) -> Path:
    d = proj.path / PLAYBACK_DIR
    d.mkdir(parents=True, exist_ok=True)
    return d


def playback_audio_path(proj, clip_id: str) -> Path:
    return proj.path / "lines" / clip_id / "playback_audio.wav"


def segment_filename(seg: dict) -> str:
    if seg["kind"] == "gap":
        return f"gap_{int(seg['start'] * 1000):010d}_{int(seg['end'] * 1000):010d}.mp4"
    return f"clip_{seg['clip_id']}.mp4"


def segment_path(proj, seg: dict) -> Path:
    return _pb_dir(proj) / segment_filename(seg)


def _gap_seg(s: float, e: float) -> dict:
    return {
        "kind": "gap", "clip_id": None, "start": s, "end": e,
        "dubbed": False, "status": None,
        "original_text": None, "translated_text": None, "ready": False,
    }


def get_playback_plan(proj) -> dict:
    """Walk timeline in order, producing contiguous segments covering the
    full episode plus metadata for the overlay layer."""
    proj._init_timeline()
    bounds = proj.get_timeline_bounds()
    if not bounds or bounds[1] <= bounds[0]:
        return {"segments": [], "total_start": 0.0, "total_end": 0.0,
                "total_duration": 0.0, "selected_clip_id": proj.state.get("selected_clip_id")}

    total_start, total_end = bounds
    clips = proj.get_timeline_clips()

    segments: list[dict] = []
    cursor = float(total_start)
    for c in clips:
        s = float(c["start_sec"])
        e = float(c["end_sec"])
        if s > cursor + 0.001:
            segments.append(_gap_seg(cursor, s))
        seg = {
            "kind": "clip",
            "clip_id": c["clip_id"],
            "start": s,
            "end": e,
            "status": c.get("status"),
            "original_text": c.get("original_text"),
            "translated_text": c.get("translated_text"),
            "character": c.get("character"),
            "dubbed": bool(c.get("clone_path")),
            "ready": False,
        }
        seg["ready"] = segment_path(proj, seg).exists()
        segments.append(seg)
        cursor = max(cursor, e)
    if cursor + 0.001 < total_end:
        segments.append(_gap_seg(cursor, total_end))

    return {
        "segments": segments,
        "total_start": float(total_start),
        "total_end": float(total_end),
        "total_duration": float(total_end - total_start),
        "selected_clip_id": proj.state.get("selected_clip_id"),
    }


def prepare_playback_audio(proj, clip_id: str) -> Path:
    """Mix no_vocals slice + TTS at the clip's current offset.

    Result is cached at ``lines/<cid>/playback_audio.wav`` and the segment mp4
    cache for that clip is invalidated so the next render re-muxes. Idempotent
    and safe to call after every clone regardless of caller.
    """
    entry = proj._get_clip_entry(clip_id)
    if not entry.get("clone_path"):
        raise RuntimeError(f"Clip {clip_id} has no clone")
    if entry.get("status") in (ClipStatus.NON_DUB.value, ClipStatus.SIGN.value):
        raise RuntimeError(f"Clip {clip_id} ({entry['status']}) cannot be prepared")

    no_vocals = proj.path / "no_vocals.wav"
    if not no_vocals.exists():
        proj.run_demucs()

    start = float(entry["start_sec"])
    dur = float(entry["end_sec"]) - start
    line_dir = proj.path / "lines" / clip_id
    line_dir.mkdir(parents=True, exist_ok=True)

    bg = line_dir / "_pb_bg.wav"
    _slice_audio(no_vocals, start, dur, bg)

    tts = proj._abs(entry["clone_path"])
    offset = float(entry.get("audio_offset_ms", 0.0))
    voice_input = tts
    delay_ms = offset
    if offset < -1:
        from tempfile import NamedTemporaryFile
        trim_fd = NamedTemporaryFile(suffix=".wav", delete=False)
        trim_fd.close()
        trim_path = Path(trim_fd.name)
        bin_path = _ffmpeg_bin()
        trim_start = abs(offset) / 1000.0
        subprocess.run([bin_path, "-y", "-loglevel", "error",
                        "-ss", f"{trim_start:.3f}", "-i", str(tts),
                        "-c:a", "pcm_s16le", str(trim_path)], check=True)
        voice_input = trim_path
        delay_ms = 0

    out = playback_audio_path(proj, clip_id)
    _mix_background_voice(bg, voice_input, out, delay_ms=delay_ms)

    if voice_input != tts:
        voice_input.unlink(missing_ok=True)
    bg.unlink(missing_ok=True)

    seg_path = segment_path(proj, {"kind": "clip", "clip_id": clip_id})
    seg_path.unlink(missing_ok=True)
    return out


def render_segment(proj, seg: dict) -> Path:
    """Produce (or return cached) mp4 for one plan segment. CPU-only."""
    out = segment_path(proj, seg)
    if out.exists() and out.stat().st_size > 0:
        return out
    out.parent.mkdir(parents=True, exist_ok=True)
    bin_path = _ffmpeg_bin()
    video_src = proj._abs(proj.state.get("video_only", "video_only.mkv"))
    start = float(seg["start"])
    end = float(seg["end"])
    dur = end - start

    audio_src: Path | None = None
    if seg["kind"] == "clip" and seg.get("dubbed"):
        audio_path = playback_audio_path(proj, seg["clip_id"])
        if not audio_path.exists():
            try:
                prepare_playback_audio(proj, seg["clip_id"])
                audio_src = audio_path
            except Exception:
                audio_src = None
        else:
            audio_src = audio_path
    if audio_src is None:
        audio_src = proj._audio_path()

    if not audio_src or not Path(audio_src).exists():
        subprocess.run([bin_path, "-y", "-loglevel", "error",
                        "-ss", f"{start:.3f}", "-t", f"{dur:.3f}",
                        "-i", str(video_src),
                        "-c:v", "libx264", "-preset", "ultrafast", "-crf", "23",
                        "-movflags", "+faststart", "-an", str(out)], check=True)
    else:
        audio_src = Path(audio_src)
        subprocess.run([bin_path, "-y", "-loglevel", "error",
                        "-ss", f"{start:.3f}", "-t", f"{dur:.3f}",
                        "-i", str(video_src),
                        "-ss", f"{start:.3f}", "-t", f"{dur:.3f}",
                        "-i", str(audio_src),
                        "-c:v", "libx264", "-preset", "ultrafast", "-crf", "23",
                        "-c:a", "aac", "-b:a", "128k",
                        "-movflags", "+faststart", "-shortest", str(out)], check=True)
    return out


def ensure_playback_ready(proj, clip_ids: list[str]) -> dict:
    """Bulk CPU ffmpeg preparation of dubbed playback audio for cloned clips.

    Walks the given ids (or the whole order when empty), prepares any cloned
    clip whose playback_audio.wav is missing or stale. Returns counts.
    """
    if not clip_ids:
        clip_ids = list(proj.state.get("order", []))
    prepared = 0
    skipped = 0
    failed: list[dict] = []
    for cid in clip_ids:
        clip = proj.get_clip(cid)
        if not clip or not clip.clone_path:
            skipped += 1
            continue
        if clip.status in (ClipStatus.NON_DUB, ClipStatus.SIGN):
            skipped += 1
            continue
        try:
            prepare_playback_audio(proj, cid)
            prepared += 1
        except Exception as e:
            failed.append({"clip_id": cid, "error": str(e)})
    return {"prepared": prepared, "skipped": skipped, "failed": failed,
            "total": len(clip_ids)}


def invalidate_clip(proj, clip_id: str):
    """Drop cached playback audio + segment for a clip (call on reset/clone)."""
    playback_audio_path(proj, clip_id).unlink(missing_ok=True)
    segment_path(proj, {"kind": "clip", "clip_id": clip_id}).unlink(missing_ok=True)