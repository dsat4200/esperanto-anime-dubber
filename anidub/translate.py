import argparse
import json
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from anidub.ass import parse_ass, strip_override_tags, filter_dialogue, merge_duplicate_lines
from anidub.config import get_ffmpeg_location, discover_anime, auto_detect_ass, ANIME_ROOT


def _ffmpeg_bin():
    loc = get_ffmpeg_location()
    if not loc:
        raise RuntimeError("ffmpeg not found")
    return str(Path(loc) / "ffmpeg.exe")


def _probe_sub_streams(mkv_path: Path) -> list[dict]:
    bin_path = str(Path(_ffmpeg_bin()).parent / "ffprobe.exe")
    out = subprocess.run([
        bin_path, "-v", "error",
        "-show_entries", "stream=index,codec_type,codec_name:stream_tags=language,title",
        "-of", "json", str(mkv_path),
    ], capture_output=True, text=True, check=True).stdout
    info = json.loads(out)
    subs = []
    for s in info.get("streams", []):
        if s.get("codec_name") == "ass" and s.get("codec_type") == "subtitle":
            tags = s.get("tags", {})
            subs.append({
                "index": s["index"],
                "language": tags.get("language", ""),
                "title": tags.get("title", ""),
            })
    return subs


def _pick_best_sub_stream(mkv_path: Path, streams: list[dict]) -> int:
    if len(streams) == 1:
        idx = streams[0]["index"]
        print(f"  Single subtitle stream (absolute idx={idx}), using it.")
        return 0

    best_rel = 0
    best_count = -1
    for rel_idx, s in enumerate(streams):
        tmp_ass = Path(tempfile.mktemp(suffix=".ass"))
        try:
            _extract_embedded_ass_raw(mkv_path, tmp_ass, rel_idx)
            events = parse_ass(tmp_ass)
            dialogue = filter_dialogue(events)
            count = len(dialogue)
            label = f"lang={s['language'] or '?'}"
            if s.get("title"):
                label += f" [{s['title']}]"
            print(f"  Track {rel_idx} (abs={s['index']}, {label}): "
                  f"{count} dialogue lines")
            if count > best_count:
                best_count = count
                best_rel = rel_idx
        finally:
            tmp_ass.unlink(missing_ok=True)

    print(f"  Auto-selected track {best_rel} ({best_count} dialogue lines)")
    return best_rel


def _extract_embedded_ass_raw(mkv_path: Path, out_path: Path, stream_index: int) -> Path:
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    bin_path = _ffmpeg_bin()
    subprocess.run([
        bin_path, "-y", "-loglevel", "error",
        "-i", str(mkv_path),
        "-map", f"0:s:{stream_index}",
        "-c:s", "copy",
        str(out_path),
    ], check=True)
    return out_path


def extract_embedded_ass(
    mkv_path: Path, out_path: Path, stream_index: int | None = None,
) -> Path:
    if stream_index is not None:
        return _extract_embedded_ass_raw(mkv_path, out_path, stream_index)

    subs = _probe_sub_streams(mkv_path)
    if not subs:
        raise RuntimeError(f"No ASS subtitle streams found in {mkv_path}")
    chosen = _pick_best_sub_stream(mkv_path, subs)
    return _extract_embedded_ass_raw(mkv_path, out_path, chosen)


def translate_text(text: str, translator) -> str:
    clean = strip_override_tags(text)
    if not clean or not clean.strip():
        return text
    result = translator.translate(clean)
    if not result:
        return text
    tags = re.findall(r"\{[^}]*\}", text)
    prefix = "".join(tags) + " " if tags else ""
    return prefix + result


def translate_single(text: str) -> str:
    from deep_translator import GoogleTranslator
    translator = GoogleTranslator(source="auto", target="eo")
    clean = strip_override_tags(text)
    if not clean or not clean.strip():
        return text
    for attempt in range(2):
        try:
            result = translator.translate(clean)
            break
        except Exception as e:
            err = str(e)
            if ("500" in err or "Server Error" in err) and attempt == 0:
                time.sleep(30)
                continue
            raise
    if not result:
        return text
    tags = re.findall(r"\{[^}]*\}", text)
    prefix = "".join(tags) + " " if tags else ""
    return prefix + result


def translate_ass_to_esperanto(
    in_path: Path,
    out_path: Path,
    delay: float = 1.0,
    verbose: bool = True,
    auto: bool = False,
) -> dict:
    from deep_translator import GoogleTranslator

    translator = GoogleTranslator(source="auto", target="eo")
    events = parse_ass(in_path)

    with in_path.open("r", encoding="utf-8-sig") as f:
        lines = f.readlines()

    before = sum(1 for l in lines if l.startswith("Dialogue:"))
    lines = merge_duplicate_lines(lines, min_repeat=4, interactive=not auto)
    after = sum(1 for l in lines if l.startswith("Dialogue:"))
    if after < before:
        print(f"  Merged {before - after} duplicate lines ({before} -> {after} dialogue lines)")

    dlg_indices = [i for i, l in enumerate(lines) if l.startswith("Dialogue:")]
    total_dlg = len(dlg_indices)

    translated = 0
    failed = 0
    t0 = time.perf_counter()
    dlg_count = 0

    out_lines = list(lines)

    for i, line in enumerate(lines):
        if not line.startswith("Dialogue:"):
            continue

        dlg_count += 1
        try:
            m = re.match(r"^(Dialogue:\s*[^,]*,[^,]*,[^,]*,[^,]*,[^,]*,[^,]*,[^,]*,[^,]*,[^,]*),(.*)$", line)
            if not m:
                continue
            prefix = m.group(1)
            original_text = m.group(2).rstrip("\r\n")
            clean = strip_override_tags(original_text)
            if not clean or not clean.strip():
                continue

            req_start = time.perf_counter()
            for attempt in range(2):
                try:
                    result = translator.translate(clean)
                    break
                except Exception as e:
                    err = str(e)
                    if ("500" in err or "Server Error" in err) and attempt == 0:
                        if verbose:
                            print(f"  Google returned 500, retrying in 30s...")
                        time.sleep(30)
                        req_start = time.perf_counter()
                        continue
                    raise
            req_ms = (time.perf_counter() - req_start) * 1000
            elapsed = time.perf_counter() - t0
            remaining = total_dlg - dlg_count
            eta_s = remaining * (delay + 0.3)

            new_text = result if result else original_text
            tags = re.findall(r"\{[^}]*\}", original_text)
            prefix_tagged = "".join(tags) + " " if tags else ""
            new_full = prefix_tagged + new_text
            out_lines[i] = f"{prefix},{new_full}\n"
            translated += 1

            if verbose:
                eta_str = f"{int(eta_s // 60)}m{int(eta_s % 60):02d}s"
                src = clean[:60] + ("..." if len(clean) > 60 else "")
                dst = new_text[:60] + ("..." if len(new_text) > 60 else "")
                print(f"[{dlg_count:>4d}/{total_dlg}  ETA {eta_str}]  {src}")
                print(f"                    → {dst}")

            time.sleep(delay)
        except Exception:
            failed += 1

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as f:
        f.writelines(out_lines)

    elapsed = time.perf_counter() - t0
    return {
        "total": total_dlg,
        "translated": translated,
        "failed": failed,
        "elapsed_sec": elapsed,
        "out_path": str(out_path),
    }


def main():
    ap = argparse.ArgumentParser(
        prog="anidub-translate",
        description="Extract/tranlate anime subtitles to Esperanto",
    )
    ap.add_argument("--anime", default=None)
    ap.add_argument("--mkv", type=Path, default=None)
    ap.add_argument("--ass", type=Path, default=None)
    ap.add_argument("--delay", type=float, default=1.0)
    ap.add_argument(
        "--auto", action="store_true",
        help="auto-merge all duplicate/progressive lines without prompting",
    )
    args = ap.parse_args()

    if args.mkv:
        mkv_path = args.mkv
    elif args.anime:
        anime_list = discover_anime(ANIME_ROOT)
        match = [a for a in anime_list if a["name"] == args.anime]
        if not match:
            print(f"Anime '{args.anime}' not found in {ANIME_ROOT}/")
            names = [a["name"] for a in anime_list]
            if names:
                print(f"Available: {', '.join(names)}")
            return 1
        mkv_path = match[0]["mkv"]
    else:
        anime_list = discover_anime(ANIME_ROOT)
        if not anime_list:
            print(f"No anime found in {ANIME_ROOT}/")
            return 1
        print("Available anime:")
        for i, a in enumerate(anime_list):
            print(f"  [{i+1}] {a['name']}  ({a['mkv'].name})")
        try:
            choice = int(input("Pick number: ")) - 1
            mkv_path = anime_list[choice]["mkv"]
        except (ValueError, IndexError):
            print("Invalid choice.")
            return 1

    if not mkv_path.exists():
        print(f"MKV not found: {mkv_path}")
        return 1

    if args.ass:
        src_ass = args.ass
    else:
        src_ass = auto_detect_ass(mkv_path)

    if src_ass and src_ass.exists():
        print(f"Found existing ASS: {src_ass}")
    else:
        src_ass = mkv_path.parent / f"{mkv_path.stem}_orig.ass"
        print(f"Extracting embedded ASS from MKV...")
        extract_embedded_ass(mkv_path, src_ass)
        print(f"  -> {src_ass}")

    out_ass = mkv_path.parent / f"{mkv_path.stem}_eo.ass"
    print(f"Translating to Esperanto (delay={args.delay}s)...")
    result = translate_ass_to_esperanto(src_ass, out_ass, delay=args.delay, auto=args.auto)

    print(f"Done: {result['translated']}/{result['total']} lines translated, "
          f"{result['failed']} failed in {result['elapsed_sec']:.0f}s")
    print(f"Output: {result['out_path']}")
    if result["failed"]:
        print("Review the output .ass for failed lines (kept original text).")
    return 0


if __name__ == "__main__":
    sys.exit(main())