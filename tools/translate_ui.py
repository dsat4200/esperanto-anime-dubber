#!/usr/bin/env python3
"""Generate i18n/eo.json, usage_eo.txt and README_eo.md from English sources.

Reuses the Google-Translate path from anidub.translate so we don't add
new dependencies: just deep-translator with a 500-backoff retry.

Usage::

    python tools/translate_ui.py               # UI + usage + README
    python tools/translate_ui.py --ui-only      # en.json -> eo.json
    python tools/translate_ui.py --usage-only   # usage.txt -> usage_eo.txt
    python tools/translate_ui.py --readme-only  # README.md -> README_eo.md
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

# Windows consoles default to cp1252; the output contains Esperanto
# diacritics (ĉ, ĝ, ĥ, ĵ, ŝ, ŭ) that cp1252 can't encode.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = Path(__file__).resolve().parent.parent
EN_JSON = ROOT / "anidub" / "gui" / "static" / "i18n" / "en.json"
EO_JSON = ROOT / "anidub" / "gui" / "static" / "i18n" / "eo.json"
USAGE_EN = ROOT / "usage.txt"
USAGE_EO = ROOT / "usage_eo.txt"
README_EN = ROOT / "README.md"
README_EO = ROOT / "README_eo.md"

# UI strings that are purely symbolic / literal placeholders; copied verbatim.
UI_SKIP = {
    "nav.language",            # "Language" — the word used as the dropdown label sits next to the dropdown itself; keep neutral
    "home.picker_load_default",
    "editor.character_none_option",
    "editor.episode_select_default",
    "chars.close",
    "editor.clip_title_initial",
}

# Regex matching lines in usage.txt we must leave untouched (commands, paths,
# ASCII art).  We translate prose only.
_CMD_RE = re.compile(r"^\s*>")
_BOX_ART_RE = re.compile(r"^[=\u2500-\u257F\s]+$")
# A line with no spaces and only path/filename-ish chars: skip.
_PATHY_RE = re.compile(r"^[\w.\-\\/{}\s]+\.\w{1,4}$")
_PURE_PATH_RE = re.compile(r"^[\w.\-\\/{}]+\s*$")


def _translator():
    from deep_translator import GoogleTranslator
    return GoogleTranslator(source="auto", target="eo")


def _safe_translate(translator, text: str, retries: int = 2) -> str:
    """Translate text, retrying once on Google 500s (mirrors translate.py)."""
    if not text or not text.strip():
        return text
    last_err = None
    for attempt in range(retries):
        try:
            result = translator.translate(text)
            return result or text
        except Exception as e:
            err = str(e)
            if "500" in err or "Server Error" in err:
                if attempt < retries - 1:
                    print(f"  Google 500, retrying in 30s...", file=sys.stderr)
                    time.sleep(30)
                    continue
            last_err = e
            break
    if last_err:
        print(f"  !! translate failed, keeping source: {last_err}", file=sys.stderr)
    return text


def _post_template_check(src: str, dst: str) -> str:
    """Ensure all {token} placeholders from src survived in dst.

    Google Translate usually preserves {foo} braced tokens; if it dropped any
    (or added some), fall back to the source string so templates still parse.
    """
    src_toks = set(re.findall(r"\{[^}]+\}", src))
    if not src_toks:
        return dst
    if src_toks == set(re.findall(r"\{[^}]+\}", dst)):
        return dst
    print(f"  !! template tokens changed by Google, keeping source: {src!r}", file=sys.stderr)
    return src


def translate_ui(translator) -> int:
    print(f"Loading {EN_JSON}")
    data = json.loads(EN_JSON.read_text(encoding="utf-8"))
    out: dict[str, str] = {}
    keys = list(data.keys())
    for i, key in enumerate(keys, 1):
        src = data[key]
        if key in UI_SKIP or not src.strip():
            out[key] = src
            print(f"  [{i:>3d}/{len(keys)}] {key}: (skip) {src!r}")
            continue
        dst = _safe_translate(translator, src)
        dst = _post_template_check(src, dst)
        out[key] = dst
        print(f"  [{i:>3d}/{len(keys)}] {key}: {src!r} -> {dst!r}")
    EO_JSON.write_text(json.dumps(out, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"Wrote {EO_JSON}")
    return 0


def _line_should_skip(line: str) -> bool:
    stripped = line.strip()
    if not stripped:
        return True
    if _CMD_RE.match(line):
        return True
    if _BOX_ART_RE.match(line):
        return True
    if _PATHY_RE.match(stripped):
        return True
    if _PURE_PATH_RE.match(stripped):
        return True
    return False


def translate_usage(translator) -> int:
    print(f"Loading {USAGE_EN}")
    lines = USAGE_EN.read_text(encoding="utf-8").splitlines(keepends=True)
    out_lines: list[str] = []
    for i, line in enumerate(lines, 1):
        if _line_should_skip(line):
            out_lines.append(line)
            # Log skipped lines as-is for visibility.
            if line.strip():
                print(f"  [{i:>3d}] (skip) {line.rstrip()!r}")
            continue
        # Translate without trailing newline, then reattach the newline.
        src = line.rstrip("\r\n")
        dst = _safe_translate(translator, src)
        # Preserve the original line terminator shape (LF/CRLF).
        if line.endswith("\r\n"):
            out_lines.append(dst + "\r\n")
        elif line.endswith("\n"):
            out_lines.append(dst + "\n")
        else:
            out_lines.append(dst)
        print(f"  [{i:>3d}] {src[:60]!r} -> {dst[:60]!r}")
    USAGE_EO.write_text("".join(out_lines), encoding="utf-8")
    print(f"Wrote {USAGE_EO}")
    return 0


# Markdown fence detection: a line whose stripped content is one of
# ``` / ```python / ```powershell etc.  We treat ``` fence boundaries
# specially below rather than dropping them via _line_should_skip.
_FENCE_RE = re.compile(r"^\s*```")
_HTML_COMMENT_OPEN_RE = re.compile(r"^\s*<!--")
_MD_TABLE_RE = re.compile(r"^\s*\|")
_MD_IMAGE_RE = re.compile(r"^\s*!\[")


def translate_readme(translator) -> int:
    """Translate README.md prose to README_eo.md.

    Skips (preserves verbatim):
        * fenced code blocks (inclusive backtick lines)
        * pipe-table rows (lines beginning with ``|``)
        * HTML comments (lines beginning with ``<!--``) — keeps
          ``<!-- SCREENCAP: ... -->`` markers identical in both files
        * standalone image lines (``![alt](path)``)
        * shell commands (lines beginning with ``>``), path-shaped
          lines, and ASCII-only box-art / divider lines (reuses
          _line_should_skip)
    """
    print(f"Loading {README_EN}")
    lines = README_EN.read_text(encoding="utf-8").splitlines(keepends=True)
    out_lines: list[str] = []
    in_fence = False
    for i, line in enumerate(lines, 1):
        stripped = line.rstrip("\r\n")

        # Fence state machine: toggles on ``` lines; keeps fence lines
        # and any content inside the block in source language.
        if _FENCE_RE.match(stripped):
            in_fence = not in_fence
            out_lines.append(line)
            print(f"  [{i:>3d}] (fence) {stripped[:60]!r}")
            continue
        if in_fence:
            out_lines.append(line)
            continue

        # Skip-on-preserve rules specific to markdown:
        if _HTML_COMMENT_OPEN_RE.match(line) or _MD_TABLE_RE.match(line) or _MD_IMAGE_RE.match(line):
            out_lines.append(line)
            print(f"  [{i:>3d}] (skip) {stripped[:60]!r}")
            continue

        if _line_should_skip(line):
            out_lines.append(line)
            if line.strip():
                print(f"  [{i:>3d}] (skip) {stripped[:60]!r}")
            continue

        src = stripped
        dst = _safe_translate(translator, src)
        if line.endswith("\r\n"):
            out_lines.append(dst + "\r\n")
        elif line.endswith("\n"):
            out_lines.append(dst + "\n")
        else:
            out_lines.append(dst)
        print(f"  [{i:>3d}] {src[:60]!r} -> {dst[:60]!r}")
    README_EO.write_text("".join(out_lines), encoding="utf-8")
    print(f"Wrote {README_EO}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ui-only", action="store_true")
    ap.add_argument("--usage-only", action="store_true")
    ap.add_argument("--readme-only", action="store_true")
    args = ap.parse_args()

    # If any specific flag is set, run only those; otherwise run all.
    any_specific = args.ui_only or args.usage_only or args.readme_only
    if any_specific:
        do_ui = args.ui_only
        do_usage = args.usage_only
        do_readme = args.readme_only
    else:
        do_ui = True
        do_usage = True
        do_readme = True

    translator = _translator()

    if do_ui:
        if (rc := translate_ui(translator)) != 0:
            return rc
    if do_usage:
        if (rc := translate_usage(translator)) != 0:
            return rc
    if do_readme:
        if (rc := translate_readme(translator)) != 0:
            return rc
    return 0


if __name__ == "__main__":
    sys.exit(main())