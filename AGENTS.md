# AGENTS.md — DYST (did you see that? 👀)

Project specification and agent instructions for building **DYST (did you see that? 👀)**, a Windows
background "scare/funny mod" app inspired by Steam Workshop mods (e.g., 1/1000 chance of a
skeleton running across your screen every second).

> Status: **SPEC v1** — locked after Q&A. Any future change to these decisions must be
> confirmed with the user before implementation.

---

## 1. What the app must do (locked requirements)

1. Runs **in the background** on Windows; appears **only as a hidden tray icon** for
   configuration. No main window, ever.
2. A timer rolls a chance every **tick** (default **1 second**, user-editable).
3. On a successful roll, it picks a **random media file** and plays it **fullscreen over
   whatever is on screen** — with no new taskbar button, no default window chrome, no
   activation stealing focus, and clicks passing through.
4. Overlays may **overlap** (multiple at once). On a successful roll, **re-roll immediately
   within the same tick** — one second can produce several media plays at once (burst).
5. Media lives in a **user-customizable folder** so users can drop in their own content.
6. Media may optionally have a **green screen (chroma key) filter** applied at runtime so
   users can supply green-screen footage and have the green removed automatically, like in
   a video editor.
7. Settings are stored in a hand-editable `config.json` next to the app. (A GUI settings
   window may replace this in a future version — noted for extensibility.)
8. Optional **autostart with Windows login** (silent boot into tray), toggled from the tray
   menu (stored in Windows Registry `Run` key).
9. Tray menu includes a **manual "Test Trigger"** item to preview media on demand.

## 2. Locked decisions (from Q&A — do not silently change)

| Topic | Decision |
|---|---|
| Stack v1 | **Python 3.11+** prototype (easiest first). C#/.NET is the documented future port (see §5). |
| Odds model | Global probability `1 in N` (default **N=1000**), applied every tick, then random pick among media. |
| Tick | `tick_seconds` (default **1.0**), user-editable. |
| Media types | Images (PNG/GIF/APNG/WebP with alpha) **and** videos (MP4/WebM/AVI). Start with videos to validate transparency. |
| Overlay placement | One monitor only (configurable; default primary). Fullscreen. **Fit** (preserve aspect ratio, no crop) for both images and videos. |
| Image duration | `image_display_seconds` (default **1.0**). |
| Video duration | Until the media ends. |
| Dismissal | **Auto-only** (after image timer or video end). No click-to-close. Windows are click-through. |
| Audio | **Sidecar audio wins** ("layered audio if it exists"): a matching audio file in the same folder (e.g. `scare.mp3` beside `scare.png`). If no sidecar and it's a video with its own audio track → play the video audio. Otherwise silent. |
| Cooldown | **None.** Raw odds every tick; unpredictability is the point. |
| Reroll | On success, **re-roll immediately in the same tick** until a fail or `max_concurrent` cap. |
| Concurrency | Overlap allowed. Cap = **`max_concurrent` (default 3, 0 = unlimited)**. |
| Firing window | Anytime the PC is on / app is running. No idle detection. |
| Config UI | v1: none — hand-edit `config.json`. Tray handles quick toggles (autostart, test trigger, quit). |
| Config file | `config.json` beside the app. |
| Autostart | Registry `Run` key; silent boot straight to tray. |
| Media layout | `media/images/` and `media/videos/` subfolders; each media file's audio sidecar lives **in the same subfolder** (avoids name collisions between images and videos). |
| Per-file settings | Optional same-named `.json` (or `.txt`) sidecar next to the media overrides display: `mode` (`fit`/`cover`/`stretch`), `duration` (image seconds), `volume` (0–1). Overrides win over global config. |
| Chroma key | Global on/off (default **on**), applies to both images and videos, with a per-file exception list. |
| Layered audio precedence | Sidecar file wins → else video's own audio track → else silent. |

## 3. Tech stack (Python v1) and why

| Purpose | Library | Notes |
|---|---|---|
| Overlay windows | **PySide6 (Qt6)** | `Qt.FramelessWindowHint`, `Qt.WindowStaysOnTopHint`, `Qt.Tool` (no taskbar/Alt-Tab), `WA_TranslucentBackground` (per-pixel alpha), `WA_ShowWithoutActivating` (no focus steal), `WA_TransparentForMouseEvents` (click-through). |
| Tray icon | **pystray** | Right-click menu: Test Trigger, Startup toggle, Open Media Folder, Quit; tooltip shows status. |
| Chroma key (images + video frames) | **opencv-python** | HSV green mask → blur/feather edges → optional despill → use as alpha. Same pipeline for stills and every video frame. |
| Video decode (when filtering) | opencv-python (`VideoCapture`) | Chroma key forces us to decode frames ourselves — libVLC cannot chroma-key. |
| Images / animations | **Pillow** | Load PNG alpha, decode GIF/APNG/WebP frames. |
| Audio | **PySide6 (QtMultimedia)** | `QMediaPlayer` for sidecar/video audio; volume from config. |
| Config | stdlib `json` | Load with defaults merge; validate types; write back only for autostart changes. |
| Autostart | stdlib `winreg` | Add/remove `HKCU\Software\Microsoft\Windows\CurrentVersion\Run` value. Guarded by `os.name == "nt"`. |
| Logging | stdlib `logging` | `app.log` beside the app; `debug` flag in config. |

**Fallback path without chroma key:** for videos played with the filter off, VLC
(`python-vlc`) direct playback is a documented future optimization to save CPU — **not** in v1.

### Why PySide6 specifically
Qt's translucent-window support is the entire hard part of this app in ~5 lines of flags —
no ctypes surgery. OpenCV handles both stills and video frames with one function pipeline.
Together they keep v1 to ~4 dependencies.

### Linux / cross-platform note
pystray + PySide6 do run on Linux, but transparent topmost overlays depend on a compositor
and are unreliable on Wayland; autostart and config paths differ per distro. **Windows is
the only supported target.** Do not spend effort on Linux parity.

## 4. Transparency realities (constraints that apply in ANY language)

- **Regular MP4 can never be transparent.** Transparent *video* requires WebM/VP9-alpha,
  OR green-screen footage + chroma key. This is a content constraint, not a code one.
- Transparent **images**: PNG/APNG/GIF/WebP with alpha channel — trivial.
- Chroma key only looks good if the footage is decently shot: **even lighting, no green on
  the subject** (green clothing = holes), non-green background objects. Document this for
  users; the code can only do so much.
- CPU-bound: chroma key runs per frame in Python. Expect 720p/1080p short clips to be fine;
  long 4K clips will be slow. Document for users.

## 5. C#/.NET future port (documented, not built now)

**Why C#/.NET (WPF) is preferable/better for this app than Python** (recorded for the future port):

- Layered/per-pixel-alpha windows are first-class Win32 (`WS_EX_LAYERED`,
  `WS_EX_TOOLWINDOW`, `WS_EX_NOACTIVATE`) — no Qt-borrowing for click-through/hide-from-taskbar.
- Fast startup, low RAM with multiple overlapping videos, no interpreter.
- Single-file publish (`dotnet publish -P:PublishSingleFile`), no PyInstaller AV false positives.
- GPU chroma key via an **HLSL pixel shader** (`WPF Effect`) instead of CPU OpenCV.
- Trade-offs: Windows-only (`net8.0-windows`), slower to prototype, needs .NET SDK,
  tray icon comes from WinForms `NotifyIcon` (or a lib) since WPF ships none,
  more boilerplate overall.

**Port plan (when requested):** mirror the same module layout (§7) 1:1; the core logic is a
timer + queue + config and ports mechanically. Keep `config.json` schema identical so both
implementations share user assets.

## 6. Runtime behavior rules (implementation must obey)

### Trigger loop
- `QTimer` (or equivalent) at `tick_seconds`. Each tick:
  1. Roll: `random.random() < 1 / odds` → success?
  2. On success: pick random media from the merged pool (images + videos), spawn overlay,
     then **immediately re-roll** in the same tick (loop) until a fail, or
     `max_concurrent` active overlays reached (0 = unlimited).
  3. No cooldown, no suppression while another plays (overlap allowed).
- Invalid/skipped files do not count as a success.

### Overlay window (every overlay is its own window)
- One QWidget per playback, on the configured monitor, screen-sized.
- Flags: frameless, always-on-top (`WindowStaysOnTopHint` **and** re-assert on show),
  `Tool` window type (no taskbar icon, hidden from Alt-Tab), translucent background,
  show-without-activating, transparent-for-mouse.
- Render: **fit** (preserve aspect ratio, letterbox, centered) by default; per-file
  `mode` sidecar can set `cover` (fill + centered crop) or `stretch` (fill, squish).
- Images: show `image_display_seconds` (or per-file `duration`), then **fade out**
  (`fade_seconds`, default 0.2) and close.
- Videos: QtMultimedia playback by default (audio + modern codecs); an OpenCV
  frame-decoder path exists for future chroma. Videos **end instantly** when the
  media ends (no fade-out — locked user decision).
- Audio: start in parallel with the visual; stop + release on window close.
- Cleanup: destroy widget + stop audio + remove from active set on completion;
  `max_concurrent` counts only currently-playing overlays.

### Chroma key pipeline (config key `chroma_key`)
- Global `enabled` (default `true`), applies to images **and** video frames.
- Per-file exception: `chroma_key.exceptions` = list of filenames to skip (played as-is,
  must then have real alpha).
- Defaults: HSV green range (~`(35, 40, 40)–(85, 255, 255)`, tunable), 3–7px blur +
  slight erosion on mask, optional despill flag default off.
- Output: RGBA frame/image used as the overlay's pixmap source.

### Audio precedence (locked)
1. Sidecar audio file in the **same subfolder** as the visual, matched by base filename
   (case-insensitive, any of `.mp3/.wav/.ogg/.flac/.m4a` preferred order `.mp3 .wav .ogg`).
2. Else video's own embedded audio track (if the video has one).
3. Else nothing.
- `audio_volume` (0.0–1.0, default 0.8) applies to all playback.

**Status:** implemented (Phase 2). Sidecar audio also plays over **images**
and replaces extracted audio for **AV1** videos (no temp file needed). Videos
with a sidecar mute their embedded track (dual-player approach).

### Tray icon
- Tooltip: app name + status (e.g., "running, 2 overlays").
- Menu: **Test Trigger** (spawn a random overlay immediately), **Media Folder** (open
  folder in Explorer), **Start on login** (checkbox ↔ registry Run key), **Quit**.
- No tray menu item that opens a settings window in v1.

### Autostart
- Registry: `HKCU\Software\Microsoft\Windows\CurrentVersion\Run` →
  `DYST` = quoted path to app executable/boot script.
- Silent boot into tray; never show a window at startup.

## 7. Folder structure

```
app_root/                     (e.g. C:/Users/ahmed/Downloads/Pi/)
├── AGENTS.md
├── PLAN.md
├── PROGRESS.md               (overall progress tracker — MUST update after each phase)
├── README.md                 (user/how-it-works docs — config, modes, CLI, troubleshooting)
├── main.py                   (entry point; guard via __main__)
├── run.bat                   (windows launcher: activates venv, runs main.py)
├── config.json               (user-editable; see §8)
├── video-urls.txt            (YouTube/URL list for scripts/download_videos.py)
├── app.log                   (created at runtime)
├── dyst/                     (package)
│   ├── __init__.py
│   ├── config.py             (load/validate/defaults)
│   ├── ticker.py             (timer + odds + reroll logic)
│   ├── media.py              (folder scanning, validation, sidecar pairing)
│   ├── chroma.py             (OpenCV chroma key: images + frames)
│   ├── overlay.py            (Qt translucent overlay widget)
│   ├── manager.py            (overlay lifecycle, max_concurrent, audio)
│   ├── tray.py               (pystray icon + menu)
│   └── autostart.py          (winreg Run-key helpers, Windows-only guard)
├── media/
│   ├── images/               (png, gif, apng, webp) + sidecar audio + settings here
│   └── videos/               (mp4, webm, avi) + optional sidecar audio + settings here
└── scripts/
    ├── make_test_asset.py    (generates a green-screen test clip, dev use)
    ├── download_videos.py    (downloads URLs from video-urls.txt into media/videos)
    ├── test_phase0.py        (config loader unit checks, dev use)
    ├── test_phase1.py        (overlay + ticker checks, dev use)
    └── run_dev.bat           (create venv, install deps, launch)
```

## 8. `config.json` schema (v1)

```jsonc
{
  // Core loop
  "tick_seconds": 1.0,          // seconds between rolls (must be > 0)
  "odds": 1000,                 // 1 in N chance per tick (must be >= 1)
  "max_concurrent": 3,          // max overlays playing at once; 0 = unlimited
  "reroll_in_same_tick": true,  // locked true in v1 (kept for forward-compat)

  // Media
  "media_folder": "media",      // relative to app root
  "image_display_seconds": 1.0, // how long a still image stays up
  "fade_seconds": 0.2,          // fade-out duration at end of playback

  // Display
  "monitor": "primary",         // "primary" or 0-based index of a monitor

  // Audio
  "audio_volume": 0.8,          // 0.0–1.0

  "download_max_height": 1080,  // max video height (px) for the downloader

  // Chroma key
  "chroma_key": {
    "enabled": true,
    "exceptions": [],           // filenames that skip chroma key (must have alpha)
    "hue_range": [35, 85],      // optional tuning; defaults if absent
    "saturation_range": [40, 255],
    "value_range": [40, 255],
    "despill": false            // remove green spill on edges; default off
  },

  // Misc
  "autostart": false,           // synced with tray toggle + registry
  "debug": false                // verbose logging to app.log
}
```

Loading rules: missing keys → use defaults above; malformed values → log a warning and use
defaults; never crash the app on config errors.

## 9. Media authoring guide (for end users — display in README later)

- **Videos**: green-screen clips work best (chroma key removes the green). Shoot with
  even lighting, no green on the subject. MP4 works when chroma key is on; WebM/VP9-alpha is
  the only way to get *direct* transparency (chroma off) — note: v1 requires chroma key ON
  for normal MP4/AVI to appear transparent, otherwise you'll see a full rectangle.
  Best-quality YouTube downloads are AV1 — software-decode fallback shows video but drops
  audio on machines without AV1 hw-decode.
- **Images**: transparent PNG/APNG/GIF/WebP are displayed as-is. A green background image
  also works (chroma key applies to images too).
- **Per-file display settings** (optional): put a same-named `.json` (or `.txt`) next to a
  media file, e.g. `scare.mp4` + `scare.json`:
  ```json
  { "mode": "cover", "duration": 3, "volume": 0.8 }
  ```
  - `mode` × `fit` (default, letterbox) · `cover` (fill+center-crop) · `stretch` (fill+squish)
  - `duration` — overrides `image_display_seconds` for images (video ignores it)
  - `volume` — overrides global `audio_volume` (0–1); ignored with no audio
  - TXT form (one per line): `mode=cover`, `duration=3`, `volume=0.8` (also `key: value`)
  Unknown keys / bad values are dropped with a warning, never fatal.
- **Audio sidecar**: same base name in the same subfolder, e.g. `scare.png` + `scare.mp3`
  or `boo.mp4` + `boo.wav`. Sidecar always wins over video's own audio.
- If a media file is corrupt/undecodable → skip it, log a warning, keep running.

## 10. Development environment & tooling (Windows)

- OS: **Microsoft Windows (x64)**. Commands run via `cmd`/PowerShell/this agent's bash.
- Python: use **`python`** (never `python3`; our bash tool needs forward slashes in paths).
- Setup: `python -m venv .venv`, activate, `pip install PySide6 pystray opencv-python pillow`.
- Build deps into `requirements.txt` (add when created).
- Verify after any change: `python -m py_compile <files>`; run smoke tests, then
  `QT_QPA_PLATFORM=offscreen` GUI sanity checks. Manual tray/overlay test on real desktop.
- Do not commit `.venv/`, `__pycache__/`, `app.log`, or build outputs.

## 12. Reporting & progress tracking (MANDATORY for every agent)

Every agent that works on this project MUST follow these rules. They exist so any future
agent (or the user) can pick up the project without re-discovering context.

### 12.1 Phase completion reports

After completing work on **any phase** (or any significant chunk of work), the agent MUST
deliver a report covering, in order:

1. **Work done** — what was built/changed in that phase, files touched.
2. **Functions added** — every new public function/class, its signature and purpose.
3. **Why** — the reasoning for the design choices made (tie back to AGENTS.md decisions where
   possible; note any deviation and why).
4. **How they work** — a concise walkthrough of the flow/logic of the new code.
5. **Recap** — a short summary of everything complete so far, and what the next phase is.

### 12.2 PROGRESS.md (single source of truth for overall state)

`PROGRESS.md` in the app root is the **overall progress tracker**. After finishing any
phase, the agent MUST update it. It must contain at minimum:

- Project status summary (phase currently underway / completed).
- Per-phase table: phase, tasks, status (not started / in progress / done / verified).
- What has been implemented and **tested/verified** (with the command/results).
- Known limitations and open questions.
- Next steps (concrete).
- Environment notes (venv path, deps installed, how to run).

Keep it updated cumulatively — it should always reflect reality, never just the last phase.

### 12.3 Rules of thumb

- Never claim a phase is "done" unless its verification steps (PLAN.md §2) pass.
- Log verification commands and their results in PROGRESS.md, not just in chat.
- If a decision deviates from AGENTS.md, flag it in PROGRESS.md and to the user — do not
  silently change locked decisions.

- Never delete any .md files (e.g., AGENTS.md, PLAN.md, PROGRESS.md, README.md) from the project without explicit user confirmation. If a file must be removed, first consult the user and obtain a clear confirmation.

## 11. Definition of done (v1)

- App boots silently into tray on Windows; configurable odds/tick via `config.json`;
  test trigger works from tray.
- Successful roll spawns a fullscreen, click-through, chrome-less, topmost overlay
  (image with timer or video to completion), with fade-out, per locked audio precedence.
- Green-screen media has its green removed; exceptions list honored; images and videos both.
- Reroll/burst works up to `max_concurrent`; overlays overlap correctly.
- Autostart toggle works (registry), silent boot.
- `app.log` records rolls, plays, skipped files, errors; `debug` flag adds detail.
- `make_test_asset.py` produces a verified green-screen test clip + a test PNG.