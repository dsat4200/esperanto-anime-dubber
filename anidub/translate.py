import argparse
import re
import sys
import time
from pathlib import Path

from anidub.ass import parse_ass, strip_override_tags
from anidub.config import get_ffmpeg_location, discover_anime, auto_detect_ass, ANIME_ROOT


def _ffmpeg_bin():
    loc = get_ffmpeg_location()
    if not loc:
        raise RuntimeError("ffmpeg not found")
    return str(Path(loc) / "ffmpeg.exe")


def extract_embedded_ass(mkv_path: Path, out_path: Path, stream_index: int = 0) -> Path:
    import subprocess
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


def translate_ass_to_esperanto(
    in_path: Path,
    out_path: Path,
    delay: float = 1.0,
    verbose: bool = True,
) -> dict:
    from deep_translator import GoogleTranslator

    translator = GoogleTranslator(source="auto", target="eo")
    events = parse_ass(in_path)

    with in_path.open("r", encoding="utf-8-sig") as f:
        lines = f.readlines()

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
            result = translator.translate(clean)
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
    result = translate_ass_to_esperanto(src_ass, out_ass, delay=args.delay)

    print(f"Done: {result['translated']}/{result['total']} lines translated, "
          f"{result['failed']} failed in {result['elapsed_sec']:.0f}s")
    print(f"Output: {result['out_path']}")
    if result["failed"]:
        print("Review the output .ass for failed lines (kept original text).")
    return 0


if __name__ == "__main__":
    sys.exit(main())