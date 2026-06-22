from datetime import date
from pathlib import Path
import shutil

MODEL_NAME = "htdemucs"
OUTPUT_ROOT = Path("downloads")
DEFAULT_MKV = Path("anime/oreimo/Oreimo - 01.mkv")
DEFAULT_ASS = Path("anime/oreimo/Oreimo - 01.ass")
ANIME_ROOT = Path("anime")
TEST_OUTPUT = Path("test_output")
BATCH_OUTPUT = Path("batch_output")


def today_output_dir():
    return TEST_OUTPUT / date.today().isoformat()


def anime_test_dir(anime_name: str) -> Path:
    return TEST_OUTPUT / anime_name / date.today().isoformat()


def today_batch_dir():
    return BATCH_OUTPUT / date.today().isoformat()


def anime_batch_dir(anime_name: str) -> Path:
    return BATCH_OUTPUT / anime_name / date.today().isoformat()


def discover_anime(root: Path = ANIME_ROOT) -> list[dict]:
    results = []
    for subdir in sorted(root.iterdir()):
        if not subdir.is_dir():
            continue
        mkvs = sorted(subdir.glob("*.mkv"))
        for mkv in mkvs:
            ass_files = sorted(subdir.glob("*.ass"))
            ass_eo = [a for a in ass_files if a.stem.endswith("_eo") or a.stem.endswith(".eo")]
            results.append({
                "name": subdir.name,
                "mkv": mkv,
                "ass_dir": subdir,
                "ass_all": ass_files,
                "ass_eo": ass_eo,
            })
    return results


def auto_detect_ass(mkv_path: Path, ass_dir: Path | None = None) -> Path | None:
    if ass_dir is None:
        ass_dir = mkv_path.parent
    stem = mkv_path.stem

    candidates = [
        ass_dir / f"{stem}_eo.ass",
        ass_dir / f"{stem}.eo.ass",
    ]
    for c in candidates:
        if c.exists():
            return c

    eo_globs = list(ass_dir.glob("*.eo.ass")) + list(ass_dir.glob("*_eo.ass"))
    if eo_globs:
        return eo_globs[0]

    first_ass = next(ass_dir.glob("*.ass"), None)
    if first_ass:
        return first_ass

    return None


def get_ffmpeg_location():
    local = Path("ffmpeg") / "bin"
    if (local / "ffmpeg.exe").exists():
        return str(local)
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg:
        return str(Path(ffmpeg).parent)
    return None