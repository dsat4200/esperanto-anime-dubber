import shutil
import subprocess
from pathlib import Path

from anidub.config import MODEL_NAME, get_ffmpeg_location
from anidub.extract import extract_video_clip

import logging as _logging

_log = _logging.getLogger("anidub.assembler")

_MIX_WEIGHT_BG = 1.2
_MIX_WEIGHT_VOICE = 0.8


def _ffmpeg_bin():
    loc = get_ffmpeg_location()
    if not loc:
        raise RuntimeError("ffmpeg not found")
    return str(Path(loc) / "ffmpeg.exe")


def ensure_demucs_cache_from_wav(source_wav: Path, out_root: Path) -> tuple[Path, Path]:
    no_vocals_cache = out_root / "full_no_vocals.wav"
    vocals_cache = out_root / "full_vocals.wav"

    if no_vocals_cache.exists():
        return no_vocals_cache, vocals_cache

    out_root.mkdir(parents=True, exist_ok=True)

    from anidub.separator import separate_audio
    sep_dir = out_root / "_full_separated"
    result = separate_audio(source_wav, sep_dir)

    result["no_vocals"].rename(no_vocals_cache)
    result["vocals"].rename(vocals_cache)
    shutil.rmtree(sep_dir, ignore_errors=True)
    return no_vocals_cache, vocals_cache


def _slice_audio(source: Path, start_sec: float, dur: float, out_path: Path):
    bin_path = _ffmpeg_bin()
    subprocess.run([
        bin_path, "-y", "-loglevel", "error",
        "-ss", f"{start_sec:.3f}",
        "-t", f"{dur:.3f}",
        "-i", str(source),
        "-c:a", "pcm_s16le",
        str(out_path),
    ], check=True)


def _mix_background_voice(
    bg_path: Path,
    voice_path: Path,
    out_path: Path,
    delay_ms: float = 0.0,
):
    bin_path = _ffmpeg_bin()
    delay_part = f"adelay={delay_ms}|{delay_ms}," if delay_ms > 0 else ""
    chain = (
        f"[1:a]{delay_part}aformat=channel_layouts=stereo[voice];"
        f"[0:a][voice]amix=inputs=2:duration=first:"
        f"weights={_MIX_WEIGHT_BG} {_MIX_WEIGHT_VOICE}[out]"
    )
    subprocess.run([
        bin_path, "-y", "-loglevel", "error",
        "-i", str(bg_path),
        "-i", str(voice_path),
        "-filter_complex", chain,
        "-map", "[out]",
        "-c:a", "pcm_s16le",
        str(out_path),
    ], check=True)


def _make_single_line_ass(ass_header: str, line_dur: float, text: str) -> str:
    ts = f"0:00:00.00,0:00:{int(line_dur):02d}.{int(line_dur*100)%100:02d}"
    dialogue = f"Dialogue: 0,{ts},main,,0000,0000,0000,,{text}"
    return ass_header + "\n" + dialogue


def _mux_final(
    video_path: Path,
    audio_path: Path,
    ass_path: Path,
    out_path: Path,
):
    bin_path = _ffmpeg_bin()
    ass_path_safe = str(ass_path).replace("\\", "/")
    subprocess.run([
        bin_path, "-y", "-loglevel", "error",
        "-i", str(video_path),
        "-i", str(audio_path),
        "-filter_complex", f"[0:v]ass={ass_path_safe}[subbed]",
        "-map", "[subbed]",
        "-map", "1:a",
        "-c:v", "libx264", "-preset", "medium", "-crf", "18",
        "-c:a", "aac", "-b:a", "192k",
        "-shortest",
        str(out_path),
    ], check=True)


def _mux_preview(
    video_path: Path,
    audio_path: Path,
    ass_path: Path,
    out_path: Path,
):
    bin_path = _ffmpeg_bin()
    ass_path_safe = str(ass_path).replace("\\", "/")
    subprocess.run([
        bin_path, "-y", "-loglevel", "error",
        "-i", str(video_path),
        "-i", str(audio_path),
        "-filter_complex", f"[0:v]ass={ass_path_safe}[subbed]",
        "-map", "[subbed]",
        "-map", "1:a",
        "-c:v", "libx264", "-preset", "ultrafast", "-crf", "23",
        "-c:a", "aac", "-b:a", "128k",
        "-movflags", "+faststart",
        "-shortest",
        str(out_path),
    ], check=True)


def assemble_line(
    mkv_path: Path,
    line: dict,
    tts_wav: Path,
    full_no_vocals: Path,
    ass_header: str,
    out_dir: Path,
) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    start_sec = float(line["start_sec"])
    end_sec = float(line["end_sec"])
    dur = end_sec - start_sec

    video_clip = out_dir / "video_only.mkv"
    extract_video_clip(mkv_path, start_sec, end_sec, video_clip)

    no_vocals_clip = out_dir / "no_vocals_clip.wav"
    _slice_audio(full_no_vocals, start_sec, dur, no_vocals_clip)

    dubbed = out_dir / "dubbed.wav"
    _mix_background_voice(no_vocals_clip, tts_wav, dubbed)

    sub_ass = out_dir / "sub_line.ass"
    sub_text = line.get("clean_text") or line["text"]
    sub_ass.write_text(
        _make_single_line_ass(ass_header, dur, sub_text),
        encoding="utf-8",
    )

    final = out_dir / "final.mkv"
    _mux_final(video_clip, dubbed, sub_ass, final)

    return {
        "video_clip": str(video_clip),
        "no_vocals_clip": str(no_vocals_clip),
        "dubbed": str(dubbed),
        "sub_ass": str(sub_ass),
        "final": str(final),
    }


def preview_clip(
    video_only: Path,
    no_vocals: Path,
    tts_wav: Path,
    ass_path: Path,
    line_index: int,
    start_sec: float,
    end_sec: float,
    text: str,
    offset_ms: float = 0.0,
    out_dir: Path | None = None,
) -> Path:
    out_dir = Path(out_dir) if out_dir else Path(".")
    out_dir.mkdir(parents=True, exist_ok=True)

    dur = end_sec - start_sec

    bg_clip = out_dir / "no_vocals_clip.wav"
    _slice_audio(no_vocals, start_sec, dur, bg_clip)

    voice_input = tts_wav
    delay_ms = offset_ms
    if offset_ms < -1:
        from tempfile import NamedTemporaryFile
        trim_fd = NamedTemporaryFile(suffix=".wav", delete=False)
        trim_fd.close()
        trim_path = Path(trim_fd.name)
        bin_path = _ffmpeg_bin()
        trim_start = abs(offset_ms) / 1000.0
        subprocess.run([bin_path, "-y", "-loglevel", "error",
                        "-ss", f"{trim_start:.3f}",
                        "-i", str(tts_wav),
                        "-c:a", "pcm_s16le",
                        str(trim_path)], check=True)
        voice_input = trim_path
        delay_ms = 0

    dubbed = out_dir / "dubbed.wav"
    _mix_background_voice(bg_clip, voice_input, dubbed, delay_ms=delay_ms)

    if voice_input != tts_wav:
        voice_input.unlink(missing_ok=True)

    sub_ass = out_dir / "sub_line.ass"
    header = ""
    if ass_path.exists():
        from anidub.ass import get_ass_header
        header = get_ass_header(ass_path)
    sub_ass.write_text(
        _make_single_line_ass(header, dur, text),
        encoding="utf-8",
    )

    video_clip = out_dir / "video_only.mkv"
    from anidub.extract import extract_video_clip
    extract_video_clip(video_only, start_sec, start_sec + dur, video_clip)

    preview = out_dir / "preview.mp4"
    _mux_preview(video_clip, dubbed, sub_ass, preview)
    return preview


def assemble_full(
    mkv_path: Path,
    ass_events: list,
    batch_out_dir: Path,
    full_no_vocals: Path,
    full_original_audio: Path,
    voiced_results: list,
    eo_ass_path: Path,
    errors: list | None = None,
) -> Path:
    _log.info("assemble_full -> build_full_episode")
    _log.info("  mkv=%s", mkv_path)
    _log.info("  batch_out_dir=%s", batch_out_dir)
    _log.info("  full_no_vocals=%s", full_no_vocals)
    _log.info("  full_original_audio=%s", full_original_audio)
    _log.info("  eo_ass_path=%s", eo_ass_path)
    _log.info("  voiced=%d errors=%d", len(voiced_results), len(errors or []))
    from anidub.full_episode import build_full_episode
    return build_full_episode(
        mkv_path, ass_events, batch_out_dir,
        full_no_vocals, full_original_audio,
        voiced_results, eo_ass_path, errors=errors,
    )