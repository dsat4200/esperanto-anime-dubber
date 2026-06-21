# anidub

Anime dubbing: separate audio into background + vocals, then generate Esperanto voice tracks from `.ass` subtitles using local TTS models on an RTX 5060.

## Setup

```powershell
.\install.ps1
```

Creates a `.venv/`, installs PyTorch with CUDA 12.8 (Blackwell), Demucs, OmniVoice (k2-fsa), Qwen3-TTS, and ffmpeg.

> **Note**: `omnivoice` on PyPI is the k2-fsa TTS model. This project's package is named `anidub` to avoid a name collision.

## Test voice CLI

Pick one subtitle line and voice it with full logging:

```powershell
.\.venv\Scripts\Activate.ps1
anidub-test-voice
```

Options:
- `--mkv PATH` (default: `anime/oreimo/Oreimo - 01.mkv`)
- `--ass PATH` (default: `anime/oreimo/Oreimo - 01.ass`)
- `--engine {omnivoice,qwen3,both}` (default: `omnivoice`)
- `--qwen-variant {custom,base,design}` (default: `custom`)
- `--qwen-speaker NAME` (default: `Serena`)
- `--line N` (skip interactive picker)

Output goes to `test_output/`:
- `line_<idx>_<engine>.wav` — generated audio
- `line_<idx>.json` — full logs (prompts, params, diagnostics)
- `ref_<idx>.wav` — extracted voice reference clip
- `skipped.json` — lines skipped (non-Esperanto, no pre-roll ref, etc.)

## Engines

- **OmniVoice** (k2-fsa, default): trained on 1,396 hours of Esperanto; native `duration=` param fits subtitle windows exactly; zero-shot voice clone from a 3-sec ref clip
- **Qwen3-TTS** (Alibaba): no Esperanto, no duration control, but supports free-form `instruct` prompt (your Esperanto phonetics guide); audio fit to window via `ffmpeg atempo` post-process

## Future pipeline (not yet built)

```
output/
├── vocals.wav            # Demucs: original Japanese vocals
├── no_vocals.wav         # Demucs: background + music (vocals removed)
└── dubbed/
    ├── line_001.wav
    ├── line_002.wav
    └── final.mkv         # Muxed result
```