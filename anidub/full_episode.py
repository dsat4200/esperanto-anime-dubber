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

_MKVMERGE_HINT_PATHS = (
    r"C:\Program Files\MKVToolNix\mkvmerge.exe",
    r"C:\Program Files (x86)\MKVToolNix\mkvmerge.exe",
)
_EPO_AUDIO_FILENAME = "epo_audio.mka"


def _ffmpeg_bin():
    loc = get_ffmpeg_location()
    if not loc:
        raise RuntimeError("ffmpeg not found")
    return str(Path(loc) / "ffmpeg.exe")


def _ffprobe_bin():
    return str(Path(_ffmpeg_bin()).parent / "ffprobe.exe")


def _mkvmerge_bin() -> str:
    found = shutil.which("mkvmerge")
    if found:
        return found
    for p in _MKVMERGE_HINT_PATHS:
        if Path(p).is_file():
            return p
    raise FileNotFoundError(
        "mkvmerge not found on PATH or in C:\\Program Files\\MKVToolNix. "
        "Install MKVToolNix: https://mkvtoolnix.download/"
    )


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


def _probe_mkvmerge_tracks(file: Path) -> list[dict]:
    mkvmerge = _mkvmerge_bin()
    cmd = [mkvmerge, "-J", str(file)]
    _log.info("mkvmerge -J cmd: %s", " ".join(cmd))
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        _log.error("mkvmerge identify FAILED rc=%d", proc.returncode)
        _log.error("mkvmerge stdout:\n%s", proc.stdout)
        _log.error("mkvmerge stderr:\n%s", proc.stderr)
        raise RuntimeError(
            f"mkvmerge -J failed (rc={proc.returncode}) on {file}. "
            f"output:\n{proc.stdout or proc.stderr or '(empty)'}"
        )
    data = _json.loads(proc.stdout)
    tracks = data.get("tracks", [])
    for t in tracks:
        props = t.get("properties", {}) or {}
        _log.info("  track id=%s type=%s codec=%s lang=%r lang_ietf=%r name=%r default=%s",
                  t.get("id"), t.get("type"), t.get("codec_id"),
                  props.get("language"), props.get("language_ietf"),
                  props.get("track_name"), props.get("default_track"))
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
_TARGET_VOICE_PEAK = 0.5


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
    #  Final mux (two-stage: ffmpeg-encode Epo audio -> mkvmerge)
    # ═══════════════════════════════════════════
    # Output goes to projects/{anime}/exported episodes/{stem}_Dubbed.mkv
    # (batch_out_dir is projects/{anime}/{stem}, so its parent is projects/{anime}).
    #
    # Why two stages? The ffmpeg Matroska muxer has three intentional
    # limitations that break strict players (VLC/PotPlayer/WMP):
    #   1. It NEVER writes FlagDefault=1 (only clears to 0; relies on EBML default)
    #      — strict players treat "missing" as 0 → Epo tracks never auto-select.
    #   2. It NEVER writes LanguageBCP47, only the legacy Language element.
    #   3. It always emits Audio.BitDepth for opus (meaningless for lossy codecs).
    # mkvmerge writes all three correctly. So we use ffmpeg just to encode the
    # Epo audio bitstream (its one good job) and let mkvmerge assemble the MKV.
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

    mkvmerge = _mkvmerge_bin()
    _log.info("mkvmerge = %s", mkvmerge)

    _log.info("--- probing source MKV tracks ---")
    src_tracks = _probe_mkvmerge_tracks(mkv_path)
    src_video = [t for t in src_tracks if t.get("type") == "video"]
    src_audios = [t for t in src_tracks if t.get("type") == "audio"]
    src_subs = [t for t in src_tracks if t.get("type") == "subtitles"]
    _log.info("src summary: video=%d audio=%d subs=%d",
              len(src_video), len(src_audios), len(src_subs))
    if not src_video:
        raise RuntimeError(f"No video track found in {mkv_path}")
    if not src_audios:
        _log.warning("source MKV has no audio tracks; Epo dub will be the only audio")

    src_audio_codec_id = ""
    if src_audios:
        a0 = src_audios[0]
        src_audio_codec_id = (a0.get("properties", {}) or {}).get("codec_id") or a0.get("codec") or ""

    _log.info("--- encoding Esperanto audio (ffmpeg) ---")
    epo_mka = _encode_epo_audio(dubbed_wav,
                                batch_out_dir / _EPO_AUDIO_FILENAME,
                                src_audio_codec_id)

    _log.info("--- probing epo_mka ---")
    eo_aud_tracks = _probe_mkvmerge_tracks(epo_mka)
    eo_aud = [t for t in eo_aud_tracks if t.get("type") == "audio"]
    if not eo_aud:
        raise RuntimeError(f"No audio track found in {epo_mka}")
    eo_aud_id = eo_aud[0].get("id")
    _log.info("epo audio id in mka: %s", eo_aud_id)

    mkvmerge_cmd: list[str] = [
        mkvmerge, "-o", str(final_mkv),
    ]

    # Input 0: Epo audio (mka). Give language/title/default inline.
    mkvmerge_cmd += [
        "--language", f"{eo_aud_id}:epo",
        "--track-name", f"{eo_aud_id}:Esperanto Dub",
        "--default-track-flag", f"{eo_aud_id}:yes",
        "--no-attachments",
        str(epo_mka),
    ]

    # Input 1: source MKV. Clear default on every original audio + sub track so
    # the Epo tracks become the auto-selected ones. mkvmerge automatically
    # carries video, attachments (fonts), and chapters from this input.
    for at in src_audios:
        tid = at.get("id")
        mkvmerge_cmd += [f"--default-track-flag", f"{tid}:no"]
    for st in src_subs:
        tid = st.get("id")
        mkvmerge_cmd += [f"--default-track-flag", f"{tid}:no"]
    mkvmerge_cmd += [str(mkv_path)]

    # Input 2: Epo ASS. Language=epo, title=Esperanto, default.
    # On an external .ass file mkvmerge gives every Dialogue line the id 0.
    mkvmerge_cmd += [
        "--language", "0:epo",
        "--track-name", "0:Esperanto",
        "--default-track-flag", "0:yes",
        str(eo_ass_path),
    ]

    # --track-order: required so Epo audio+sub land in their preferred positions.
    # cmdline input-index:track-id pairs.
    #   video  <- src.video (input 1)
    #   audio1 <- epo_audio (input 0)
    #   audio2 <- src.audio[0] (input 1)
    #   audio3 <- src.audio[1] (input 1) ...
    #   sub1   <- eo_ass (input 2)
    #   sub2.. <- src.subs[*] (input 1)
    order: list[str] = []
    order.append(f"1:{src_video[0].get('id')}")
    order.append(f"0:{eo_aud_id}")
    for at in src_audios:
        order.append(f"1:{at.get('id')}")
    # Epo ASS has one track, id 0 by mkvmerge convention for external text files.
    order.append("2:0")
    for st in src_subs:
        order.append(f"1:{st.get('id')}")
    mkvmerge_cmd += ["--track-order", ",".join(order)]
    _log.info("track-order: %s", ",".join(order))

    _log.info("mkvmerge cmd (%d tokens):", len(mkvmerge_cmd))
    _log.info("  %s", " ".join(f'"{a}"' if " " in str(a) else str(a) for a in mkvmerge_cmd))

    _log.info("--- running mkvmerge ---")
    # mkvmerge rc: 0 = success, 1 = success with warnings, 2 = error.
    mm_proc = subprocess.run(mkvmerge_cmd, capture_output=True, text=True)
    _log.info("mkvmerge rc=%d", mm_proc.returncode)
    if mm_proc.stdout:
        _log.info("mkvmerge stdout:\n%s", mm_proc.stdout)
    if mm_proc.stderr:
        _log.info("mkvmerge stderr:\n%s", mm_proc.stderr)
    if mm_proc.returncode == 2:
        # Check if file was produced anyway (mkvmerge sometimes writes despite rc=2).
        if final_mkv.exists():
            _log.warning("mkvmerge rc=2 but output file exists; proceeding with caution")
        else:
            raise RuntimeError(
                f"mkvmerge failed (rc={mm_proc.returncode}). "
                f"stderr:\n{mm_proc.stderr or mm_proc.stdout or '(empty)'}"
            )
    elif mm_proc.returncode == 1:
        _log.warning("mkvmerge succeeded WITH WARNINGS (rc=1) — verify output manually")
    else:
        _log.info("mkvmerge OK -> %s (%d bytes)",
                  final_mkv, final_mkv.stat().st_size if final_mkv.exists() else 0)

    export_ass_copy = batch_out_dir / f"{ep_name}_Dubbed.ass"
    try:
        shutil.copyfile(str(eo_ass_path), str(export_ass_copy))
        _log.info("copied Esperanto ASS -> %s (project folder, for debugging)", export_ass_copy)
    except Exception as e:
        _log.warning("failed to copy Esperanto ASS to project folder: %s", e)

    _log.info("=== build_full_episode DONE ===")
    return final_mkv