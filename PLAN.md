# PLAN.md — DYST (did you see that? 👀) implementation plan

Build order, module map, verification steps, and roadmap for the **DYST (did you see that? 👀)** app.
Read `AGENTS.md` first: it is the locked spec this plan implements.

**Target:** Windows, Python 3.11+, PySide6 + pystray + opencv-python + Pillow.
**Working dir:** `C:/Users/ahmed/Downloads/Pi/`.

---

## 0. Principles

- **Incremental, always-runnable:** after every phase the app still launches (tray only)
  and we can smoke-test with a synthetic trigger (`--test` CLI flag driving the same code
  path as a real roll).
- **No over-engineering:** implement exactly the locked spec; leave VLC/GUI-settings/C#
  port as documented future work, not code.
- **Verify each phase** with the listed checks before moving on. Use `python -m py_compile`
  after every edit; our agent environment runs `python` (never `python3`) and bash paths use
  forward slashes: `cd "C:/Users/ahmed/Downloads/Pi"`.

## 1. Module map (mirrors AGENTS.md §7)

| File | Responsibility |
|---|---|
| `dyst/config.py` | load/validate/merge `config.json` with defaults |
| `dyst/ticker.py` | timer + 1-in-N roll + same-tick reroll loop |
| `dyst/media.py` | scan folders, validate files, sidecar pairing, random pick |
| `dyst/chroma.py` | OpenCV HSV green mask → RGBA for images and video frames |
| `dyst/overlay.py` | single Qt translucent topmost overlay widget |
| `dyst/manager.py` | spawn/track overlays, `max_concurrent`, audio binding |
| `dyst/tray.py` | pystray icon + menu (Test, Media Folder, Autostart toggle, Quit) |
| `dyst/autostart.py` | winreg Run-key helpers (Windows-only, guarded) |
| `main.py` | entry point: load config → start tray + ticker; `--test` flag |
| `scripts/make_test_asset.py` | generate green-screen test clip + test PNG |
| `scripts/run_dev.bat` | create venv, install deps, launch |

## 1a. Playback spike — DONE (user priority, 2025)

Per user request, bare-minimum media playback was built first to start testing
images/videos on screen before any fancier work. Implemented:
- minimal `dyst/overlay.py` — frameless topmost translucent click-through window;
  image timer or OpenCV video frame loop; fade-out; `finished` signal.
- minimal `dyst/media.py` — extension scan of `media/images` + `media/videos`,
  random pick, `kind_of`.
- minimal `dyst/ticker.py` — spec-compliant roll + same-tick burst, capped per tick.
- `main.py` modes: `--test` (random media), `--play PATH`, `--daemon` (chance loop,
  no tray), `--roll` (headless).
- `scripts/make_test_asset.py` (test PNG + green-bg MP4), `scripts/test_phase1.py`
  (6 automated checks), and `scripts/download_videos.py` (mass downloads from
  `video-urls.txt` into media/videos via yt-dlp, no audio extraction).

### 1b. Per-file display settings + best-quality downloads (user request)

- Downloader now uses `-f bv*+ba/b` = **highest quality available** (AV1/VP9),
  not H.264.
- Added **same-named settings sidecar** support: `<media>` + `<media>.json` or
  `<media>.txt` (=KEY `mode|duration|volume`, `key:value` allowed) overrides how
  media is displayed. `mode`: `fit` (default) | `cover` (fill+crop) | `stretch`
  (fill+squish). Implemented in `dyst/media.py` (load_settings) + `dyst/overlay.py`
  (paint modes) + `main.py` (spawner passes settings).
- Added **AV1 hw-decode watchdog**: if QtMultimedia yields no valid video frame ~1.5s
  after buffering (hardware AV1 unsupported -> audio only), overlay auto-falls back
  to OpenCV software decode so video still shows (audio lost, logged).

NOT yet (later phases): file validation (P2), chroma key (P3), overlay
polish/monitor selection (P4), audio/sidecar + manager concurrency (P5),
tray/autostart (P6), packaging (P7).

## 2. Phases

### Phase 0 — Scaffolding (½ day)
Tasks:
- `python -m venv .venv`; activate; install `PySide6 pystray opencv-python pillow`;
  write `requirements.txt`.
- Create package skeleton: `dyst/__init__.py` (empty), stub modules,
  `scripts/run_dev.bat`.
- `config.py`: defaults dict (exactly AGENTS.md §8), `load_config(path)` with missing-key
  merge, type/value validation (tick>0, odds>=1, volume 0–1, monitor ok), warning + default
  on error. `save_config(path, cfg)` for autostart sync.
- `main.py` stub: parse `--test`, load config, print status, exit.

**Verify:** `python -m py_compile` all files; `python main.py` prints loaded config with
and without a `config.json` present; malformed config logs warning, keeps defaults; exit 0.

### Phase 1 — Core loop (ticker) (½ day)
Tasks:
- `ticker.py`: class `Ticker(cfg, on_trigger)` using `QTimer` at `tick_seconds`.
  `roll()` = `random.random() < 1/odds`.
  On success → call `on_trigger(pick)` then **immediately re-roll** in the same tick until
  a fail or hit `max_concurrent` (0 = unlimited). Keep tick count and a simple log line per
  roll (debug: rolls logged only with `debug: true`).
- Media selection is injected via a callable so we can unit-test with a fake picker.

**Verify:** unit test with odds=1 and max_concurrent=3 → 3 triggers then stop; odds=1000000
→ ~0 triggers; odds string "abc" → defaults + warning. `python -m py_compile`.

### Phase 2 — Media scanning & pairing (½ day)
Tasks:
- `media.py`: `scan(media_folder)` → two lists: `images` (png/gif/apng/webp),
  `videos` (mp4/webm/avi), case-insensitive, recursive not required (flat subfolders).
- Validation: open/decode header (Pillow for images, OpenCV `VideoCapture` probe for
  videos); undecodable → log skip, exclude from pool. Cache results; rescan on
  `--rescan`/manual call (menu item later or debug flag).
- Sidecar pairing: for each media file, look in **same directory** for base-name match
  (case-insensitive) with audio ext precedence `.mp3 > .wav > .ogg > .flac > .m4a`.
  Store `MediaItem(path, kind, sidecar_audio|None)`.
- `pick_random(images, videos)` respecting config: one merged pool, uniform random.
- Handle empty pools: log warning; still run (tray tooltip shows "no media").

**Verify:** create temp folders with valid/invalid/alpha images and a green clip from
Phase-0 script; assert only valid items + correct sidecars in pool; empty-folder warning;
`python -m py_compile`.

### Phase 3 — Chroma key module (1 day)
Tasks:
- `chroma.py`: `chroma_key_image(img: PIL.Image, params) -> PIL.Image (RGBA)`: convert to
  NumPy BGR, HSV mask via `inRange` on `hue_range/saturation_range/value_range` (defaults
  from config), `GaussianBlur(3–7px)` + slight `erode` on mask, `bitwise_and` to keep
  subject, mask → alpha channel, return RGBA.
- `chroma_key_frame(frame_bgr, params) -> RGBA` — same pipeline for a video frame.
- `despill` flag (default off): if on, reduce green channel of edge pixels (simple
  despill pass, documented as basic).
- Exceptions honored by caller (manager), not here: `chroma_key.enabled` false or filename
  in `exceptions` → skip filter.
- Performance guard: measure per-frame ms; log warning if > 50ms/frame for 720p (so we can
  add a `scale_down` future option).

**Verify:** `scripts/make_test_asset.py` produces a 640x360, 3s clip: solid green
background + moving red square + embedded silent audio? (make_test_asset writes no audio —
sidecar test uses a generated tone wav). Assert output RGBA has ~0 alpha where green,
~255 where subject; sample a few pixels; `python -m py_compile`.

### Phase 4 — Overlay window (1 day)
Tasks:
- `overlay.py`: single `OverlayWindow(QWidget)` per playback.
  - `setWindowFlags(FramelessWindowHint | WindowStaysOnTopHint | Tool)`
  - `setAttribute(WA_TranslucentBackground)`; `WA_ShowWithoutActivating`;
    `WA_TransparentForMouseEvents` (click-through, locked auto-dismiss).
  - Geometry = screen rectangle of configured monitor (`QScreen`/`QGuiApplication`),
    call `raise_()`/`show()` with `WindowStaysOnTopHint` re-asserted after show.
  - Fit scaling: pixmap/`QImage` fitted into window with `KeepAspectRatio`, centered;
    images paint via `paintEvent`; videos drive a `QTimer` ~30–60 fps painting the current
    RGB frame (chroma-applied RGBA for filtered clips).
  - Audio: manager plays audio (see Phase 5) — overlay only handles visuals; on
    `finished` emit signal → manager stops audio, fades `windowOpacity` 1→0 over
    `fade_seconds`, then closes + deletes.
  - Image path: show `image_display_seconds` then finish.

**Verify:** `QT_QPA_PLATFORM=offscreen` instantiate overlay with a red-100x100 RGBA image;
assert flags/attrs set, geometry equals monitor rect, no exception in paint.
Manual check on real desktop (phase 7 CLI): overlay appears fullscreen, no taskbar entry,
doesn't steal focus, cannot be clicked.

### Phase 5 — Manager & audio (1 day)
Tasks:
- `manager.py`: `OverlayManager(cfg, media_pool)`.
  - `spawn(MediaItem)`: respect `max_concurrent` (active count); create OverlayWindow;
    bind audio: sidecar (QMediaPlayer → `QAudioOutput`, set volume cfg) else video embedded
    audio (OpenCV frame decoding has no audio — so in v1, embedded audio only works for
    non-chroma direct-path? **Decision per spec §6:** chroma-on videos play sidecar; video
    with no sidecar and chroma on → silent until VLC fallback path in future. For the
    present: decode audio from the video file is out of scope v1; instead use sidecar-only
    + document.). Implement as spec says: sidecar → play; else none (v1 limitation
    documented, matches "easiest first").
  - On overlay `finished` → fade (overlay does it) → stop/release QMediaPlayer, remove from
    active set.
- Autorescan hook optional: manager exposes `refresh()` called by tray/debug.

**Verify:** unit: spawn 5 with cap 3 → active set stays ≤3, others dropped with log;
`media_item_finished` path releases audio; py_compile.

### Phase 6 — Tray + autostart (½ day)
Tasks:
- `tray.py`: pystray Icon with menu:
  - **Test Trigger** → `manager.spawn(pick_random(...))` (same code path as real roll).
  - **Open Media Folder** → `os.startfile(media_folder)`.
  - **Start on login** checkbox ↔ `autostart.py` registry Run value.
  - **Quit** → stop ticker, close windows, destroy icon, exit.
  - Tooltip = "DYST (did you see that? 👀) — running (N overlays)".
- `autostart.py`: `enable()/disable()/is_enabled()` on
  `HKCU\Software\Microsoft\Windows\CurrentVersion\Run`; value = quoted path to
  `sys.executable` + `main.py` (dev) — final packaging later uses frozen exe; guard
  `os.name == "nt"` (non-Windows → no-op + log).
- `main.py`: assemble everything; `--test` flag triggers one spawn immediately then keeps
  running (for dev verification without waiting on odds); `--roll` prints one simulated
  roll result and exits (headless sanity check).

**Verify:** run app → tray icon appears; Test Trigger works; Media Folder opens Explorer;
Start on login writes/reads registry (check via `reg query`); Quit exits cleanly;
`python main.py --roll` prints deterministic-ish roll stats (e.g., 10k rolls → ~N/1000).

### Phase 7 — Polish, packaging, docs (1 day)
Tasks:
- Fade-out animation (QPropertyAnimation on `windowOpacity`) if not already via timer.
- Logging recap: `logging` → `app.log`, `debug` flag gates verbose roll logs.
- Empty-pool and corrupt-file handling surfacing (tray tooltip warning).
- Optional `--autostart-on`/`--autostart-off` CLI for tests.
- PyInstaller packaging recipe in `scripts/`: `--noconsole --windowed`, collect
  `pystray`, PySide6 plugins, OpenCV; note AV false-positive caveat; **not** required for
  dev, document only.
- Write `README.md` (media authoring guide from AGENTS.md §9; how to install/run).

**Verify:** full manual run on desktop for 30+ minutes with odds=50 → several overlays
spawn, overlap allowed, no focus steal, click-through confirmed, audio precedence
(sidecar-only) confirmed; `app.log` sensible; packaged exe boots to tray.

## 3. Acceptance criteria (Definition of Done, from AGENTS.md §11)

1. Silent boot into tray; no main window ever. ✔ requires Phase 6
2. Odds/tick/image duration from `config.json`; malformed config never crashes. ✔ P0
3. Roll → random media → fullscreen chrome-less click-through topmost overlay, auto-dismiss
   (image timer / video end), fade-out. ✔ P4/P5
4. Green-screen images and videos have green removed; exceptions honored. ✔ P3
5. Same-tick reroll bursts up to `max_concurrent`; overlap allowed. ✔ P1/P5
6. Audio precedence: sidecar wins, else silent (v1 limitation logged). ✔ P5
7. Tray: Test Trigger, Media Folder, Start on login toggle, Quit. ✔ P6
8. Autostart via registry; silent. ✔ P6
9. `app.log` records rolls/plays/skips/errors; debug flag. ✔ P7
10. `make_test_asset.py` gives verified green-screen test assets. ✔ P3

## 4. Risks & mitigations

| Risk | Impact | Mitigation |
|---|---|---|
| Chroma key CPU cost (Python per-frame) | janky video overlays | Default 720p guidance; `scale_down` future config; VLC fallback path documented |
| PySide6 packaging size/AV false positives | shipping pain | Document PyInstaller caveats; C# port is the eventual fix |
| Click-through + topmost quirks per Windows version | overlay doesn't behave | Manual test matrix (Win10/11); re-assert `WindowStaysOnTopHint` on show |
| GIF/APNG decode memory | huge files eat RAM | Cap animated image frame count (e.g., 300) in v1 |
| Wayland/Linux | broken overlays | Windows-only, documented (§5 AGENTS.md) |
| Embedded video audio unavailable v1 | some videos silent | Sidecar-first workflow documented; VLC path future |

## 5. Roadmap (post-v1, not now)

1. GUI settings window replacing hand-edited `config.json` (per user note in idea).
2. `python-vlc` direct playback for non-chroma videos (embedded audio support,
   WebM-alpha direct, low CPU).
3. Per-file `no_chroma` naming convention or per-item overrides UI.
4. Per-media-type custom odds & tick.
5. **C#/.NET 8 WPF port** (AGENTS.md §5): same config schema, HLSL-shader chroma key,
   `WS_EX_LAYERED/TOOLWINDOW/NOACTIVATE` native flags, WinForms `NotifyIcon`, single-file
   publish, GPU performance for heavy green-screen use.

## 6. Suggested first sessions (if picked up by an agent)

1. **Session A (P0+P1):** venv, deps, config loader + tests, ticker + tests, `--roll` CLI.
2. **Session B (P2+P3):** media scanner, test asset generator, chroma module + pixel tests.
3. **Session C (P4+P5):** overlay widget, manager, audio; offscreen tests.
4. **Session D (P6+P7):** tray, autostart, polish, README; full manual test.

## 7. File protection policy

Never delete any .md files (e.g., PLAN.md, PROGRESS.md, README.md, AGENTS.md) from
the project without explicit user confirmation. If a file must be removed, first
consult the user and obtain a clear confirmation.

