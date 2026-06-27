import time
from pathlib import Path

import numpy as np
import torch


class OmniVoiceTTSBackend:
    def __init__(
        self,
        model_id: str = "k2-fsa/OmniVoice",
        dtype=torch.float16,
        whisper_model: str = "openai/whisper-tiny",
    ):
        from omnivoice import OmniVoice
        self.model_id = model_id
        self.dtype = dtype
        self.whisper_model = whisper_model
        self.device = "cuda:0" if torch.cuda.is_available() else "cpu"
        self._model = OmniVoice.from_pretrained(
            model_id,
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
        num_step: int = 32,
    ) -> dict:
        if ref_audio is None:
            raise ValueError("OmniVoice backend requires ref_audio")
        ref_path = str(Path(ref_audio).resolve())

        from anidub.asr import transcribe_ref
        ref_transcription = transcribe_ref(
            Path(ref_audio),
            model_name=self.whisper_model,
            device=self.device,
            language="japanese",
        )
        ref_text = ref_transcription["text"]

        params = {
            "text": text,
            "ref_audio": ref_path,
            "ref_text": ref_text,
            "language_id": "eo",
            "duration": target_duration,
            "num_step": num_step,
            "postprocess_output": False,
        }
        diagnostics = {
            "model_id": self.model_id,
            "device": self.device,
            "dtype": str(self.dtype),
            "attn_impl": "sdpa",
            "params": dict(params),
            "prompt_text": instruct,
            "prompt_chars": len(instruct),
            "target_duration": target_duration,
            "ref_transcription": ref_transcription,
        }

        if torch.cuda.is_available():
            torch.cuda.empty_cache()
        mem_before = (
            torch.cuda.memory_allocated() / 1024**2
            if torch.cuda.is_available() else 0.0
        )
        diagnostics["cuda_mem_before_mb"] = round(mem_before, 1)

        t0 = time.perf_counter()
        with torch.inference_mode():
            audio_list = self._model.generate(
                text=params["text"],
                ref_audio=params["ref_audio"],
                ref_text=params["ref_text"],
                language_id=params["language_id"],
                duration=params["duration"],
                num_step=params["num_step"],
                postprocess_output=params["postprocess_output"],
            )
        inference_ms = (time.perf_counter() - t0) * 1000

        mem_after = (
            torch.cuda.memory_allocated() / 1024**2
            if torch.cuda.is_available() else 0.0
        )

        wav = np.asarray(audio_list[0], dtype=np.float32)
        sr = 24000
        out_dur = wav.shape[-1] / sr

        diagnostics["inference_ms"] = round(inference_ms, 1)
        diagnostics["cuda_mem_after_mb"] = round(mem_after, 1)
        diagnostics["output_duration"] = out_dur
        diagnostics["postprocess"] = "none (native duration control)"

        if torch.cuda.is_available():
            torch.cuda.empty_cache()

        return {
            "wav": wav,
            "sr": sr,
            "output_duration": out_dur,
            "diagnostics": diagnostics,
        }