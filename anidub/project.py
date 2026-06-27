import json
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from anidub.ass import parse_ass, filter_dialogue, get_ass_header, strip_override_tags
from anidub.config import get_ffmpeg_location
from anidub.decompose import decompose_mkv
from anidub.characters import (
    save_character_clip, delete_character_clip,
    list_characters, list_character_moods,
    get_character_clip, get_all_character_clips,
)


PROJECTS_ROOT = Path("projects")


def project_dir(mkv_path: Path) -> Path:
    return PROJECTS_ROOT / mkv_path.stem


class ClipStatus(str, Enum):
    PENDING = "pending"
    TRANSLATING = "translating"
    TRANSLATED = "translated"
    CLONING = "cloning"
    CLONED = "cloned"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    SKIPPED = "skipped"
    NON_DUB = "non_dub"


class RefSource(str, Enum):
    CLIP = "clip"
    CHARACTER = "character"


@dataclass
class TimelineRegion:
    start_sec: float
    end_sec: float
    kind: str  # "op", "ed", "clip", "gap"
    clip_index: int | None = None
    status: ClipStatus | None = None


@dataclass
class ClipState:
    index: int
    start_sec: float
    end_sec: float
    original_text: str
    translated_text: str | None = None
    offset_ms: float = 0.0
    character: str | None = None
    character_mood: str | None = None
    ref_source: RefSource = RefSource.CLIP
    ref_clip: str | None = None
    status: ClipStatus = ClipStatus.PENDING
    clone_path: str | None = None
    clone_ms: float | None = None
    attempts: int = 0
    instruct_extra: str | None = None


class Project:
    def __init__(self, path: Path, chars_dir: Path | None = None):
        self.path = Path(path)
        self.state: dict = {}
        self._ass_header: str | None = None
        self._ass_events: list | None = None
        self._chars_dir = chars_dir or self.path

    # ═══════════════════════════════════════════
    #  Lifecycle
    # ═══════════════════════════════════════════

    @classmethod
    def create(cls, mkv_path: Path, name: str | None = None) -> "Project":
        mkv_path = Path(mkv_path)
        if name is None:
            name = mkv_path.stem
        pd = Project(PROJECTS_ROOT / name)
        pd.path.mkdir(parents=True, exist_ok=True)

        result = decompose_mkv(mkv_path, pd.path)

        pd.state = {
            "version": 1,
            "source": {
                "mkv_path": str(mkv_path.resolve()),
                "audio_track_rel": 0,
                "subtitle_track_rel": 0,
            },
            "tracks": {
                "audio": [
                    {
                        "rel_index": t["rel_index"],
                        "path": str(t["path"].relative_to(pd.path)),
                        "language": t["language"],
                        "title": t["title"],
                        "codec": t["codec"],
                        "channels": t["channels"],
                    }
                    for t in result["audio_tracks"]
                ],
                "subtitle": [
                    {
                        "rel_index": t["rel_index"],
                        "path": str(t["path"].relative_to(pd.path)),
                        "language": t["language"],
                        "title": t["title"],
                        "codec": t["codec"],
                    }
                    for t in result["subtitle_tracks"]
                ],
            },
            "video_only": str(result["video_only"].relative_to(pd.path)),
            "demucs_done": False,
            "op_range": [0.0, 0.0],
            "ed_range": [0.0, 0.0],
            "timeline": [],
            "characters": {},
            "selected_clip_index": None,
        }
        pd.save()
        return pd

    @classmethod
    def load(cls, project_dir: Path) -> "Project":
        pd = Project(project_dir)
        pj = pd.path / "project.json"
        if not pj.exists():
            raise FileNotFoundError(f"project.json not found in {pd.path}")
        with pj.open("r", encoding="utf-8") as f:
            pd.state = json.load(f)
        return pd

    def save(self):
        with (self.path / "project.json").open("w", encoding="utf-8") as f:
            json.dump(self.state, f, indent=2, ensure_ascii=False)

    # ═══════════════════════════════════════════
    #  Internal helpers
    # ═══════════════════════════════════════════

    def _abs(self, rel: str | Path) -> Path:
        return self.path / rel

    def _audio_path(self) -> Path | None:
        ts = self.state.get("tracks", {})
        audio = ts.get("audio", [])
        idx = self.state.get("source", {}).get("audio_track_rel", 0)
        return self._abs(audio[idx]["path"]) if idx < len(audio) else None

    def _sub_path(self) -> Path | None:
        ts = self.state.get("tracks", {})
        subs = ts.get("subtitle", [])
        idx = self.state.get("source", {}).get("subtitle_track_rel", 0)
        return self._abs(subs[idx]["path"]) if idx < len(subs) else None

    def _parse_ass(self):
        if self._ass_events is not None:
            return self._ass_events
        sub = self._sub_path()
        if not sub or not sub.exists():
            self._ass_events = []
            return []
        self._ass_header = get_ass_header(sub)
        self._ass_events = parse_ass(sub)
        return self._ass_events

    def _init_timeline(self, force: bool = False):
        if self.state.get("timeline") and not force:
            return
        events = self._parse_ass()
        main = filter_dialogue(events)
        from anidub.pipeline import get_op_ed_ranges, is_in_range

        intro_s, intro_e, outro_s, outro_e = get_op_ed_ranges(events)
        self.state["op_range"] = [intro_s, intro_e]
        self.state["ed_range"] = [outro_s, outro_e]

        op_events = [e for e in events if "op" in e.get("style", "").lower()]
        ed_events = [e for e in events if "ed" in e.get("style", "").lower()]

        usable = []
        for e in main:
            if is_in_range(e["start_sec"], e["end_sec"], intro_s, intro_e):
                continue
            if is_in_range(e["start_sec"], e["end_sec"], outro_s, outro_e):
                continue
            clean = strip_override_tags(e["text"])
            if not clean:
                continue
            usable.append({"index": e["index"], "start_sec": e["start_sec"],
                           "end_sec": e["end_sec"], "text": clean})

        tl = []
        idx = 0

        for e in op_events:
            clean = strip_override_tags(e["text"])
            if not clean:
                continue
            idx += 1
            tl.append({
                "index": idx,
                "start_sec": e["start_sec"],
                "end_sec": e["end_sec"],
                "original_text": clean,
                "translated_text": None,
                "offset_ms": 0.0,
                "character": None,
                "character_mood": None,
                "ref_source": "clip",
                "ref_clip": None,
                "status": "non_dub",
                "clone_path": None,
                "clone_ms": None,
                "attempts": 0,
                "instruct_extra": None,
            })

        for i, u in enumerate(usable):
            idx += 1
            tl.append({
                "index": idx,
                "start_sec": u["start_sec"],
                "end_sec": u["end_sec"],
                "original_text": u["text"],
                "translated_text": None,
                "offset_ms": 0.0,
                "character": None,
                "character_mood": None,
                "ref_source": "clip",
                "ref_clip": None,
                "status": "pending",
                "clone_path": None,
                "clone_ms": None,
                "attempts": 0,
                "instruct_extra": None,
            })

        for e in ed_events:
            clean = strip_override_tags(e["text"])
            if not clean:
                continue
            idx += 1
            tl.append({
                "index": idx,
                "start_sec": e["start_sec"],
                "end_sec": e["end_sec"],
                "original_text": clean,
                "translated_text": None,
                "offset_ms": 0.0,
                "character": None,
                "character_mood": None,
                "ref_source": "clip",
                "ref_clip": None,
                "status": "non_dub",
                "clone_path": None,
                "clone_ms": None,
                "attempts": 0,
                "instruct_extra": None,
            })

        self.state["timeline"] = tl
        if tl:
            self.state["selected_clip_index"] = 1

    # ═══════════════════════════════════════════
    #  Setup
    # ═══════════════════════════════════════════

    def get_audio_tracks(self) -> list[dict]:
        return self.state.get("tracks", {}).get("audio", [])

    def get_subtitle_tracks(self) -> list[dict]:
        return self.state.get("tracks", {}).get("subtitle", [])

    def select_audio_track(self, index: int):
        self.state.setdefault("source", {})["audio_track_rel"] = index
        self.save()

    def select_subtitle_track(self, index: int):
        self.state.setdefault("source", {})["subtitle_track_rel"] = index
        self._ass_events = None
        self._ass_header = None
        self._init_timeline(force=True)
        self.save()

    def run_demucs(self) -> tuple[Path, Path]:
        no_vocals_cache = self.path / "no_vocals.wav"
        vocals_cache = self.path / "vocals.wav"
        if no_vocals_cache.exists() and vocals_cache.exists():
            self.state["demucs_done"] = True
            self.save()
            return no_vocals_cache, vocals_cache

        audio = self._audio_path()
        if not audio or not audio.exists():
            raise FileNotFoundError(f"Audio track not found: {audio}")

        from anidub.separator import separate_audio
        sep_dir = self.path / "_separated"
        result = separate_audio(audio, sep_dir)
        result["no_vocals"].rename(no_vocals_cache)
        result["vocals"].rename(vocals_cache)
        import shutil
        shutil.rmtree(sep_dir, ignore_errors=True)

        self.state["demucs_done"] = True
        self.save()
        return no_vocals_cache, vocals_cache

    # ═══════════════════════════════════════════
    #  Timeline
    # ═══════════════════════════════════════════

    def get_timeline_bounds(self) -> tuple[float, float]:
        events = self._parse_ass()
        if not events:
            return 0.0, 0.0
        return events[0]["start_sec"], events[-1]["end_sec"]

    def get_timeline_regions(self) -> list[TimelineRegion]:
        self._init_timeline()
        tl = self.state.get("timeline", [])
        if not tl:
            return []

        regions: list[TimelineRegion] = []
        prev_end = 0.0

        for clip in tl:
            cs = clip["start_sec"]
            ce = clip["end_sec"]
            if ce <= prev_end:
                continue
            gap = cs - prev_end
            if gap > 2.0 and prev_end > 0:
                regions.append(TimelineRegion(prev_end, cs, "gap"))
            regions.append(TimelineRegion(
                cs, ce, "clip",
                clip_index=clip["index"],
                status=ClipStatus(clip["status"]),
            ))
            prev_end = ce

        return regions

    # ═══════════════════════════════════════════
    #  Clip navigation
    # ═══════════════════════════════════════════

    def get_clip(self, index: int) -> ClipState | None:
        self._init_timeline()
        tl = self.state.get("timeline", [])
        if 1 <= index <= len(tl):
            return self._to_clip_state(tl[index - 1])
        return None

    def _to_clip_state(self, entry: dict) -> ClipState:
        return ClipState(
            index=entry["index"],
            start_sec=entry["start_sec"],
            end_sec=entry["end_sec"],
            original_text=entry["original_text"],
            translated_text=entry.get("translated_text"),
            offset_ms=entry.get("offset_ms", 0.0),
            character=entry.get("character"),
            character_mood=entry.get("character_mood"),
            ref_source=RefSource(entry.get("ref_source", "clip")),
            ref_clip=entry.get("ref_clip"),
            status=ClipStatus(entry.get("status", "pending")),
            clone_path=entry.get("clone_path"),
            clone_ms=entry.get("clone_ms"),
            attempts=entry.get("attempts", 0),
            instruct_extra=entry.get("instruct_extra"),
        )

    def get_current_clip(self) -> ClipState | None:
        sel = self.state.get("selected_clip_index")
        return self.get_clip(sel) if sel else None

    def get_next_clip(self) -> ClipState | None:
        sel = self.state.get("selected_clip_index")
        if not sel:
            return self.get_clip(1)
        return self.get_clip(sel + 1)

    def get_prev_clip(self) -> ClipState | None:
        sel = self.state.get("selected_clip_index")
        if not sel:
            return None
        return self.get_clip(sel - 1)

    def select_clip(self, index: int):
        self._init_timeline()
        tl = self.state["timeline"]
        if 1 <= index <= len(tl):
            self.state["selected_clip_index"] = index
            self.save()

    def seek_clip(self, time_sec: float) -> ClipState | None:
        self._init_timeline()
        for entry in self.state.get("timeline", []):
            if entry["start_sec"] <= time_sec < entry["end_sec"]:
                self.state["selected_clip_index"] = entry["index"]
                self.save()
                return self._to_clip_state(entry)
        return None

    def get_clip_count(self) -> int:
        self._init_timeline()
        return len(self.state.get("timeline", []))

    # ═══════════════════════════════════════════
    #  Per-clip operations
    # ═══════════════════════════════════════════

    def translate_clip(self, index: int, text_override: str | None = None) -> str:
        entry = self._get_clip_entry(index)
        if text_override:
            entry["translated_text"] = text_override
            entry["status"] = ClipStatus.TRANSLATED.value
            self.save()
            return text_override

        from anidub.translate import translate_single
        entry["status"] = ClipStatus.TRANSLATING.value
        self.save()

        try:
            result = translate_single(entry["original_text"])
            entry["translated_text"] = result
            entry["status"] = ClipStatus.TRANSLATED.value
        except Exception:
            entry["translated_text"] = entry["original_text"]
            entry["status"] = ClipStatus.REJECTED.value
        self.save()
        return entry["translated_text"]

    def clone_clip(self, index: int, *,
                   character: str | None = None,
                   mood: str = "normal",
                   instruct: str | None = None) -> dict:
        entry = self._get_clip_entry(index)
        if entry.get("status") == ClipStatus.NON_DUB.value:
            return {"error": "Cannot clone non-dub (OP/ED/gap) clips"}
        entry["status"] = ClipStatus.CLONING.value
        if character:
            entry["character"] = character
            entry["character_mood"] = mood
        self.save()

        from anidub.pipeline import clone_line
        from anidub.esperanto import build_instruct_prompt
        from anidub.extract import extract_ref_clip_from_wav

        audio = self._audio_path()
        no_vocals_cache = self.path / "no_vocals.wav"
        if not no_vocals_cache.exists():
            self.run_demucs()

        line_dir = self.path / "lines" / f"{index:03d}"
        line_dir.mkdir(parents=True, exist_ok=True)

        ref_wav = line_dir / "ref.wav"
        if entry.get("ref_source") == "character" and entry.get("ref_clip"):
            ref_wav = self._abs(entry["ref_clip"])
        else:
            extract_ref_clip_from_wav(
                audio, entry["start_sec"],
                next_line_start=self._next_line_start(index),
                out_path=ref_wav,
            )

        instruct_prompt = instruct or build_instruct_prompt(entry.get("character"))

        tts_out = line_dir / "tts.wav"
        result = clone_line(
            text=entry.get("translated_text") or entry["original_text"],
            ref_audio=ref_wav,
            target_duration=entry["end_sec"] - entry["start_sec"],
            instruct=instruct_prompt,
            instruct_extra=entry.get("instruct_extra"),
            out_path=tts_out,
            whisper_model="openai/whisper-tiny",
        )

        entry["clone_path"] = str(tts_out.relative_to(self.path))
        entry["clone_ms"] = result.get("inference_ms", 0)
        entry["status"] = ClipStatus.CLONED.value
        entry["attempts"] = entry.get("attempts", 0) + 1
        if entry.get("ref_source") == "character":
            entry["ref_clip"] = str(ref_wav.relative_to(self.path))
        self.save()
        return result

    def preview_clip(self, index: int) -> Path:
        entry = self._get_clip_entry(index)
        if not entry.get("clone_path"):
            raise RuntimeError(f"Clip {index} has not been cloned yet")

        from anidub.assembler import preview_clip as _preview_clip

        no_vocals_cache = self.path / "no_vocals.wav"
        if not no_vocals_cache.exists():
            self.run_demucs()

        tts_wav = self._abs(entry["clone_path"])
        sub_ass = self._sub_path()
        line_dir = self.path / "lines" / f"{index:03d}"
        line_dir.mkdir(parents=True, exist_ok=True)

        return _preview_clip(
            video_only=self._abs(self.state["video_only"]),
            no_vocals=no_vocals_cache,
            tts_wav=tts_wav,
            ass_path=sub_ass,
            line_index=index,
            start_sec=entry["start_sec"],
            end_sec=entry["end_sec"],
            text=entry.get("translated_text") or entry["original_text"],
            offset_ms=entry.get("offset_ms", 0.0),
            out_dir=line_dir,
        )

    def set_clip_offset(self, index: int, offset_ms: float):
        entry = self._get_clip_entry(index)
        dur = entry["end_sec"] - entry["start_sec"]
        entry["offset_ms"] = max(-dur * 1000, min(dur * 1000, offset_ms))
        self.save()

    def set_clip_character(self, index: int, character: str | None, mood: str = "normal"):
        entry = self._get_clip_entry(index)
        entry["character"] = character
        entry["character_mood"] = mood
        if character and get_character_clip(self._chars_dir, character, mood):
            entry["ref_source"] = RefSource.CHARACTER.value
            clip = get_character_clip(self._chars_dir, character, mood)
            entry["ref_clip"] = str(clip.relative_to(self.path))
        else:
            entry["ref_source"] = RefSource.CLIP.value
            entry["ref_clip"] = None
        self.save()

    def set_instruct_extra(self, index: int, instruct_extra: str | None):
        entry = self._get_clip_entry(index)
        entry["instruct_extra"] = instruct_extra
        self.save()

    def accept_clip(self, index: int):
        entry = self._get_clip_entry(index)
        if entry.get("status") == ClipStatus.NON_DUB.value:
            return
        entry["status"] = ClipStatus.ACCEPTED.value
        self._append_line_to_ass(entry)
        self.save()

    def reject_clip(self, index: int):
        entry = self._get_clip_entry(index)
        if entry.get("status") == ClipStatus.NON_DUB.value:
            return
        entry["status"] = ClipStatus.REJECTED.value
        self.save()

    def reset_clip(self, index: int):
        entry = self._get_clip_entry(index)
        entry["status"] = ClipStatus.PENDING.value
        entry["translated_text"] = None
        entry["clone_path"] = None
        entry["clone_ms"] = None
        entry["offset_ms"] = 0.0
        entry["ref_source"] = RefSource.CLIP.value
        entry["ref_clip"] = None
        self.save()

    # ═══════════════════════════════════════════
    #  Bulk operations
    # ═══════════════════════════════════════════

    def translate_range(self, start: int, end: int | None = None):
        end = end or self.get_clip_count()
        for i in range(start, end + 1):
            clip = self.get_clip(i)
            if clip and clip.status in (ClipStatus.PENDING, ClipStatus.REJECTED):
                self.translate_clip(i)

    def clone_range(self, start: int, end: int | None = None, character: str | None = None):
        end = end or self.get_clip_count()
        for i in range(start, end + 1):
            clip = self.get_clip(i)
            if not clip or clip.status not in (ClipStatus.TRANSLATED, ClipStatus.CLONED, ClipStatus.REJECTED):
                continue
            if clip.status == ClipStatus.REJECTED and not clip.translated_text:
                continue
            if character or clip.character:
                self.clone_clip(i, character=character or clip.character,
                                mood=clip.character_mood or "normal")
            else:
                self.clone_clip(i)

    def accept_all(self):
        for i in range(1, self.get_clip_count() + 1):
            clip = self.get_clip(i)
            if clip and clip.status == ClipStatus.CLONED:
                self.accept_clip(i)

    def get_stats(self) -> dict:
        self._init_timeline()
        tl = self.state.get("timeline", [])
        counts = {s.value: 0 for s in ClipStatus}
        for t in tl:
            st = t.get("status", "pending")
            counts[st] = counts.get(st, 0) + 1
        return {"total": len(tl), **counts}

    # ═══════════════════════════════════════════
    #  Character bank
    # ═══════════════════════════════════════════

    def save_character_clip(self, name: str, audio: Path, mood: str = "normal") -> Path:
        p = save_character_clip(self._chars_dir, name, audio, mood)
        chars = self.state.get("characters", {})
        chars.setdefault(name, {})[mood] = str(p.relative_to(self.path))
        self.state["characters"] = chars
        self.save()
        return p

    def delete_character_clip(self, name: str, mood: str):
        delete_character_clip(self._chars_dir, name, mood)
        chars = self.state.get("characters", {})
        if name in chars:
            chars[name].pop(mood, None)
            if not chars[name]:
                del chars[name]
        self.state["characters"] = chars
        self.save()

    def list_characters(self) -> list[str]:
        return list_characters(self._chars_dir)

    def list_character_moods(self, name: str) -> list[str]:
        return list_character_moods(self._chars_dir, name)

    def get_character_clip(self, name: str, mood: str) -> Path | None:
        return get_character_clip(self._chars_dir, name, mood)

    def get_all_character_clips(self) -> dict[str, dict[str, Path]]:
        return get_all_character_clips(self._chars_dir)

    # ═══════════════════════════════════════════
    #  Final
    # ═══════════════════════════════════════════

    def export_ass(self) -> Path:
        sub = self._sub_path()
        self._parse_ass()
        if not sub:
            raise RuntimeError("No subtitle track selected")
        eo_ass = self.path / "lines" / "_eo.ass"
        header = get_ass_header(sub)
        with eo_ass.open("w", encoding="utf-8") as f:
            f.write(header + "\n")
            for entry in self.state.get("timeline", []):
                text = entry.get("translated_text") or entry["original_text"]
                ts = (
                    f"{entry['start_sec']:.2f},{entry['end_sec']:.2f}"
                    .replace(".", ":").replace(":", ".", 1).replace(",", ":", 1)
                )
                f.write(f"Dialogue: 0,{ts},main,,0000,0000,0000,,{text}\n")
        return eo_ass

    def assemble_full(self) -> Path:
        from anidub.assembler import assemble_full
        self._parse_ass()
        no_vocals_cache = self.path / "no_vocals.wav"
        if not no_vocals_cache.exists():
            self.run_demucs()
        audio = self._audio_path()
        return assemble_full(
            mkv_path=Path(self.state["source"]["mkv_path"]),
            ass_events=self._ass_events,
            batch_out_dir=self.path,
            full_no_vocals=no_vocals_cache,
            full_original_audio=audio,
            voiced_results=self._collect_voiced_results(),
            ass_path=self._sub_path(),
            errors=self._collect_errors(),
        )

    def needs_processing(self, index: int) -> bool:
        entry = self._get_clip_entry(index)
        status = entry.get("status", "pending")
        if status in ("accepted", "non_dub", "skipped"):
            return False
        has_translation = bool(entry.get("translated_text"))
        has_clone = bool(entry.get("clone_path"))
        if has_translation and has_clone:
            return False
        if status in ("rejected",):
            return False
        if status in ("translating", "cloning"):
            return False
        return True

    def process_clip(self, index: int, *,
                     character: str | None = None,
                     mood: str = "normal") -> dict:
        entry = self._get_clip_entry(index)
        result: dict = {"status": entry["status"]}

        if not entry.get("translated_text"):
            from anidub.translate import translate_single
            entry["status"] = ClipStatus.TRANSLATING.value
            self.save()
            try:
                entry["translated_text"] = translate_single(entry["original_text"])
                entry["status"] = ClipStatus.TRANSLATED.value
            except Exception:
                entry["translated_text"] = entry["original_text"]
                entry["status"] = ClipStatus.REJECTED.value
            result["translated_text"] = entry["translated_text"]
            result["status"] = entry["status"]
            self.save()

        if entry.get("status") == ClipStatus.NON_DUB.value:
            return result

        if not entry.get("clone_path") and entry.get("status") != ClipStatus.REJECTED.value:
            self.clone_clip(index, character=character, mood=mood)

        if entry.get("clone_path"):
            preview = self.preview_clip(index)
            result["preview_url"] = f"/preview/{index:03d}.mp4"

        result["status"] = entry["status"]
        return result
    # ═══════════════════════════════════════════

    def _get_clip_entry(self, index: int) -> dict:
        self._init_timeline()
        tl = self.state["timeline"]
        if index < 1 or index > len(tl):
            raise IndexError(f"Clip index {index} out of range 1..{len(tl)}")
        return tl[index - 1]

    def _next_line_start(self, index: int) -> float | None:
        tl = self.state.get("timeline", [])
        if index < len(tl):
            return tl[index]["start_sec"]
        return None

    def _append_line_to_ass(self, entry: dict):
        lines_dir = self.path / "lines"
        lines_dir.mkdir(parents=True, exist_ok=True)
        eo_ass = lines_dir / "_eo.ass"
        text = entry.get("translated_text") or entry["original_text"]
        line = (
            f"Dialogue: 0,"
            f"{self._fmt_ass_ts(entry['start_sec'])}," 
            f"{self._fmt_ass_ts(entry['end_sec'])},"
            f"main,,0000,0000,0000,,{text}\n"
        )
        existing = []
        if eo_ass.exists():
            with eo_ass.open("r", encoding="utf-8") as f:
                existing = f.readlines()
        hdr = self._ass_header or get_ass_header(self._sub_path())
        existing = [ln for ln in existing if not ln.startswith(
            f"Dialogue: 0,{self._fmt_ass_ts(entry['start_sec'])}"
        )]
        existing.append(line)
        existing.sort(key=self._sort_key)
        with eo_ass.open("w", encoding="utf-8") as f:
            if hdr not in "".join(existing[:1]):
                f.write(hdr + "\n")
                f.write("\n")
                f.write("[Events]\n")
                f.write("Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n")
            f.writelines(existing)

    @staticmethod
    def _fmt_ass_ts(sec: float) -> str:
        h = int(sec // 3600)
        m = int((sec % 3600) // 60)
        s = sec % 60
        return f"{h}:{m:02d}:{s:05.2f}"

    @staticmethod
    def _sort_key(line: str) -> float:
        if not line.startswith("Dialogue:"):
            return 0.0
        parts = line.split(",", 2)
        if len(parts) < 2:
            return 0.0
        ts = parts[1]
        try:
            h, m, s = ts.split(":")
            return float(h) * 3600 + float(m) * 60 + float(s)
        except Exception:
            return 0.0

    def _collect_voiced_results(self) -> list[dict]:
        results = []
        for entry in self.state.get("timeline", []):
            if entry.get("status") not in (ClipStatus.ACCEPTED.value, ClipStatus.NON_DUB.value):
                continue
            if not entry.get("clone_path") and entry.get("status") != ClipStatus.NON_DUB.value:
                continue
            results.append({
                "line_index": entry["index"],
                "start_sec": entry["start_sec"],
                "end_sec": entry["end_sec"],
                "text": entry.get("translated_text") or entry["original_text"],
                "tts_wav": str(self._abs(entry["clone_path"])) if entry.get("clone_path") else "",
                "raw_dur": entry["end_sec"] - entry["start_sec"],
                "effective_dur": entry["end_sec"] - entry["start_sec"],
                "slack_ms": 0,
                "atempo": "none",
                "inference_ms": entry.get("clone_ms", 0),
                "non_dub": entry.get("status") == ClipStatus.NON_DUB.value,
            })
        return results

    def _collect_errors(self) -> list[dict]:
        errors = []
        for entry in self.state.get("timeline", []):
            st = entry.get("status")
            if st in (ClipStatus.REJECTED.value, ClipStatus.SKIPPED.value):
                errors.append({
                    "line_index": entry["index"],
                    "text": entry.get("translated_text") or entry["original_text"],
                    "start_sec": entry["start_sec"],
                    "end_sec": entry["end_sec"],
                    "error": f"Status: {st}",
                })
        return errors


ANIME_CONFIG = "anime.json"


class AnimeProject:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.state: dict = {}
        self._active: Project | None = None
        self._active_stem: str | None = None

    @classmethod
    def create(cls, anime_name: str, anime_dir: Path) -> "AnimeProject":
        anime_dir = Path(anime_dir)
        if not anime_dir.is_dir():
            raise FileNotFoundError(f"Anime directory not found: {anime_dir}")

        mkvs = sorted(anime_dir.glob("*.mkv"))
        if not mkvs:
            raise FileNotFoundError(f"No .mkv files found in {anime_dir}")

        path = PROJECTS_ROOT / anime_name
        path.mkdir(parents=True, exist_ok=True)

        ap = AnimeProject(path)
        ap.state = {
            "version": 1,
            "anime_name": anime_name,
            "anime_dir": str(anime_dir.resolve()),
            "episodes": [],
        }

        for mkv in mkvs:
            episode_dir = path / mkv.stem
            ap.state["episodes"].append({
                "stem": mkv.stem,
                "mkv_path": str(mkv.resolve()),
                "decomposed": episode_dir.is_dir() and (episode_dir / "project.json").exists(),
            })

        ap.save()
        return ap

    @classmethod
    def load(cls, path: Path) -> "AnimeProject":
        ap = AnimeProject(path)
        cfg = ap.path / ANIME_CONFIG
        if not cfg.exists():
            raise FileNotFoundError(f"{ANIME_CONFIG} not found in {ap.path}")
        with cfg.open("r", encoding="utf-8") as f:
            ap.state = json.load(f)
        return ap

    def save(self):
        self.path.mkdir(parents=True, exist_ok=True)
        with (self.path / ANIME_CONFIG).open("w", encoding="utf-8") as f:
            json.dump(self.state, f, indent=2, ensure_ascii=False)

    @staticmethod
    def discover() -> list[dict]:
        if not PROJECTS_ROOT.is_dir():
            return []
        results = []
        for d in sorted(PROJECTS_ROOT.iterdir()):
            if not d.is_dir():
                continue
            cfg = d / ANIME_CONFIG
            if cfg.exists():
                with cfg.open("r", encoding="utf-8") as f:
                    data = json.load(f)
                ep_count = len(data.get("episodes", []))
                results.append({
                    "name": d.name,
                    "anime_name": data.get("anime_name", d.name),
                    "episode_count": ep_count,
                    "path": str(d),
                })
        return results

    def get_episodes(self) -> list[dict]:
        episodes = self.state.get("episodes", [])
        result = []
        for ep in episodes:
            stem = ep["stem"]
            ep_dir = self.path / stem
            pj = ep_dir / "project.json"
            status = "idle"
            clip_count = 0
            stats = {}
            if pj.exists():
                try:
                    proj = Project(ep_dir)
                    proj.state = json.loads(pj.read_text(encoding="utf-8"))
                    proj._init_timeline()
                    stats = proj.get_stats()
                except Exception:
                    stats = {}
                accepted = stats.get("accepted", 0)
                total = stats.get("total", 0)
                if accepted >= total and total > 0:
                    status = "done"
                elif stats.get("cloned", 0) > 0 or stats.get("translated", 0) > 0:
                    status = "in_progress"
                else:
                    status = "loaded"
                clip_count = total
            result.append({
                "stem": stem,
                "mkv_path": ep.get("mkv_path", ""),
                "decomposed": ep.get("decomposed", False),
                "status": status,
                "clip_count": clip_count,
                "stats": stats,
            })
        return result

    def select_episode(self, stem: str) -> Project:
        if self._active_stem == stem and self._active is not None:
            return self._active

        self._active_stem = stem
        ep_dir = self.path / stem
        pj = ep_dir / "project.json"

        if pj.exists():
            self._active = Project.load(ep_dir)
            self._active._chars_dir = self._chars_dir()
        else:
            episodes = self.state.get("episodes", [])
            mkv = None
            for ep in episodes:
                if ep["stem"] == stem:
                    mkv = Path(ep["mkv_path"])
                    break
            if not mkv:
                raise FileNotFoundError(f"No MKV found for episode {stem}")
            if not mkv.exists():
                raise FileNotFoundError(f"MKV not found: {mkv}")
            self._active = Project.create(mkv, name=f"{self.state['anime_name']}/{stem}")
            self._active._chars_dir = self._chars_dir()

            for ep in self.state.get("episodes", []):
                if ep["stem"] == stem:
                    ep["decomposed"] = True
            self.save()

        return self._active

    def get_active_project(self) -> Project | None:
        return self._active

    # Character bank (shared across episodes)

    def _chars_dir(self) -> Path:
        d = self.path / "characters"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def save_character_clip(self, name: str, audio: Path, mood: str = "normal") -> Path:
        char_dir = self._chars_dir() / name
        char_dir.mkdir(parents=True, exist_ok=True)
        dst = char_dir / f"{mood}.wav"
        import shutil
        shutil.copy2(str(audio), str(dst))
        return dst

    def delete_character_clip(self, name: str, mood: str):
        clip = self._chars_dir() / name / f"{mood}.wav"
        if clip.exists():
            clip.unlink()
        char_dir = clip.parent
        if char_dir.is_dir() and not list(char_dir.iterdir()):
            char_dir.rmdir()

    def list_characters(self) -> list[str]:
        d = self._chars_dir()
        return sorted(p.name for p in d.iterdir() if p.is_dir())

    def list_character_moods(self, name: str) -> list[str]:
        char_dir = self._chars_dir() / name
        if not char_dir.is_dir():
            return []
        return sorted(w.stem for w in char_dir.glob("*.wav"))

    def get_character_clip(self, name: str, mood: str) -> Path | None:
        clip = self._chars_dir() / name / f"{mood}.wav"
        return clip if clip.exists() else None

    def get_all_character_clips(self) -> dict[str, dict[str, Path]]:
        result = {}
        d = self._chars_dir()
        for char_dir in sorted(d.iterdir()):
            if not char_dir.is_dir():
                continue
            moods = {}
            for wav in sorted(char_dir.glob("*.wav")):
                moods[wav.stem] = wav
            if moods:
                result[char_dir.name] = moods
        return result
