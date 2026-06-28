import json as _json
import logging
import math
import shutil
import subprocess
from pathlib import Path

import numpy as np
import soundfile as sf

from anidub.config import get_ffmpeg_location
from anidub.pipeline import get_op_ed_ranges, is_in_range


_log = logging.getLogger("anidub.full_episode")

_MIX_WEIGHT_BG = 1.0
_MIX_WEIGHT_VOICE = 0.8
_GLOBAL_VOLUME = 1.5
_TARGET_SR = 44100
_SILENCE_THRESHOLD = 1e-4
_TARGET_VOICE_PEAK = 0.5
_HEADROOM_TARGET = 0.95

_EPO_AUDIO_FILENAME = "epo_audio.mka"


def _ffmpeg_bin():
    loc = get_ffmpeg_location()
    if not loc:
        raise RuntimeError("ffmpeg not found")
    return str(Path(loc) / "ffmpeg.exe")


def _ffprobe_bin():
    return str(Path(_ffmpeg_bin()).parent / "ffprobe.exe")


_CODEC_DISPATCH = {
    "opus":   ("libopus",    "256k", 48000),
    "aac":    ("aac",        "256k", None),
    "flac":   ("flac",       None,   None),
    "mp3":    ("libmp3lame", "320k", None),
    "ac3":    ("ac3",        "448k", None),
    "vorbis": ("libvorbis",  "256k", None),
}


def _codec_id_to_ffenc(codec_str: str) -> tuple[str, str | None, int | None]:
    """Accepts either a 'codec' (e.g. 'Opus') or codec_id ('A_OPUS') form."""
    s = (codec_str or "").lower()
    codec_key = ""
    if s in ("opus", "a_opus"):
        codec_key = "opus"
    elif "aac" in s:
        codec_key = "aac"
    elif s in ("flac", "a_flac"):
        codec_key = "flac"
    elif s in ("mp3", "a_mp3", "mpeg-1 layer 3"):
        codec_key = "mp3"
    elif s in ("ac3", "a_ac3"):
        codec_key = "ac3"
    elif s in ("vorbis", "a_vorbis"):
        codec_key = "vorbis"
    return _CODEC_DISPATCH.get(codec_key, ("aac", "256k", None))


def _probe_ffprobe_tracks(file: Path) -> list[dict]:
    ffprobe = _ffprobe_bin()
    cmd = [ffprobe, "-v", "error",
           "-show_entries", "stream=index,codec_type,codec_name:stream_tags=language,title",
           "-of", "json", str(file)]
    _log.info("ffprobe cmd: %s", " ".join(cmd))
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        _log.error("ffprobe FAILED rc=%d stderr:\n%s", proc.returncode, proc.stderr or "(empty)")
        raise RuntimeError(
            f"ffprobe failed (rc={proc.returncode}) on {file}. "
            f"stderr:\n{proc.stderr or '(empty)'}"
        )
    data = _json.loads(proc.stdout)
    tracks: list[dict] = []
    for s in data.get("streams", []):
        tags = s.get("tags", {}) or {}
        tracks.append({
            "index": s["index"],
            "type": s.get("codec_type"),
            "codec": s.get("codec_name", ""),
            "language": tags.get("language", ""),
            "title": tags.get("title", ""),
        })
    for t in tracks:
        _log.info("  track idx=%d type=%s codec=%s lang=%r title=%r",
                  t["index"], t["type"], t["codec"], t["language"], t["title"])
    return tracks


def _encode_epo_audio(dubbed_wav: Path, out_mka: Path, source_codec_id: str) -> Path:
    if out_mka.exists() and dubbed_wav.exists() and \
       out_mka.stat().st_mtime >= dubbed_wav.stat().st_mtime:
        _log.info("epo_audio cache HIT: %s (newer than full_dubbed.wav)", out_mka)
        return out_mka
    _log.info("epo_audio cache MISS -> re-encoding")

    encoder, bitrate, ar = _codec_id_to_ffenc(source_codec_id)
    _log.info("epo audio encoder: src_codec_id=%s -> encoder=%s bitrate=%s sr=%s",
              source_codec_id, encoder, bitrate, ar if ar else "(unchanged)")

    bin_path = _ffmpeg_bin()
    cmd: list[str] = [bin_path, "-y", "-loglevel", "error", "-i", str(dubbed_wav)]
    cmd += ["-map", "0:a:0", "-c:a:0", encoder]
    if bitrate:
        cmd += ["-b:a:0", bitrate]
    if ar:
        cmd += ["-ar:a:0", str(ar)]
    cmd += ["-ac:a:0", "2",
            "-metadata:s:a:0", "language=epo",
            "-metadata:s:0", "title=Esperanto Dub",
            str(out_mka)]

    _log.info("ffmpeg encode cmd (%d tokens):", len(cmd))
    _log.info("  %s", " ".join(f'"{a}"' if " " in str(a) else str(a) for a in cmd))

    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        _log.error("ffmpeg encode FAILED rc=%d", proc.returncode)
        _log.error("ffmpeg stderr:\n%s", proc.stderr or "(empty)")
        raise RuntimeError(
            f"ffmpeg epo audio encode failed (rc={proc.returncode}). "
            f"stderr:\n{proc.stderr or '(empty)'}"
        )
    if proc.stderr:
        _log.info("ffmpeg encode stderr (rc=0):\n%s", proc.stderr)
    _log.info("epo_audio encoded OK -> %s (%d bytes)",
              out_mka, out_mka.stat().st_size if out_mka.exists() else 0)
    return out_mka


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
    eo_ass_path: Path,
    errors: list[dict] | None = None,
) -> Path:
    batch_out_dir.mkdir(parents=True, exist_ok=True)

    _log.info("=== build_full_episode START ===")
    _log.info("mkv_path=%s", mkv_path)
    _log.info("batch_out_dir=%s", batch_out_dir)
    _log.info("full_no_vocals=%s (exists=%s)", full_no_vocals, Path(full_no_vocals).is_file())
    _log.info("full_original_audio=%s (exists=%s)", full_original_audio, Path(full_original_audio).is_file())
    _log.info("eo_ass_path=%s (exists=%s)", eo_ass_path,
              Path(eo_ass_path).is_file() if eo_ass_path else "NO PATH")
    _log.info("voiced_results=%d entries, errors=%d entries",
              len(voiced_results), len(errors or []))
    if _log.isEnabledFor(logging.DEBUG):
        for vr in voiced_results:
            _log.debug("  voiced: clip=%s start=%.3f end=%.3f audio_start=%.3f status=%s tts_wav=%r",
                       vr.get("clip_id"), vr["start_sec"], vr["end_sec"],
                       vr.get("audio_start_sec", vr["start_sec"]),
                       vr.get("status"), vr.get("tts_wav"))
        for er in (errors or []):
            _log.debug("  error: clip=%s start=%.3f end=%.3f err=%s",
                       er.get("clip_id"), er["start_sec"], er["end_sec"], er.get("error"))

    # ── Read & normalize the background (no-vocals) stem ──
    if not Path(full_no_vocals).is_file():
        raise FileNotFoundError(f"no_vocals WAV not found: {full_no_vocals}")
    _log.info("reading no_vocals: %s", full_no_vocals)
    nv, nv_sr = sf.read(str(full_no_vocals), dtype="float32")
    _log.info("no_vocals: raw shape=%s sr=%d ndim=%d", nv.shape, nv_sr, nv.ndim)
    if nv.ndim == 1:
        nv = np.stack([nv, nv], axis=1)
    if nv_sr != _TARGET_SR:
        _log.info("resampling no_vocals %d -> %d Hz", nv_sr, _TARGET_SR)
        nv_l = _resample_mono(nv[:, 0], nv_sr, _TARGET_SR)
        nv_r = _resample_mono(nv[:, 1], nv_sr, _TARGET_SR)
        nv = np.column_stack([nv_l, nv_r])

    # ── Read & normalize the original full audio (used for OP/ED/gaps/errors) ──
    if not Path(full_original_audio).is_file():
        raise FileNotFoundError(f"original audio WAV not found: {full_original_audio}")
    _log.info("reading original_audio: %s", full_original_audio)
    orig, orig_sr = sf.read(str(full_original_audio), dtype="float32")
    _log.info("original: raw shape=%s sr=%d ndim=%d", orig.shape, orig_sr, orig.ndim)
    if orig.ndim == 1:
        orig = np.stack([orig, orig], axis=1)
    if orig_sr != _TARGET_SR:
        _log.info("resampling original %d -> %d Hz", orig_sr, _TARGET_SR)
        orig_l = _resample_mono(orig[:, 0], orig_sr, _TARGET_SR)
        orig_r = _resample_mono(orig[:, 1], orig_sr, _TARGET_SR)
        orig = np.column_stack([orig_l, orig_r])

    total_samples = max(nv.shape[0], orig.shape[0])
    total_dur = total_samples / _TARGET_SR
    _log.info("total_samples=%d (%.2f s @ %d Hz)", total_samples, total_dur, _TARGET_SR)

    intro_start_s, intro_end_s, outro_start_s, outro_end_s = get_op_ed_ranges(ass_events)
    _log.info("OP/ED ranges (s): intro=[%.3f, %.3f] outro=[%.3f, %.3f]",
              intro_start_s, intro_end_s,
              outro_start_s if outro_start_s < float("inf") else -1.0,
              outro_end_s if outro_end_s < float("inf") else -1.0)

    intro_start = int(intro_start_s * _TARGET_SR)
    intro_end = int(intro_end_s * _TARGET_SR)
    outro_start = int(outro_start_s * _TARGET_SR) if outro_start_s < float("inf") else total_samples
    outro_end = int(outro_end_s * _TARGET_SR) if outro_end_s < float("inf") else total_samples

    intro_start = min(intro_start, total_samples)
    intro_end = min(intro_end, total_samples)
    outro_start = min(outro_start, total_samples)
    outro_end = min(outro_end, total_samples)
    _log.info("OP/ED samples (clamped): intro=[%d, %d] outro=[%d, %d] (total=%d)",
              intro_start, intro_end, outro_start, outro_end, total_samples)

    # ── Build the voice stem by summing every clip's TTS at its offset ──
    voice = np.zeros(total_samples, dtype=np.float32)
    mixed_count = 0
    missing_skipped = 0
    silent_skipped = 0
    silent_errors: list[dict] = []
    for vr in voiced_results:
        cid = vr.get("clip_id", "?")
        tts_str = vr.get("tts_wav", "") or ""
        if not tts_str:
            _log.info("voice-skip clip=%s reason=no tts_wav (status=%s, likely non_dub)",
                      cid, vr.get("status"))
            missing_skipped += 1
            continue
        tts_path = Path(tts_str)
        if not tts_path.is_file():
            _log.warning("voice-skip clip=%s reason=not a file: %s", cid, tts_path)
            missing_skipped += 1
            continue
        _log.info("voice-mix clip=%s start=%.3f end=%.3f audio_start=%.3f tts=%s",
                  cid, vr["start_sec"], vr["end_sec"],
                  vr.get("audio_start_sec", vr["start_sec"]), tts_path.name)
        try:
            tts, tts_sr = sf.read(str(tts_path), dtype="float32")
        except Exception as e:
            _log.error("voice-read-failed clip=%s path=%s err=%s", cid, tts_path, e)
            raise
        if tts.ndim > 1:
            tts = tts[:, 0]
        tts = _resample_mono(tts.astype(np.float32), tts_sr, _TARGET_SR)
        peak = float(np.max(np.abs(tts))) if tts.size else 0.0
        if peak < _SILENCE_THRESHOLD:
            _log.warning("voice-silent clip=%s tts=%s peak=%.6e (threshold=%.2e) -> skipping; window will fall back to original Japanese audio",
                         cid, tts_path.name, peak, _SILENCE_THRESHOLD)
            silent_skipped += 1
            silent_errors.append({
                "clip_id": cid,
                "start_sec": vr["start_sec"],
                "end_sec": vr["end_sec"],
                "error": "silent TTS (peak below silence threshold)",
            })
            continue
        norm_gain = _TARGET_VOICE_PEAK / peak
        _log.info("voice-norm clip=%s tts_peak=%.4f norm_gain=%.2f -> normalized to peak %.2f",
                  cid, peak, norm_gain, _TARGET_VOICE_PEAK)
        tts = tts * norm_gain
        offset = int(vr.get("audio_start_sec", vr["start_sec"]) * _TARGET_SR)
        if offset < 0:
            _log.warning("voice-mix clip=%s negative offset=%d, clamping to 0", cid, offset)
            tts = tts[-offset:] if -offset < len(tts) else np.zeros(0, dtype=np.float32)
            offset = 0
        end_idx = offset + len(tts)
        if end_idx > total_samples:
            _log.info("voice-mix clip=%s truncates tts %d -> %d samples (offset=%d total=%d)",
                      cid, len(tts), total_samples - offset, offset, total_samples)
            tts = tts[:total_samples - offset]
            end_idx = total_samples
        voice[offset:end_idx] += tts.astype(np.float32) * _MIX_WEIGHT_VOICE
        mixed_count += 1

    _log.info("voice-stem: mixed=%d skipped=%d (missing=%d silent=%d) peak=%.4f",
              mixed_count, missing_skipped + silent_skipped, missing_skipped, silent_skipped,
              float(np.max(np.abs(voice))) if voice.size else 0.0)
    if silent_errors:
        _log.warning("voice-stem: %d silent clips will fall back to original audio: %s",
                     len(silent_errors), ", ".join(e["clip_id"] for e in silent_errors))
    voice *= _GLOBAL_VOLUME
    voice_stereo = np.column_stack([voice, voice])

    # ── Assemble the output buffer across the timeline ──
    output = nv[:total_samples].copy() if nv.shape[0] >= total_samples else np.pad(
        nv, ((0, total_samples - nv.shape[0]), (0, 0)), mode="constant",
    )[:total_samples]

    _log.info("mix: pre-intro [0, %d)", intro_start)
    output[:intro_start] = nv[:intro_start].copy() + voice_stereo[:intro_start]

    if intro_end > intro_start:
        _log.info("mix: intro(OP) [%d, %d) <- original audio", intro_start, intro_end)
        src_slice = orig[intro_start:intro_end]
        output[intro_start:intro_end] = src_slice.astype(np.float32)

    if outro_start > intro_end:
        _log.info("mix: body [intro_end=%d, outro_start=%d) <- no_vocals+voice", intro_end, outro_start)
        body_bg = output[intro_end:outro_start].copy()
        body_voice = voice_stereo[intro_end:outro_start]
        body_mixed = body_bg + body_voice
        output[intro_end:outro_start] = body_mixed

    if outro_end > outro_start:
        _log.info("mix: outro(ED) [%d, %d) <- original audio", outro_start, outro_end)
        src_slice = orig[outro_start:outro_end]
        output[outro_start:outro_end] = src_slice.astype(np.float32)

    if outro_end < total_samples:
        _log.info("mix: post-outro [%d, %d) <- no_vocals+voice", outro_end, total_samples)
        output[outro_end:] = nv[outro_end:total_samples].copy() + voice_stereo[outro_end:]

    # ── Fill gaps > 2 s between voiced lines with original audio ──
    _GAP_THRESHOLD = 2.0
    voiced_sorted = sorted(voiced_results, key=lambda r: r["start_sec"])
    prev_end = intro_end_s
    gaps_filled = 0
    for vr in voiced_sorted:
        gap = vr["start_sec"] - prev_end
        if gap > _GAP_THRESHOLD:
            gs = int(prev_end * _TARGET_SR)
            ge = int(vr["start_sec"] * _TARGET_SR)
            gs = min(gs, total_samples)
            ge = min(ge, total_samples)
            if ge > gs:
                _log.info("gap-fill [%.2f, %.2f] (%.2fs) clip_after=%s",
                          prev_end, vr["start_sec"], gap, vr.get("clip_id"))
                output[gs:ge] = orig[gs:ge].astype(np.float32)
                gaps_filled += 1
        prev_end = max(prev_end, vr["end_sec"])
    _log.info("gap-fill: %d inter-clip gaps filled", gaps_filled)

    # Also fill the gap after the last voiced line up to outro_start
    if outro_start_s < float("inf"):
        gap = outro_start_s - prev_end
        if gap > _GAP_THRESHOLD:
            gs = int(prev_end * _TARGET_SR)
            ge = int(outro_start_s * _TARGET_SR)
            _log.info("gap-fill-to-outro [%.2f, %.2f] (%.2fs)", prev_end, outro_start_s, gap)
            output[gs:ge] = orig[gs:ge].astype(np.float32)

    # ── Replace error/skipped/rejected line windows with original audio ──
    all_errors = list(errors or []) + silent_errors
    _log.info("error windows: %d original + %d silent-tts = %d total",
              len(errors or []), len(silent_errors), len(all_errors))
    if all_errors:
        for err in all_errors:
            es = int(err["start_sec"] * _TARGET_SR)
            ee = int(err["end_sec"] * _TARGET_SR)
            es = min(es, total_samples)
            ee = min(ee, total_samples)
            if ee > es:
                _log.info("error-window clip=%s [%s, %s] (%.2fs) reason=%s <- original audio",
                          err.get("clip_id"), err["start_sec"], err["end_sec"],
                          err["end_sec"] - err["start_sec"], err.get("error"))
                output[es:ee] = orig[es:ee].astype(np.float32)

    output *= _GLOBAL_VOLUME
    raw_peak = float(np.max(np.abs(output))) if output.size else 0.0
    if raw_peak > _HEADROOM_TARGET:
        scale = _HEADROOM_TARGET / raw_peak
    else:
        scale = 1.0
    if scale != 1.0:
        output = output * scale
    output = np.clip(output, -_HEADROOM_TARGET, _HEADROOM_TARGET)
    final_peak = float(np.max(np.abs(output))) if output.size else 0.0
    headroom_db = 20.0 * math.log10(_HEADROOM_TARGET) if _HEADROOM_TARGET > 0 else 0.0
    _log.info("output normalize: pre_clip_peak=%.4f scale=%.4f -> final_peak=%.4f (headroom=%.2f dB) shape=%s",
              raw_peak, scale, final_peak, headroom_db, output.shape)

    dubbed_wav = batch_out_dir / "full_dubbed.wav"
    _log.info("writing full_dubbed.wav -> %s (shape=%s)", dubbed_wav, output.shape)
    sf.write(str(dubbed_wav), output, _TARGET_SR)
    _log.info("full_dubbed.wav written OK")

    # ═══════════════════════════════════════════
    #  Final mux: ffmpeg-encode Epo audio, then ffmpeg-assemble MKV
    # ═══════════════════════════════════════════
    ep_name = mkv_path.stem
    export_dir = batch_out_dir.parent / "exported episodes"
    export_dir.mkdir(parents=True, exist_ok=True)
    final_mkv = export_dir / f"{ep_name}_Dubbed.mkv"
    _log.info("final output -> %s", final_mkv)

    if not eo_ass_path:
        raise FileNotFoundError("eo_ass_path is empty (None)")
    if not Path(eo_ass_path).is_file():
        raise FileNotFoundError(
            f"Esperanto ASS not found: {eo_ass_path}. "
            "Run export_ass() / accept clips before assembling."
        )
    if not Path(dubbed_wav).is_file():
        raise FileNotFoundError(f"dubbed WAV not found: {dubbed_wav}")

    ffmpeg = _ffmpeg_bin()

    _log.info("--- probing source tracks ---")
    src_tracks = _probe_ffprobe_tracks(mkv_path)
    src_video = [t for t in src_tracks if t["type"] == "video"]
    src_audios = [t for t in src_tracks if t["type"] == "audio"]
    src_subs = [t for t in src_tracks if t["type"] == "subtitle"]
    _log.info("src summary: video=%d audio=%d subs=%d",
              len(src_video), len(src_audios), len(src_subs))
    if not src_video:
        raise RuntimeError(f"No video track found in {mkv_path}")

    src_audio_codec = ""
    if src_audios:
        src_audio_codec = src_audios[0].get("codec", "")

    _log.info("--- encoding Esperanto audio ---")
    epo_mka = _encode_epo_audio(dubbed_wav,
                                batch_out_dir / _EPO_AUDIO_FILENAME,
                                src_audio_codec)

    _log.info("--- ffmpeg final mux ---")
    cmd: list[str] = [ffmpeg, "-y", "-loglevel", "error"]
    cmd += ["-i", str(mkv_path)]
    cmd += ["-i", str(epo_mka)]
    cmd += ["-i", str(eo_ass_path)]

    for vt in src_video:
        cmd += ["-map", f"0:{vt['index']}"]
    cmd += ["-c:v", "copy"]

    cmd += ["-map", "1:a:0"]
    for at in src_audios:
        cmd += ["-map", f"0:{at['index']}"]
    cmd += ["-c:a", "copy"]

    cmd += ["-map", "2:s:0"]
    for st in src_subs:
        cmd += ["-map", f"0:{st['index']}"]
    cmd += ["-c:s", "copy"]

    cmd += ["-metadata:s:a:0", "language=epo",
            "-metadata:s:a:0", "title=Esperanto Dub",
            "-disposition:a:0", "default"]
    for i, at in enumerate(src_audios):
        a_idx = i + 1
        lang = at.get("language", "")
        title = at.get("title", "")
        cmd += [f"-disposition:a:{a_idx}", "0"]
        if lang:
            cmd += [f"-metadata:s:a:{a_idx}", f"language={lang}"]
        if title:
            cmd += [f"-metadata:s:a:{a_idx}", f"title={title}"]

    cmd += ["-metadata:s:s:0", "language=epo",
            "-metadata:s:s:0", "title=Esperanto",
            "-disposition:s:0", "default"]
    for i, st in enumerate(src_subs):
        s_idx = i + 1
        lang = st.get("language", "")
        title = st.get("title", "")
        cmd += [f"-disposition:s:{s_idx}", "0"]
        if lang:
            cmd += [f"-metadata:s:s:{s_idx}", f"language={lang}"]
        if title:
            cmd += [f"-metadata:s:s:{s_idx}", f"title={title}"]

    cmd += [str(final_mkv)]

    _log.info("ffmpeg mux cmd (%d tokens):", len(cmd))
    _log.info("  %s", " ".join(f'"{a}"' if " " in str(a) else str(a) for a in cmd))

    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        _log.error("ffmpeg mux FAILED rc=%d stderr:\n%s", proc.returncode, proc.stderr or "(empty)")
        raise RuntimeError(
            f"ffmpeg mux failed (rc={proc.returncode}). "
            f"stderr:\n{proc.stderr or '(empty)'}"
        )
    if proc.stderr:
        _log.info("ffmpeg mux stderr (rc=0):\n%s", proc.stderr)
    _log.info("ffmpeg mux OK -> %s (%d bytes)",
              final_mkv, final_mkv.stat().st_size if final_mkv.exists() else 0)

    export_ass_copy = batch_out_dir / f"{ep_name}_Dubbed.ass"
    try:
        shutil.copyfile(str(eo_ass_path), str(export_ass_copy))
        _log.info("copied Esperanto ASS -> %s (project folder, for debugging)", export_ass_copy)
    except Exception as e:
        _log.warning("failed to copy Esperanto ASS to project folder: %s", e)

    _log.info("=== build_full_episode DONE ===")
    return final_mkv