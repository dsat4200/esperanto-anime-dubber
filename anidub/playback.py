"""Auto-play backend: build a contiguous segment plan for the timeline.

The frontend orchestration serves each segment's mp4 via:

  * Cloned clips with ``preview.mp4`` → the existing preview file (dubbed
    audio + burned-in subtitle) served directly.
  * Everything else (gaps, un-previewed clips) → a short lived ffmpeg slice
    of ``video_only + original audio`` (same path as ``/api/preview-raw``).

This module itself does NOT touch ffmpeg or produce cached files.
"""
import logging
from pathlib import Path

_log = logging.getLogger("anidub.playback")


def _gap_seg(s: float, e: float) -> dict:
    return {
        "kind": "gap", "clip_id": None, "start": s, "end": e,
        "dubbed": False, "status": None,
        "original_text": None, "translated_text": None,
        "ready": False, "has_preview": False,
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
        if e <= cursor + 0.001:
            # Entire clip lies before or at cursor — already covered.
            continue
        if s <= cursor:
            # Clip overlaps the cursor — only the tail [cursor, e] is new.
            s = cursor
        elif s > cursor + 0.001:
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
            "has_preview": False,
        }
        preview_mp4 = proj.path / "lines" / c["clip_id"] / "preview.mp4"
        if preview_mp4.exists() and preview_mp4.stat().st_size > 1024:
            seg["ready"] = True
            seg["has_preview"] = True
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
