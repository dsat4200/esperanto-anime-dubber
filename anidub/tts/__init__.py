from pathlib import Path
from typing import Protocol, TypedDict


class TTSResult(TypedDict):
    wav: object
    sr: int
    output_duration: float
    diagnostics: dict


class TTSBackend(Protocol):
    def generate(
        self,
        text: str,
        ref_audio: Path | None,
        target_duration: float,
        instruct: str,
    ) -> TTSResult:
        ...