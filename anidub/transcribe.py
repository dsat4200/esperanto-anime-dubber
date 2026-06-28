import gc
import logging
import time
from pathlib import Path

import numpy as np
import soundfile as sf
import torch

_log = logging.getLogger("anidub.transcribe")

_SAMPLE_RATE = 16000
_CHUNK_S = 30
_STRIDE_S = 25

_MIN_ASS_HEADER = (
    "[Script Info]\n"
    "ScriptType: v4.00+\n"
    "\n"
    "[V4+ Styles]\n"
    "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
    "Style: main,Arial,20,&H00FFFFFF,&H000088FF,&H00000000,&H80000000,0,0,0,0,100,100,0,0,1,2,0,2,10,10,10,1\n"
    "\n"
    "[Events]\n"
    "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
)


def _sec_to_ass_ts(total_sec: float) -> str:
    h = int(total_sec // 3600)
    m = int((total_sec % 3600) // 60)
    s = total_sec % 60
    return f"{h}:{m:02d}:{s:05.2f}"


def _extract_segments(predicted_ids: torch.Tensor, processor, offset_sec: float = 0.0) -> list[tuple[float, float, str]]:
    """Extract timestamped segments from a generate() output tensor.

    Each timestamp token encodes ``(token_id - timestamp_begin) * 0.02``
    seconds.  Groups of text tokens between timestamps form a segment.
    """
    timestamp_begin = getattr(processor.tokenizer, "timestamp_begin", None)
    if timestamp_begin is None:
        # Whisper timestamp tokens start after the last special token
        timestamp_begin = max(processor.tokenizer.all_special_ids) + 1
    time_precision = 0.02

    text_tokens: list[int] = []
    segment_start: float | None = None
    segments: list[tuple[float, float, str]] = []

    for tid in predicted_ids:
        tok = tid.item()
        if tok >= timestamp_begin:
            end_time = (tok - timestamp_begin) * time_precision
            if tok == timestamp_begin:
                end_time = 0.0
            if text_tokens:
                text = processor.decode(text_tokens, skip_special_tokens=True).strip()
                if text:
                    start = offset_sec + (segment_start if segment_start is not None else 0.0)
                    end = offset_sec + end_time
                    if end - start > 0.1:
                        segments.append((start, end, text))
                text_tokens = []
            segment_start = end_time
        elif tok >= processor.tokenizer.vocab_size or tok == processor.tokenizer.eos_token_id:
            continue
        else:
            text_tokens.append(tok)

    if text_tokens:
        text = processor.decode(text_tokens, skip_special_tokens=True).strip()
        if text:
            end = offset_sec + _CHUNK_S
            start = offset_sec + (segment_start if segment_start is not None else end - 1.0)
            if end - start > 0.1:
                segments.append((start, end, text))

    return segments


def transcribe_full_audio(
    audio_path: Path,
    out_ass_path: Path,
    model_name: str = "openai/whisper-large-v3-turbo",
    language: str | None = None,
    device: str | None = None,
) -> int:
    audio_path = Path(audio_path)
    out_ass_path = Path(out_ass_path)
    out_ass_path.parent.mkdir(parents=True, exist_ok=True)

    if device is None:
        device = "cuda:0" if torch.cuda.is_available() else "cpu"
    dtype = torch.float16 if str(device).startswith("cuda") else torch.float32

    _log.info("Reading audio: %s", audio_path)
    audio, sr = sf.read(str(audio_path), dtype="float32")
    if audio.ndim > 1:
        audio = audio.mean(axis=1)
    audio = np.squeeze(audio)

    if sr != _SAMPLE_RATE:
        _log.info("Resampling %d -> %d Hz", sr, _SAMPLE_RATE)
        try:
            import librosa
            audio = librosa.resample(audio, orig_sr=sr, target_sr=_SAMPLE_RATE)
        except ImportError:
            import torchaudio
            import torchaudio.functional as F
            t = torch.from_numpy(audio).unsqueeze(0)
            t = F.resample(t, sr, _SAMPLE_RATE)
            audio = t.squeeze(0).numpy()
        sr = _SAMPLE_RATE

    _log.info("Loading whisper model=%s device=%s", model_name, device)
    from anidub.asr import _load_whisper
    processor, model = _load_whisper(model_name, device, dtype)

    forced_decoder_ids = processor.get_decoder_prompt_ids(
        language=language or "en", task="transcribe"
    ) or None

    max_new = 448
    if forced_decoder_ids:
        max_new = 448 - len(forced_decoder_ids)

    t0 = time.perf_counter()
    total_samples = len(audio)
    chunk_samples = _CHUNK_S * _SAMPLE_RATE
    stride_samples = _STRIDE_S * _SAMPLE_RATE

    all_segments: list[tuple[float, float, str]] = []
    last_text = ""

    for offset_samples in range(0, total_samples, stride_samples):
        chunk = audio[offset_samples:offset_samples + chunk_samples]
        if len(chunk) < _SAMPLE_RATE:
            break

        inputs = processor.feature_extractor(
            chunk, sampling_rate=_SAMPLE_RATE, return_tensors="pt"
        )
        input_features = inputs.input_features.to(device=device, dtype=dtype)

        with torch.inference_mode():
            predicted_ids = model.generate(
                input_features,
                forced_decoder_ids=forced_decoder_ids,
                return_timestamps=True,
                max_new_tokens=max_new,
            )

        offset_sec = offset_samples / _SAMPLE_RATE
        segs = _extract_segments(predicted_ids[0], processor, offset_sec)

        for s, e, t in segs:
            # Deduplicate across strides
            if t == last_text:
                continue
            all_segments.append((s, e, t))
            last_text = t

        del input_features, predicted_ids

    gc.collect()
    if torch.cuda.is_available():
        torch.cuda.synchronize()
        torch.cuda.empty_cache()

    _log.info("Writing %d segments to ASS", len(all_segments))

    lines: list[str] = []
    prev_end = -1.0
    min_gap = 0.05

    for i, (start_s, end_s, text) in enumerate(all_segments):
        start_s = float(start_s)
        end_s = float(end_s)
        if start_s < prev_end + min_gap:
            start_s = prev_end + 0.01
        if end_s <= start_s:
            end_s = start_s + 1.0
        prev_end = end_s
        if i + 1 < len(all_segments):
            next_start = all_segments[i + 1][0]
            if end_s > next_start:
                end_s = next_start - 0.01

        start_ts = _sec_to_ass_ts(start_s)
        end_ts = _sec_to_ass_ts(end_s)
        lines.append(f"Dialogue: 0,{start_ts},{end_ts},main,,0000,0000,0000,,{text}")

    with out_ass_path.open("w", encoding="utf-8") as f:
        f.write(_MIN_ASS_HEADER)
        for line in lines:
            f.write(line + "\n")

    elapsed = time.perf_counter() - t0
    _log.info(
        "Transcription complete: %d segments in %.1fs -> %s",
        len(lines), elapsed, out_ass_path,
    )
    return len(lines)
