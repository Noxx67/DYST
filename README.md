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

Full key reference with hints: `config.json` itself carries a `_hints` block
documenting every global key (and how it interacts with the per-file keys).
The **per-file** sidecar reference/template is `config_template.json` (copy it
next to a media file and rename it to match).

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
  "end_on_audio_end": false,   // images: disappear when the sidecar audio ends (ignores image_display_seconds; max_duration still applies)
  "fade_out_seconds": 0.2,      // fade-out duration at end of playback (renamed from fade_seconds)
  "fade_in_seconds": 0,         // fade-in before display (images/gifs); 0 = off
  "max_duration": 0,            // hard cap (seconds) on any overlay + its audio; 0 = no cap
  "speed": 1.0,                 // playback speed multiplier (>0): videos/gifs/audio/image display + fades
  "pitch": 1.0,                 // audio pitch multiplier (>0): sidecar + audio-bearing media
  "speed_pitch": 0,             // combined speed+pitch: >0 sets BOTH and overrides speed/pitch; 0 = off
  "max_playback_height": 480,  // cap decode/key/paint height (px): taller videos/GIFs are downscaled keeping aspect (stable playback for high-res clips); 0 = native
  "max_playback_fps": 30,      // cap effective playback fps (videos; frame-sampled, audio untouched); 0 = native

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

  // Green/blue-screen removal. 'custom' takes the hue/sat/val ranges
  // from the keys below (ignored for any other preset).
  "chroma_key": "green",      // "off" | "green" (default) | "blue" |
                                // "weak green" | "strong green" | "weak blue"
                                // | "strong blue" | "custom"
  "chroma_hue_range": [35, 85],        // hue 0..179 (green ~60, blue ~120)
  "chroma_saturation_range": [40, 255], // saturation 0..255
  "chroma_value_range": [40, 255],      // value 0..255
  // ^^^^ the three range keys are IGNORED unless chroma_key == "custom"
  "autostart": false,           // start with Windows (Phase 6, coming)
  "kill_hotkey": "ctrl+shift+alt+k",  // global dead man's switch ("" = disabled)
  "kill_notify": true,          // show a Windows notification when the kill switch fires
  "debug": false                // verbose logging to app.log
}
```

**Green-screen presets** — instead of hunting for HSV numbers, set the
`chroma_key` value to a named preset (or use the old dict form for expert
tuning). `despill` (edge de-greening, default ON) and the hue window are
auto-applied; the hue window is also auto-re-centred on each video's actual
screen colour at cache-build time.

| `preset` | hue | saturation | value | Use for |
|---|---|---|---|---|
| `"green"` (default) | 35–85 | 40–255 | 40–255 | normal, well-lit green screens |
| `"weak green"` | 28–92 | 20–255 | 30–255 | faint/uneven/dim green — wider catch |
| `"strong green"` | 42–78 | 60–255 | 50–255 | vivid uniform green — less risk of punching holes in the subject |
| `"blue"` | 100–130 | 40–255 | 40–255 | standard blue screens |
| `"weak blue"` | 90–145 | 20–255 | 30–255 | faint/uneven blue — wider catch |
| `"strong blue"` | 105–130 | 60–255 | 50–255 | vivid uniform blue — less risk of punching holes |

A preset fills in the three ranges for you; any `hue_range` / `saturation_range` / `value_range` you also set explicitly **still override** it per-key. If you want something else entirely, leave `preset` empty (`""`) and set the ranges by hand. Case doesn't matter (`"GREEN"` works).

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
| `scale` | any number > 0 | **Custom mode only.** A convenient **uniform** scale: sets BOTH `scale_x` AND `scale_y` to the same value in one key (`"scale": 2` = twice as wide **and** twice as tall). It is **overwritten** when BOTH `scale_x` and `scale_y` are given explicitly in the same sidecar — then those per-axis values are used as-is; a lone `scale_x`/`scale_y` overrides just its own axis. **Use string format for ranges:** `"0.5~1.5"` — for a range, **ONE** random value is drawn and applied to both axes, so X and Y always match. |
| `flip_h` / `flip_v` | `true` / `false` / `"random"` (default `false`) | **Custom mode only.** Mirror the media horizontally / vertically. **Use string format:** `"random"` to randomly pick true/false on each trigger. |
| `rotation` | any number (degrees, default `0`) | **Custom mode only.** Rotate the media around its own center (e.g. `45`, `-90`, `180`). **Use string format for ranges:** `"0~360"`. |
| `duration` | any number > 0 (seconds) | How long to show (images). Videos ignore it. |
| `image_display_seconds` | any number > 0 (seconds) | Override for the global `image_display_seconds` (images only). Takes priority over `duration`. |
| `end_on_audio_end` | `true` / `false` (default `false`) | **Images only** (global + per-file). When `true`, the image/GIF **ignores its display time entirely** and stays up until its sidecar audio **ends**, then fades out and closes. `max_duration` still applies (and wins over a long audio track). No sidecar audio = no effect — the normal display timer runs. |
| `fade_out_seconds` | any number >= 0 (seconds) | Override for the global `fade_out_seconds` — how long the fade-out lasts. Image-only; ignored by videos. The old name `fade_seconds` is accepted as a deprecated alias (a warning asks you to rename it). |
| `fade_in_seconds` | any number >= 0 (seconds, default `0`) | Override for the global `fade_in_seconds` — the image/GIF fades in from transparent **before** the display clock starts, so total lifetime = fade_in + display + fade_out. `0` = appears instantly. Image/GIF-only; ignored by videos. |
| `volume` | 0.0 – 5.0 (default `0.8`) | Volume/gain for that file (multiplied with the global `volume`). `1.0` = 100% (same as global), `2.0` = twice as loud. Values above 1.0 boost; final = `global * per-file`, capped at 5.0. |
| `weight` | any number >= 0 (default `1`; floats allowed) | How likely this media is **picked** by the random trigger (per-file; works in **any** `mode`). `2` = twice as likely as a weight-`1` file, `0.5` = half as likely. `0` = **never picked** (a warning is logged: "…will NOT show (never picked)"). The chance is `weight / total_weight` across all media — e.g. 3 files where one has `weight: 2` → that one gets 2/4 = 50%, the other two 1/4 = 25% each. If every file has weight `0`, nothing is picked at all. |
| `speed` | any number > 0 (default `1`) | Playback speed multiplier for this file: video + its audio, GIFs, sidecar audio, **and** image display time + fades (all timings scale by 1/speed). `2` = twice as fast, `0.5` = half. With `pitch=1` the audio speeds up tape-style (pitch rises with speed); with `pitch != 1` speed and pitch are independent (audio is re-encoded via ffmpeg). **Use string format for ranges:** `"1.0~2.0"`. |
| `pitch` | any number > 0 (default `1`) | Audio pitch multiplier for this file: sidecar audio and the audio of videos (the embedded track is extracted + re-encoded). `2` = an octave up, `0.5` = an octave down. Requires ffmpeg; independent of `speed`. **Use string format for ranges:** `"0.5~1.5"`. |
| `speed_pitch` | any number >= 0 (default `0` = off) | Sets **both** speed and pitch at once (`speed = pitch = this value`, e.g. `1.5` = 1.5× speed AND 1.5× pitch). When set (per-file > global) it **overrides** the individual `speed`/`pitch` values — handy for randomizing both together later. `0`/absent = use `speed` and `pitch` separately. **Use string format for ranges:** `"1.0~2.0"`. |
| `max_playback_height` | any number >= 0 (px, default `0` = no cap) | Per-file override of the global `max_playback_height`: cap the height THIS file is decoded/chroma-keyed/copied/painted at (videos + GIFs; still images ignore it). Taller sources are downscaled keeping aspect **before** processing — stable playback for high-res clips at the cost of a little softness. Changing it rebuilds that file's precache. |
| `max_playback_fps` | any number >= 0 (fps, default `0` = no cap) | Per-file override of the global `max_playback_fps`: cap the effective playback framerate (videos only — OpenCV/chroma/AV1 paths). Sources above it are frame-sampled (every Nth frame presented); duration and audio are untouched. Changing it rebuilds that file's precache. |
| `max_duration` | any number >= 0 (seconds, default `0`) | Hard cap for this file. When the timer runs out, the video/image/gif **and** its sidecar audio stop **immediately** and the overlay closes **instantly — no fade-out**. `0` = no cap (play naturally). Setting it smaller than `image_display_seconds` truncates the image display; smaller than a video's length cuts the video off early. Per-file wins over the global `max_duration` — use `0` per-file to disable a global cap for one file. **Use string format for ranges:** `"1.0~5.0"`. |
| `chroma` | `true` / `false` (default: follow the global `chroma_key`) | Per-file override of the green/blue-screen removal. `false` = **skip keying** even when the global `chroma_key` is on (use it for assets that already have real alpha, or for videos that aren't green-screen at all — they'll also get faster, normal playback). `true` = force keying on this file. |
| `chroma_key` | PRESET: `"green"` (default) | `"blue"` | `"weak green"` | `"strong green"` | `"weak blue"` | `"strong blue"` | `"custom"` | Per-file chroma-key preset (each file its own key, independent of global). `"custom"` ignores the presets and uses the per-file range keys below instead. Setting a preset turns keying ON for that file; `chroma: false` always wins. |
| `chroma_hue_range` | `[lo, hi]` (default `[35, 85]`) | | | Hue range 0..179 (OpenCV scale; green ~60, blue ~120). **Only used when `chroma_key` is `"custom"` — ignored for every other preset.** |
| `chroma_saturation_range` | `[lo, hi]` (default `[40, 255]`) | | | Saturation range 0..255. **Only used when `chroma_key` is `"custom"`.** |
| `chroma_value_range` | `[lo, hi]` (default `[40, 255]`) | | | Value range 0..255. **Only used when `chroma_key` is `"custom"`.** |
| `play_once` | `true` / `false` (default `false`) | | | true = this media won’t be picked again while any copy of it is already playing. false = repeats are allowed. Per-file value overrides the global `play_once`. |

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
   independently with `scale_x/scale_y` (relative to the fit size) — or
   uniformly with a single `scale` key (sets BOTH X and Y; overwritten
   when `scale_x` AND `scale_y` are both given explicitly) —
   mirror it with `flip_h/flip_v`, and rotate it with `rotation` (degrees
   around its center). The custom values only apply when `mode` is `custom`.
3. **Images:** shown for `image_display_seconds` (or sidecar `duration`),
   then fade out over `fade_out_seconds` (the overlay opacity animates
   `opacity`→0 while any sidecar audio keeps playing). With `fade_in_seconds`
   set, the image first fades in from transparent (opacity 0→`opacity`)
   **before** the display clock starts — total lifetime = fade_in + display +
   fade_out. With `speed` (or `speed_pitch`) set, the display time and both
   fades scale — divided by speed (2× speed = half the display + fade times).
   Fades always run **from/to your configured `opacity`** and take exactly the
   configured time to get there (a `0.4` opacity with a 2 s fade-in spends the
   full 2 s going 0→0.4, never overshooting to 1.0).
4. **Videos:** played to the end via QtMultimedia (audio + modern codecs),
   then fade out over `fade_out_seconds` (opacity `opacity`→0).

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

**Audio:** videos play their own audio track (respecting `volume` and
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

### Kill switch (dead man's switch)

Press `kill_hotkey` (default **`ctrl+shift+alt+k`**) at any time and DYST
**terminates immediately** — the safety net for when overlays go haywire and
the tray is unreachable. Set `kill_hotkey` to `""` to disable it.

When `kill_notify` is `true` (default), a **Windows notification** pops up
right after the hotkey fires — *"Kill switch pressed (ctrl+shift+alt+k).
Overlays stopped, app closed."* — so it's clear the app vanished on purpose
and how to get it back. The balloon is drawn by a tiny detached helper
process (`dyst/notify.py`), so it stays on screen even though DYST has
already exited. Set `kill_notify` to `false` to quit silently.

The hotkey is detected twice, so it works even when DYST is wedged:

- **in-process** (`dyst/hotkey.py`, a 50 ms Qt poll) fires while the app is
  healthy and quits it normally;
- **out-of-process** (`dyst/killswitch.py`, a 50 ms Win32 poll in a tiny
  detached helper process) is immune to a frozen GUI thread. It waits ~1.5 s
  for the app to quit on its own — the in-process poller usually gets there
  first — and then **force-terminates it** (`TerminateProcess`), which works
  on a fully hung process. The helper exits by itself as soon as DYST is
  gone, so a normal quit never leaves it behind.

So a stuck overlay can always be cleared with the hotkey, even if the app is
not responding at all.

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

Run the generator to create a test PNG and a test MP4 (`scripts/make_test_asset.py`).
The green-screen helper clip it generates is used by `scripts/test_chroma.py`
to verify the chroma-key + cache pipeline.

---

## Chroma key & the preprocessing cache (videos)

Green/blue-screen videos are keyed **once** per (file × settings), cached into
`.cache/precache/` (memmapped per-frame alpha masks + a one-time extraction
of the video's audio), and played back from the cache so even high-fps clips
stay smooth (~2–5 ms/frame of mask work; playback paces to a steady rate,
so a clip that is only decode-fit for e.g. 30 fps shows ~30 fps instead of
freezing). Images and GIFs are keyed the same way at load time.

The first trigger of an uncached chroma video pauses the chance loop while
it is preprocessed on a worker thread (one time only); `--play`/`--test`
build the cache before showing. Delete the `.cache/` folder to clear
everything; the cache is invalidated automatically when the media file or
the `chroma_key` settings change (toggling `despill` does NOT rebuild — it
is applied at playback).

Key behaviour:
- Applies to the whole pool while `chroma_key` is on — a per-file
  `"chroma": false` sidecar skips it (e.g. assets with real alpha, or
  non-green-screen videos you'd rather play on the fast path).
- The hue window is auto-re-centred on each video's actual screen colour at
  cache-build time (`despill` — edge de-greening — is on by default), so
  most clips just work with `"chroma_key": "green"`.
- A sidecar audio file always wins over the video's own audio.
- The first ~half-second of some clips is all green screen — that is the
  video's content, not a bug (the overlay is just invisible then).
- Keying quality depends on the footage: even lighting, no green on the
  subject, non-green shadows.

---

## Logging

Everything is logged to **`app.log`** next to the app (and the console). Set
`"debug": true` in `config.json` for verbose per-roll detail.

---

## Building & packaging

Build a standalone `DYST.exe` with the bundled icon:

```
.venv\Scripts\python build.py            # full build + stage config/media
.venv\Scripts\python build.py --skip-copy # build only
```

`build.py` runs PyInstaller via `DYST.spec`, converts `icon.webp` to `icon.ico`
(multi-size) at build time, then copies `config.json` and `media/` into
`dist/DYST/` beside the exe so it stays user-editable (nothing bundled).
The exe is a console-subsystem binary (`show_console` config toggles it).

Result: `dist/DYST/DYST.exe` (~260 MB with dependencies).

---

## Recent Changes

- **Fixed image closing prematurely**: Updated `_close_if_ready` in `dyst/overlay.py` to require that the visual media has finished displaying (`_visual_done`), the fade-out animation has completed (`_fade_done`), and the audio has finished (`_audio_done`) before closing the overlay. This prevents the overlay from closing early when the side‑car audio is shorter than the configured display time.

- **Fixed image centering**: Changed the `mode` in `media/images/woolly-mammoth.json` from `"cover"` to `"fit"`. The `"fit"` mode preserves the aspect ratio and centers the image within the window (adding letter‑boxing if needed), ensuring the image appears in the middle.

## What's coming (see PROGRESS.md for details)

- **Phase 2** — media validation (skip corrupt files), sidecar audio files ✅ done
- **Phase 3** — chroma key (green-screen removal) ✅ done (despill on by default, hole-filling, auto hue calibration, one-time cached audio)
- **Phase 4** — overlay polish (GIF/APNG animation, monitor selection)
- **Phase 5** — overlay manager (global max concurrency) + full audio
- **Phase 6** — tray icon, autostart, test-trigger menu
- **Phase 7** — packaging + this doc becoming the real user README
