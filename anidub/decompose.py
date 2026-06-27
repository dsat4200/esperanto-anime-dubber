import json
import subprocess
from pathlib import Path

from anidub.config import get_ffmpeg_location


def _ffmpeg_bin():
    loc = get_ffmpeg_location()
    if not loc:
        raise RuntimeError("ffmpeg not found")
    return str(Path(loc) / "ffmpeg.exe")


def _ffprobe_bin():
    return str(Path(_ffmpeg_bin()).parent / "ffprobe.exe")


def _probe_all_streams(mkv_path: Path) -> list[dict]:
    out = subprocess.run([
        _ffprobe_bin(), "-v", "error",
        "-show_entries",
        "stream=index,codec_type,codec_name,channels,sample_rate:stream_tags=language,title",
        "-of", "json", str(mkv_path),
    ], capture_output=True, text=True, check=True).stdout
    info = json.loads(out)
    streams = []
    for s in info.get("streams", []):
        tags = s.get("tags", {})
        codec_type = s.get("codec_type", "")
        entry = {
            "index": s["index"],
            "type": codec_type,
            "codec": s.get("codec_name", "?"),
            "language": tags.get("language", ""),
            "title": tags.get("title", ""),
        }
        if codec_type == "audio":
            entry["channels"] = s.get("channels", 0)
            entry["sample_rate"] = s.get("sample_rate", 0)
        streams.append(entry)
    return streams


def decompose_mkv(mkv_path: Path, out_dir: Path) -> dict:
    mkv_path = Path(mkv_path)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    streams = _probe_all_streams(mkv_path)
    ffmpeg = _ffmpeg_bin()

    audio_tracks = []
    subtitle_tracks = []

    for s in streams:
        if s["type"] == "video":
            video_out = out_dir / "video_only.mkv"
            subprocess.run([
                ffmpeg, "-y", "-loglevel", "error",
                "-i", str(mkv_path),
                "-map", f"0:{s['index']}",
                "-c:v", "copy",
                "-an",
                str(video_out),
            ], check=True)

        elif s["type"] == "audio":
            idx = len(audio_tracks)
            audio_out = out_dir / f"audio_track_{idx}.wav"
            subprocess.run([
                ffmpeg, "-y", "-loglevel", "error",
                "-i", str(mkv_path),
                "-map", f"0:{s['index']}",
                "-ar", "44100",
                "-ac", "2",
                str(audio_out),
            ], check=True)
            audio_tracks.append({
                "index": s["index"],
                "rel_index": idx,
                "path": audio_out,
                "language": s["language"],
                "title": s["title"],
                "codec": s["codec"],
                "channels": s.get("channels", 0),
            })

        elif s["type"] == "subtitle":
            idx = len(subtitle_tracks)
            sub_out = out_dir / f"subtitle_track_{idx}.ass"
            subprocess.run([
                ffmpeg, "-y", "-loglevel", "error",
                "-i", str(mkv_path),
                "-map", f"0:{s['index']}",
                "-c:s", "copy",
                str(sub_out),
            ], check=True)
            subtitle_tracks.append({
                "index": s["index"],
                "rel_index": idx,
                "path": sub_out,
                "language": s["language"],
                "title": s["title"],
                "codec": s["codec"],
            })

    return {
        "video_only": out_dir / "video_only.mkv",
        "audio_tracks": audio_tracks,
        "subtitle_tracks": subtitle_tracks,
    }
