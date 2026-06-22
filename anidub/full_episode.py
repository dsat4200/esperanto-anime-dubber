import math
import subprocess
from pathlib import Path

import numpy as np
import soundfile as sf

from anidub.config import get_ffmpeg_location
from anidub.pipeline import get_op_ed_ranges, is_in_range


_MIX_WEIGHT_BG = 1.0
_MIX_WEIGHT_VOICE = 0.8
_GLOBAL_VOLUME = 1.5
_TARGET_SR = 44100


def _ffmpeg_bin():
    loc = get_ffmpeg_location()
    if not loc:
        raise RuntimeError("ffmpeg not found")
    return str(Path(loc) / "ffmpeg.exe")


def _resample_mono(wav: np.ndarray, src_sr: int, dst_sr: int) -> np.ndarray:
    if src_sr == dst_sr:
        return wav
    try:
        import librosa
        return librosa.resample(wav.astype(np.float64), orig_sr=src_sr, target_sr=dst_sr).astype(np.float32)
    except ImportError:
        from scipy.signal import resample_poly
        import math
        g = math.gcd(src_sr, dst_sr)
        up = dst_sr // g
        down = src_sr // g
        return resample_poly(wav.astype(np.float64), up, down).astype(np.float32)


def build_full_episode(
    mkv_path: Path,
    ass_events: list,
    batch_out_dir: Path,
    full_no_vocals: Path,
    full_original_audio: Path,
    voiced_results: list[dict],
    ass_path: Path,
    errors: list[dict] | None = None,
) -> Path:
    batch_out_dir.mkdir(parents=True, exist_ok=True)

    nv, nv_sr = sf.read(str(full_no_vocals), dtype="float32")
    if nv.ndim == 1:
        nv = np.stack([nv, nv], axis=1)
    if nv_sr != _TARGET_SR:
        nv_l = _resample_mono(nv[:, 0], nv_sr, _TARGET_SR)
        nv_r = _resample_mono(nv[:, 1], nv_sr, _TARGET_SR)
        nv = np.column_stack([nv_l, nv_r])

    orig, orig_sr = sf.read(str(full_original_audio), dtype="float32")
    if orig.ndim == 1:
        orig = np.stack([orig, orig], axis=1)
    if orig_sr != _TARGET_SR:
        orig_l = _resample_mono(orig[:, 0], orig_sr, _TARGET_SR)
        orig_r = _resample_mono(orig[:, 1], orig_sr, _TARGET_SR)
        orig = np.column_stack([orig_l, orig_r])

    total_samples = max(nv.shape[0], orig.shape[0])

    intro_start_s, intro_end_s, outro_start_s, outro_end_s = get_op_ed_ranges(ass_events)

    intro_start = int(intro_start_s * _TARGET_SR)
    intro_end = int(intro_end_s * _TARGET_SR)
    outro_start = int(outro_start_s * _TARGET_SR) if outro_start_s < float("inf") else total_samples
    outro_end = int(outro_end_s * _TARGET_SR) if outro_end_s < float("inf") else total_samples

    intro_start = min(intro_start, total_samples)
    intro_end = min(intro_end, total_samples)
    outro_start = min(outro_start, total_samples)
    outro_end = min(outro_end, total_samples)

    voice = np.zeros(total_samples, dtype=np.float32)
    for vr in voiced_results:
        tts_path = Path(vr["tts_wav"])
        if not tts_path.exists():
            continue
        tts, tts_sr = sf.read(str(tts_path), dtype="float32")
        if tts.ndim > 1:
            tts = tts[:, 0]
        tts = _resample_mono(tts.astype(np.float32), tts_sr, _TARGET_SR)
        offset = int(vr["start_sec"] * _TARGET_SR)
        end_idx = offset + len(tts)
        if end_idx > total_samples:
            tts = tts[:total_samples - offset]
            end_idx = total_samples
        voice[offset:end_idx] += tts.astype(np.float32) * _MIX_WEIGHT_VOICE

    voice *= _GLOBAL_VOLUME
    voice_stereo = np.column_stack([voice, voice])

    output = nv[:total_samples].copy() if nv.shape[0] >= total_samples else np.pad(
        nv, ((0, total_samples - nv.shape[0]), (0, 0)), mode="constant",
    )[:total_samples]

    output[:intro_start] = nv[:intro_start].copy() + voice_stereo[:intro_start]

    if intro_end > intro_start:
        src_slice = orig[intro_start:intro_end]
        output[intro_start:intro_end] = src_slice.astype(np.float32)

    if outro_start > intro_end:
        body_len = outro_start - intro_end
        body_bg = output[intro_end:outro_start].copy()
        body_voice = voice_stereo[intro_end:outro_start]
        body_mixed = body_bg + body_voice
        output[intro_end:outro_start] = body_mixed

    if outro_end > outro_start:
        src_slice = orig[outro_start:outro_end]
        output[outro_start:outro_end] = src_slice.astype(np.float32)

    if outro_end < total_samples:
        output[outro_end:] = nv[outro_end:total_samples].copy() + voice_stereo[outro_end:]

    _GAP_THRESHOLD = 2.0
    voiced_sorted = sorted(voiced_results, key=lambda r: r["start_sec"])
    prev_end = intro_end_s
    for vr in voiced_sorted:
        gap = vr["start_sec"] - prev_end
        if gap > _GAP_THRESHOLD:
            gs = int(prev_end * _TARGET_SR)
            ge = int(vr["start_sec"] * _TARGET_SR)
            gs = min(gs, total_samples)
            ge = min(ge, total_samples)
            if ge > gs:
                output[gs:ge] = orig[gs:ge].astype(np.float32)
        prev_end = max(prev_end, vr["end_sec"])

    # Also fill gap after last voiced line to outro_start
    if outro_start_s < float("inf"):
        gap = outro_start_s - prev_end
        if gap > _GAP_THRESHOLD:
            gs = int(prev_end * _TARGET_SR)
            ge = int(outro_start_s * _TARGET_SR)
            output[gs:ge] = orig[gs:ge].astype(np.float32)

    if errors:
        for err in errors:
            es = int(err["start_sec"] * _TARGET_SR)
            ee = int(err["end_sec"] * _TARGET_SR)
            es = min(es, total_samples)
            ee = min(ee, total_samples)
            if ee > es:
                output[es:ee] = orig[es:ee].astype(np.float32)

    output *= _GLOBAL_VOLUME
    output = np.clip(output, -1.0, 1.0)

    dubbed_wav = batch_out_dir / "full_dubbed.wav"
    sf.write(str(dubbed_wav), output, _TARGET_SR)

    ep_name = mkv_path.stem.replace(".mkv", "")
    final_mkv = batch_out_dir / f"{ep_name}_Dubbed.mkv"
    bin_path = _ffmpeg_bin()
    subprocess.run([
        bin_path, "-y", "-loglevel", "error",
        "-i", str(mkv_path),
        "-i", str(dubbed_wav),
        "-i", str(ass_path),
        "-map", "0:v:0",
        "-map", "1:a",
        "-map", "0:a:0",
        "-map", "2:s",
        "-c:v", "copy",
        "-c:a:0", "aac", "-b:a:0", "256k",
        "-c:a:1", "copy",
        "-c:s", "copy",
        "-metadata:s:a:0", "language=epo",
        "-metadata:s:a:1", "language=jpn",
        str(final_mkv),
    ], check=True)

    return final_mkv