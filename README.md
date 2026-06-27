# anidub

Anime dubbing pipeline: separate audio into background + vocals, translate Japanese subtitles to Esperanto, then generate Esperanto voice tracks using the OmniVoice (k2-fsa) TTS model.

## Disclaimer

**This is a personal project developed and tested on a specific machine.**

| Component | Detail |
|-----------|--------|
| **GPU** | NVIDIA GeForce RTX 5060 (Blackwell architecture) |
| **VRAM** | 8 GB |
| **CUDA** | 12.8 |
| **OS** | Windows 11 |
| **Python** | 3.10+ |

It may not work on other GPUs, CUDA versions, or operating systems. Uses `~6 GB` of VRAM during inference. First run downloads `~2.5 GB` of model weights. **YMMV.**

---

## Requirements

- **Windows** (tested on 11; PowerShell required for `install.ps1`)
- **Python 3.10+**
- **NVIDIA GPU** with CUDA 12.8+ support (Blackwell optimized; may work on older architectures)
- **ffmpeg** (auto-downloaded by `install.ps1`)
- **Model downloads** (automatic on first run):
  - `k2-fsa/OmniVoice` — ~2.5 GB
  - `openai/whisper-tiny` — ~150 MB
  - `htdemucs` (Demucs) — ~160 MB

---

## Quick Install

```powershell
# 1. Clone the repo
git clone <repo-url> "omnivoice"
cd "omnivoice"

# 2. Run the installer (creates .venv, installs PyTorch CUDA, deps, ffmpeg)
.\install.ps1

# 3. Register CLI commands
.\.venv\Scripts\Activate.ps1
pip install -e .
```

If you skip `install.ps1` and use your own venv, install manually:

```powershell
pip install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu128
pip install demucs omnivoice deep-translator rich soundfile
pip install -e .
```

---

## Input File Structure

Place your anime in `anime/{name}/`. Each folder contains one or more MKVs and optional ASS subtitle files.

```
anime/
├── gabriel_dropout/
│   ├── Gabriel.DropOut.S00E02.mkv            # OVA
│   ├── Gabriel.DropOut.S00E02_eo.ass         # Pre-translated Esperanto subtitles
│   ├── Gabriel.DropOut.S01E03.1080p.BluRay.10-Bit.FLAC2.0.x265-YURASUKA.mkv
│   ├── Gabriel.DropOut.S01E04.1080p.BluRay.10-Bit.FLAC2.0.x265-YURASUKA.mkv
│   └── ... (S01E05 – S01E12)
│
├── hxh/
│   ├── Hunter x Hunter (2011)S01E001.mkv
│   ├── Hunter x Hunter (2011)S01E002.mkv
│   └── ... (S01E003 – S01E148)
│
├── call_of_the_night/
│   ├── Call.of.the.Night.S01E01.mkv
│   └── Call.of.the.Night.S01E01_eo.ass
│
└── oreimo/
    ├── Oreimo - 01.mkv
    └── Oreimo - 01.ass
```

### MKV requirements

- Must contain **an ASS subtitle track** (embedded). If not embedded, provide a `.ass` file next to the MKV.
- Must contain **a Japanese audio track** (or whichever language you're dubbing from). Use `--audio-lang jpn` to pick it.
- Filenames can follow any convention. Episodes are processed in **alphabetical order**.

### ASS subtitle requirements

- Standard `.ass` format with `[Events]` section.
- Dialogue lines must have **`Style: main`** (or `Default`) to be considered for dubbing.
- Lines with `Style: OP` or `Style: ED` are treated as opening/ending and are excluded from dubbing (original Japanese audio is kept for OP/ED sections).
- Non-Esperanto text (Japanese, CJK characters) is auto-skipped.
- Lines starting within `< 1.0` second of the episode are skipped (no pre-roll reference audio).

### Esperanto ASS files

If an `_eo.ass` file already exists for an episode (e.g., `Gabriel.DropOut.S00E02_eo.ass`), it is used directly. Otherwise, the tool auto-extracts the embedded Japanese ASS track, translates it to Esperanto via Google Translate, and saves it as `{mkv_stem}_eo.ass` in the anime folder.

---

## How It Works

Each subtitle line goes through this pipeline:

```
1. PARSE          Read .ass file, filter to "main" dialogue lines
                  Skip: empty text, Japanese text, music markers, no pre-roll
                  Skip: lines inside OP/ED time ranges

2. TRANSLATE      Google Translate (auto-detect → Esperanto)
                  Duplicate/progressive lines are auto-merged (--auto)
                  Writes {stem}_eo.ass in the anime folder

3. SEPARATE       Demucs (htdemucs) splits ripped audio into:
                  - vocals.wav   (original Japanese voices)
                  - no_vocals.wav (background music + SFX)

4. EXTRACT REF    ffmpeg clips ~3 seconds of original audio at the
                  subtitle's start time → ref.wav (24 kHz mono)

5. TRANSCRIBE     Whisper (tiny model) transcribes ref.wav in Japanese
                  to get the reference text for voice cloning

6. GENERATE       OmniVoice (k2-fsa) generates Esperanto speech:
                  - Voice cloned from ref.wav
                  - Duration matched to subtitle window
                  - Esperanto phonetics guide as instruct prompt

7. TRIM + FIT     Trim silence, then ffmpeg atempo speeds up audio
                  if it exceeds the subtitle window duration

8. MIX            ffmpeg mixes voice (0.8) + background (1.0)
                  → dubbed.wav

9. MUX            ffmpeg combines video + dubbed audio + subtitles
                  Video stream: copy (no re-encode)
                  Audio track 1: AAC 256k (Esperanto dub)
                  Audio track 2: copy (original Japanese)
                  Subtitle track: copy
                  → final.mkv
```

After all lines are voiced, `build_full_episode()` assembles the complete episode:
- Voiced lines replace the original Japanese vocals
- OP/ED sections keep original audio
- Gaps > 2 seconds between lines are filled with original audio
- Error lines are filled with original audio

---

## Commands

Three CLI tools are installed:

| Command | Purpose |
|---------|---------|
| `anidub-test-voice` | **Main tool** — single line tests and batch dubbing |
| `anidub-translate` | Extract + translate subtitles only (no voicing) |
| `anidub` | Banner only (future full pipeline entry point) |

### Single line test

```powershell
# Interactive: pick anime → episode → line
anidub-test-voice --anime gabriel_dropout

# Skip the line picker
anidub-test-voice --anime gabriel_dropout --line 42

# Direct path to a specific MKV
anidub-test-voice --mkv "anime/oreimo/Oreimo - 01.mkv" --line 10
```

Output goes to `test_output/{anime}/{episode}/{date}/`:

```
test_output/
├── gabriel_dropout/
│   └── Gabriel.DropOut.S00E02/
│       └── 2026-06-24/
│           ├── line_006/
│           │   ├── ref.wav            # Extracted reference audio
│           │   ├── tts_raw.wav        # Raw TTS output
│           │   ├── tts.wav            # Trimmed + tempo-fitted output
│           │   ├── no_vocals_clip.wav # Background audio for this line
│           │   ├── dubbed.wav         # Voice + background mixed
│           │   ├── video_only.mkv     # Video segment for this line
│           │   ├── sub_line.ass       # Single-line subtitle
│           │   ├── final.mkv          # Final muxed clip
│           │   └── log.json           # Full diagnostics
│           └── ...
```

### Batch one episode

```powershell
# Shows episode picker if multiple MKVs, then voices every line
anidub-test-voice --batch --anime gabriel_dropout
```

### Batch all episodes in a folder

```powershell
# Process every MKV in the folder sequentially
anidub-test-voice --batch --anime hxh -y --auto --audio-lang jpn

# Process a range (1-based, alphabetical order)
anidub-test-voice --batch --anime hxh --range 1-10
anidub-test-voice --batch --anime hxh --range 50-

# Resume after interruption (completed episodes are auto-skipped)
anidub-test-voice --batch --anime hxh --range 15- -y --auto --audio-lang jpn
```

Output goes to `batch_output/{anime}/{episode}/{date}/`:

```
batch_output/
├── gabriel_dropout/
│   ├── Gabriel.DropOut.S00E02/
│   │   └── 2026-06-24/
│   │       ├── clips/
│   │       │   ├── line_006_0-21-43.77_0-21-46.82_Mi_eliris_bone/
│   │       │   │   ├── ref.wav
│   │       │   │   ├── tts_raw.wav
│   │       │   │   ├── tts.wav
│   │       │   │   ├── no_vocals_clip.wav
│   │       │   │   ├── dubbed.wav
│   │       │   │   ├── video_only.mkv
│   │       │   │   ├── sub_line.ass
│   │       │   │   └── final.mkv
│   │       │   └── ...
│   │       ├── ripped_audio.wav
│   │       ├── full_no_vocals.wav
│   │       ├── full_vocals.wav
│   │       ├── full_dubbed.wav
│   │       ├── Gabriel.DropOut.S00E02_Dubbed.mkv   ← Final episode!
│   │       ├── skipped.json
│   │       └── batch_log.json
│   ├── Gabriel.DropOut.S01E03/
│   │   └── 2026-06-24/
│   │       └── ...
│   └── ...
```

### Translate only (no voicing)

```powershell
# Translate one episode (interactive)
anidub-test-voice --translate --anime gabriel_dropout

# Translate a range of episodes (batch translate only, no voice)
anidub-test-voice --translate --batch --anime hxh --range 1-10 -y --auto
```

Extracts embedded ASS from MKV, translates to Esperanto, writes `_eo.ass`. Does **not** voice.

### Unattended overnight run

```powershell
anidub-test-voice --batch --anime hxh -y --auto --audio-lang jpn
```

| Flag | Purpose |
|------|---------|
| `-y` | Skip "Voice N lines?" confirmation |
| `--auto` | Auto-merge duplicate lines during translation (no prompts) |
| `--audio-lang jpn` | Skip audio track picker |

---

## All Options

```
--mkv PATH            Direct path to a single MKV
--ass PATH            Direct path to a single ASS file
--anime NAME          Anime folder name under anime/ (with --batch: all episodes)

--batch               Voice all usable lines in the episode
--translate           Extract + translate embedded ASS (stops unless --batch)
--auto                Skip merge prompts during translation
--yes, -y             Skip "Voice N lines?" confirmation

--range 1-10          Episode index range (1-based, alphabetical order)
--range 50-           From episode 50 to end
--line N              Skip line picker, use specific subtitle index

--audio-lang jpn      Audio track language (e.g., jpn, eng)
--whisper-model       openai/whisper-tiny (default)
                      also: -base, -small, -medium, -large-v3-turbo
--voice-timeout N     Abort stuck line after N seconds (default: 120)
```

### Keyboard shortcuts (during batch voicing)

| Key | Action |
|-----|--------|
| `s` | Skip the current line (no Enter needed) |

---

## Source File Structure

```
anidub/
├── __init__.py          # Package init, version string
├── __main__.py          # Entry point → cli.main()
├── cli.py               # Banner
├── config.py            # Paths, ffmpeg discovery, output dir helpers
├── ass.py               # .ass parser, dialogue filter, language detection, text cleaning
├── translate.py         # Embedded ASS extraction, Google Translate (JPN → EO)
├── esperanto.py         # Esperanto phonetics table for TTS instruct prompt
├── extract.py           # ffmpeg audio extraction, silence trimming, tempo fitting
├── separator.py         # Demucs audio separation (vocals + no_vocals)
├── asr.py               # Whisper-based reference audio transcription
├── pipeline.py          # Core: process a single subtitle line end-to-end
├── assembler.py         # Assemble individual line: video clip + mix audio + mux
├── full_episode.py      # Build full dubbed episode from all voiced lines
├── test_voice.py        # Main CLI: single-line test + batch modes
└── tts/
    ├── __init__.py       # TTSBackend Protocol / TTSResult TypedDict
    └── omnivoice.py      # OmniVoiceTTSBackend (k2-fsa model wrapper)
```

---

## Known Issues

### Google Translate 500 errors
Occasionally returns HTTP 500. The translate step automatically retries once after 30 seconds. If it still fails, the line keeps its original text (marked as failed in the log).

### Timeout cascades
If a line times out (stuck in GPU generation), subsequent lines may also run slowly or timeout because the abandoned thread still holds GPU memory. When a timeout is detected, the TTS backend is **destroyed and recreated** after a 30-second cooldown to free the CUDA context.

### First-run model downloads
The following are downloaded on first inference:
- `k2-fsa/OmniVoice` (~2.5 GB)
- `openai/whisper-tiny` (~150 MB)
- `htdemucs` weights (~160 MB)

Expect a few minutes of downloading before the first line is voiced.

---

## Troubleshooting

| Problem | Fix |
|---------|-----|
| `anidub-test-voice not recognized` | Run `pip install -e .` in the activated venv |
| `ffmpeg not found` | Run `.\install.ps1` or ensure `ffmpeg\bin\ffmpeg.exe` exists |
| `CUDA out of memory` | Close other GPU applications; `torch.cuda.empty_cache()` is called between lines |
| All lines skipped | Check ASS styles — dialogue must be `Style: main`; verify text is Esperanto (not Japanese) |
| Original Japanese voices not removed | Demucs cache is at `{output_dir}/full_no_vocals.wav` — delete it to force re-separation |
| Episode uses wrong subtitles | Delete the stale `_eo.ass` file and re-run; the tool will re-extract + re-translate |
