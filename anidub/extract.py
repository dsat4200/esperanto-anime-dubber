import json
import math
import subprocess
from pathlib import Path

from anidub.config import get_ffmpeg_location


def _ffmpeg_bin():
    loc = get_ffmpeg_location()
    if not loc:
        raise RuntimeError("ffmpeg not found")
    return str(Path(loc) / "ffmpeg.exe")


def _probe_sample_count(path: Path) -> tuple[int, int]:
    bin_path = _ffmpeg_bin()
    ffprobe = str(Path(bin_path).parent / "ffprobe.exe")
    out = subprocess.run(
        [ffprobe, "-v", "error", "-show_entries",
         "stream=sample_rate,nb_samples:format=duration",
         "-of", "json", str(path)],
        capture_output=True, text=True, check=True,
    ).stdout
    info = json.loads(out)
    streams = info.get("streams", [])
    fmt = info.get("format", {})
    if streams:
        sr = int(streams[0].get("sample_rate", 0))
        nb = int(streams[0].get("nb_samples", 0))
        dur = float(fmt.get("duration", 0) or 0)
        if not nb and sr and dur:
            nb = int(sr * dur)
        return sr, nb
    return 0, 0


def extract_ref_clip(
    mkv_path: Path,
    end_sec: float,
    dur: float = 3.0,
    out_path: Path | None = None,
    audio_stream_index: int = 0,
) -> Path:
    mkv_path = Path(mkv_path)
    if out_path is None:
        out_path = Path(f"ref_{end_sec:.2f}.wav")
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    bin_path = _ffmpeg_bin()

    start = max(0.0, end_sec - dur)
    actual_dur = end_sec - start

    cmd = [
        bin_path, "-y", "-loglevel", "error",
        "-ss", f"{start:.3f}",
        "-t", f"{actual_dur:.3f}",
        "-i", str(mkv_path),
        "-map", f"0:a:{audio_stream_index}",
        "-ar", "24000",
        "-ac", "1",
        "-sample_fmt", "s16",
        str(out_path),
    ]
    subprocess.run(cmd, check=True)
    return out_path


def extract_ref_clip_forward(
    mkv_path: Path,
    line,
    max_dur: float = 3.0,
    out_path: Path | None = None,
    audio_stream_index: int = 0,
) -> Path:
    """Extract ref audio starting AT line.start_sec, capped at next line's start_sec.
    Captures the character's actual voice during this line + the gap before the next.
    """
    mkv_path = Path(mkv_path)
    start = float(line["start_sec"])
    if out_path is None:
        out_path = Path(f"ref_fwd_{start:.2f}.wav")
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    bin_path = _ffmpeg_bin()

    dur = max_dur
    next_start = line.get("next_line_start")
    if next_start is not None:
        dur = min(max_dur, next_start - start)
    if dur < 1.0:
        dur = min(max_dur, (line["end_sec"] - start) + 1.0)

    cmd = [
        bin_path, "-y", "-loglevel", "error",
        "-ss", f"{start:.3f}",
        "-t", f"{dur:.3f}",
        "-i", str(mkv_path),
        "-map", f"0:a:{audio_stream_index}",
        "-ar", "24000",
        "-ac", "1",
        "-sample_fmt", "s16",
        str(out_path),
    ]
    subprocess.run(cmd, check=True)
    return out_path


def extract_full_audio(mkv_path: Path, out_path: Path, audio_stream_index: int = 0) -> Path:
    mkv_path = Path(mkv_path)
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    bin_path = _ffmpeg_bin()
    cmd = [
        bin_path, "-y", "-loglevel", "error",
        "-i", str(mkv_path),
        "-map", f"0:a:{audio_stream_index}",
        "-ar", "44100",
        "-ac", "2",
        str(out_path),
    ]
    subprocess.run(cmd, check=True)
    return out_path


def extract_full_audio_resampled(
    mkv_path: Path, out_path: Path, target_sr: int = 44100, audio_stream_index: int = 0,
) -> Path:
    mkv_path = Path(mkv_path)
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    bin_path = _ffmpeg_bin()
    cmd = [
        bin_path, "-y", "-loglevel", "error",
        "-i", str(mkv_path),
        "-map", f"0:a:{audio_stream_index}",
        "-ar", str(target_sr),
        "-ac", "2",
        "-sample_fmt", "s16",
        str(out_path),
    ]
    subprocess.run(cmd, check=True)
    return out_path


def fit_audio_to_duration(
    in_path: Path,
    out_path: Path,
    target_duration: float,
) -> dict:
    in_path = Path(in_path)
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    sr, n_samples = _probe_sample_count(in_path)
    if not sr or not n_samples:
        raise RuntimeError(f"Could not probe source audio: {in_path}")
    input_dur = n_samples / sr
    if input_dur <= 0:
        raise RuntimeError(f"Zero-length input audio: {in_path}")

    ratio = target_duration / input_dur
    postprocess = "none (already fits)"

    if abs(ratio - 1.0) < 0.005:
        import shutil
        shutil.copy2(in_path, out_path)
        return {
            "input_duration": input_dur,
            "target_duration": target_duration,
            "atempo_chain": "none",
            "postprocess": "copy",
            "final_duration": input_dur,
        }

    atempo_filters = []
    remaining = ratio
    while remaining > 2.0:
        atempo_filters.append("atempo=2.0")
        remaining /= 2.0
    while remaining < 0.5:
        atempo_filters.append("atempo=0.5")
        remaining /= 0.5
    atempo_filters.append(f"atempo={remaining:.6f}")
    chain = ",".join(atempo_filters)

    bin_path = _ffmpeg_bin()
    cmd = [
        bin_path, "-y", "-loglevel", "error",
        "-i", str(in_path),
        "-filter:a", chain,
        str(out_path),
    ]
    subprocess.run(cmd, check=True)

    final_sr, final_n = _probe_sample_count(out_path)
    final_dur = final_n / final_sr if final_sr else target_duration

    return {
        "input_duration": input_dur,
        "target_duration": target_duration,
        "atempo_chain": chain,
        "postprocess": "atempo",
        "final_duration": final_dur,
    }


def trim_silence(wav, sr: int, top_db: float = 30):
    import librosa
    trimmed, _ = librosa.effects.trim(wav, top_db=top_db)
    if trimmed.size == 0:
        return wav
    return trimmed


def extract_video_clip(
    mkv_path: Path,
    start_sec: float,
    end_sec: float,
    out_path: Path,
    video_stream_index: int = 0,
) -> Path:
    mkv_path = Path(mkv_path)
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    bin_path = _ffmpeg_bin()
    dur = end_sec - start_sec
    cmd = [
        bin_path, "-y", "-loglevel", "error",
        "-ss", f"{start_sec:.3f}",
        "-t", f"{dur:.3f}",
        "-i", str(mkv_path),
        "-map", f"0:v:{video_stream_index}",
        "-c:v", "libx264",
        "-preset", "ultrafast",
        "-crf", "18",
        "-an",
        str(out_path),
    ]
    subprocess.run(cmd, check=True)
    return out_path


def extract_audio_slice(
    source_path: Path,
    start_sec: float,
    end_sec: float,
    out_path: Path,
) -> Path:
    source_path = Path(source_path)
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    bin_path = _ffmpeg_bin()
    dur = end_sec - start_sec
    cmd = [
        bin_path, "-y", "-loglevel", "error",
        "-ss", f"{start_sec:.3f}",
        "-t", f"{dur:.3f}",
        "-i", str(source_path),
        "-c", "copy",
        str(out_path),
    ]
    subprocess.run(cmd, check=True)
    return out_path