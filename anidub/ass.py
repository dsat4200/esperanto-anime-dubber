import re
from pathlib import Path
from typing import Literal, TypedDict


class AssEvent(TypedDict):
    index: int
    start_sec: float
    end_sec: float
    style: str
    name: str
    text: str


_TIMESTAMP_RE = re.compile(r"(\d+):(\d{2}):(\d{2})\.(\d{2,3})")
_DIALOGUE_RE = re.compile(r"^Dialogue:\s*(.*)$")
_FORMAT_RE = re.compile(r"^Format:\s*(.*)$")

_OVERRIDE_TAG_RE = re.compile(r"\{[^}]*\}")
_NEWLINE_TAG_RE = re.compile(r"\\N|\\n")
_RAW_TAG_RE = re.compile(r"\\[hH]\s*")

_CJK_RE = re.compile(r"[\u3040-\u309f\u30a0-\u30ff\u4e00-\u9fff\uff00-\uffef]")

_JP_TOKENS = {
    "onii-chan", "onee-chan", "onii", "onee", "-chan", "-kun",
    "-san", "-sama", "-senpai", "senpai", "itadakimasu", "nee", "nii",
    "nee-san", "nii-san", "okaeri", "tadaima", "gomen", "gomenasai",
    "sayonara", "baka", "kawaii", "sugoi", "nani", "urusai",
    "yahallo", "arigatou", "douzo",
}
_JP_TOKEN_RE = re.compile(
    r"(?:^|\s|\W)(" + "|".join(re.escape(t) for t in _JP_TOKENS) + r")(?:$|\s|\W)",
    re.IGNORECASE,
)


def parse_timestamp(ts: str) -> float:
    m = _TIMESTAMP_RE.match(ts.strip())
    if not m:
        raise ValueError(f"Bad ASS timestamp: {ts!r}")
    h, mm, ss, cc = (int(g) for g in m.groups())
    return h * 3600 + mm * 60 + ss + cc / 100.0


def parse_ass(path: Path) -> list[AssEvent]:
    path = Path(path)
    events: list[AssEvent] = []
    in_events = False
    fmt_fields: list[str] | None = None
    with path.open("r", encoding="utf-8-sig") as f:
        for line in f:
            line = line.rstrip("\r\n")
            if line.strip() == "[Events]":
                in_events = True
                continue
            if not in_events:
                continue
            m_fmt = _FORMAT_RE.match(line)
            if m_fmt and fmt_fields is None:
                fmt_fields = [x.strip() for x in m_fmt.group(1).split(",")]
                continue
            m_dlg = _DIALOGUE_RE.match(line)
            if m_dlg and fmt_fields is not None:
                parts = m_dlg.group(1).split(",", len(fmt_fields) - 1)
                if len(parts) < len(fmt_fields):
                    continue
                row = dict(zip(fmt_fields, (p.strip() for p in parts)))
                try:
                    event: AssEvent = {
                        "index": len(events),
                        "start_sec": parse_timestamp(row["Start"]),
                        "end_sec": parse_timestamp(row["End"]),
                        "style": row.get("Style", ""),
                        "name": row.get("Name", ""),
                        "text": row.get("Text", ""),
                    }
                    events.append(event)
                except (ValueError, KeyError):
                    continue
    return events


_NON_DIALOGUE_TOKENS = {"op", "ed", "sign", "title", "note"}


def filter_dialogue(events: list[AssEvent]) -> list[AssEvent]:
    return [
        e for e in events
        if not any(t in e["style"].lower() for t in _NON_DIALOGUE_TOKENS)
    ]


def filter_main_dialogue(events: list[AssEvent]) -> list[AssEvent]:
    return filter_dialogue(events)


def strip_override_tags(text: str) -> str:
    text = _OVERRIDE_TAG_RE.sub("", text)
    text = _NEWLINE_TAG_RE.sub(" ", text)
    text = _RAW_TAG_RE.sub("", text)
    return text.strip()


def merge_duplicate_lines(lines: list[str], min_repeat: int = 4, interactive: bool = True) -> list[str]:
    def _clean(line: str) -> str:
        parts = line.split(",", 9)
        if len(parts) < 10:
            return ""
        return strip_override_tags(parts[9]).strip().lower()

    def _end(line: str) -> str:
        return line.split(",", 9)[2]

    def _build_merged(first: str, last: str) -> str:
        fp = first.split(",", 9)
        merged = ",".join(fp[:2] + [_end(last)] + fp[3:])
        return merged + "\n" if not merged.endswith("\n") else merged

    def _ask_merge(run_len: int, text: str, first: str, last: str) -> bool:
        start_ts = first.split(",", 9)[1]
        end_ts = _end(last)
        ans = input(
            f"  Merge {run_len} copies of '{text[:70]}' "
            f"(0:{start_ts} -> 0:{end_ts})? [Y/n] "
        ).strip().lower()
        return ans in ("", "y", "yes")

    def _check_substring(out: list, cur_text: str) -> bool:
        if not out:
            return False
        prev = out[-1]
        if not prev.startswith("Dialogue:"):
            return False
        pt = _clean(prev)
        if not pt or pt == cur_text:
            return False
        shorter = pt if len(pt) < len(cur_text) else cur_text
        longer = cur_text if len(pt) < len(cur_text) else pt
        if len(shorter) >= 2 and len(shorter) >= len(longer) / 2:
            if longer.startswith(shorter):
                ans = input(
                    f"  Substring: '{pt[:50]}' -> '{cur_text[:50]}'. Merge? [Y/n] "
                ).strip().lower()
                return ans in ("", "y", "yes")
        return False

    # Pass 0: non-consecutive cyclic duplicates (runs FIRST)
    text_to_indices: dict[str, list[int]] = {}
    for idx, line in enumerate(lines):
        if line.startswith("Dialogue:"):
            t = _clean(line)
            if t:
                text_to_indices.setdefault(t, []).append(idx)

    merge_groups = [(inds, t) for t, inds in text_to_indices.items() if len(inds) >= 2]
    if merge_groups and interactive:
        total_lines = sum(len(inds) for inds, _ in merge_groups)
        msg = (
            f"  Found {len(merge_groups)} non-consecutive duplicate group(s) "
            f"({total_lines} lines). Merge? [Y/n] "
        )
        if input(msg).strip().lower() in ("", "y", "yes"):
            keep = {inds[0] for inds, _ in merge_groups}
            drop = set()
            for inds, _ in merge_groups:
                drop.update(inds[1:])
            new_lines = []
            for idx, line in enumerate(lines):
                if idx in drop:
                    continue
                if idx in keep:
                    inds = next(i for i, _ in merge_groups if i[0] == idx)
                    new_lines.append(_build_merged(line, lines[inds[-1]]))
                else:
                    new_lines.append(line)
            lines = new_lines

    # Pass 1: consecutive identical runs
    result = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if not line.startswith("Dialogue:"):
            result.append(line)
            i += 1
            continue

        cur_text = _clean(line)
        if not cur_text:
            result.append(line)
            i += 1
            continue

        j = i + 1
        while j < len(lines) and lines[j].startswith("Dialogue:"):
            if _clean(lines[j]) != cur_text:
                break
            j += 1

        run_len = j - i
        should_merge = run_len >= min_repeat
        if not should_merge and run_len >= 2 and interactive:
            should_merge = _ask_merge(run_len, cur_text, line, lines[j - 1])

        if should_merge:
            merged = _build_merged(line, lines[j - 1])

            if interactive and _check_substring(result, cur_text):
                prev_line = result.pop()
                prev_text = _clean(prev_line)
                longer_line = line if len(cur_text) >= len(prev_text) else prev_line
                merged = _build_merged(prev_line, lines[j - 1])
                mp = merged.split(",", 9)
                lp = longer_line.split(",", 9)
                if len(mp) >= 10 and len(lp) >= 10:
                    mp[9] = lp[9]
                    merged = ",".join(mp)

            result.append(merged)
        else:
            result.extend(lines[i:j])

        i = j

    if not interactive:
        return result

    # Pass 2: adjacent substring merges
    i = 0
    while i < len(result) - 1:
        a = result[i]
        b = result[i + 1]
        if not a.startswith("Dialogue:") or not b.startswith("Dialogue:"):
            i += 1
            continue
        ta = _clean(a)
        tb = _clean(b)
        if ta and tb and ta != tb:
            shorter = ta if len(ta) < len(tb) else tb
            longer = tb if len(ta) < len(tb) else ta
            if len(shorter) >= 2 and len(shorter) >= len(longer) / 2:
                if longer.startswith(shorter):
                    ans = input(
                        f"  Substring: '{ta[:50]}' -> '{tb[:50]}'. Merge? [Y/n] "
                    ).strip().lower()
                    if ans in ("", "y", "yes"):
                        result[i] = _build_merged(a, b)
                        result.pop(i + 1)
                        continue
        i += 1

    return result


def get_ass_header(path: Path) -> str:
    path = Path(path)
    with path.open("r", encoding="utf-8-sig") as f:
        header_lines = []
        for line in f:
            header_lines.append(line.rstrip("\r\n"))
            if line.strip() == "[Events]":
                break
        next_line = f.readline()
        if next_line.strip().startswith("Format:"):
            header_lines.append(next_line.rstrip("\r\n"))
    return "\n".join(header_lines)


def detect_language(text: str) -> Literal["esperanto", "japanese", "unknown"]:
    if _CJK_RE.search(text):
        return "japanese"
    if _JP_TOKEN_RE.search(text):
        return "japanese"
    return "esperanto"