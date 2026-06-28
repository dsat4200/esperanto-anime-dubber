import gc
import logging
import time
from pathlib import Path

import torch

_log = logging.getLogger("anidub.transcribe")

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

    _log.info("Loading whisper pipeline model=%s device=%s", model_name, device)
    t0 = time.perf_counter()

    from transformers import pipeline

    pipe = pipeline(
        "automatic-speech-recognition",
        model=model_name,
        device=device,
        torch_dtype=dtype,
    )

    gen_kwargs = {"task": "transcribe"}
    if language:
        gen_kwargs["language"] = language

    _log.info("Running transcription (lang=%s)...", language or "auto")
    result = pipe(
        str(audio_path),
        return_timestamps=True,
        chunk_length_s=30,
        generate_kwargs=gen_kwargs,
    )

    del pipe
    gc.collect()
    if torch.cuda.is_available():
        torch.cuda.synchronize()
        torch.cuda.empty_cache()

    chunks = result.get("chunks", [])
    if not chunks:
        _log.warning("No chunks returned; falling back to single-line ASS")
        chunks = [{"timestamp": (0.0, 30.0), "text": result.get("text", "")}]

    lines: list[str] = []
    for ch in chunks:
        ts = ch.get("timestamp", (0.0, 0.0))
        start_s = float(ts[0])
        end_s = float(ts[1])
        text = ch["text"].strip()
        if not text:
            continue
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
