import argparse
import json
import sys
from pathlib import Path

import numpy as np
import soundfile as sf

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from rich.console import Console
from rich.panel import Panel
from rich.prompt import IntPrompt
from rich.table import Table

from anidub.ass import (
    detect_language,
    filter_main_dialogue,
    get_ass_header,
    parse_ass,
    strip_override_tags,
)
from anidub.assembler import assemble_line, ensure_demucs_cache
from anidub.config import DEFAULT_ASS, DEFAULT_MKV, TEST_OUTPUT, get_ffmpeg_location, today_output_dir
from anidub.esperanto import build_instruct_prompt
from anidub.extract import extract_ref_clip_forward, trim_silence

console = Console()

SAFETY_MARGIN_SEC = 0.1
MIN_PREROLL_SEC = 1.0
REF_CLIP_DUR = 3.0
MIN_TRANSCRIPTION_CHARS = 5
SILENCE_TOP_DB = 30


def fmt_ts(sec: float) -> str:
    h = int(sec // 3600)
    m = int((sec % 3600) // 60)
    s = sec % 60
    return f"{h:d}:{m:02d}:{s:05.2f}"


def split_lines(events):
    usable, skipped = [], []
    for i, e in enumerate(events):
        clean = strip_override_tags(e["text"])
        if not clean:
            skipped.append({**e, "reason": "empty_text"})
            continue
        lang = detect_language(clean)
        if lang == "japanese":
            skipped.append({**e, "reason": f"japanese: {clean[:60]}", "clean_text": clean})
            continue
        if "(music)" in clean.lower():
            skipped.append({**e, "reason": "music_marker", "clean_text": clean})
            continue
        if e["start_sec"] < MIN_PREROLL_SEC:
            skipped.append({**e, "reason": "no_preroll_ref", "clean_text": clean})
            continue
        next_start = None
        if i + 1 < len(events):
            next_start = events[i + 1]["start_sec"]
        usable.append({**e, "clean_text": clean, "next_line_start": next_start})
    return usable, skipped


def show_picker(usable):
    table = Table(title="Pick a subtitle line", show_lines=True)
    table.add_column("Idx", style="bold cyan", justify="right")
    table.add_column("Window")
    table.add_column("Dur", justify="right")
    table.add_column("Text")
    for e in usable[:40]:
        window = f"{fmt_ts(e['start_sec'])} -> {fmt_ts(e['end_sec'])}"
        dur = e["end_sec"] - e["start_sec"]
        table.add_row(
            str(e["index"]),
            window,
            f"{dur:.2f}s",
            e["clean_text"][:70],
        )
    console.print(table)


def save_skipped(skipped, out_path: Path):
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as f:
        json.dump(
            [
                {
                    "index": s["index"],
                    "start_sec": s["start_sec"],
                    "end_sec": s["end_sec"],
                    "style": s["style"],
                    "name": s["name"],
                    "original_text": s["text"],
                    "reason": s["reason"],
                }
                for s in skipped
            ],
            f,
            indent=2,
            ensure_ascii=False,
        )


def print_ref_transcription_block(ref_transcription: dict):
    text = ref_transcription.get("text", "") or "(empty)"
    panel = Panel.fit(
        f"[bold]whisper model:[/] {ref_transcription['model']}\n"
        f"[bold]language:[/] {ref_transcription['language']}\n"
        f"[bold]audio duration:[/] {ref_transcription['audio_duration_sec']}s\n"
        f"[bold]inference:[/] {ref_transcription['inference_ms']} ms\n"
        f"[bold]transcript:[/] {text}",
        title="[bold yellow]REF TRANSCRIPTION[/]",
        border_style="yellow",
    )
    console.print(panel)


def print_params_block(result, whisper_model: str):
    d = result["diagnostics"]
    panel = Panel.fit(
        f"[bold]model_id:[/] {d['model_id']}\n"
        f"[bold]device:[/] {d['device']}    [bold]dtype:[/] {d['dtype']}    "
        f"[bold]attn:[/] {d['attn_impl']}\n"
        f"[bold]whisper:[/] {whisper_model}\n"
        f"[bold]target_duration:[/] {d['target_duration']}s\n"
        f"[bold]params:[/]\n"
        + json.dumps(d["params"], indent=2, ensure_ascii=False),
        title="[bold blue]PARAMS[/]",
        border_style="blue",
    )
    console.print(panel)


def print_prompt_block(result, ref_audio_path: Path):
    d = result["diagnostics"]
    ref_dur = 0.0
    try:
        info = sf.info(str(ref_audio_path))
        ref_dur = info.frames / info.samplerate if info.samplerate else 0.0
    except Exception:
        pass
    panel = Panel.fit(
        f"[bold]ref_audio:[/] {ref_audio_path}  ({ref_dur:.2f}s)\n"
        f"[bold]instruct ({d['prompt_chars']} chars):[/]\n"
        f"{d['prompt_text']}",
        title="[bold magenta]PROMPT[/]",
        border_style="magenta",
    )
    console.print(panel)


def print_running_block(mem_before_mb: float):
    panel = Panel.fit(
        f"[bold]cuda mem before:[/] {mem_before_mb} MB",
        title="[bold yellow]RUNNING[/]",
        border_style="yellow",
    )
    console.print(panel)


def print_result_block(stats: dict, out_file: Path, slack_ms: float):
    out_dur = stats["effective_dur"]
    slack_str = f"{slack_ms:+.1f} ms"
    verdict = "FITS" if slack_ms >= 0 else "OVERSHOOT"
    panel = Panel.fit(
        f"[bold]out_file:[/] {out_file}\n"
        f"[bold]raw_duration:[/] {stats['raw_dur']:.3f}s\n"
        f"[bold]effective_duration:[/] {out_dur:.3f}s\n"
        f"[bold]slack vs target:[/] {slack_str}\n"
        f"[bold]atempo:[/] {stats.get('atempo', 'none')}\n"
        f"[bold]inference_ms:[/] {stats.get('inference_ms', '?')}\n"
        f"[bold]cuda mem after:[/] {stats.get('cuda_mem_after_mb', '?')} MB\n"
        f"[bold]verdict:[/] [bold {'green' if slack_ms >= 0 else 'red'}]{verdict}[/]",
        title="[bold green]RESULT[/]",
        border_style="green",
    )
    console.print(panel)


def main():
    ap = argparse.ArgumentParser(
        prog="anidub-test-voice",
        description="Voice one subtitle line with OmniVoice (full logging + assembly)",
    )
    ap.add_argument("--mkv", type=Path, default=DEFAULT_MKV)
    ap.add_argument("--ass", type=Path, default=DEFAULT_ASS)
    ap.add_argument(
        "--whisper-model",
        default="openai/whisper-tiny",
        choices=[
            "openai/whisper-tiny",
            "openai/whisper-base",
            "openai/whisper-small",
            "openai/whisper-medium",
            "openai/whisper-large-v3-turbo",
        ],
        help="Whisper model for ref-audio pre-transcription",
    )
    ap.add_argument("--line", type=int, default=None, help="skip picker, use index")
    args = ap.parse_args()

    if not get_ffmpeg_location():
        console.print("[red]ffmpeg not found. Run .\\install.ps1[/]")
        return 1

    run_dir = today_output_dir()
    run_dir.mkdir(parents=True, exist_ok=True)

    console.print(
        Panel.fit(
            "[bold cyan]anidub-test-voice[/] - anime dubbing test\n"
            f"[dim]mkv: {args.mkv}\nass: {args.ass}\n"
            f"whisper: {args.whisper_model}\n"
            f"output: {run_dir}[/]",
            border_style="cyan",
        )
    )

    if not args.mkv.exists() or not args.ass.exists():
        console.print(f"[red]Missing mkv/ass: {args.mkv} / {args.ass}[/]")
        return 1

    ass_header = get_ass_header(args.ass)

    console.print("[bold]Checking Demucs cache...[/]")
    full_no_vocals, full_vocals = ensure_demucs_cache(args.mkv, run_dir)
    console.print(f"[dim]  no_vocals: {full_no_vocals}[/]")

    events = parse_ass(args.ass)
    main_events = filter_main_dialogue(events)
    console.print(
        f"[dim]Parsed {len(events)} events, "
        f"{len(main_events)} `main` dialogue.[/]"
    )

    usable, skipped = split_lines(main_events)
    skipped_path = run_dir / "skipped.json"
    save_skipped(skipped, skipped_path)
    console.print(
        f"[dim]{len(usable)} usable, {len(skipped)} skipped "
        f"(-> {skipped_path})[/]"
    )

    if not usable:
        console.print("[red]No usable lines found.[/]")
        return 1

    if args.line is not None:
        match = [e for e in usable if e["index"] == args.line]
        if not match:
            console.print(f"[red]Line index {args.line} not in usable set.[/]")
            console.print("[dim]Hint: skipped lines may need manual review.[/]")
            return 1
        line = match[0]
    else:
        show_picker(usable)
        idx = IntPrompt.ask("[bold]Enter line index[/]", default=usable[0]["index"])
        match = [e for e in usable if e["index"] == idx]
        if not match:
            console.print(f"[red]Index {idx} not in usable set.[/]")
            return 1
        line = match[0]

    console.print(
        Panel.fit(
            f"[bold]Line {line['index']}[/]\n"
            f"[bold]Window:[/] {fmt_ts(line['start_sec'])} -> "
            f"{fmt_ts(line['end_sec'])} "
            f"({line['end_sec']-line['start_sec']:.2f}s)\n"
            f"[bold]Speaker:[/] {line['name'] or '(unspecified)'}\n"
            f"[bold]Text:[/] {line['clean_text']}",
            title="[bold]Selected[/]",
            border_style="cyan",
        )
    )

    instruct = build_instruct_prompt(line.get("name") or None)
    target_dur = (line["end_sec"] - line["start_sec"]) - SAFETY_MARGIN_SEC

    out_dir = run_dir / f"line_{line['index']:03d}"
    out_dir.mkdir(parents=True, exist_ok=True)

    try:
        console.rule("[bold cyan]OmniVoice[/]")

        ref_wav = out_dir / "ref.wav"
        extract_ref_clip_forward(
            args.mkv, line, max_dur=REF_CLIP_DUR, out_path=ref_wav
        )
        console.print(f"[dim]Extracted ref clip -> {ref_wav}[/]")

        from anidub.tts.omnivoice import OmniVoiceTTSBackend
        backend = OmniVoiceTTSBackend(whisper_model=args.whisper_model)

        result = backend.generate(
            text=line["clean_text"],
            ref_audio=ref_wav,
            target_duration=target_dur,
            instruct=instruct,
        )

        ref_transcription = result["diagnostics"].get("ref_transcription")
        if ref_transcription:
            print_ref_transcription_block(ref_transcription)
            transcript_text = ref_transcription.get("text", "").strip()
            if len(transcript_text) < MIN_TRANSCRIPTION_CHARS:
                console.print(
                    f"[yellow]Ref transcription too short "
                    f"({len(transcript_text)} chars). "
                    f"Skipping (possible silent/noise ref clip).[/]"
                )
                return 1

        print_params_block(result, args.whisper_model)
        print_prompt_block(result, ref_wav)

        mem_b = result["diagnostics"].get("cuda_mem_before_mb", 0.0)
        print_running_block(mem_b)

        raw_dur = result["output_duration"]
        raw_wav = result["wav"]
        sr = result["sr"]

        tts_raw = out_dir / "tts_raw.wav"
        sf.write(tts_raw, raw_wav, sr)
        console.print(f"[dim]Saved raw TTS -> {tts_raw}[/]")

        trimmed = trim_silence(raw_wav, sr, top_db=SILENCE_TOP_DB)
        effective_dur = len(trimmed) / sr

        atempo_info = "none"
        if effective_dur > target_dur:
            console.print(
                f"[yellow]Effective voice duration {effective_dur:.3f}s > "
                f"target {target_dur:.3f}s. Applying atempo...[/]"
            )
            import tempfile
            with tempfile.NamedTemporaryFile(
                suffix=".wav", delete=False, delete_on_close=False
            ) as tmp_out:
                tmp_path = Path(tmp_out.name)
            with tempfile.NamedTemporaryFile(
                suffix=".wav", delete=False, delete_on_close=False
            ) as tmp_in:
                tmp_input = Path(tmp_in.name)
            try:
                sf.write(tmp_input, trimmed, sr)
                from anidub.extract import fit_audio_to_duration
                fit_result = fit_audio_to_duration(tmp_input, tmp_path, target_dur)
                atempo_info = fit_result.get("atempo_chain", "unknown")
                fitted_wav, fitted_sr = sf.read(tmp_path)
                trimmed = np.asarray(fitted_wav, dtype=np.float32).T
                sr = fitted_sr
                effective_dur = len(trimmed) / sr
            finally:
                tmp_input.unlink(missing_ok=True)
                tmp_path.unlink(missing_ok=True)
            console.print(f"[dim]atempo chain: {atempo_info}[/]")

        slack_ms = (target_dur - effective_dur) * 1000.0

        tts_trimmed = out_dir / "tts.wav"
        sf.write(tts_trimmed, trimmed, sr)

        stats = {
            "raw_dur": raw_dur,
            "effective_dur": effective_dur,
            "inference_ms": result["diagnostics"].get("inference_ms"),
            "cuda_mem_after_mb": result["diagnostics"].get("cuda_mem_after_mb"),
            "atempo": atempo_info,
        }
        print_result_block(stats, tts_trimmed, slack_ms)

        console.rule("[bold]Assembly[/]")
        assembly = assemble_line(
            args.mkv, line, tts_trimmed, full_no_vocals,
            ass_header, out_dir,
        )
        console.print(f"[green]final.mkv -> {assembly['final']}[/]")
        console.print(f"[dim]dubbed.wav -> {assembly['dubbed']}[/]")

        log: dict = {
            "line": {
                "index": line["index"],
                "start_sec": line["start_sec"],
                "end_sec": line["end_sec"],
                "speaker": line.get("name") or None,
                "text": line["clean_text"],
            },
            "target_duration": target_dur,
            "instruct": instruct,
            "stats": stats,
            "diagnostics": result["diagnostics"],
            "assembly": assembly,
        }
        log_path = out_dir / "log.json"
        with log_path.open("w", encoding="utf-8") as f:
            json.dump(log, f, indent=2, ensure_ascii=False, default=str)
        console.print(f"\n[bold]Logs -> {log_path}[/]")

        table = Table(title="Summary", show_lines=True)
        table.add_column("Metric", style="bold cyan")
        table.add_column("Value", justify="right")
        table.add_row("Inference", f"{stats['inference_ms']} ms")
        table.add_row("Raw duration", f"{raw_dur:.3f}s")
        table.add_row("Effective duration", f"{effective_dur:.3f}s")
        table.add_row("Target duration", f"{target_dur:.3f}s")
        table.add_row("Slack", f"{slack_ms:+.0f} ms")
        verdict = "FITS" if slack_ms >= 0 else "OVERSHOOT"
        table.add_row("Verdict", f"[{'green' if slack_ms >= 0 else 'red'}]{verdict}[/]")
        table.add_row("Final mkv", str(assembly["final"]))
        console.print(table)

    except Exception as e:
        console.print(f"[red]Failed: {e}[/]")
        import traceback
        console.print(f"[dim]{traceback.format_exc()}[/]")
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())