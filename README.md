# DYST (did you see that? 👀)

A Windows background "scare/funny mod" app — at random intervals it plays a
fullscreen image or video on top of whatever you're doing, then disappears.
No window, no taskbar icon, no focus steal; it overlays directly on the screen.

> **Status:** early prototype (Phase 1). Media plays on screen, ticker runs,
> per-file settings + highest-quality downloads work. Chroma key, tray icon,
> audio sidecars and more are still to come (see PROGRESS.md).

---

## Quick start

```bat
:: 1. one-time setup
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt

:: 2. drop media into the folders
::    media\images\  -> png, gif, jpg, webp (transparent ones work best)
::    media\videos\  -> mp4, webm, avi, mov, mkv

:: 3. try it
.venv\Scripts\python main.py --test          :: play one random file then exit
.venv\Scripts\python main.py --play path\to\file.mp4   :: play a specific file
.venv\Scripts\python main.py --daemon        :: run the chance loop (odds, tick)
.venv\Scripts\python main.py --roll          :: print one simulated roll
```

---

## Folder layout

```
app_root/
├── main.py                 entry point
├── config.json             global settings (see below)
├── video-urls.txt          list of URLs for the downloader
├── dyst/                   app code
├── media/
│   ├── images/             images you add (png/gif/jpg/webp)
│   └── videos/             videos you add (mp4/webm/avi/mov/mkv)
└── scripts/                dev + helper scripts
```

---

## Configuration

There are **3 layers** of configuration, applied in order (later wins):

1. **Built-in defaults** (coded in `dyst/config.py`)
2. **`config.json`** — global settings for the whole app
3. **Per-file settings sidecar** — overrides for one media file only

### 1. `config.json` (global)

Edit this file by hand (a settings window may come later). All keys are
optional — anything missing falls back to the default. Bad values are ignored
with a warning, never crash the app.

```jsonc
{
  "tick_seconds": 1.0,          // seconds between chance rolls (must be > 0)
  "odds": 1000,                 // 1-in-N chance per tick (e.g. 1000 = 1/1000)
  "max_concurrent": 3,          // max overlays playing at once; 0 = unlimited
  "reroll_in_same_tick": true,  // on success, re-roll immediately (bursts)

  "media_folder": "media",      // folder with images/ and videos/ subfolders
  "image_display_seconds": 1.0, // how long a still image stays up
  "fade_seconds": 0.2,          // fade-out duration at end of playback

  "monitor": "primary",         // which screen: "primary" or 0-based index

  "audio_volume": 0.8,          // master volume 0.0–1.0

  "download_max_height": 1080,  // max video height (px) for the downloader
                                // (scripts/download_videos.py reads this;
                                //  CLI 3rd arg overrides)

  "chroma_key": {               // green-screen removal (Phase 3, coming)
    "enabled": true,
    "exceptions": [],           // filenames that skip chroma key
    "hue_range": [35, 85],
    "saturation_range": [40, 255],
    "value_range": [40, 255],
    "despill": false
  },

  "autostart": false,           // start with Windows (Phase 6, coming)
  "debug": false                // verbose logging to app.log
}
```

### 2. Per-file settings sidecar (overrides)

Put a file with the **same base name** next to a media file to control how
*that* media is displayed. JSON or TXT both work; JSON wins if both exist.

**Example:** for `media/videos/scare.mp4`, create `media/videos/scare.json`:

```json
{
  "mode": "cover",
  "duration": 3,
  "volume": 0.8
}
```

Or a `.txt` version (`media/videos/scare.txt`), one key per line —
`key=value` **or** `key: value`:

```
mode=cover
duration=3
volume=0.8
```

| Key | Values | Effect |
|---|---|---|
| `mode` | `fit` (default) · `cover` · `stretch` | `fit` = keep aspect, letterbox, centered · `cover` = fill the screen, crop the overflow (centered) · `stretch` = fill the screen, squish to fit |
| `duration` | any number > 0 (seconds) | How long to show (images). Videos ignore it. |
| `image_display_seconds` | any number > 0 (seconds) | Override for the global `image_display_seconds` (images only). Takes priority over `duration`. |
| `fade_seconds` | any number >= 0 (seconds) | Override for the global `fade_seconds` — how long the fade-out lasts. Image-only; ignored by videos. |
| `volume` | 0.0 – 1.0 | Volume for that file (multiplied with global `audio_volume`). |

`duration`, `image_display_seconds`, and `fade_seconds` are **image-only** —
when attached to a video they are silently ignored (videos play to end and use
the global `fade_seconds` for their fade-out).

Anything invalid is dropped with a logged warning — it never breaks the app.

> **Tip:** the demo file `media/images/test_scare.json` shows an example
> (cover, 3s, volume 0.7). Delete or edit it to see the effect.

### 3. `video-urls.txt` (downloader input)

One URL per line; blank lines and lines starting with `#` are ignored. Then run:

```bat
.venv\Scripts\python scripts\download_videos.py
```

Downloads each video (best quality, with audio, muxed into one mp4 — **no
audio extraction**) into `media/videos\` as `Title [id].mp4`. Requires
`yt-dlp` and `ffmpeg`; the script finds ffmpeg even if your terminal was
opened before it was installed.

---

## How playback works

When an overlay triggers (a roll succeeds, or you use `--test` / `--play`):

1. A **fullscreen window** is created on the primary monitor:
   - **no window chrome** (frameless), **no taskbar button / Alt-Tab entry**
     (`Qt.Tool`), **always on top**, **never steals focus**, and
     **click-through** (clicks pass to whatever is underneath).
2. Media is drawn **fitted** to the screen (aspect preserved, centered) unless
   the file's sidecar sets `mode` to `cover` or `stretch`.
3. **Images:** shown for `image_display_seconds` (or sidecar `duration`),
   then fade out over `fade_seconds` (the window opacity animates 1→0 while
   any sidecar audio keeps playing).
4. **Videos:** played to the end via QtMultimedia (audio + modern codecs),
   then fade out over `fade_seconds` (window opacity 1→0).

**Audio lifetime — a single overlay:** when the visual content finishes
(its display time for images, or end-of-media for videos) it begins to fade
out, but **any audio keeps playing** through the fade-out and beyond.  The
overlay window only closes — and only frees its slot for `max_concurrent` —
once **both** the visual fade and the audio have fully finished.  So an
image (or video) plus its audio counts as **one** occurrence, not two.

**Audio:** videos play their own audio track (respecting `audio_volume` and
any per-file `volume`). You can also add a **sidecar audio file** — same base
name, same folder (e.g. `scare.mp4` + `scare.wav`, `boo.png` + `boo.wav`) —
and it takes priority over the video's own audio. Supported: `.mp3 > .wav >
.ogg > .flac > .m4a` (that order if several exist). Sidecar audio also gives
images sound.

### AV1 videos (best-quality YouTube downloads)

YouTube's **best quality** downloads are often AV1. Some machines can't
**hardware**-decode AV1 — DYST detects AV1 files and handles them specially:

- **video** is decoded in **software** by OpenCV (works everywhere, no errors)
- **audio** is **extracted to a temporary file with ffmpeg** and played
  alongside (an audio-only file never touches AV1 video decoding)

So best-quality AV1 downloads now play with **both picture AND sound** and
without console error spam. This needs `ffmpeg` on the machine (which the
downloader requires anyway). Temporary audio files are cleaned up after
playback.

---

## CLI reference

| Flag | What it does |
|---|---|
| `--test` | Pick one random media file, play it, exit. Great for trying things. |
| `--play PATH` | Play a specific file, then exit. |
| `--daemon` | Run the chance loop (ticks every `tick_seconds`, rolls `1/odds`, spawns overlays) with no tray icon yet. Ctrl+C / kill to stop. |
| `--roll` | Print one simulated roll result and exit (headless sanity check). |
| `--config PATH` | Use a different config file (default `config.json`). |

---

## Test assets

Run the generator to create a test PNG and a green-screen test MP4 (the green
background is for the upcoming chroma key phase, Phase 3):

```bat
.venv\Scripts\python scripts\make_test_asset.py
```

---

## Logging

Everything is logged to **`app.log`** next to the app (and the console). Set
`"debug": true` in `config.json` for verbose per-roll detail.

---

## Recent Changes

- **Fixed image closing prematurely**: Updated `_close_if_ready` in `dyst/overlay.py` to require that the visual media has finished displaying (`_visual_done`), the fade-out animation has completed (`_fade_done`), and the audio has finished (`_audio_done`) before closing the overlay. This prevents the overlay from closing early when the side‑car audio is shorter than the configured display time.

- **Fixed image centering**: Changed the `mode` in `media/images/woolly-mammoth.json` from `"cover"` to `"fit"`. The `"fit"` mode preserves the aspect ratio and centers the image within the window (adding letter‑boxing if needed), ensuring the image appears in the middle.

## What's coming (see PROGRESS.md for details)

- **Phase 2** — media validation (skip corrupt files), sidecar audio files
- **Phase 3** — chroma key: green-screen removal so green-bg videos/images
  become fully transparent
- **Phase 4** — overlay polish (GIF/APNG animation, monitor selection)
- **Phase 5** — overlay manager (global max concurrency) + full audio
- **Phase 6** — tray icon, autostart, test-trigger menu
- **Phase 7** — packaging + this doc becoming the real user README