from pathlib import Path
import shutil

MODEL_NAME = "htdemucs"
OUTPUT_ROOT = Path("downloads")
DEFAULT_MKV = Path("anime/oreimo/Oreimo - 01.mkv")
DEFAULT_ASS = Path("anime/oreimo/Oreimo - 01.ass")
TEST_OUTPUT = Path("test_output")


def get_ffmpeg_location():
    local = Path("ffmpeg") / "bin"
    if (local / "ffmpeg.exe").exists():
        return str(local)
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg:
        return str(Path(ffmpeg).parent)
    return None