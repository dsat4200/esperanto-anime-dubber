import os
import shutil
from pathlib import Path

import numpy as np
import soundfile as sf
import torch
import demucs.audio
import demucs.separate
import demucs.repitch
from demucs.separate import main as demucs_main
from demucs.audio import prevent_clip, i16_pcm

from anidub.config import MODEL_NAME

_ORIGINAL_SAVE_AUDIO = demucs.audio.save_audio


def _patched_save_audio(wav, path, samplerate, bitrate=320, clip="rescale",
                        bits_per_sample=16, as_float=False, preset=2):
    path = Path(path)
    if path.suffix.lower() == ".wav":
        wav = prevent_clip(wav, mode=clip)
        if as_float:
            data = wav.detach().cpu().numpy()
            if data.ndim == 2:
                data = data.T
            subtype = "FLOAT" if bits_per_sample == 32 else "DOUBLE"
            sf.write(str(path), data.astype(np.float32 if subtype == "FLOAT" else np.float64), samplerate, subtype=subtype)
            return
        wav = i16_pcm(wav)
        data = wav.detach().cpu().numpy().astype(np.int16)
        if data.ndim == 2:
            data = data.T
        sf.write(str(path), data, samplerate, subtype="PCM_16")
        return
    return _ORIGINAL_SAVE_AUDIO(wav, str(path), samplerate, bitrate=bitrate,
                               clip=clip, bits_per_sample=bits_per_sample,
                               as_float=as_float, preset=preset)


demucs.audio.save_audio = _patched_save_audio
demucs.separate.save_audio = _patched_save_audio
demucs.repitch.save_audio = _patched_save_audio


def separate_audio(audio_path, out_dir):
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    audio_path = Path(audio_path)
    stem_name = audio_path.stem
    model_dir = out_dir / MODEL_NAME
    stem_dir = model_dir / stem_name

    attempts = [
        (
            ["-n", MODEL_NAME, "--two-stems", "vocals", "-o", str(out_dir), str(audio_path)],
            {},
        ),
        (
            ["-n", MODEL_NAME, "--two-stems", "vocals", "--segment", "7", "-o", str(out_dir), str(audio_path)],
            {},
        ),
        (
            ["-n", MODEL_NAME, "--two-stems", "vocals", "--segment", "4", "-o", str(out_dir), str(audio_path)],
            {"PYTORCH_NO_CUDA_MEMORY_CACHING": "1"},
        ),
        (
            ["-n", MODEL_NAME, "--two-stems", "vocals", "-d", "cpu", "-o", str(out_dir), str(audio_path)],
            {},
        ),
    ]

    last_error = None
    for args, env_extra in attempts:
        for k, v in env_extra.items():
            os.environ[k] = v
        try:
            demucs_main(args)
            break
        except SystemExit as e:
            if e.code is not None and e.code != 0:
                last_error = RuntimeError(f"Demucs exited with code {e.code}")
                _cleanup(model_dir)
                continue
            break
        except Exception as e:
            last_error = e
            _cleanup(model_dir)
            continue
        finally:
            for k in env_extra:
                os.environ.pop(k, None)
    else:
        raise RuntimeError(f"All separation attempts failed. Last error: {last_error}")

    vocals = stem_dir / "vocals.wav"
    no_vocals = stem_dir / "no_vocals.wav"
    if not vocals.exists() or not no_vocals.exists():
        raise RuntimeError(
            f"Demucs completed but output files not found in {stem_dir}"
        )

    final_vocals = out_dir / "vocals.wav"
    final_no_vocals = out_dir / "no_vocals.wav"
    if final_vocals.exists():
        final_vocals.unlink()
    if final_no_vocals.exists():
        final_no_vocals.unlink()
    vocals.rename(final_vocals)
    no_vocals.rename(final_no_vocals)

    _cleanup(model_dir)
    return {"vocals": final_vocals, "no_vocals": final_no_vocals}


def _cleanup(path):
    if path.exists():
        shutil.rmtree(path, ignore_errors=True)
