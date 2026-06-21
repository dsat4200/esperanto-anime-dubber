import argparse
import json
from pathlib import Path

import soundfile as sf
from rich.console import Console
from rich.panel import Panel
from rich.prompt import IntPrompt
from rich.table import Table

from anidub.ass import (
    detect_language,
    filter_main_dialogue,
    parse_ass,
    strip_override_tags,
)
from anidub.config import DEFAULT_ASS, DEFAULT_MKV, TEST_OUTPUT, get_ffmpeg_location
from anidub.esperanto import build_instruct_prompt
from anidub.extract import extract_ref_clip_forward

console = Console()

ENGINE_OMNIVOICE = "omnivoice"
ENGINE_QWEN3 = "qwen3"
SAFETY_MARGIN_SEC = 0.1
MIN_PREROLL_SEC = 1.0
REF_CLIP_DUR = 3.0
MIN_TRANSCRIPTION_CHARS = 5


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
    table.add_column("Speaker", style="magenta")
    table.add_column("Text")
    for e in usable[:40]:
        window = f"{fmt_ts(e['start_sec'])} -> {fmt_ts(e['end_sec'])}"
        dur = e["end_sec"] - e["start_sec"]
        table.add_row(
            str(e["index"]),
            window,
            f"{dur:.2f}s",
            e["name"] or "-",
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


def append_skipped(skipped_path: Path, entry: dict):
    existing = []
    if skipped_path.exists():
        try:
            with skipped_path.open("r", encoding="utf-8") as f:
                existing = json.load(f)
        except Exception:
            existing = []
    existing.append(entry)
    skipped_path.parent.mkdir(parents=True, exist_ok=True)
    with skipped_path.open("w", encoding="utf-8") as f:
        json.dump(existing, f, indent=2, ensure_ascii=False)


def print_params_block(result, engine_label):
    d = result["diagnostics"]
    panel = Panel.fit(
        f"[bold]engine:[/] {engine_label}\n"
        f"[bold]model_id:[/] {d['model_id']}\n"
        f"[bold]device:[/] {d['device']}    [bold]dtype:[/] {d['dtype']}    "
        f"[bold]attn:[/] {d['attn_impl']}\n"
        f"[bold]target_duration:[/] {d['target_duration']}s\n"
        f"[bold]params:[/]\n"
        + json.dumps(d["params"], indent=2, ensure_ascii=False),
        title="[bold blue]PARAMS[/]",
        border_style="blue",
    )
    console.print(panel)


def print_ref_transcription_block(ref_transcription: dict):
    panel = Panel.fit(
        f"[bold]whisper model:[/] {ref_transcription['model']}\n"
        f"[bold]language:[/] {ref_transcription['language']}\n"
        f"[bold]audio duration:[/] {ref_transcription['audio_duration_sec']}s\n"
        f"[bold]inference:[/] {ref_transcription['inference_ms']} ms\n"
        f"[bold]transcript:[/] {ref_transcription['text'] or '(empty)'}",
        title="[bold yellow]REF TRANSCRIPTION[/]",
        border_style="yellow",
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


def print_result_block(result, out_file: Path, slack_ms: float):
    d = result["diagnostics"]
    out_dur = result["output_duration"]
    slack_str = f"{slack_ms:+.1f} ms"
    verdict = "FITS" if slack_ms >= 0 else "OVERSHOOT"
    pp = d.get("postprocess", {})
    pp_chain = pp.get("atempo_chain", "none") if isinstance(pp, dict) else str(pp)
    panel = Panel.fit(
        f"[bold]out_file:[/] {out_file}\n"
        f"[bold]output_duration:[/] {out_dur:.3f}s\n"
        f"[bold]slack vs target:[/] {slack_str}\n"
        f"[bold]atempo chain:[/] {pp_chain}\n"
        f"[bold]inference_ms:[/] {d.get('inference_ms', '?')}\n"
        f"[bold]cuda mem after:[/] {d.get('cuda_mem_after_mb', '?')} MB\n"
        f"[bold]verdict:[/] [bold {'green' if slack_ms >= 0 else 'red'}]{verdict}[/]",
        title="[bold green]RESULT[/]",
        border_style="green",
    )
    console.print(panel)


def run_engine(
    engine_name, mkv, line, instruct, target_dur,
    qwen_variant, qwen_speaker, whisper_model,
):
    console.rule(f"[bold cyan]{engine_name}[/]")

    ref_wav = TEST_OUTPUT / f"ref_{line['index']}.wav"
    extract_ref_clip_forward(
        mkv, line, max_dur=REF_CLIP_DUR, out_path=ref_wav
    )
    console.print(f"[dim]Extracted ref clip -> {ref_wav}[/]")

    if engine_name == ENGINE_OMNIVOICE:
        from anidub.tts.omnivoice import OmniVoiceTTSBackend
        backend = OmniVoiceTTSBackend(whisper_model=whisper_model)
    else:
        from anidub.tts.qwen3 import Qwen3TTSBackend
        backend = Qwen3TTSBackend(variant=qwen_variant, speaker=qwen_speaker)

    result = backend.generate(
        text=line["clean_text"],
        ref_audio=ref_wav,
        target_duration=target_dur,
        instruct=instruct,
    )

    ref_transcription = result["diagnostics"].get("ref_transcription")
    if engine_name == ENGINE_OMNIVOICE and ref_transcription:
        print_ref_transcription_block(ref_transcription)
        if len(ref_transcription.get("text", "").strip()) < MIN_TRANSCRIPTION_CHARS:
            raise RuntimeError(
                f"ref transcription empty or too short "
                f"({len(ref_transcription['text'].strip())} chars); "
                f"ref clip may be silent/SFX"
            )

    print_params_block(result, engine_name)
    print_prompt_block(result, ref_wav)
    mem_b = result["diagnostics"].get("cuda_mem_before_mb", 0.0)
    print_running_block(mem_b)

    out_file = TEST_OUTPUT / f"line_{line['index']}_{engine_name}.wav"
    sf.write(out_file, result["wav"], result["sr"])
    slack_ms = (target_dur - result["output_duration"]) * 1000.0
    print_result_block(result, out_file, slack_ms)
    return result, out_file, slack_ms


def main():
    ap = argparse.ArgumentParser(
        prog="anidub-test-voice",
        description="Voice one subtitle line with TTS (full logging)",
    )
    ap.add_argument("--mkv", type=Path, default=DEFAULT_MKV)
    ap.add_argument("--ass", type=Path, default=DEFAULT_ASS)
    ap.add_argument(
        "--engine",
        choices=[ENGINE_OMNIVOICE, ENGINE_QWEN3, "both"],
        default=ENGINE_OMNIVOICE,
    )
    ap.add_argument(
        "--qwen-variant",
        choices=["custom", "base", "design"],
        default="custom",
    )
    ap.add_argument("--qwen-speaker", default="Serena")
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
        help="Whisper model for ref-audio pre-transcription (OmniVoice only)",
    )
    ap.add_argument("--line", type=int, default=None, help="skip picker, use index")
    args = ap.parse_args()

    if not get_ffmpeg_location():
        console.print("[red]ffmpeg not found. Run .\\install.ps1[/]")
        return 1

    console.print(
        Panel.fit(
            "[bold cyan]anidub-test-voice[/] - anime dubbing test\n"
            f"[dim]mkv: {args.mkv}\nass: {args.ass}\n"
            f"engine: {args.engine}\nwhisper: {args.whisper_model}[/]",
            border_style="cyan",
        )
    )

    if not args.mkv.exists() or not args.ass.exists():
        console.print(f"[red]Missing mkv/ass: {args.mkv} / {args.ass}[/]")
        return 1

    events = parse_ass(args.ass)
    main_events = filter_main_dialogue(events)
    console.print(
        f"[dim]Parsed {len(events)} events, "
        f"{len(main_events)} `main` dialogue.[/]"
    )

    usable, skipped = split_lines(main_events)
    save_skipped(skipped, TEST_OUTPUT / "skipped.json")
    console.print(
        f"[dim]{len(usable)} usable, {len(skipped)} skipped "
        f"(-> {TEST_OUTPUT / 'skipped.json'})[/]"
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

    engines = [ENGINE_OMNIVOICE]
    if args.engine == ENGINE_QWEN3:
        engines = [ENGINE_QWEN3]
    elif args.engine == "both":
        engines = [ENGINE_OMNIVOICE, ENGINE_QWEN3]

    summary = []
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
        "engines": {},
    }

    for eng in engines:
        try:
            result, out_file, slack_ms = run_engine(
                eng, args.mkv, line, instruct, target_dur,
                args.qwen_variant, args.qwen_speaker, args.whisper_model,
            )
            log["engines"][eng] = result["diagnostics"]
            summary.append((eng, result, out_file, slack_ms))
        except Exception as e:
            console.print(f"[red]{eng} failed: {e}[/]")
            import traceback
            console.print(f"[dim]{traceback.format_exc()}[/]")
            log["engines"][eng] = {"error": str(e)}

    if summary:
        table = Table(title="Summary", show_lines=True)
        table.add_column("Engine", style="bold cyan")
        table.add_column("Inference ms", justify="right")
        table.add_column("Output ms", justify="right")
        table.add_column("Slack vs target", justify="right")
        table.add_column("Verdict")
        table.add_column("File")
        for eng, result, out_file, slack_ms in summary:
            out_ms = result["output_duration"] * 1000.0
            verdict = "FITS" if slack_ms >= 0 else "OVERSHOOT"
            verdict_style = "green" if slack_ms >= 0 else "red"
            table.add_row(
                eng,
                str(result["diagnostics"].get("inference_ms", "?")),
                f"{out_ms:.0f}",
                f"{slack_ms:+.0f}",
                f"[{verdict_style}]{verdict}[/]",
                str(out_file),
            )
        console.print(table)

    log_path = TEST_OUTPUT / f"line_{line['index']}.json"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("w", encoding="utf-8") as f:
        json.dump(log, f, indent=2, ensure_ascii=False, default=str)
    console.print(f"\n[bold]Logs -> {log_path}[/]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())