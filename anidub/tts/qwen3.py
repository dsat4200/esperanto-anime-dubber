import json
import os
import tempfile
import time
from pathlib import Path

import numpy as np
import soundfile as sf
import torch

from anidub.extract import fit_audio_to_duration

# Valid Qwen3-TTS preset speakers (CustomVoice variant)
VALID_SPEAKERS = (
    "Vivian", "Serena", "Uncle_Fu", "Dylan", "Eric",
    "Ryan", "Aiden", "Ono_Anna", "Sohee",
)


class Qwen3TTSBackend:
    def __init__(
        self,
        variant: str = "custom",
        speaker: str = "Serena",
        dtype=torch.bfloat16,
    ):
        if variant not in ("custom", "base", "design"):
            raise ValueError(f"Unknown qwen variant: {variant}")
        self.variant = variant
        self.speaker = speaker
        self.dtype = dtype
        self.device = "cuda:0" if torch.cuda.is_available() else "cpu"

        model_map = {
            "custom": "Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice",
            "base":  "Qwen/Qwen3-TTS-12Hz-1.7B-Base",
            "design": "Qwen/Qwen3-TTS-12Hz-1.7B-VoiceDesign",
        }
        self.model_id = model_map[variant]

        try:
            from qwen_tts import Qwen3TTSModel
        except ImportError as e:
            raise ImportError(
                "qwen-tts not installed. It conflicts with omnivoice's "
                "transformers>=5.3 requirement. To use Qwen3-TTS, create a "
                "separate venv: `pip install qwen-tts` and run this tool from "
                "that venv with --engine qwen3."
            ) from e
        self._model = Qwen3TTSModel.from_pretrained(
            self.model_id,
            device_map=self.device,
            dtype=dtype,
            attn_implementation="sdpa",
        )

    def generate(
        self,
        text: str,
        ref_audio: Path | None,
        target_duration: float,
        instruct: str,
    ) -> dict:
        params: dict = {"text": text, "language": "Auto"}

        if self.variant == "custom":
            params["speaker"] = self.speaker
            params["instruct"] = instruct
            call = self._model.generate_custom_voice
        elif self.variant == "base":
            if ref_audio is None:
                raise ValueError("Base variant requires ref_audio")
            params["ref_audio"] = str(Path(ref_audio).resolve())
            params["ref_text"] = None
            call = self._model.generate_voice_clone
        else:
            params["instruct"] = instruct
            call = self._model.generate_voice_design

        diagnostics = {
            "model_id": self.model_id,
            "device": self.device,
            "dtype": str(self.dtype),
            "attn_impl": "sdpa",
            "variant": self.variant,
            "speaker": self.speaker if self.variant == "custom" else None,
            "params": {k: v for k, v in params.items()},
            "prompt_text": instruct,
            "prompt_chars": len(instruct),
            "target_duration": target_duration,
        }

        if torch.cuda.is_available():
            torch.cuda.empty_cache()
        mem_before = (
            torch.cuda.memory_allocated() / 1024**2
            if torch.cuda.is_available() else 0.0
        )
        diagnostics["cuda_mem_before_mb"] = round(mem_before, 1)

        t0 = time.perf_counter()
        wavs, sr = call(**params)
        inference_ms = (time.perf_counter() - t0) * 1000

        mem_after = (
            torch.cuda.memory_allocated() / 1024**2
            if torch.cuda.is_available() else 0.0
        )
        diagnostics["inference_ms"] = round(inference_ms, 1)
        diagnostics["cuda_mem_after_mb"] = round(mem_after, 1)

        wav = np.asarray(wavs[0] if isinstance(wavs, list) else wavs, dtype=np.float32)
        raw_dur = wav.shape[-1] / sr
        diagnostics["raw_output_duration"] = raw_dur

        post_info: dict
        if abs(raw_dur - target_duration) > 0.01:
            with tempfile.NamedTemporaryFile(
                suffix=".wav", delete=False, delete_on_close=False
            ) as raw_f:
                raw_path = Path(raw_f.name)
            with tempfile.NamedTemporaryFile(
                suffix=".wav", delete=False, delete_on_close=False
            ) as fit_f:
                fit_path = Path(fit_f.name)
            try:
                sf.write(raw_path, wav, sr)
                post_info = fit_audio_to_duration(raw_path, fit_path, target_duration)
                fit_wav, fit_sr = sf.read(fit_path)
                wav = np.asarray(fit_wav, dtype=np.float32).T
                sr = fit_sr
            finally:
                raw_path.unlink(missing_ok=True)
                fit_path.unlink(missing_ok=True)
        else:
            post_info = {
                "atempo_chain": "none",
                "postprocess": "none (already fits)",
                "final_duration": raw_dur,
            }

        diagnostics["postprocess"] = post_info
        out_dur = wav.shape[-1] / sr

        return {
            "wav": wav,
            "sr": sr,
            "output_duration": out_dur,
            "diagnostics": diagnostics,
        }