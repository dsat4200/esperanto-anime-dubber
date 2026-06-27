import gc
import time
from pathlib import Path

import numpy as np
import soundfile as sf
import torch


_WHISPER_CACHE: dict[str, tuple] = {}


def clear_whisper_cache():
    """Drop any cached Whisper models and reclaim their VRAM."""
    if not _WHISPER_CACHE:
        return
    for processor, model in _WHISPER_CACHE.values():
        try:
            if torch.cuda.is_available() and hasattr(model, "cpu"):
                model.cpu()
        except Exception:
            pass
    _WHISPER_CACHE.clear()
    gc.collect()
    if torch.cuda.is_available():
        torch.cuda.synchronize()
        torch.cuda.empty_cache()
        torch.cuda.ipc_collect()
        torch.cuda.empty_cache()


def _load_whisper(model_name: str, device: str, dtype: torch.dtype):
    if model_name in _WHISPER_CACHE:
        return _WHISPER_CACHE[model_name]
    from transformers import WhisperProcessor, WhisperForConditionalGeneration
    processor = WhisperProcessor.from_pretrained(model_name)
    model = WhisperForConditionalGeneration.from_pretrained(
        model_name,
        dtype=dtype,
        attn_implementation="sdpa",
    ).to(device)
    model.eval()
    _WHISPER_CACHE[model_name] = (processor, model)
    return processor, model


def transcribe_ref(
    ref_audio: Path,
    model_name: str = "openai/whisper-tiny",
    device: str | None = None,
    language: str = "japanese",
) -> dict:
    ref_audio = Path(ref_audio)
    if device is None:
        device = "cuda:0" if torch.cuda.is_available() else "cpu"
    dtype = torch.float16 if str(device).startswith("cuda") else torch.float32

    audio, sr = sf.read(str(ref_audio), dtype="float32")
    if audio.ndim > 1:
        audio = audio.mean(axis=1)
    audio = np.squeeze(audio)

    if sr != 16000:
        try:
            import librosa
            audio = librosa.resample(audio, orig_sr=sr, target_sr=16000)
        except ImportError:
            import torchaudio
            import torchaudio.functional as F
            t = torch.from_numpy(audio).unsqueeze(0)
            t = F.resample(t, sr, 16000)
            audio = t.squeeze(0).numpy()
        sr = 16000

    audio_duration = len(audio) / sr

    processor, model = _load_whisper(model_name, device, dtype)

    inputs = processor.feature_extractor(
        audio, sampling_rate=16000, return_tensors="pt"
    )
    input_features = inputs.input_features.to(device=device, dtype=dtype)

    forced_decoder_ids = processor.get_decoder_prompt_ids(
        language=language, task="transcribe"
    )

    t0 = time.perf_counter()
    with torch.inference_mode():
        predicted_ids = model.generate(
            input_features, forced_decoder_ids=forced_decoder_ids, max_new_tokens=128
        )
    inference_ms = (time.perf_counter() - t0) * 1000

    text = processor.batch_decode(predicted_ids, skip_special_tokens=True)[0].strip()

    del input_features
    del predicted_ids
    if torch.cuda.is_available():
        torch.cuda.synchronize()
        torch.cuda.empty_cache()

    return {
        "text": text,
        "model": model_name,
        "language": language,
        "inference_ms": round(inference_ms, 1),
        "audio_duration_sec": round(audio_duration, 2),
    }