import shutil
from pathlib import Path


def _chars_dir(project_dir: Path) -> Path:
    d = project_dir / "characters"
    d.mkdir(parents=True, exist_ok=True)
    return d


def save_character_clip(
    project_dir: Path, name: str, audio: Path, mood: str = "normal",
) -> Path:
    char_dir = _chars_dir(project_dir) / name
    char_dir.mkdir(parents=True, exist_ok=True)
    dst = char_dir / f"{mood}.wav"
    shutil.copy2(str(audio), str(dst))
    return dst


def delete_character_clip(project_dir: Path, name: str, mood: str):
    clip = _chars_dir(project_dir) / name / f"{mood}.wav"
    if clip.exists():
        clip.unlink()
    char_dir = clip.parent
    if char_dir.is_dir() and not list(char_dir.iterdir()):
        char_dir.rmdir()


def list_characters(project_dir: Path) -> list[str]:
    d = _chars_dir(project_dir)
    return sorted(p.name for p in d.iterdir() if p.is_dir())


def list_character_moods(project_dir: Path, name: str) -> list[str]:
    char_dir = _chars_dir(project_dir) / name
    if not char_dir.is_dir():
        return []
    return sorted(w.stem for w in char_dir.glob("*.wav"))


def get_character_clip(project_dir: Path, name: str, mood: str) -> Path | None:
    clip = _chars_dir(project_dir) / name / f"{mood}.wav"
    return clip if clip.exists() else None


def get_all_character_clips(project_dir: Path) -> dict[str, dict[str, Path]]:
    result = {}
    d = _chars_dir(project_dir)
    for char_dir in sorted(d.iterdir()):
        if not char_dir.is_dir():
            continue
        moods = {}
        for wav in sorted(char_dir.glob("*.wav")):
            moods[wav.stem] = wav
        if moods:
            result[char_dir.name] = moods
    return result
