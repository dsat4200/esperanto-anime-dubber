# Esperanto Anime Dubber

Anime dubbing pipeline: separate audio into background + vocals, translate Japanese subtitles to Esperanto, then generate Esperanto voice tracks using the OmniVoice (k2-fsa) TTS model. All dubbing work happens in a browser-based editor — no CLI workflow commands required.
![alt text](image-8.png)
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
- Must contain **a Japanese audio track** (or whichever language you're dubbing from).
- Filenames can follow any convention. Episodes are processed in **alphabetical order**.

### ASS subtitle requirements

- Standard `.ass` format with `[Events]` section.
- Dialogue lines must have **`Style: main`** (or `Default`) to be considered for dubbing.
- Lines with `Style: OP` or `Style: ED` are treated as opening/ending and are excluded from dubbing (original Japanese audio is kept for OP/ED sections).
- Non-Esperanto text (Japanese, CJK characters) is auto-skipped.
- Lines starting within `< 1.0` second of the episode are skipped (no pre-roll reference audio).

### Esperanto ASS files

If an `_eo.ass` file already exists for an episode (e.g., `Gabriel.DropOut.S00E02_eo.ass`), it is used directly. Otherwise, the editor auto-extracts the embedded Japanese ASS track, translates it to Esperanto via Google Translate, and saves it as `{mkv_stem}_eo.ass` in the anime folder.

---

## Starting the Editor

```powershell
anidub-edit
```

Opens `http://127.0.0.1:5000` in your browser. Optional flags: `--port N`, `--host ADDR`, `--project PATH` (open an existing project on startup).

<!-- SCREENCAP: anidub-edit window just opened, empty home screen -->
![alt text](image.png)
---

## 1. Creating a Project

Enter the anime folder name (e.g. `gabriel_dropout`) in the text box on the home panel and click **Create Project**. The editor discovers all MKVs in that folder, extracts their audio/subtitle track listings, and shows each episode as a card on the home screen.

To re-open an existing project, pick it from the dropdown at the top of the home panel and click **Load**.

<!-- SCREENCAP: home screen with episode cards populated, showing Tr/Cl/Ac progress bars -->

---

## 2. Opening an Episode

Double-click an episode card to open the editor.

- **Left pane** — video preview, plus the audio shift, dialogue speed, and per-clip buttons (Clone, Preview, Accept, Reject, Reset).
- **Right pane** — current clip header, original text, translation text box, pronunciation override, character + mood selectors, additional clone instructions.
- **Bottom** — timeline bar showing every clip in the episode, color-coded by status.

<!-- SCREENCAP: editor view with a clip loaded, left + right panes visible -->
![alt text](image-1.png)
---

## 3. Audio + Subtitle Tracks

When you trigger a **Batch Translate** or **Batch Clone** on multiple episodes, the editor opens a track-picker modal:

- Pick the **Japanese audio track** (usually the one labeled `jpn`).
- Pick the **ASS subtitle track**.
- These choices apply to all selected episodes; episodes with different track counts are skipped and reported.

For a single-episode open, the editor auto-selects the first audio + subtitle tracks. Episode cards must already be set up via the modal before running batch operations.

<!-- SCREENCAP: track-picker modal showing audio and subtitle radio choices -->
![alt text](image-2.png)
---

## 4. Demucs Separation

The first time you open an episode, the editor automatically runs Demucs (`htdemucs`) to split the ripped audio into:

- `vocals.wav` (original Japanese voices)
- `no_vocals.wav` (background music + SFX)

This runs once per episode and may take a few minutes. A status overlay shows progress.

<!-- SCREENCAP: "Running Demucs (may take a few minutes)..." overlay -->
![alt text](image-3.png)
---

## 5. Translating

Three ways to translate subtitle lines to Esperanto:

- **Translate All** (bulk bar at the bottom) — translates every pending clip in the current episode via Google Translate (auto-detect → Esperanto).
- **Batch Translate** (home screen, after Shift-clicking multiple episode cards) — translates all selected episodes at once, prompting for audio/subtitle tracks.
- **Per-clip** — type a manual translation in the **Translation** text area on the right pane, then click **Save settings**. Or click **Restore original translation** to re-run Google Translate on this clip.

Translated lines are visible immediately in the right pane. The clip's status changes from `pending` to `translated`.

<!-- SCREENCAP: clip with original text + translation box filled, status showing "translated" -->
![alt text](image-4.png)
---

## 6. Voice Cloning

Three ways to generate Esperanto voice:

- **Clone All** (bulk bar) — clones every translated clip in the current episode.
- **Batch Clone** (home screen) — clones all selected episodes at once.
- **Per-clip Clone** (left pane) — clones the currently-loaded clip only.

OmniVoice (k2-fsa) generates Esperanto speech cloned from a ~3-second reference clip of the original Japanese actor. The TTS model stays in VRAM between manual clones for fast iteration; if VRAM usage exceeds 75% before a clone, the shared model is automatically unloaded and recreated. Live tensors, reserved memory, and active TTS backends are visible in the GPU panel (see [GPU Memory Panel](#gpu-memory-panel)).

<!-- SCREENCAP: editor after a successful clone, clone-info string showing inference time and status -->

---

## 7. Reviewing

- **Preview** button — plays the video with the cloned Esperanto voice mixed over the original background audio.
- **Audio shift slider** (-500ms to +500ms) — fine-tunes the cloned voice's timing offset. Updates the timeline's red audio-offset handle in real time.
- **Dialogue speed slider** (0.50× to 2.00×) — applied via ffmpeg `atempo` during mixing.
- **Accept / Reject / Reset** — `Accept` marks the clip done and advances to the next unaccepted clip; `Reject` flags it for re-cloning; `Reset` clears translation + clone.
- **Timeline bar** (bottom) — every clip shown as a colored block by status:
  - `pending` (dark), `translating` (purple), `translated` (blue),
  - `cloned` (gold), `accepted` (green), `rejected` (red),
  - `non_dub` (grey), `sign` (teal, hatched).
  - Drag block edges to resize a clip's timing window.
  - Drag the red handle at the bottom of a clip to nudge its audio offset.
  - Right-click a clip for **Delete clip** or **Toggle sign/audio**.

<!-- SCREENCAP: timeline bar showing mixed-status clips, with the current clip highlighted -->
![alt text](image-5.png)
---

## 8. Characters & Moods

To reuse a voice print across episodes:

1. Load a clip whose voice you want to preserve.
2. Pick a **Character** name from the dropdown (or type a new one).
3. Pick a **Mood** (defaults to `normal`).
4. Click **Save as character clip** — this copies the current clip's `ref.wav` into a character voice library.

The **Manage** button opens the character panel, which lists every character×mood pair and lets you delete individual entries. Before cloning, set the Character + Mood dropdowns — OmniVoice will use that voice print for the generated clip.

<!-- SCREENCAP: character Manage panel showing multiple characters with moods -->
![alt text](image-6.png)
---

## 9. Signs / Non-Dubbed Clips

Some subtitle lines aren't dialogue (on-screen signs, titles) and shouldn't be voiced.

- Click **Mark as Sign** on the left pane to toggle the current clip's status to `sign`. The button label flips to **Mark as Vocal** when status is `sign`.
- Right-click a timeline clip → **Toggle sign/audio** to do the same from the timeline.
- `sign` clips keep original audio and subtitles, and are skipped during cloning and assembly. They render with a hatched pattern on the timeline.

The editor auto-detects some non-dub clips (no usable reference audio, text too short, etc.) and marks them `non_dub`; these cannot be toggled back to `sign`.

---

## 10. Assembling

Click **Assemble** in the top bar to build the full dubbed episode from all accepted clips:

- Voiced lines replace the original Japanese vocals.
- OP/ED sections keep original audio.
- Gaps > 2 seconds between lines are filled with original audio.
- Error / skipped lines are filled with original audio.
- Video stream is copied (no re-encode).
- Audio track 1: AAC 256k (Esperanto dub); Audio track 2: original Japanese (copy).
- Subtitle track: copy.

Output: `projects/{anime}/{episode}/out/{episode}_Dubbed.mkv`. A completion dialog shows the final path.

To skip an episode in future batch runs, mark it **Complete** (per-episode flag).

<!-- SCREENCAP: completion dialog showing the final assembled .mkv path -->

---

## GPU Memory Panel

Click the **GPU --%** indicator in the top-right of the header to open the GPU panel:

- **Live tensors** — VRAM allocated by active tensors.
- **Reserved (driver)** — VRAM held by the caching allocator.
- **% reserved** — fraction of total VRAM reserved.
- **Live models** — names of active TTS backends (including `Shared (manual clones)` for the long-lived single-clip backend).

Buttons:

- **Clear GPU Memory** — frees the allocator's reserved-but-unallocated pool (lightweight).
- **Force Unload Models** — drops every loaded OmniVoice + Whisper backend from VRAM (heavier; reshapes the live-models list to "No live TTS models loaded").

<!-- SCREENCAP: GPU panel open showing device, live tensors, reserved bar, and live backends -->

---

## Language Selector

Next to the **Home** button, a **Language** dropdown lets you switch the editor UI between:

- **English** (default)
- **Esperanto**

The choice is saved in `localStorage`, so it persists across browser sessions. Switching re-renders the current clip, episode home, timeline, and GPU panel in the chosen language. Status enum words (`pending`, `translated`, `cloned`, `accepted`, `rejected`, `sign`, `non_dub`) are also localized; the timeline color coding is unchanged.

<!-- SCREENCAP: header showing the language dropdown with Esperanto selected and UI text in Esperanto -->
![alt text](image-7.png)
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
| `ffmpeg not found` | Run `.\install.ps1` or ensure `ffmpeg\bin\ffmpeg.exe` exists |
| `CUDA out of memory` | Close other GPU applications; `torch.cuda.empty_cache()` is called between lines |
| All lines skipped | Check ASS styles — dialogue must be `Style: main`; verify text is Esperanto (not Japanese) |
| Original Japanese voices not removed | Demucs cache is at `{output_dir}/full_no_vocals.wav` — delete it to force re-separation |
| Episode uses wrong subtitles | Delete the stale `_eo.ass` file and re-open the episode; the editor will re-extract + re-translate |