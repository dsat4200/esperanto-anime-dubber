import argparse
import json
import sys
import msvcrt
import time
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from rich.console import Console
from rich.panel import Panel
from rich.prompt import IntPrompt, Confirm
from rich.progress import (
    Progress,
    BarColumn,
    TextColumn,
    TimeRemainingColumn,
    SpinnerColumn,
)
from rich.table import Table

import soundfile as sf

from anidub.ass import (
    detect_language,
    filter_dialogue,
    get_ass_header,
    parse_ass,
    strip_override_tags,
)
from anidub.assembler import ensure_demucs_cache_from_wav
from anidub.config import (
    DEFAULT_ASS, DEFAULT_MKV, TEST_OUTPUT, ANIME_ROOT,
    get_ffmpeg_location, today_output_dir, today_batch_dir, anime_batch_dir, anime_test_dir,
    discover_anime, auto_detect_ass,
)
from anidub.extract import trim_silence, rip_audio_track
from anidub.pipeline import (
    process_line,
    get_op_ed_ranges,
    is_in_range,
    make_line_dir_name,
)
from anidub.translate import extract_embedded_ass, translate_ass_to_esperanto

console = Console()

MIN_PREROLL_SEC = 1.0
REF_CLIP_DUR = 3.0
MIN_TRANSCRIPTION_CHARS = 5
SILENCE_TOP_DB = 30
SAFETY_MARGIN_SEC = 0.1


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


def show_anime_picker(anime_list):
    table = Table(title="Pick an anime", show_lines=True)
    table.add_column("#", style="bold cyan", justify="right")
    table.add_column("Name")
    table.add_column("MKV")
    for i, a in enumerate(anime_list):
        table.add_row(str(i + 1), a["name"], a["mkv"].name)
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


def resolve_anime(args) -> tuple[Path, Path | None, str]:
    if args.mkv:
        return args.mkv, args.ass, ""

    if args.anime:
        anime_list = discover_anime(ANIME_ROOT)
        match = [a for a in anime_list if a["name"] == args.anime]
        if not match:
            raise SystemExit(f"Anime '{args.anime}' not found in {ANIME_ROOT}/")
        return match[0]["mkv"], None, match[0]["name"]

    anime_list = discover_anime(ANIME_ROOT)
    if not anime_list:
        raise SystemExit(f"No anime found in {ANIME_ROOT}/")

    if len(anime_list) == 1:
        console.print(f"[dim]Auto-selected: {anime_list[0]['name']}[/]")
        return anime_list[0]["mkv"], None, anime_list[0]["name"]

    show_anime_picker(anime_list)
    idx = IntPrompt.ask("[bold]Pick anime[/]", default=1) - 1
    if idx < 0 or idx >= len(anime_list):
        raise SystemExit("Invalid choice")
    return anime_list[idx]["mkv"], None, anime_list[idx]["name"]


def resolve_audio_lang(args, mkv_path: Path) -> int:
    from anidub.extract import probe_audio_streams
    from rich.prompt import Prompt
    streams = probe_audio_streams(mkv_path)
    if len(streams) <= 1:
        return 0
    if args.audio_lang is not None:
        lang = args.audio_lang.lower()
        for i, s in enumerate(streams):
            if s["language"].lower() == lang:
                return i
        langs = [s["language"] for s in streams]
        raise SystemExit(
            f"Language '{args.audio_lang}' not found. Available: {', '.join(langs)}"
        )
    table = Table(title="Audio tracks", show_lines=True)
    table.add_column("#", style="bold cyan", justify="right")
    table.add_column("Language", style="green")
    table.add_column("Codec")
    table.add_column("Ch")
    table.add_column("Rate")
    table.add_column("Title")
    for i, s in enumerate(streams):
        table.add_row(
            str(i), s["language"], s["codec"], str(s["channels"]),
            str(s.get("sample_rate", "?")), s.get("title", "-")[:30],
        )
    console.print(table)
    choice = Prompt.ask(
        "[bold]Pick audio language[/]", default=streams[0]["language"]
    ).strip().lower()
    for i, s in enumerate(streams):
        if s["language"].lower() == choice:
            return i
    for i, s in enumerate(streams):
        if choice in s.get("title", "").lower():
            return i
    return 0


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


def run_single_line(args):
    mkv_path, ass_path, anime_name = resolve_anime(args)
    audio_stream = resolve_audio_lang(args, mkv_path)
    if ass_path is None:
        ass_path = auto_detect_ass(mkv_path)
    if ass_path is None:
        console.print("[red]No ASS file found. Use --translate to extract/translate from MKV.[/]")
        return 1

    run_dir = anime_test_dir(anime_name) if anime_name else today_output_dir()
    run_dir.mkdir(parents=True, exist_ok=True)

    ripped_wav = run_dir / "ripped_audio.wav"
    if not ripped_wav.exists():
        rip_audio_track(mkv_path, ripped_wav, audio_stream_index=audio_stream)
        console.print(f"[dim]Ripped audio -> {ripped_wav}[/]")

    console.print(
        Panel.fit(
            "[bold cyan]anidub-test-voice[/] - anime dubbing test\n"
            f"[dim]mkv: {mkv_path}\nass: {ass_path}\n"
            f"whisper: {args.whisper_model}\n"
            f"output: {run_dir}[/]",
            border_style="cyan",
        )
    )

    ass_header = get_ass_header(ass_path)

    console.print("[bold]Checking Demucs cache...[/]")
    full_no_vocals, full_vocals = ensure_demucs_cache_from_wav(ripped_wav, run_dir)
    console.print(f"[dim]  no_vocals: {full_no_vocals}[/]")

    events = parse_ass(ass_path)
    main_events = filter_dialogue(events)
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
            f"[bold]Text:[/] {line['clean_text']}",
            title="[bold]Selected[/]",
            border_style="cyan",
        )
    )

    out_dir = run_dir / f"line_{line['index']:03d}"
    out_dir.mkdir(parents=True, exist_ok=True)

    try:
        console.rule("[bold cyan]OmniVoice[/]")

        from anidub.tts.omnivoice import OmniVoiceTTSBackend
        backend = OmniVoiceTTSBackend(whisper_model=args.whisper_model)

        result = process_line(
            ripped_wav, mkv_path, line, args.whisper_model, out_dir,
            backend, full_no_vocals, ass_header,
            voice_timeout=args.voice_timeout,
        )

        rt = result["diagnostics"].get("ref_transcription")
        if rt:
            print_ref_transcription_block(rt)

        print_params_block(result, args.whisper_model)

        ref_wav = out_dir / "ref.wav"
        print_prompt_block(result, ref_wav)
        print_running_block(result["diagnostics"].get("cuda_mem_before_mb", 0.0))

        stats = {
            "raw_dur": result["raw_dur"],
            "effective_dur": result["effective_dur"],
            "inference_ms": result["inference_ms"],
            "cuda_mem_after_mb": result["cuda_mem_after_mb"],
            "atempo": result["atempo"],
        }
        print_result_block(stats, out_dir / "tts.wav", result["slack_ms"])

        console.print(f"[green]final.mkv -> {result['assembly']['final']}[/]")

        log: dict = {
            "line": {
                "index": line["index"],
                "start_sec": line["start_sec"],
                "end_sec": line["end_sec"],
                "speaker": line.get("name") or None,
                "text": line["clean_text"],
            },
            "target_duration": result["target_dur"],
            "instruct": result["diagnostics"]["prompt_text"],
            "stats": stats,
            "diagnostics": result["diagnostics"],
            "assembly": result["assembly"],
        }
        log_path = out_dir / "log.json"
        with log_path.open("w", encoding="utf-8") as f:
            json.dump(log, f, indent=2, ensure_ascii=False, default=str)
        console.print(f"\n[bold]Logs -> {log_path}[/]")

        table = Table(title="Summary", show_lines=True)
        table.add_column("Metric", style="bold cyan")
        table.add_column("Value", justify="right")
        table.add_row("Inference", f"{stats['inference_ms']} ms")
        table.add_row("Raw duration", f"{result['raw_dur']:.3f}s")
        table.add_row("Effective duration", f"{result['effective_dur']:.3f}s")
        table.add_row("Target duration", f"{result['target_dur']:.3f}s")
        table.add_row("Slack", f"{result['slack_ms']:+.0f} ms")
        verdict = "FITS" if result["slack_ms"] >= 0 else "OVERSHOOT"
        table.add_row("Verdict", f"[{'green' if result['slack_ms'] >= 0 else 'red'}]{verdict}[/]")
        table.add_row("Final mkv", str(result["assembly"]["final"]))
        console.print(table)

    except Exception as e:
        console.print(f"[red]Failed: {e}[/]")
        import traceback
        console.print(f"[dim]{traceback.format_exc()}[/]")
        return 1

    return 0


def run_batch(args):
    mkv_path, ass_path, anime_name = resolve_anime(args)
    audio_stream = resolve_audio_lang(args, mkv_path)
    if ass_path is None:
        ass_path = auto_detect_ass(mkv_path)

    if args.translate or ass_path is None:
        if ass_path is None or not ass_path.exists():
            src_ass = mkv_path.parent / f"{mkv_path.stem}_orig.ass"
            console.print("[bold]Extracting embedded ASS...[/]")
            extract_embedded_ass(mkv_path, src_ass)
        else:
            src_ass = ass_path
        ass_path = mkv_path.parent / f"{mkv_path.stem}_eo.ass"
        console.print(f"[bold]Translating to Esperanto...[/]")
        result = translate_ass_to_esperanto(src_ass, ass_path, delay=1.0, auto=args.auto)
        console.print(f"  {result['translated']}/{result['total']} lines, "
                      f"{result['failed']} failed, {result['elapsed_sec']:.0f}s")
        console.print(f"  -> {ass_path}")
        if not args.batch:
            console.print("[green]Translation complete. Review the .ass, then run --batch.[/]")
            return 0

    if not ass_path or not ass_path.exists():
        console.print("[red]No ASS file found.[/]")
        return 1

    batch_dir = anime_batch_dir(anime_name) if anime_name else today_batch_dir()
    batch_dir.mkdir(parents=True, exist_ok=True)

    console.print(
        Panel.fit(
            "[bold cyan]anidub-test-voice --batch[/]\n"
            f"[dim]mkv: {mkv_path}\nass: {ass_path}\n"
            f"whisper: {args.whisper_model}\n"
            f"output: {batch_dir}[/]",
            border_style="cyan",
        )
    )

    ass_header = get_ass_header(ass_path)

    ripped_wav = batch_dir / "ripped_audio.wav"
    if not ripped_wav.exists():
        console.print("[bold]Ripping selected audio track...[/]")
        rip_audio_track(mkv_path, ripped_wav, audio_stream_index=audio_stream)
        console.print(f"[dim]  ripped: {ripped_wav}[/]")

    console.print("[bold]Checking Demucs cache...[/]")
    full_no_vocals, full_vocals = ensure_demucs_cache_from_wav(ripped_wav, batch_dir)
    console.print(f"[dim]  no_vocals: {full_no_vocals}[/]")

    full_original_audio = ripped_wav

    events = parse_ass(ass_path)
    main_events = filter_dialogue(events)
    console.print(
        f"[dim]Parsed {len(events)} events, "
        f"{len(main_events)} `main` dialogue.[/]"
    )

    usable, skipped = split_lines(main_events)
    skipped_path = batch_dir / "skipped.json"
    save_skipped(skipped, skipped_path)
    console.print(
        f"[dim]{len(usable)} usable, {len(skipped)} skipped "
        f"(-> {skipped_path})[/]"
    )

    intro_start, intro_end, outro_start, outro_end = get_op_ed_ranges(events)
    console.print(
        f"[dim]OP: {fmt_ts(intro_start)} -> {fmt_ts(intro_end)}  "
        f"ED: {fmt_ts(outro_start)} -> {fmt_ts(outro_end)}[/]"
    )

    body_lines = []
    for line in usable:
        in_op = is_in_range(line["start_sec"], line["end_sec"], intro_start, intro_end)
        in_ed = is_in_range(line["start_sec"], line["end_sec"], outro_start, outro_end)
        if in_op or in_ed:
            continue
        body_lines.append(line)
    console.print(
        f"[dim]{len(body_lines)} body lines to voice "
        f"({len(usable) - len(body_lines)} skipped - in OP/ED range)[/]"
    )

    if not body_lines:
        console.print("[red]No lines to voice.[/]")
        return 1

    if not Confirm.ask(f"Voice {len(body_lines)} lines?"):
        return 0

    console.print("[bold]Loading TTS backends...[/]")
    from anidub.tts.omnivoice import OmniVoiceTTSBackend
    tts_backend = OmniVoiceTTSBackend(whisper_model=args.whisper_model)
    console.print("[green]TTS backend ready.[/]")

    console.print("[dim]Press 's' to skip the next line (no Enter needed)[/]")

    clips_dir = batch_dir / "clips"
    clips_dir.mkdir(parents=True, exist_ok=True)

    voiced_results = []
    errors = []
    t0_total = time.perf_counter()

    with Progress(
        SpinnerColumn(),
        TextColumn("[bold cyan]{task.description}"),
        BarColumn(bar_width=30),
        TextColumn("[dim]{task.completed}/{task.total}[/]"),
        TimeRemainingColumn(),
        console=console,
    ) as progress:
        overall = progress.add_task(
            f"Voicing {len(body_lines)} lines...", total=len(body_lines),
        )

        for line in body_lines:
            skip_this = False
            while msvcrt.kbhit():
                ch = msvcrt.getch().lower()
                if ch == b's':
                    skip_this = True
            if skip_this:
                progress.update(overall, advance=1,
                                description=f"[yellow]Skip[/] {line['clean_text'][:60]}")
                errors.append({
                    "line_index": line["index"],
                    "text": line["clean_text"],
                    "start_sec": line["start_sec"],
                    "end_sec": line["end_sec"],
                    "error": "Manual skip (user pressed 's')",
                })
                continue

            dir_name = make_line_dir_name(line)
            line_dir = clips_dir / dir_name
            final_mkv = line_dir / "final.mkv"

            if final_mkv.exists():
                progress.update(overall, advance=1,
                                description=f"[dim]Skip[/] {line['clean_text'][:60]}")
                continue

            progress.update(
                overall,
                description=f"[bold]{line['clean_text'][:60]}[/]",
            )

            try:
                result = process_line(
                    ripped_wav, mkv_path, line, args.whisper_model, line_dir,
                    tts_backend, full_no_vocals, ass_header,
                    voice_timeout=args.voice_timeout,
                )
                voiced_results.append(result)
            except Exception as e:
                errors.append({
                    "line_index": line["index"],
                    "text": line["clean_text"],
                    "start_sec": line["start_sec"],
                    "end_sec": line["end_sec"],
                    "error": str(e),
                })
            progress.update(overall, advance=1)

    elapsed = time.perf_counter() - t0_total

    succeeded = len(voiced_results)
    failed = len(errors)
    console.print(
        f"[bold]Voicing complete:[/] {succeeded} succeeded, "
        f"{failed} failed ({elapsed:.0f}s)"
    )

    if voiced_results:
        console.print("[bold]Assembling full episode...[/]")
        try:
            from anidub.full_episode import build_full_episode
            final_episode = build_full_episode(
                mkv_path, events, batch_dir,
                full_no_vocals, full_original_audio,
                voiced_results, ass_path, errors=errors if errors else None,
            )
            console.print(f"[green]Full episode -> {final_episode}[/]")
        except Exception as e:
            console.print(f"[red]Full episode assembly failed: {e}[/]")
            import traceback
            console.print(f"[dim]{traceback.format_exc()}[/]")

    batch_log = {
        "total_lines": len(body_lines),
        "succeeded": succeeded,
        "failed": failed,
        "elapsed_sec": elapsed,
        "whisper_model": args.whisper_model,
        "errors": errors,
        "results": [
            {
                "line_index": r["line_index"],
                "start_sec": r["start_sec"],
                "end_sec": r["end_sec"],
                "text": r["text"],
                "raw_dur": r["raw_dur"],
                "effective_dur": r["effective_dur"],
                "slack_ms": r["slack_ms"],
                "atempo": r["atempo"],
                "inference_ms": r["inference_ms"],
            }
            for r in voiced_results
        ],
    }
    log_path = batch_dir / "batch_log.json"
    with log_path.open("w", encoding="utf-8") as f:
        json.dump(batch_log, f, indent=2, ensure_ascii=False, default=str)
    console.print(f"[bold]Logs -> {log_path}[/]")

    table = Table(title="Batch Summary", show_lines=True)
    table.add_column("Metric", style="bold cyan")
    table.add_column("Value", justify="right")
    table.add_row("Total body lines", str(len(body_lines)))
    table.add_row("Succeeded", f"[green]{succeeded}[/]")
    table.add_row("Failed", f"[red]{failed}[/]" if failed else "0")
    table.add_row("Elapsed", f"{elapsed:.0f}s")
    table.add_row("Full episode", str(final_episode) if voiced_results else "N/A")
    if voiced_results:
        fits = sum(1 for r in voiced_results if r["slack_ms"] >= 0)
        table.add_row("Fit windows", f"{fits}/{succeeded}")
        total_inf = sum(r["inference_ms"] or 0 for r in voiced_results)
        table.add_row("Total inference", f"{total_inf:.0f} ms")
    console.print(table)

    return 0


def main():
    ap = argparse.ArgumentParser(
        prog="anidub-test-voice",
        description="Voice subtitle lines with OmniVoice",
    )
    ap.add_argument("--mkv", type=Path, default=None)
    ap.add_argument("--ass", type=Path, default=None)
    ap.add_argument("--anime", default=None, help="pick anime by folder name")
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
    ap.add_argument("--batch", action="store_true", help="process all usable lines")
    ap.add_argument(
        "--translate", action="store_true",
        help="extract/translate embedded ASS to Esperanto before voicing",
    )
    ap.add_argument(
        "--auto", action="store_true",
        help="auto-merge duplicate/progressive lines during translate (no prompts)",
    )
    ap.add_argument(
        "--audio-lang", default=None,
        help="audio track language for Demucs + voice clone (e.g. jpn, eng)",
    )
    ap.add_argument(
        "--voice-timeout", type=int, default=120,
        help="seconds before aborting a stuck voice generation (default 120)",
    )
    args = ap.parse_args()

    if not get_ffmpeg_location():
        console.print("[red]ffmpeg not found. Run .\\install.ps1[/]")
        return 1

    if args.batch:
        return run_batch(args)
    return run_single_line(args)


if __name__ == "__main__":
    raise SystemExit(main())