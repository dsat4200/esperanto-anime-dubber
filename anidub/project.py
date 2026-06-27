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
    SIGN = "sign"


class RefSource(str, Enum):
    CLIP = "clip"
    CHARACTER = "character"


@dataclass
class TimelineRegion:
    start_sec: float
    end_sec: float
    kind: str
    clip_id: str | None = None
    status: ClipStatus | None = None


@dataclass
class ClipState:
    clip_id: str
    start_sec: float
    end_sec: float
    original_text: str
    translated_text: str | None = None
    audio_offset_ms: float = 0.0
    character: str | None = None
    character_mood: str | None = None
    ref_source: RefSource = RefSource.CLIP
    ref_clip: str | None = None
    status: ClipStatus = ClipStatus.PENDING
    clone_path: str | None = None
    clone_ms: float | None = None
    attempts: int = 0
    instruct_extra: str | None = None
    speed_factor: float = 1.0
    pronunciation_override: str | None = None

    @property
    def display_index(self) -> int:
        return int(self.clip_id[1:])


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
            "version": 2,
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
            "clips": {},
            "order": [],
            "next_clip_id": 0,
            "characters": {},
            "selected_clip_id": None,
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
        pd._migrate_if_needed()
        return pd

    def save(self):
        with (self.path / "project.json").open("w", encoding="utf-8") as f:
            json.dump(self.state, f, indent=2, ensure_ascii=False)

    def _migrate_if_needed(self):
        if self.state.get("clips") is not None:
            return
        old_tl = self.state.get("timeline", [])
        if not old_tl:
            self.state["clips"] = {}
            self.state["order"] = []
            self.state["next_clip_id"] = 0
            self.state.pop("selected_clip_index", None)
            self.state["selected_clip_id"] = None
            self.state.pop("timeline", None)
            self.save()
            return

        clips = {}
        order = []
        max_id = 0
        for entry in old_tl:
            idx = entry.get("index", len(order) + 1)
            clip_id = f"c{idx:04d}"
            max_id = max(max_id, idx)
            entry.pop("index", None)
            entry["clip_id"] = clip_id
            entry.setdefault("audio_offset_ms", entry.pop("offset_ms", 0.0))
            clips[clip_id] = entry
            order.append(clip_id)

        old_sel = self.state.pop("selected_clip_index", None)
        if old_sel and 1 <= old_sel <= len(order):
            self.state["selected_clip_id"] = order[old_sel - 1]
        else:
            self.state["selected_clip_id"] = order[0] if order else None

        self.state["clips"] = clips
        self.state["order"] = order
        self.state["next_clip_id"] = max_id + 1
        self.state.pop("timeline", None)
        self.save()

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

    def _next_clip_id(self) -> str:
        nid = self.state.get("next_clip_id", 0)
        if nid == 0:
            existing = self.state.get("clips", {})
            nid = max((int(cid[1:]) for cid in existing), default=0) + 1
        cid = f"c{nid:04d}"
        self.state["next_clip_id"] = nid + 1
        return cid

    def _new_clip_entry(self, start_sec: float, end_sec: float, text: str,
                        status: str = "pending") -> dict:
        return {
            "clip_id": self._next_clip_id(),
            "start_sec": start_sec,
            "end_sec": end_sec,
            "original_text": text,
            "translated_text": None,
            "audio_offset_ms": 0.0,
            "character": None,
            "character_mood": None,
            "ref_source": "clip",
            "ref_clip": None,
            "status": status,
            "clone_path": None,
            "clone_ms": None,
            "attempts": 0,
            "instruct_extra": None,
            "speed_factor": 1.0,
            "pronunciation_override": None,
        }

    def _ensure_clips_dict(self):
        if "clips" not in self.state:
            self._migrate_if_needed()

    def _init_timeline(self, force: bool = False):
        self._ensure_clips_dict()
        if self.state.get("order") and not force:
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
            usable.append({"start_sec": e["start_sec"], "end_sec": e["end_sec"], "text": clean})

        clips = {}
        order = []
        pending_statuses = {"non_dub", "sign"}

        for e in op_events:
            clean = strip_override_tags(e["text"])
            if not clean:
                continue
            entry = self._new_clip_entry(e["start_sec"], e["end_sec"], clean, status="non_dub")
            clips[entry["clip_id"]] = entry
            order.append(entry["clip_id"])

        for u in usable:
            entry = self._new_clip_entry(u["start_sec"], u["end_sec"], u["text"], status="pending")
            clips[entry["clip_id"]] = entry
            order.append(entry["clip_id"])

        for e in ed_events:
            clean = strip_override_tags(e["text"])
            if not clean:
                continue
            entry = self._new_clip_entry(e["start_sec"], e["end_sec"], clean, status="non_dub")
            clips[entry["clip_id"]] = entry
            order.append(entry["clip_id"])

        self.state["clips"] = clips
        self.state["order"] = order
        if order:
            self.state["selected_clip_id"] = order[0]
        self.save()

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

    def get_timeline_clips(self) -> list[dict]:
        self._init_timeline()
        clips = self.state.get("clips", {})
        return [clips[cid] for cid in self.state.get("order", []) if cid in clips]

    # ═══════════════════════════════════════════
    #  Clip navigation
    # ═══════════════════════════════════════════

    def get_clip(self, clip_id: str) -> ClipState | None:
        self._init_timeline()
        entry = self.state.get("clips", {}).get(clip_id)
        if entry:
            return self._to_clip_state(entry)
        return None

    def _to_clip_state(self, entry: dict) -> ClipState:
        return ClipState(
            clip_id=entry["clip_id"],
            start_sec=entry["start_sec"],
            end_sec=entry["end_sec"],
            original_text=entry["original_text"],
            translated_text=entry.get("translated_text"),
            audio_offset_ms=entry.get("audio_offset_ms", 0.0),
            character=entry.get("character"),
            character_mood=entry.get("character_mood"),
            ref_source=RefSource(entry.get("ref_source", "clip")),
            ref_clip=entry.get("ref_clip"),
            status=ClipStatus(entry.get("status", "pending")),
            clone_path=entry.get("clone_path"),
            clone_ms=entry.get("clone_ms"),
            attempts=entry.get("attempts", 0),
            instruct_extra=entry.get("instruct_extra"),
            speed_factor=entry.get("speed_factor", 1.0),
            pronunciation_override=entry.get("pronunciation_override"),
        )

    def get_current_clip(self) -> ClipState | None:
        sel = self.state.get("selected_clip_id")
        return self.get_clip(sel) if sel else None

    def get_next_clip(self, clip_id: str | None = None) -> ClipState | None:
        if clip_id is None:
            clip_id = self.state.get("selected_clip_id")
        if not clip_id:
            return None
        order = self.state.get("order", [])
        try:
            idx = order.index(clip_id)
        except ValueError:
            return None
        for nxt in order[idx + 1:]:
            if nxt in self.state.get("clips", {}):
                return self._to_clip_state(self.state["clips"][nxt])
        return None

    def get_prev_clip(self, clip_id: str | None = None) -> ClipState | None:
        if clip_id is None:
            clip_id = self.state.get("selected_clip_id")
        if not clip_id:
            return None
        order = self.state.get("order", [])
        try:
            idx = order.index(clip_id)
        except ValueError:
            return None
        for prv in reversed(order[:idx]):
            if prv in self.state.get("clips", {}):
                return self._to_clip_state(self.state["clips"][prv])
        return None

    def select_clip(self, clip_id: str):
        self._init_timeline()
        if clip_id in self.state.get("clips", {}):
            self.state["selected_clip_id"] = clip_id
            self.save()

    def seek_clip(self, time_sec: float) -> ClipState | None:
        self._init_timeline()
        for cid in self.state.get("order", []):
            entry = self.state.get("clips", {}).get(cid)
            if entry and entry["start_sec"] <= time_sec < entry["end_sec"]:
                self.state["selected_clip_id"] = cid
                self.save()
                return self._to_clip_state(entry)
        return None

    def get_clip_count(self) -> int:
        self._init_timeline()
        return len(self.state.get("order", []))

    # ═══════════════════════════════════════════
    #  Clip editing (new timeline features)
    # ═══════════════════════════════════════════

    def resize_clip(self, clip_id: str, start_sec: float, end_sec: float):
        entry = self._get_clip_entry(clip_id)
        entry["start_sec"] = max(0.0, start_sec)
        entry["end_sec"] = max(entry["start_sec"] + 0.1, end_sec)
        self._resort_order()
        self.save()

    def delete_clip(self, clip_id: str):
        clips = self.state.get("clips", {})
        order = self.state.get("order", [])
        if clip_id not in clips:
            return
        del clips[clip_id]
        if clip_id in order:
            order.remove(clip_id)
        if self.state.get("selected_clip_id") == clip_id:
            self.state["selected_clip_id"] = order[0] if order else None
        self.save()

    def set_audio_offset(self, clip_id: str, offset_ms: float):
        entry = self._get_clip_entry(clip_id)
        entry["audio_offset_ms"] = offset_ms
        self.save()

    def set_clip_status(self, clip_id: str, status: str):
        entry = self._get_clip_entry(clip_id)
        entry["status"] = ClipStatus(status).value
        self.save()

    def _resort_order(self):
        clips = self.state.get("clips", {})
        self.state["order"] = sorted(
            self.state.get("order", []),
            key=lambda cid: clips.get(cid, {}).get("start_sec", 0.0),
        )

    # ═══════════════════════════════════════════
    #  Per-clip operations
    # ═══════════════════════════════════════════

    def translate_clip(self, clip_id: str, text_override: str | None = None) -> str:
        entry = self._get_clip_entry(clip_id)
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

    def clone_clip(self, clip_id: str, *,
                   character: str | None = None,
                   mood: str = "normal",
                   instruct: str | None = None,
                   backend=None) -> dict:
        entry = self._get_clip_entry(clip_id)
        if entry.get("status") in (ClipStatus.NON_DUB.value, ClipStatus.SIGN.value):
            return {"error": "Cannot clone non-dub or sign clips"}
        entry["status"] = ClipStatus.CLONING.value
        if character:
            self.set_clip_character(clip_id, character, mood)

        from anidub.pipeline import clone_line
        from anidub.esperanto import build_instruct_prompt
        from anidub.extract import extract_ref_clip_from_wav

        audio = self._audio_path()
        no_vocals_cache = self.path / "no_vocals.wav"
        if not no_vocals_cache.exists():
            self.run_demucs()

        line_dir = self.path / "lines" / clip_id
        line_dir.mkdir(parents=True, exist_ok=True)

        ref_wav = line_dir / "ref.wav"
        if entry.get("ref_source") == "character" and entry.get("ref_clip"):
            ref_wav = self._abs(entry["ref_clip"])
        else:
            extract_ref_clip_from_wav(
                audio, entry["start_sec"],
                next_line_start=self._next_line_start(clip_id),
                out_path=ref_wav,
            )

        instruct_prompt = instruct or build_instruct_prompt(entry.get("character"))

        tts_out = line_dir / "tts.wav"
        spoken_text = entry.get("pronunciation_override") or entry.get("translated_text") or entry["original_text"]
        result = clone_line(
            text=spoken_text,
            ref_audio=ref_wav,
            target_duration=entry["end_sec"] - entry["start_sec"],
            instruct=instruct_prompt,
            instruct_extra=entry.get("instruct_extra"),
            speed_factor=entry.get("speed_factor", 1.0),
            out_path=tts_out,
            whisper_model="openai/whisper-tiny",
            backend=backend,
        )

        entry["clone_path"] = str(tts_out.relative_to(self.path))
        entry["clone_ms"] = result.get("inference_ms", 0)
        entry["status"] = ClipStatus.CLONED.value
        entry["attempts"] = entry.get("attempts", 0) + 1
        if entry.get("ref_source") == "character":
            entry["ref_clip"] = str(ref_wav.relative_to(self.path))
        self.save()
        return result

    def preview_clip(self, clip_id: str) -> Path:
        entry = self._get_clip_entry(clip_id)
        if not entry.get("clone_path"):
            raise RuntimeError(f"Clip {clip_id} has not been cloned yet")

        from anidub.assembler import preview_clip as _preview_clip

        no_vocals_cache = self.path / "no_vocals.wav"
        if not no_vocals_cache.exists():
            self.run_demucs()

        tts_wav = self._abs(entry["clone_path"])
        sub_ass = self._sub_path()
        line_dir = self.path / "lines" / clip_id
        line_dir.mkdir(parents=True, exist_ok=True)

        offset_ms = entry.get("audio_offset_ms", 0.0)
        return _preview_clip(
            video_only=self._abs(self.state["video_only"]),
            no_vocals=no_vocals_cache,
            tts_wav=tts_wav,
            ass_path=sub_ass,
            line_index=entry["clip_id"],
            start_sec=entry["start_sec"],
            end_sec=entry["end_sec"],
            text=entry.get("translated_text") or entry["original_text"],
            offset_ms=offset_ms,
            out_dir=line_dir,
        )

    def set_clip_offset(self, clip_id: str, offset_ms: float):
        self.set_audio_offset(clip_id, offset_ms)

    def set_clip_character(self, clip_id: str, character: str | None, mood: str = "normal"):
        entry = self._get_clip_entry(clip_id)
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

    def set_clip_speed(self, clip_id: str, speed_factor: float):
        entry = self._get_clip_entry(clip_id)
        entry["speed_factor"] = max(0.5, min(2.0, speed_factor))
        self.save()

    def set_clip_pronunciation(self, clip_id: str, pronunciation_override: str | None):
        entry = self._get_clip_entry(clip_id)
        entry["pronunciation_override"] = pronunciation_override if pronunciation_override and pronunciation_override.strip() else None
        self.save()

    def set_instruct_extra(self, clip_id: str, instruct_extra: str | None):
        entry = self._get_clip_entry(clip_id)
        entry["instruct_extra"] = instruct_extra
        self.save()

    def accept_clip(self, clip_id: str):
        entry = self._get_clip_entry(clip_id)
        if entry.get("status") in (ClipStatus.NON_DUB.value, ClipStatus.SIGN.value):
            return
        entry["status"] = ClipStatus.ACCEPTED.value
        self._append_line_to_ass(entry)
        self.save()

    def reject_clip(self, clip_id: str):
        entry = self._get_clip_entry(clip_id)
        if entry.get("status") in (ClipStatus.NON_DUB.value, ClipStatus.SIGN.value):
            return
        entry["status"] = ClipStatus.REJECTED.value
        self.save()

    def reset_clip(self, clip_id: str):
        entry = self._get_clip_entry(clip_id)
        entry["status"] = ClipStatus.PENDING.value
        entry["translated_text"] = None
        entry["clone_path"] = None
        entry["clone_ms"] = None
        entry["audio_offset_ms"] = 0.0
        entry["ref_source"] = RefSource.CLIP.value
        entry["ref_clip"] = None
        entry["speed_factor"] = 1.0
        entry["pronunciation_override"] = None
        self.save()

    # ═══════════════════════════════════════════
    #  Bulk operations
    # ═══════════════════════════════════════════

    def translate_all(self):
        for cid in self.state.get("order", []):
            clip = self.get_clip(cid)
            if clip and clip.status in (ClipStatus.PENDING, ClipStatus.REJECTED):
                self.translate_clip(cid)

    def clone_range(self, start: int | None = None, end: int | None = None,
                    character: str | None = None, backend=None):
        order = self.state.get("order", [])
        if start is not None and end is not None:
            ids = order[start - 1:end]
        else:
            ids = order
        for cid in ids:
            clip = self.get_clip(cid)
            if not clip or clip.status not in (ClipStatus.TRANSLATED, ClipStatus.CLONED, ClipStatus.REJECTED):
                continue
            if clip.status == ClipStatus.REJECTED and not clip.translated_text:
                continue
            if character or clip.character:
                self.clone_clip(cid, character=character or clip.character,
                                mood=clip.character_mood or "normal",
                                backend=backend)
            else:
                self.clone_clip(cid, backend=backend)

    def accept_all(self):
        for cid in self.state.get("order", []):
            clip = self.get_clip(cid)
            if clip and clip.status == ClipStatus.CLONED:
                self.accept_clip(cid)

    def get_stats(self) -> dict:
        self._init_timeline()
        clips = self.state.get("clips", {})
        counts = {s.value: 0 for s in ClipStatus}
        for entry in clips.values():
            st = entry.get("status", "pending")
            counts[st] = counts.get(st, 0) + 1
        return {"total": len(clips), **counts}

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
            for entry in self._sorted_clips():
                text = entry.get("translated_text") or entry["original_text"]
                style = "sign" if entry.get("status") == ClipStatus.SIGN.value else "main"
                ts = self._sec_to_ass_ts(entry["start_sec"], entry["end_sec"])
                f.write(f"Dialogue: 0,{ts},{style},,0000,0000,0000,,{text}\n")
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

    def needs_processing(self, clip_id: str) -> bool:
        entry = self._get_clip_entry(clip_id)
        status = entry.get("status", "pending")
        if status in ("accepted", "non_dub", "skipped", "sign"):
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

    def process_clip(self, clip_id: str, *,
                     character: str | None = None,
                     mood: str = "normal") -> dict:
        entry = self._get_clip_entry(clip_id)
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

        if entry.get("status") in (ClipStatus.NON_DUB.value, ClipStatus.SIGN.value):
            return result

        if not entry.get("clone_path") and entry.get("status") != ClipStatus.REJECTED.value:
            self.clone_clip(clip_id, character=character, mood=mood)

        if entry.get("clone_path"):
            preview = self.preview_clip(clip_id)
            result["preview_url"] = f"/preview/{clip_id}.mp4"

        result["status"] = entry["status"]
        return result

    # ═══════════════════════════════════════════

    def _get_clip_entry(self, clip_id: str) -> dict:
        self._init_timeline()
        entry = self.state.get("clips", {}).get(clip_id)
        if entry is None:
            raise IndexError(f"Clip {clip_id} not found")
        return entry

    def _next_line_start(self, clip_id: str) -> float | None:
        order = self.state.get("order", [])
        clips = self.state.get("clips", {})
        try:
            idx = order.index(clip_id)
        except ValueError:
            return None
        for nid in order[idx + 1:]:
            if nid in clips:
                return clips[nid]["start_sec"]
        return None

    def _sorted_clips(self):
        return sorted(
            self.state.get("clips", {}).values(),
            key=lambda e: e["start_sec"],
        )

    def _append_line_to_ass(self, entry: dict):
        lines_dir = self.path / "lines"
        lines_dir.mkdir(parents=True, exist_ok=True)
        eo_ass = lines_dir / "_eo.ass"
        text = entry.get("translated_text") or entry["original_text"]
        style = "sign" if entry.get("status") == ClipStatus.SIGN.value else "main"
        line = (
            f"Dialogue: 0,"
            f"{self._fmt_ass_ts(entry['start_sec'])},"
            f"{self._fmt_ass_ts(entry['end_sec'])},"
            f"{style},,0000,0000,0000,,{text}\n"
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
    def _sec_to_ass_ts(start_sec: float, end_sec: float) -> str:
        def _fmt(s):
            h = int(s // 3600)
            m = int((s % 3600) // 60)
            sec = s % 60
            return f"{h}:{m:02d}:{sec:05.2f}"
        return f"{_fmt(start_sec)},{_fmt(end_sec)}"

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
        for entry in self._sorted_clips():
            st = entry.get("status")
            if st not in (ClipStatus.ACCEPTED.value, ClipStatus.NON_DUB.value) and st != ClipStatus.SIGN.value:
                continue
            if st == ClipStatus.SIGN.value:
                continue
            if not entry.get("clone_path") and st != ClipStatus.NON_DUB.value:
                continue
            audio_start = entry["start_sec"] + entry.get("audio_offset_ms", 0.0) / 1000.0
            results.append({
                "line_index": entry.get("display_index", 0),
                "clip_id": entry["clip_id"],
                "start_sec": entry["start_sec"],
                "end_sec": entry["end_sec"],
                "audio_start_sec": audio_start,
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
        for entry in self.state.get("clips", {}).values():
            st = entry.get("status")
            if st in (ClipStatus.REJECTED.value, ClipStatus.SKIPPED.value):
                errors.append({
                    "line_index": entry.get("display_index", 0),
                    "clip_id": entry["clip_id"],
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
                    proj._migrate_if_needed()
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
