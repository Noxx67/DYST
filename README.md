# DYST (did you see that? 👀)

A Windows background app — at random intervals it plays an image or video on top of whatever you're doing, then disappears.
No window, no taskbar icon, no focus steal; it overlays directly on the screen.

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
  "fade_out_seconds": 0.2,      // fade-out duration at end of playback (renamed from fade_seconds)
  "fade_in_seconds": 0,         // fade-in before display (images/gifs); 0 = off
  "max_duration": 0,            // hard cap (seconds) on any overlay + its audio; 0 = no cap
  "speed": 1.0,                 // playback speed multiplier (>0): videos/gifs/audio/image display + fades
  "pitch": 1.0,                 // audio pitch multiplier (>0): sidecar + audio-bearing media
  "speed_pitch": 0,             // combined speed+pitch: >0 sets BOTH and overrides speed/pitch; 0 = off

  "monitor": "primary",         // which screen: "primary" or 0-based index
  "mode": "fit",                // how media covers the screen: fit (default) | stretch | cover-height | cover-width | custom

  // Only used when "mode": "custom" (per-file sidecar values win):
  "position_x": 0.5,           // X position -1..2: 0 = left edge at screen left, 1 = right edge at screen right, 0.5 = centered; -1 = fully off-screen left, 2 = fully off-screen right (lets media peek in / get cropped)
  "position_y": 0.5,           // Y position -1..2: 0 = top edge at screen top, 1 = bottom edge at screen bottom, 0.5 = centered; same off-screen range
  "scale_x": 1.0,              // width multiplier relative to the "fit" size (1 = whole media visible, aspect kept)
  "scale_y": 1.0,              // height multiplier relative to the "fit" size
  "flip_h": false,             // mirror horizontally
  "flip_v": false,             // mirror vertically
  "rotation": 0,               // rotation in degrees (around the media's center)

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
  "mode": "cover-height",
  "duration": 3,
  "volume": 0.8
}
```

Or a `.txt` version (`media/videos/scare.txt`), one key per line —
`key=value` **or** `key: value`:

```
mode=cover-height
duration=3
volume=0.8
```

| Key | Values | Effect |
|---|---|---|
| `mode` | `fit` (default) · `stretch` · `cover-height` · `cover-width` · `custom` | How the media covers the screen (also set globally via the `mode` key in `config.json`; the sidecar wins). `fit` = whole media visible, aspect kept, centered · `stretch` = squished to exactly the screen size · `cover-height` = fit the entire screen **horizontally** · `cover-width` = fit the entire screen **vertically** · `custom` = use position/scale/flip/rotation below. The old `cover` value was removed — it falls back to `fit`. |
| `position_x` / `position_y` | −1.0 – 2.0 (default `0.5`) | **Custom mode only.** Where the media sits. `0` pins the edge to the screen edge (`position_x=0` → left edge at screen left), `1` pins the other edge (`position_x=1` → right edge at screen right), `0.5` centers it. Same for Y (top/bottom). Values outside 0–1 push the media **off-screen** so it can peek in or be cropped at the screen edge: `-1` = fully off-screen left/top, `2` = fully off-screen right/bottom, e.g. `1.5` → the media pokes out past the right edge and gets cropped. **Use string format for ranges:** `"-0.5~1.5"` (min~max). |
| `scale_x` / `scale_y` | any number > 0 (default `1`) | **Custom mode only.** Stretch multipliers relative to the **fit size** (scale 1×1 = whole media visible, aspect kept, nothing cropped). `scale_x=2` doubles the width, `scale_y=0.5` halves the height. If a scaled-up media overflows the screen it's cropped; keep `1×1` (or smaller) to stay fully visible. **Use string format for ranges:** `"0.5~1.5"`. |
| `flip_h` / `flip_v` | `true` / `false` / `"random"` (default `false`) | **Custom mode only.** Mirror the media horizontally / vertically. **Use string format:** `"random"` to randomly pick true/false on each trigger. |
| `rotation` | any number (degrees, default `0`) | **Custom mode only.** Rotate the media around its own center (e.g. `45`, `-90`, `180`). **Use string format for ranges:** `"0~360"`. |
| `duration` | any number > 0 (seconds) | How long to show (images). Videos ignore it. |
| `image_display_seconds` | any number > 0 (seconds) | Override for the global `image_display_seconds` (images only). Takes priority over `duration`. |
| `fade_out_seconds` | any number >= 0 (seconds) | Override for the global `fade_out_seconds` — how long the fade-out lasts. Image-only; ignored by videos. The old name `fade_seconds` is accepted as a deprecated alias (a warning asks you to rename it). |
| `fade_in_seconds` | any number >= 0 (seconds, default `0`) | Override for the global `fade_in_seconds` — the image/GIF fades in from transparent **before** the display clock starts, so total lifetime = fade_in + display + fade_out. `0` = appears instantly. Image/GIF-only; ignored by videos. |
| `volume` | 0.0 – 1.0 | Volume for that file (multiplied with global `audio_volume`). |
| `speed` | any number > 0 (default `1`) | Playback speed multiplier for this file: video + its audio, GIFs, sidecar audio, **and** image display time + fades (all timings scale by 1/speed). `2` = twice as fast, `0.5` = half. With `pitch=1` the audio speeds up tape-style (pitch rises with speed); with `pitch != 1` speed and pitch are independent (audio is re-encoded via ffmpeg). **Use string format for ranges:** `"1.0~2.0"`. |
| `pitch` | any number > 0 (default `1`) | Audio pitch multiplier for this file: sidecar audio and the audio of videos (the embedded track is extracted + re-encoded). `2` = an octave up, `0.5` = an octave down. Requires ffmpeg; independent of `speed`. **Use string format for ranges:** `"0.5~1.5"`. |
| `speed_pitch` | any number >= 0 (default `0` = off) | Sets **both** speed and pitch at once (`speed = pitch = this value`, e.g. `1.5` = 1.5× speed AND 1.5× pitch). When set (per-file > global) it **overrides** the individual `speed`/`pitch` values — handy for randomizing both together later. `0`/absent = use `speed` and `pitch` separately. **Use string format for ranges:** `"1.0~2.0"`. |
| `max_duration` | any number >= 0 (seconds, default `0`) | Hard cap for this file. When the timer runs out, the video/image/gif **and** its sidecar audio stop **immediately** and the overlay closes **instantly — no fade-out**. `0` = no cap (play naturally). Setting it smaller than `image_display_seconds` truncates the image display; smaller than a video's length cuts the video off early. Per-file wins over the global `max_duration` — use `0` per-file to disable a global cap for one file. **Use string format for ranges:** `"1.0~5.0"`. |

`duration`, `image_display_seconds`, and `fade_out_seconds` are **image-only** —
when attached to a video they are silently ignored (videos play to end and use
the global `fade_out_seconds` for their fade-out).

Anything invalid is dropped with a logged warning — it never breaks the app.

> **Tip:** the demo file `media/images/test_scare.json` shows an example
> (cover-height, 3s, volume 0.7). Delete or edit it to see the effect.

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
2. Media is drawn per the **`mode`** setting — `fit` (whole media visible,
   aspect kept, centered) by default. Set a global default in `config.json`
   (`"mode": "cover-height"`) or per-file in the sidecar. Options: `stretch`
   (squish to screen size), `cover-height` (fit the entire screen horizontally
   — crop top/bottom when the media is taller than the screen),
   `cover-width` (fit the entire screen vertically — crop left/right when the
   media is wider), `fit` (no stretching), and `custom` — position the media
   anywhere with `position_x/position_y` (0..1 edge-pinning), stretch it
   independently with `scale_x/scale_y` (relative to the fit size),
   mirror it with `flip_h/flip_v`, and rotate it with `rotation` (degrees
   around its center). The custom values only apply when `mode` is `custom`.
3. **Images:** shown for `image_display_seconds` (or sidecar `duration`),
   then fade out over `fade_out_seconds` (the window opacity animates 1→0 while
   any sidecar audio keeps playing). With `fade_in_seconds` set, the image
   first fades in from transparent (opacity 0→1) **before** the display clock
   starts — total lifetime = fade_in + display + fade_out. With `speed` (or
   `speed_pitch`) set, the display time and both fades scale — divided by
   speed (2× speed = half the display + fade times).
4. **Videos:** played to the end via QtMultimedia (audio + modern codecs),
   then fade out over `fade_out_seconds` (window opacity 1→0).

**`max_duration`** caps any overlay: once the timer runs out, the visual
(image/gif/video) **and any audio** (sidecar, extracted, or embedded) stop
immediately and the overlay closes **instantly with no fade-out** — the
media simply vanishes. With `0` the overlay plays to its natural end
(image timer / video length, with the normal fade behavior).

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
