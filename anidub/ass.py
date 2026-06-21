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


def filter_main_dialogue(events: list[AssEvent]) -> list[AssEvent]:
    return [e for e in events if e["style"] == "main"]


def strip_override_tags(text: str) -> str:
    text = _OVERRIDE_TAG_RE.sub("", text)
    text = _NEWLINE_TAG_RE.sub(" ", text)
    return text.strip()


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