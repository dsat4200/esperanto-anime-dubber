import concurrent.futures
import re
import tempfile
from pathlib import Path

import numpy as np
import soundfile as sf

from anidub.assembler import assemble_line
from anidub.config import MODEL_NAME
from anidub.esperanto import build_instruct_prompt
from anidub.extract import extract_ref_clip_from_wav, trim_silence, fit_audio_to_duration

REF_CLIP_DUR = 3.0
SILENCE_TOP_DB = 45
MIN_TRANSCRIPTION_CHARS = 5
SAFETY_MARGIN_SEC = 0.1


def process_line(
    source_wav: Path,
    mkv_path: Path,
    line: dict,
    whisper_model: str,
    out_dir: Path,
    tts_backend,
    full_no_vocals: Path,
    ass_header: str,
    voice_timeout: int = 120,
) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)

    instruct = build_instruct_prompt(line.get("name") or None)
    target_dur = (line["end_sec"] - line["start_sec"]) - SAFETY_MARGIN_SEC

    ref_wav = out_dir / "ref.wav"
    extract_ref_clip_from_wav(
        source_wav,
        line["start_sec"],
        max_dur=REF_CLIP_DUR,
        next_line_start=line.get("next_line_start"),
        out_path=ref_wav,
    )

    ex = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    try:
        fut = ex.submit(
            tts_backend.generate,
            text=line["clean_text"],
            ref_audio=ref_wav,
            target_duration=target_dur,
            instruct=instruct,
        )
        try:
            result = fut.result(timeout=voice_timeout)
        except concurrent.futures.TimeoutError:
            ex.shutdown(wait=False)
            import torch
            if torch.cuda.is_available():
                torch.cuda.synchronize()
            raise RuntimeError(
                f"Voice generation timed out after {voice_timeout}s "
                f"for line: {line['clean_text'][:80]!r}"
            )
    except Exception:
        ex.shutdown(wait=False)
        raise
    else:
        ex.shutdown(wait=True)

    ref_transcription = result["diagnostics"].get("ref_transcription")
    transcript_text = ""
    if ref_transcription:
        transcript_text = ref_transcription.get("text", "").strip()
        if len(transcript_text) < MIN_TRANSCRIPTION_CHARS:
            raise RuntimeError(
                f"ref transcription too short ({len(transcript_text)} chars); "
                f"ref clip may be silent/SFX"
            )

    raw_dur = result["output_duration"]
    raw_wav = result["wav"]
    sr = result["sr"]

    tts_raw = out_dir / "tts_raw.wav"
    sf.write(tts_raw, raw_wav, sr)

    trimmed = trim_silence(raw_wav, sr, top_db=SILENCE_TOP_DB)
    effective_dur = len(trimmed) / sr

    atempo_info = "none"
    if effective_dur > target_dur:
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp_out:
            tmp_path = Path(tmp_out.name)
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp_in:
            tmp_input = Path(tmp_in.name)
        try:
            sf.write(tmp_input, trimmed, sr)
            fit_result = fit_audio_to_duration(tmp_input, tmp_path, target_dur)
            atempo_info = fit_result.get("atempo_chain", "unknown")
            fitted_wav, fitted_sr = sf.read(tmp_path)
            trimmed = np.asarray(fitted_wav, dtype=np.float32).T
            sr = fitted_sr
            effective_dur = len(trimmed) / sr
        finally:
            Path(tmp_input).unlink(missing_ok=True)
            Path(tmp_path).unlink(missing_ok=True)

    slack_ms = (target_dur - effective_dur) * 1000.0

    tts_out = out_dir / "tts.wav"
    sf.write(tts_out, trimmed, sr)

    assembly = assemble_line(
        mkv_path, line, tts_out, full_no_vocals,
        ass_header, out_dir,
    )

    return {
        "line_index": line["index"],
        "start_sec": line["start_sec"],
        "end_sec": line["end_sec"],
        "text": line["clean_text"],
        "target_dur": target_dur,
        "raw_dur": raw_dur,
        "effective_dur": effective_dur,
        "slack_ms": slack_ms,
        "inference_ms": result["diagnostics"].get("inference_ms"),
        "cuda_mem_after_mb": result["diagnostics"].get("cuda_mem_after_mb"),
        "atempo": atempo_info,
        "ref_transcription": transcript_text,
        "diagnostics": result["diagnostics"],
        "assembly": assembly,
        "tts_wav": str(tts_out),
        "tts_sr": sr,
    }


def clone_line(
    text: str,
    ref_audio: Path,
    target_duration: float,
    instruct: str,
    out_path: Path,
    whisper_model: str = "openai/whisper-tiny",
    instruct_extra: str | None = None,
    speed_factor: float = 1.0,
    voice_timeout: int = 120,
    backend=None,
) -> dict:
    if instruct_extra:
        instruct = instruct + "\n" + instruct_extra
    target_dur = target_duration - SAFETY_MARGIN_SEC

    _owns_backend = backend is None
    if _owns_backend:
        from anidub.tts.omnivoice import OmniVoiceTTSBackend
        backend = OmniVoiceTTSBackend(whisper_model=whisper_model)

    try:
        ex = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        try:
            fut = ex.submit(
                backend.generate,
                text=text,
                ref_audio=ref_audio,
                target_duration=target_dur,
                instruct=instruct,
            )
            try:
                result = fut.result(timeout=voice_timeout)
            except concurrent.futures.TimeoutError:
                ex.shutdown(wait=False)
                import torch
                if torch.cuda.is_available():
                    torch.cuda.synchronize()
                raise RuntimeError(
                    f"Voice generation timed out after {voice_timeout}s "
                    f"for text: {text[:80]!r}"
                )
        except Exception:
            ex.shutdown(wait=False)
            raise
        else:
            ex.shutdown(wait=True)

        raw_wav = result["wav"]
        sr = result["sr"]
        out_dur = result["output_duration"]

        trimmed = trim_silence(raw_wav, sr, top_db=SILENCE_TOP_DB)
        effective_dur = len(trimmed) / sr

        if abs(speed_factor - 1.0) > 0.01:
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp_out:
                speed_tmp = Path(tmp_out.name)
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp_in:
                speed_in = Path(tmp_in.name)
            try:
                sf.write(speed_in, trimmed, sr)
                fit_audio_to_duration(speed_in, speed_tmp, effective_dur / speed_factor)
                sped_wav, sped_sr = sf.read(speed_tmp)
                trimmed = np.asarray(sped_wav, dtype=np.float32).T
                sr = sped_sr
                effective_dur = len(trimmed) / sr
            finally:
                Path(speed_in).unlink(missing_ok=True)
                Path(speed_tmp).unlink(missing_ok=True)

        if effective_dur > target_dur:
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp_out:
                tmp_path = Path(tmp_out.name)
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp_in:
                tmp_input = Path(tmp_in.name)
            try:
                sf.write(tmp_input, trimmed, sr)
                fit_audio_to_duration(tmp_input, tmp_path, target_dur)
                fitted_wav, fitted_sr = sf.read(tmp_path)
                trimmed = np.asarray(fitted_wav, dtype=np.float32).T
                sr = fitted_sr
                effective_dur = len(trimmed) / sr
            finally:
                Path(tmp_input).unlink(missing_ok=True)
                Path(tmp_path).unlink(missing_ok=True)

        out_path.parent.mkdir(parents=True, exist_ok=True)
        sf.write(out_path, trimmed, sr)

        return {
            "wav": trimmed,
            "sr": sr,
            "output_duration": out_dur,
            "effective_duration": effective_dur,
            "inference_ms": result["diagnostics"].get("inference_ms"),
            "diagnostics": result["diagnostics"],
        }
    finally:
        # If we created the backend locally (CLI / anidub-test-voice path),
        # tear it down so a Python process running many clips in series does
        # not stack model weights in VRAM between clips.
        if _owns_backend:
            import gc
            import torch
            try:
                backend.unload()
            except Exception:
                pass
            del backend
            gc.collect()
            if torch.cuda.is_available():
                torch.cuda.synchronize()
                torch.cuda.empty_cache()
                torch.cuda.ipc_collect()
                torch.cuda.empty_cache()


def get_op_ed_ranges(events: list) -> tuple[float, float, float, float]:
    op_events = [e for e in events if "op" in e["style"].lower()]
    ed_events = [e for e in events if "ed" in e["style"].lower()]

    intro_start = float("inf")
    intro_end = 0.0
    for e in op_events:
        intro_start = min(intro_start, e["start_sec"])
        intro_end = max(intro_end, e["end_sec"])

    outro_start = float("inf")
    outro_end = 0.0
    for e in ed_events:
        outro_start = min(outro_start, e["start_sec"])
        outro_end = max(outro_end, e["end_sec"])

    if intro_start == float("inf"):
        intro_start = 0.0
        intro_end = 0.0
    if outro_start == float("inf"):
        outro_start = float("inf")
        outro_end = float("inf")

    return intro_start, intro_end, outro_start, outro_end


def is_in_range(start_sec: float, end_sec: float,
                range_start: float, range_end: float) -> bool:
    if range_start == float("inf"):
        return False
    return not (end_sec <= range_start or start_sec >= range_end)


def make_line_dir_name(line: dict) -> str:
    start_s = line["start_sec"]
    end_s = line["end_sec"]
    ts = f"{int(start_s//3600):d}-{int((start_s%3600)//60):02d}-{start_s%60:05.2f}_"
    ts += f"{int(end_s//3600):d}-{int((end_s%3600)//60):02d}-{end_s%60:05.2f}"
    text_slug = re.sub(r"[^a-zA-Z0-9ĉĝĥĵŝŭĈĜĤĴŜŬ_-]", "_", line.get("clean_text", ""))
    text_slug = re.sub(r"_+", "_", text_slug).strip("_")[:50]
    return f"line_{line['index']:03d}_{ts}_{text_slug}"