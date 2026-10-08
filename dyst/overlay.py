"""DYST (did you see that? 👀) — overlay window.

One frameless, always-on-top, click-through, taskbar-less window that shows
one image or plays one video, then fades out and emits `finished`.

Playback kinds:
  "image"                — still image for image_seconds, then fade
  "video"                — OpenCV frame decode (no audio)
  "video-qt"             — QtMultimedia (audio + modern codecs) for non-AV1 videos
  "video-av1"            — AV1: OpenCV software video (no hwaccel errors) + ffmpeg-
                            extracted temp audio played by Qt (see ffmpeg_util)
  "video-chroma"         — OpenCV frames keyed LIVE (fallback when no cache)
  "video-chroma-cached"  — OpenCV frames + pre-processed per-frame alpha masks
                            from dyst.precache (fast path; audio may be the
                            cached one-time extraction)

Monitor selection: the global `monitor` config key ("primary", "all" or a
0-based monitor index) is resolved to a QScreen in __init__; "all" picks a
random monitor per overlay, and an invalid value falls back to primary.
"""

from __future__ import annotations

import logging
import os
import random
import queue
import threading
from typing import Optional

import cv2
import numpy as np
from PIL import Image
from PySide6.QtCore import Property, QPropertyAnimation, QRect, QRectF, QSize, Qt, QTimer, QUrl, Signal, Slot, QMetaObject
from PySide6.QtGui import QImage, QPainter
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer, QVideoSink
from PySide6.QtWidgets import QApplication, QWidget

from dyst import chroma as chroma_mod
from dyst import ffmpeg_util

log = logging.getLogger("dyst.overlay")


def _is_random_kw(v) -> bool:
    """True for the "random" keyword (case-insensitive) used by the
    custom-mode mirror keys flip_h / flip_v (matches config._is_bool_or_random)."""
    return isinstance(v, str) and v.strip().lower() == "random"


def resolve_screen(monitor: object):
    """Return the QScreen for a `monitor` config value, or None.

    Accepts "primary" (default), "all" (pick a monitor at random for THIS
    overlay) or a 0-based monitor index. Any invalid or out-of-range value
    logs a warning and falls back to the primary screen, so a stale config
    can never crash a spawn.
    """
    screens = QApplication.screens()
    if not screens:
        return None
    if isinstance(monitor, str):
        name = monitor.strip().lower()
        if name == "primary":
            return QApplication.primaryScreen()
        if name == "all":
            # Spread overlays across every monitor: each spawn rolls its own.
            return random.choice(screens)
    # bool is an int subclass; treat it as invalid rather than index 0/1.
    if isinstance(monitor, int) and not isinstance(monitor, bool):
        if 0 <= monitor < len(screens):
            return screens[monitor]
        log.warning("overlay: monitor index %d out of range (%d screen(s)) — using primary",
                    monitor, len(screens))
    else:
        log.warning("overlay: invalid monitor %r (use 'primary', 'all' or an index) — using primary",
                    monitor)
    return QApplication.primaryScreen()


class OverlayWindow(QWidget):
    """Plays one media item on top of everything, then fades out.

    kind: "image" (shown for image_seconds)
          "video" (played to the end)
          "video-qt" (video via QtMultimedia: has AUDIO, supports AV1)

    Video playback routes through QtMultimedia (QMediaPlayer) so audio plays
    and modern codecs (AV1) work via system codecs; AV1 additionally uses the
    OpenCV software path with the extracted audio as the sync clock.

    Opacity (base `opacity` + fade-in/out) is applied by the PAINTER, not by
    QWidget.windowOpacity: the overlay is a per-pixel-alpha layered window
    (WA_TranslucentBackground -> UpdateLayeredWindow on Windows), and
    windowOpacity is applied through SetLayeredWindowAttributes, which
    UpdateLayeredWindow overrides on every repaint — so it silently does
    nothing on Windows (the fade would show the raw media, never reaching the
    configured opacity). Animating `paint_opacity` and calling
    QPainter.setOpacity() in paintEvent multiplies the media's own alpha and
    works everywhere.

    Animates `paint_opacity` to 0 over fade_out_seconds, then emits `finished`.
    """

    finished = Signal()
    # Cross-thread completions (emitted from worker threads -> queued to GUI):
    # ffmpeg audio prep (extract and/or pitch bake) and GIF frame decode.
    _audio_baked = Signal(str, str)                # (playable_path, temp_path or "")
    _gif_loaded = Signal(object, object, object)   # (raw_frames, display_frames, durations)

    FRAME_MS = 33  # ≈30 fps video playback

    # Bound the decoder thread's queue so memory doesn't balloon for fast
    # decodes; the GUI drains at _decode_budget frames/tic. Frames in the
    # queue are WINDOW-SIZE display images (pre-cropped/scaled by the
    # worker), so keep the bound tight.
    _DECODE_QUEUE_MAX = 4

    def __init__(self, parent=None, monitor: object = "primary"):
        super().__init__(parent)
        self._path: str | None = None
        self._kind: str | None = None
        self._image_seconds = 1.0
        self._fade_out_seconds = 0.2
        self._volume = 1.0
        self._current: QImage | None = None
        self._cap: cv2.VideoCapture | None = None
        self._player: QMediaPlayer | None = None       # video (+own audio) player
        self._audio_player: QMediaPlayer | None = None  # sidecar / extracted audio
        self._audio_output: QAudioOutput | None = None
        self._temp_audio: str | None = None
        self._temp_files: list[str] = []      # temp audio files to clean on close
        self._speed: float = 1.0              # playback speed multiplier (video/gif/audio)
        self._pitch: float = 1.0              # audio pitch multiplier (baked via ffmpeg)
        self._sidecar: str | None = None
        self._mode = "fit"
        self._fps = 30.0
        self._frame_index = 0
        self._gif_frames: list[QImage] = []
        self._gif_frame_index = 0
        self._gif_timer: QTimer | None = None
        self._image_end_timer: QTimer | None = None  # cancellable image/GIF display timer
        self._max_duration = 0.0                     # hard cap in seconds; 0 = no cap
        self._max_timer: QTimer | None = None        # fires at max_duration to force-stop everything
        self._visual_done = False   # True once the visual media has finished playing/displaying
        self._closing = False       # True once close teardown has begun (one-shot guard)
        self._audio_done = False   # True once all audio has finished (or there is no audio player)
        self._audio_started = False  # True once an audio player actually reached PlayingState
        self._video_clock_pending = False  # video timer deferred until the audio clock starts
        self._video_clock_fallback = None  # QTimer: force-start the video clock if audio never starts
        self._fade_started = False  # True once the fade-out animation has begun
        self._fade_done = False     # True once the fade-out animation has completed

        self._chroma_params: dict | None = None   # validated chroma_key dict (ranges + despill)
        self._cached_masks = None                 # np.memmap of per-frame alpha (video-chroma-cached)
        self._cached_meta: dict | None = None     # precache meta.json (fps, count, h, w)
        self._cached_audio: str = ""              # one-time extracted audio from the cache dir
        self._audio_is_cached = False             # audio lives in the cache dir (never delete it)
        self._decode_budget = 1                   # max frames decoded+PAINTED per timer tick
        self._max_pb_h = 0                        # playback height cap (px; 0 = native res)
        self._max_pb_fps = 0.0                    # playback fps cap (0 = native framerate)
        self._pb_dst = None                       # (w, h) downscale target for video frames
        self._fps_step = 1                        # present every Nth source frame when fps-capped
        self._presented = 0                       # frames actually presented (cached-mask index base)
        self._end_on_audio_end = False            # images: end the visual when the sidecar audio ends
        self._audio_start_watchdog: QTimer | None = None  # guard: audio never reached PlayingState

        self._video_timer = QTimer(self)
        self._video_timer.setInterval(self.FRAME_MS)
        self._video_timer.timeout.connect(self._next_frame)

        # Off-GUI-thread video decode + chroma key pipeline.
        # The decode worker runs cv2 reads (slow, blocking) on a dedicated
        # thread and pushes finished QImage frames to _frame_queue; the GUI
        # timer drains at most _decode_budget frames per tick so the event
        # loop is never blocked by slow decode/key.
        self._frame_queue: queue.Queue = queue.Queue()  # maxsize set in _load_video_cv
        self._decoder_thread: Optional[threading.Thread] = None
        self._decoder_stop = threading.Event()
        self._decoder_lock = threading.Lock()

        # Paint opacity (0..1): the ACTUAL rendering opacity. Animated by the
        # fade animations instead of windowOpacity (see the class docstring).
        self._paint_opacity_value = 1.0
        self._opacity = 1.0
        # Pre-rendered frame at the window size (scale/crop/rotate/flip done
        # ONCE per frame, not on every repaint). paintEvent blits this 1:1.
        self._render_cache: QImage | None = None
        self._preparing = False
        # Layout snapshot (GUI thread, _update_layout) + latest raw source
        # frame. _build_display() consumes _layout from the decoder/render
        # WORKER threads so per-frame crop/scale never touches the GUI
        # thread; _last_raw is only read on resize for recovery.
        self._layout: dict | None = None
        self._last_raw: QImage | None = None
        # Async audio prep: True while ffmpeg extraction/bake is in flight
        # (the video clock waits for it: audio-first start).
        self._audio_extracting = False
        self._started = False         # start() has run
        # Async GIF decode: frames arrive via _gif_loaded; the image clock
        # is deferred (_gif_start_pending) until they do.
        self._gif_loading = False
        self._gif_start_pending = False
        self._qt_deferred = False        # video-qt: wait for pitch-baked audio
        self._qt_audio_late = False      # video-qt: seek late audio to video pos
        self._gif_disps: list = []    # per-frame display caches (built off-GUI)
        self._render_thread: threading.Thread | None = None
        self._produced = 0   # frames finished by workers (diagnostics)
        # qt path: raw QVideoSink frames -> render thread (crop/scale off-GUI).
        self._render_in: queue.Queue = queue.Queue(4)

        self._audio_baked.connect(self._on_audio_baked)
        self._gif_loaded.connect(self._on_gif_loaded)

        self._fade = QPropertyAnimation(self, b"paint_opacity", self)
        self._fade.finished.connect(self._fade_finished)

        self._fade_in = QPropertyAnimation(self, b"paint_opacity", self)
        self._fade_in.finished.connect(self._on_fade_in_finished)

        self.setWindowFlags(
            Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool | Qt.WindowTransparentForInput
        )
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)

        self._screen_rect = QRect(0, 0, 1920, 1080)
        screen = resolve_screen(monitor)
        if screen is not None:
            self._screen_rect = screen.geometry()
            # Start tiny; _prepare_current() resizes the window to the media's
            # display rect (keeps the layered/composited surface small).
            self.setGeometry(self._screen_rect.x(), self._screen_rect.y(), 1, 1)
        # Re-assert topmost after geometry is set (spec: re-assert on show).
        self.setWindowFlags(self.windowFlags() | Qt.WindowStaysOnTopHint)
        self.raise_()
        self.setCursor(Qt.BlankCursor)

    # -- paint opacity ----------------------------------------------------

    def _get_paint_opacity(self) -> float:
        return self._paint_opacity_value

    def _set_paint_opacity(self, value: float) -> None:
        """Animated property: alpha multiplier used by paintEvent.

        Clamped to 0..1 (setWindowOpacity did the same). Repaints on every
        change so the fades are visible for stills/GIFs as well as videos.
        """
        try:
            value = float(value)
        except (TypeError, ValueError):
            value = 1.0
        new = max(0.0, min(1.0, value))
        old = self._paint_opacity_value
        if new == old:
            return
        self._paint_opacity_value = new
        # Repaint on every animation step: the frame is pre-scaled into
        # _render_cache, so each repaint is a cheap 1:1 blit.
        self.update()

    paint_opacity = Property(float, _get_paint_opacity, _set_paint_opacity)

    def showEvent(self, event):
        super().showEvent(event)
        # Ensure mouse transparency is retained after show (some platforms may reset)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        # Our geometry is authoritative; if it changed, the cached render is
        # stale — rebuild it (guarded against re-entrancy in _prepare_current).
        if self._preparing:
            return
        src = self._current
        if src is None or src.isNull():
            src = self._last_raw   # video overlays: latest raw frame
        if src is None or src.isNull():
            return
        self._preparing = True
        try:
            # Geometry changed behind our back: invalidate + recompute.
            if self._layout is not None:
                self._layout["stale"] = True
            self._update_layout(src.width(), src.height())
            built = self._build_display(src, self._layout)
            if built is not None:
                self._render_cache = built
        finally:
            self._preparing = False

    # -- public -----------------------------------------------------------

    def load(self, path: str, kind: str, image_seconds: float = 1.0,
             fade_out_seconds: float = 0.2, volume: float = 1.0,
             mode: str = "fit", sidecar_audio: str | None = None,
             custom: dict | None = None,
             chroma: dict | None = None,
             cached: dict | None = None,
             max_duration: float = 0.0,
             speed: float = 1.0, pitch: float = 1.0,
             fade_in_seconds: float = 0.0,
             opacity: float = 1.0,
             max_playback_height: int = 0, max_playback_fps: float = 0,
             end_on_audio_end: bool = False) -> bool:
        """Prepare media for display. Returns False on load failure.

        Pass kind="video" for OpenCV playback (no audio), kind="video-qt"
        for QtMultimedia playback, or kind="video-av1" for
        AV1 (OpenCV video + ffmpeg-extracted audio).
        chroma: validated chroma_key dict (ranges + despill) to green/blue-
              screen the media; None = no filtering. Applies to images, GIF
              frames and every OpenCV-decoded video frame.
        cached: precache handle {masks, meta, audio} — when given, video
              frames use the pre-computed alpha masks instead of live keying
              and the cached one-time extracted audio is used (sidecar wins).
        mode: "fit" (preserve aspect, letterbox) | "cover-height" /
              "cover-width" (fit one screen axis, crop the other) |
              "stretch" (fill, squish) | "custom" (position/scale/flip/rotate).
        custom: dict used ONLY when mode == "custom": position_x/y (-1..2,
              edge-pinning; 0.5 = centered, values outside 0..1 push the
              media off-screen so it can peek / be cropped), scale_x/y
              (multipliers of the aspect-preserving "fit" size; (1,1) =
              whole media visible), flip_h/flip_v (bool), rotation (degrees).
              Missing keys keep sane defaults; values are clamped
              defensively here too.
        max_duration: hard cap in seconds (0 = disabled). When it runs out,
              the video/image/gif AND any audio (sidecar / extracted /
              embedded) stop immediately and the overlay closes instantly —
              no fade-out.
        speed: playback speed multiplier (>0). Applies to videos (frame rate
              + own audio), GIF frame rate, and any audio playback. When
              pitch == 1.0, QtMultimedia's setPlaybackRate is used (tape
              style: pitch follows speed); when pitch != 1.0, audio is
              re-encoded so pitch and speed are independent.
        pitch: audio pitch multiplier (>0). Requires ffmpeg (baked via
              asetrate/aresample/atempo) — CPU cost only on spawn, and only
              when pitch != 1.0. For video-qt the embedded track is extracted
              into a temp file so it can be pitched (dual-player pattern).
        sidecar_audio: paired audio file; if given it WINS over the video's
              own audio (spec: sidecar > embedded > silent) and gives
              images sound too.
        """
        self._path = path
        self._kind = kind
        self._image_seconds = max(0.05, self._resolve(image_seconds))
        self._fade_out_seconds = max(0.0, self._resolve(fade_out_seconds))
        self._volume = max(0.0, min(5.0, self._resolve(volume)))
        self._mode = mode if mode in ("fit", "cover-height", "cover-width", "stretch", "custom") else "fit"
        self._apply_custom(custom or {})
        # Chroma key (green/blue screen removal): None = off; else the
        # validated chroma_key dict (ranges + despill). Caller decides
        # whether it applies (enabled / exceptions / per-file override).
        self._chroma_params = chroma
        # Pre-processed chroma cache (from dyst.precache): {masks, meta,
        # audio}. When set, video frames are NOT keyed live — the cached
        # per-frame alpha masks are applied instead (~2–5 ms/frame).
        if cached:
            self._cached_masks = cached.get("masks")
            self._cached_meta = cached.get("meta") or {}
            self._cached_audio = cached.get("audio") or ""
            self._audio_is_cached = bool(self._cached_audio)
        self._max_duration = max(0.0, self._resolve(max_duration))
        self._speed = max(0.05, self._resolve(speed))
        self._pitch = max(0.05, self._resolve(pitch))
        # Fade-in (images/GIFs only): opacity 0->1 BEFORE the display clock
        # starts, so lifetime = fade_in + display + fade_out. Videos ignore it
        # (they also end instantly). Scales with speed like every other timing.
        self._fade_in_seconds = max(0.0, self._resolve(fade_in_seconds))
        # Base window opacity (0..1). Fades compose with it: fade-in runs
        # 0 -> opacity, fade-out runs opacity -> 0. 1.0 = fully opaque.
        self._opacity = self._resolve(opacity)
        # Playback performance caps (0 = native): the height cap downscales
        # GIF/video frames BEFORE keying/copying/painting; the fps cap
        # presents every Nth source frame (video clock stays audio-synced).
        self._max_pb_h = max(0, int(max_playback_height or 0))
        self._max_pb_fps = max(0.0, float(max_playback_fps or 0.0))
        # Images only: when the (sidecar) audio ends, end the visual too —
        # the image disappears as the sound finishes (whichever occurs
        # first with the display timer still applies).
        self._end_on_audio_end = bool(end_on_audio_end)
        self._display_ms = 0  # display-phase duration in ms (set in start())
        if self._fade_in_seconds > 0:
            # Be invisible from the moment the window is shown (start() then
            # animates opacity 0->base opacity).
            self.paint_opacity = 0.0
        else:
            # Apply the base opacity immediately (no full-opacity first frame).
            self.paint_opacity = self._opacity
        # Sidecar audio: explicit argument wins; else auto-detect by basename
        if sidecar_audio and os.path.isfile(sidecar_audio):
            self._sidecar = sidecar_audio
        else:
            self._sidecar = None
            if not self._sidecar:
                base, _ = os.path.splitext(path)
                for ext in (".mp3", ".wav", ".ogg", ".flac", ".aac", ".m4a"):
                    candidate = base + ext
                    if os.path.isfile(candidate):
                        self._sidecar = candidate
                        log.info("overlay: found sidecar audio %s for %s", candidate, path)
                        break

        if kind == "image":
            # Animated GIF support. Decode + chroma + per-frame display
            # caches run on a WORKER thread: a big GIF used to freeze the
            # GUI for hundreds of ms per spawn during a same-tick burst.
            # The layout comes from the image header only (no decode here),
            # and the display clock waits for _gif_loaded.
            if path.lower().endswith(".gif"):
                try:
                    with Image.open(path) as im:
                        animated = bool(getattr(im, "is_animated", False))
                        n_frames = int(getattr(im, "n_frames", 1) or 1)
                        w0, h0 = im.size
                    if animated and n_frames > 1:
                        lw, lh = w0, h0
                        if self._max_pb_h and lh > self._max_pb_h:
                            lh = self._max_pb_h
                            lw = max(1, int(round(w0 * lh / h0)))
                        self._update_layout(lw, lh)
                        self._gif_loading = True
                        threading.Thread(target=self._gif_load_worker,
                                         args=(path, n_frames), daemon=True,
                                         name="dyst-gif-load").start()
                        if self._sidecar:
                            self._create_audio_player(self._sidecar)
                        return True
                except Exception as exc:
                    log.warning("overlay: cannot load gif %s (%s)", path, exc)

            # Static image (png, jpg, webp, etc.)
            if self._current is None:
                self._current = self._load_image(path)
                if self._current is None:
                    return False
                self._prepare_current()
                if self._sidecar:
                    self._create_audio_player(self._sidecar)
                return True


        if kind in ("video", "video-qt", "video-av1", "video-chroma", "video-chroma-cached"):
            if kind == "video-qt":
                return self._load_video_qt(path)
            if kind in ("video-av1", "video-chroma", "video-chroma-cached"):
                # OpenCV software frames (chroma applied in _paint_frame —
                # live keying or pre-cached alpha masks) + sidecar-or-
                # extracted audio.
                return self._load_video_av1(path, embedded_audio=self._cached_audio)
            return self._load_video_cv(path)

        log.error("overlay: unknown kind %r", kind)
        return False

    def _apply_custom(self, custom: dict) -> None:
        """Store custom-mode layout values with defensive clamping.
        Ignored by the other modes (paintEvent only reads them for "custom")."""
        self._position_x = min(2.0, max(-1.0, self._resolve(custom.get("position_x", 0.5))))
        self._position_y = min(2.0, max(-1.0, self._resolve(custom.get("position_y", 0.5))))
        self._scale_x = min(50.0, max(0.01, self._resolve(custom.get("scale_x", 1.0))))
        self._scale_y = min(50.0, max(0.01, self._resolve(custom.get("scale_y", 1.0))))
        # Boolean randomization: "random" keyword picks True/False randomly
        # (case-insensitive, matching config.py's _is_bool_or_random).
        flip_h = custom.get("flip_h", False)
        flip_v = custom.get("flip_v", False)
        self._flip_h = random.choice([True, False]) if _is_random_kw(flip_h) else bool(flip_h)
        self._flip_v = random.choice([True, False]) if _is_random_kw(flip_v) else bool(flip_v)
        self._rotation = self._resolve(custom.get("rotation", 0.0))

    def _resolve(self, val):
        """Resolve a value that might be a (lo, hi) range tuple.
        Returns a random value in the range, or the value itself if not a range."""
        if isinstance(val, tuple) and len(val) == 2:
            try:
                lo, hi = val
                return random.uniform(lo, hi)
            except (TypeError, ValueError):
                return val[0] if val else 0.0
        return val

    def _media_display_rect(self, img_w: float, img_h: float):
        """On-screen rect (x, y, dw, dh) the media occupies for the current
        mode, in screen coordinates. Used both to size the overlay window and
        to build the cached render."""
        if img_w <= 0 or img_h <= 0:
            return 0.0, 0.0, 0.0, 0.0
        W = float(self._screen_rect.width())
        H = float(self._screen_rect.height())
        mode = self._mode
        if mode == "stretch":
            return 0.0, 0.0, W, H
        if mode == "cover-height":
            # Fit the screen width; taller media is cropped top/bottom.
            scale = W / img_w
            dw = W
            dh = img_h * scale
            return 0.0, (H - dh) * 0.5, dw, dh
        if mode == "cover-width":
            # Fit the screen height; wider media is cropped left/right.
            scale = H / img_h
            dw = img_w * scale
            dh = H
            return (W - dw) * 0.5, 0.0, dw, dh
        if mode == "custom":
            fit = min(W / img_w, H / img_h)
            dw = img_w * fit * self._scale_x
            dh = img_h * fit * self._scale_y
            return (W - dw) * self._position_x, (H - dh) * self._position_y, dw, dh
        # fit: whole media visible, centered, aspect kept
        scale = min(W / img_w, H / img_h)
        dw = img_w * scale
        dh = img_h * scale
        return (W - dw) * 0.5, (H - dh) * 0.5, dw, dh

    def _rotated_bounds(self, dw: float, dh: float) -> tuple[float, float]:
        """Axis-aligned bounding box of a dw x dh rect rotated by the
        custom-mode rotation (0 for every other mode)."""
        if self._mode != "custom" or not self._rotation:
            return dw, dh
        import math
        rad = math.radians(self._rotation)
        c, s = abs(math.cos(rad)), abs(math.sin(rad))
        return dw * c + dh * s, dw * s + dh * c

    def _window_rect(self, dx: float, dy: float, dw: float, dh: float):
        """Window geometry (clamped to the screen) for a display rect — the
        visible intersection with the screen. Keeps the layered surface as
        small as possible."""
        bw, bh = self._rotated_bounds(dw, dh)
        cx, cy = dx + dw * 0.5, dy + dh * 0.5
        wx, wy = cx - bw * 0.5, cy - bh * 0.5
        W = float(self._screen_rect.width())
        H = float(self._screen_rect.height())
        x0 = max(0.0, wx)
        y0 = max(0.0, wy)
        x1 = min(W, wx + bw)
        y1 = min(H, wy + bh)
        if x1 <= x0 or y1 <= y0:
            return 0, 0, 1, 1  # fully off-screen
        return (int(round(x0)), int(round(y0)),
                max(1, int(round(x1 - x0))), max(1, int(round(y1 - y0))))

    def _update_layout(self, iw: int, ih: int) -> None:
        """(Re)compute the per-frame layout snapshot: window geometry on the
        screen + the source->window mapping (crop + target size + custom
        transform). MUST run on the GUI thread (setGeometry); the snapshot is
        then read-only, so workers can build display frames from it safely.
        Any frame whose source size differs from iw/ih is treated as an
        oversize source (image inside a video) and uses the fit branch."""
        layout = self._layout
        if (layout is not None and layout["iw"] == iw and layout["ih"] == ih
                and not layout.get("stale")):
            return
        dx, dy, dw, dh = self._media_display_rect(iw, ih)
        if dw <= 0 or dh <= 0:
            self._layout = None
            return
        wx, wy, ww, wh = self._window_rect(dx, dy, dw, dh)
        ox, oy = self._screen_rect.x(), self._screen_rect.y()
        try:
            cur = self.geometry()
            if (cur.x(), cur.y(), cur.width(), cur.height()) != (wx + ox, wy + oy, ww, wh):
                self.setGeometry(wx + ox, wy + oy, ww, wh)
        except RuntimeError:
            return  # C++ side already deleted
        custom = (self._mode == "custom"
                  and (self._rotation or self._flip_h or self._flip_v))
        inv_x = iw / dw if dw else 1.0
        inv_y = ih / dh if dh else 1.0
        if custom:
            sx0 = sy0 = 0
            sx1, sy1 = iw, ih
        else:
            sx0 = int(min(iw - 1, max(0, round((wx - dx) * inv_x))))
            sy0 = int(min(ih - 1, max(0, round((wy - dy) * inv_y))))
            sx1 = int(min(iw, max(sx0 + 1, round((wx + ww - dx) * inv_x))))
            sy1 = int(min(ih, max(sy0 + 1, round((wy + wh - dy) * inv_y))))
        self._layout = {"iw": iw, "ih": ih, "stale": False,
                        "wx": wx, "wy": wy, "ww": ww, "wh": wh,
                        "dx": dx, "dy": dy, "dw": dw, "dh": dh,
                        "sx0": sx0, "sy0": sy0, "sx1": sx1, "sy1": sy1,
                        "custom": custom, "flip_h": self._flip_h,
                        "flip_v": self._flip_v, "rotation": self._rotation,
                        "pos_x": self._position_x, "pos_y": self._position_y}

    @staticmethod
    def _build_display(src: QImage, layout: dict | None = None) -> QImage | None:
        """Crop + scale (and custom flip/rotate) *src* into the exact window
        size — ONE transform per frame. Read-only, so any thread may call it.
        Returns None for a dead source/layout."""
        if layout is None or src is None or src.isNull():
            return None
        iw, ih = src.width(), src.height()
        if iw != layout["iw"] or ih != layout["ih"]:
            return None  # source size changed: layout is stale, skip frame
        ww, wh = layout["ww"], layout["wh"]
        if layout["custom"]:
            cache = QImage(ww, wh, QImage.Format_ARGB32_Premultiplied)
            cache.fill(Qt.transparent)
            p = QPainter(cache)
            p.setRenderHint(QPainter.SmoothPixmapTransform)
            p.translate((layout["dx"] + layout["dw"] * 0.5) - layout["wx"],
                        (layout["dy"] + layout["dh"] * 0.5) - layout["wy"])
            if layout["flip_h"] or layout["flip_v"]:
                p.scale(-1.0 if layout["flip_h"] else 1.0,
                        -1.0 if layout["flip_v"] else 1.0)
            if layout["rotation"]:
                p.rotate(layout["rotation"])
            p.drawImage(QRectF(-layout["dw"] * 0.5, -layout["dh"] * 0.5,
                               layout["dw"], layout["dh"]), src)
            p.end()
            return cache
        crop = src.copy(layout["sx0"], layout["sy0"],
                        layout["sx1"] - layout["sx0"],
                        layout["sy1"] - layout["sy0"])
        if crop.width() != ww or crop.height() != wh:
            crop = crop.scaled(ww, wh, Qt.IgnoreAspectRatio,
                               Qt.SmoothTransformation)
        return crop

    def _present(self, src: QImage, keep_raw: bool = False) -> None:
        """Set the current frame + build its display cache + repaint.
        GUI-thread fast path when the layout is current (per-frame cost is a
        single crop+scale); falls back through _prepare_current when the
        window geometry changed."""
        if src is None or src.isNull():
            return
        if keep_raw:
            self._last_raw = src
        self._current = src
        self._update_layout(src.width(), src.height())
        built = self._build_display(src, self._layout)
        if built is None:
            self._prepare_current()   # stale layout / size mismatch
            return
        self._render_cache = built
        self.update()

    def _prepare_current(self) -> None:
        """GUI-thread rebuild of _render_cache from the current frame (geometry
        may have just changed). Workers build display frames themselves via
        _build_display, so this only runs on resize/geometry changes."""
        if self._preparing:
            return
        img = self._current
        if img is None or img.isNull():
            self._render_cache = None
            return
        self._preparing = True
        try:
            self._update_layout(img.width(), img.height())
            if self._layout is None:
                self._render_cache = None
                return
            self._render_cache = self._build_display(img, self._layout)
        finally:
            self._preparing = False

    def start(self) -> None:
        """Begin playback: image timer, Qt video, or OpenCV frame loop."""
        # Base opacity for the whole overlay (fades compose on top of it).
        self.paint_opacity = self._opacity
        self._started = True
        self._start_max_timer()
        if self._kind == "image":
            if self._audio_player is not None:
                self._audio_player.play()  # sidecar audio over the image
            if self._gif_loading:
                # GIF frames are decoding on a worker thread; the display
                # clock starts in _on_gif_loaded (audio already playing).
                self._gif_start_pending = True
                return
            self._begin_image_clock()

        elif self._kind == "video-qt":
            # Render thread (crop/scale off-GUI) + a fast drain timer that
            # presents whatever it produced.
            self._ensure_decoder()
            self._video_timer.setInterval(16)
            self._video_timer.start()
            if self._audio_extracting:
                # pitch-baked audio still extracting: start the video with
                # the audio in _on_audio_baked (synced), with a 5s fallback.
                self._qt_deferred = True
                QTimer.singleShot(5000, self._qt_deferred_start)
            elif self._player is not None:
                self._player.play()
            if self._audio_player is not None:
                self._audio_player.play()  # sidecar
        else:  # "video" / "video-av1"
            if self._cap is not None:
                self._ensure_decoder()
                if (self._kind != "video"
                        and (self._audio_player is not None
                             or self._audio_extracting)):
                    # AUDIO-FIRST START: QMediaPlayer has startup latency
                    # (media warm-up + Windows audio session setup). If the
                    # frame timer ran freely meanwhile, the video would run
                    # AHEAD of the audio clock — and the sync loop only
                    # corrects being behind, so the visuals would permanently
                    # lead the audio. The video clock therefore starts only
                    # once the audio ACTUALLY reaches PlayingState (its clock
                    # is authoritative), with a short fallback in case the
                    # audio never starts (or ffmpeg extraction still runs).
                    # A late-joining audio track is seek-aligned to the
                    # already-presented frames in _next_frame instead of
                    # freezing the video.
                    self._video_clock_pending = True
                    self._video_clock_fallback = QTimer(self)
                    self._video_clock_fallback.setSingleShot(True)
                    self._video_clock_fallback.timeout.connect(self._start_video_clock)
                    self._video_clock_fallback.start(2000)
                else:
                    self._video_timer.start()
            if self._audio_player is not None:
                self._audio_player.play()

    def _qt_deferred_start(self) -> None:
        """Fallback for a stuck/failed pitch-bake: start the video anyway."""
        if self._closing or not self._qt_deferred:
            return
        self._qt_deferred = False
        if self._player is not None and self._player.playbackState() \
                != QMediaPlayer.PlaybackState.PlayingState:
            self._player.play()

    def _begin_image_clock(self) -> None:
        """Image/GIF display clock (the non-visual part of start()'s image
        branch), started immediately or deferred until async GIF frames land."""
        if self._gif_frames:
            # Frame-0 timer starts in _on_gif_loaded; here we only arm the
            # display clock (deferred while _gif_loading).
            # GIFs: play through once, then begin the visual fade-out while
            # any sidecar audio keeps playing (single overlay for max_concurrent).
            gif_duration = getattr(self, "_gif_duration_ms", len(self._gif_frames) * 50)
            # Speed scales BOTH the image hold time and the GIF animation
            # (image display duration and fades time-scale with 1/speed).
            self._display_ms = max(int(self._image_seconds * 1000 / self._speed),
                                   int(gif_duration / self._speed))
        else:
            self._display_ms = int(self._image_seconds * 1000 / self._speed)
        # Cancellable member timer (not QTimer.singleShot) so max_duration
        # can stop it when it force-closes the overlay.
        self._image_end_timer = QTimer(self)
        self._image_end_timer.setSingleShot(True)
        self._image_end_timer.timeout.connect(self._visual_end)
        # end_on_audio_end: IGNORE image_display_seconds entirely — the
        # visual lasts until the sidecar audio ENDS (max_duration still
        # applies via _start_max_timer above). No sidecar audio = the
        # flag is a no-op and the normal display timer runs.
        wait_for_audio_end = (self._end_on_audio_end
                              and self._audio_player is not None)
        if self._fade_in_seconds > 0:
            # Fade in FIRST (opacity 0 -> base opacity); the display clock
            # starts when it completes, so lifetime = fade_in + display +
            # fade_out.
            self.paint_opacity = 0.0
            self._fade_in.setDuration(int(self._fade_in_seconds * 1000 / self._speed))
            self._fade_in.setStartValue(0.0)
            self._fade_in.setEndValue(self._opacity)
            self._fade_in.start()
            if wait_for_audio_end:
                self._arm_audio_end_watchdog()
        elif wait_for_audio_end:
            self._arm_audio_end_watchdog()
        else:
            self._image_end_timer.start(self._display_ms)

    def _start_video_clock(self) -> None:
        """Start the OpenCV frame timer (deferred until the audio clock runs).
        Also called as the fallback when the audio never reaches PlayingState."""
        self._video_clock_pending = False
        if self._video_clock_fallback is not None:
            self._video_clock_fallback.stop()
            self._video_clock_fallback.deleteLater()
            self._video_clock_fallback = None
        if self._closing or self._cap is None or self._video_timer.isActive():
            return
        self._ensure_decoder()
        if (self._audio_player is not None and not self._audio_started
                and self._audio_player.playbackState()
                    != QMediaPlayer.PlaybackState.PlayingState):
            log.warning("overlay: audio never started — running the video "
                        "clock alone for %s", self._path)
        self._video_timer.start()

    # -- internals --------------------------------------------------------

    def _load_video_cv(self, path: str) -> bool:
        """OpenCV video path (frames only, no audio). Opens the stream and
        reads dimensions — NO frame decode on the GUI thread (the old
        sync frame-0 read + paint stalled every spawn for ~50-100 ms).
        The first frame arrives through the normal decode queue."""
        cap = cv2.VideoCapture(path)
        if not cap.isOpened():
            log.error("overlay: cannot open video %s", path)
            return False
        self._cap = cap
        # Play at the video's real frame rate (fallback ~30fps), minus the
        # playback caps: the fps cap presents every Nth source frame, the
        # height cap downscales before keying/copying/painting (the same
        # values the precache used to build the alpha-mask cache).
        self._fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
        self._fps_step = (max(1, int(round(self._fps / self._max_pb_fps)))
                          if (self._max_pb_fps and self._fps > self._max_pb_fps) else 1)
        eff_fps = self._fps / self._fps_step
        if self._fps > 1:
            # Speed multiplier: interval = base / speed (faster = shorter).
            self._video_timer.setInterval(max(1, int(1000.0 / (eff_fps * self._speed))))
        w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
        h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
        # Height cap: remember the downscale target; _paint_frame applies it.
        if w > 0 and h > 0 and self._max_pb_h and h > self._max_pb_h:
            h2 = self._max_pb_h
            w2 = max(2, int(round(w * h2 / h)))
            self._pb_dst = (w2, h2)
        # Size the window NOW from stream properties so the very first frame
        # presents without a geometry change mid-playback.
        if w > 0 and h > 0:
            pw, ph = self._pb_dst or (w, h)
            self._update_layout(pw, ph)
        # Per-tick decode budget for the audio-synced kinds. QMediaPlayer's
        # position() updates coarsely (tens of ms), so chasing it frame-for-
        # frame used to decode BURSTS (up to 30 frames!) per tick — freezing
        # the GUI then jumping, which reads as stutter. Capped, even sampling
        # is much smoother: each tick decodes at most this many frames and
        # paints every one of them (the newest is displayed).
        #   cached/AV1: paint is cheap (~3 ms) -> 2 frames/tick is fine
        #   live video-chroma: keying is ~50 ms/frame -> never burst
        if self._kind == "video-chroma":
            self._decode_budget = 1
        elif self._kind in ("video-av1", "video-chroma-cached"):
            self._decode_budget = 2
        else:
            self._decode_budget = 1
        self._frame_index = 0
        self._decoder_presented = 0
        # Nothing painted until the worker delivers frame 0 (fits the fps
        # sampling rule too: with step > 1 the first sampled frame is not 0).
        self._current = None
        self._presented = 0
        return True

    def _create_audio_player(self, path: str) -> None:
        """Dedicated audio player (sidecar or extracted track). Speed/pitch
        are baked into a temp file when either != 1.0 (QtMultimedia has no
        pitch API) — that ffmpeg bake runs on a WORKER thread and the player
        is attached by _on_audio_baked, so spawning never freezes the GUI."""
        if self._speed != 1.0 or self._pitch != 1.0:
            self._audio_extracting = True
            threading.Thread(target=self._bake_audio_async, args=(path,),
                             daemon=True, name="dyst-audio-bake").start()
            return
        self._create_audio_player_now(path)

    def _emit_audio_baked(self, playable: str, temp: str) -> None:
        """Worker-thread-safe emit: the overlay may already be destroyed
        (close raced the ffmpeg run) — then the temp file leaks at worst,
        never crash."""
        try:
            self._audio_baked.emit(playable, temp)
        except RuntimeError:
            pass

    def _bake_audio_async(self, path: str) -> None:
        """Worker: bake speed/pitch into a temp copy. Falls back to the
        original file when ffmpeg is unavailable / fails."""
        baked = ffmpeg_util.pitch_shift(path, self._pitch, self._speed)
        if baked is None:
            self._emit_audio_baked(path, "")
        else:
            self._emit_audio_baked(baked, baked)

    def _create_audio_player_now(self, src: str) -> None:
        """GUI thread: build the QMediaPlayer for an already-playable file."""
        audio_out = QAudioOutput(self)
        audio_out.setVolume(self._volume)
        self._audio_output = audio_out
        player = QMediaPlayer(self)
        player.setAudioOutput(audio_out)
        player.setSource(QUrl.fromLocalFile(src))
        player.mediaStatusChanged.connect(self._audio_end)
        player.playbackStateChanged.connect(self._on_audio_playback_state)
        self._audio_player = player

    def _load_video_av1(self, path: str, embedded_audio: str = "") -> bool:
        """OpenCV-decoded video path (AV1 / chroma): frames via OpenCV.
        Audio precedence: sidecar wins; else the one-time CACHED extraction
        (from precache, when present — no ffmpeg run at spawn time); else
        extract the embedded track ASYNCHRONOUSLY on a worker thread (a
        sync ffmpeg run blocked the GUI for ~1s per AV1 spawn) — the player
        and video clock start via _on_audio_baked when it lands.
        Speed/pitch are baked into whatever audio plays."""
        if not self._load_video_cv(path):
            return False
        if self._sidecar:
            self._create_audio_player(self._sidecar)  # sidecar wins; no temp file
            return True
        if embedded_audio:
            # Cached one-time extraction — plays from the cache dir and is
            # NEVER deleted on close (unlike _temp_audio).
            self._create_audio_player(embedded_audio)  # may bake speed/pitch (async)
            return True
        # No audio yet: extract on a worker; _on_audio_baked attaches it.
        self._audio_extracting = True
        threading.Thread(target=self._extract_and_bake_async, args=(path,),
                         daemon=True, name="dyst-audio-extract").start()
        return True

    def _load_video_qt(self, path: str) -> bool:
        """QtMultimedia video path: audio plays, modern codecs decode.
        If a sidecar exists, the video's own audio is muted and the sidecar
        is played on a second player (sidecar wins per spec).
        speed: setPlaybackRate (tape style; when pitch == 1 the embedded
        audio speeds up naturally). pitch: the embedded track is extracted
        ASYNCHRONOUSLY (worker thread, no spawn-time freeze) and baked
        (dual-player pattern) so pitch is independent of speed."""
        needs_embedded_pitch = (self._pitch != 1.0 and not self._sidecar)
        audio_out = QAudioOutput(self)
        audio_out.setVolume(0.0 if (self._sidecar or needs_embedded_pitch) else self._volume)
        self._audio_output = audio_out

        player = QMediaPlayer(self)
        player.setAudioOutput(audio_out)
        player.setVideoSink(QVideoSink(self))
        player.setSource(QUrl.fromLocalFile(path))
        if self._speed != 1.0:
            player.setPlaybackRate(self._speed)
        player.videoSink().videoFrameChanged.connect(self._on_qt_frame)
        player.mediaStatusChanged.connect(self._on_qt_status)
        self._player = player
        if self._sidecar:
            self._create_audio_player(self._sidecar)
        elif needs_embedded_pitch:
            # Extract+bake on a WORKER thread (~1s of ffmpeg); the player
            # then receives the finished file via _on_audio_baked.
            self._audio_extracting = True
            threading.Thread(target=self._extract_and_bake_async,
                             args=(path,), daemon=True,
                             name="dyst-audio-extract").start()
        return True

    def _extract_and_bake_async(self, path: str) -> None:
        """Worker: extract the embedded track, then bake speed/pitch.
        Nothing to do when pitch == 1 (the raw extraction plays directly)."""
        audio_path = ffmpeg_util.extract_audio(path)
        if audio_path is None:
            self._emit_audio_baked("", "")
            return
        if self._pitch == 1.0 and self._speed == 1.0:
            self._emit_audio_baked(audio_path, audio_path)
            return
        baked = ffmpeg_util.pitch_shift(audio_path, self._pitch, self._speed)
        if baked is None:
            self._emit_audio_baked(audio_path, audio_path)
            return
        # Keep tracking the raw extraction too (the emit below tracks only
        # the baked copy) so cleanup removes both.
        self._temp_files.append(audio_path)
        self._emit_audio_baked(baked, baked)

    @Slot(str, str)
    def _on_audio_baked(self, playable: str, temp: str) -> None:
        """GUI thread: async audio prep finished — attach + start it (and the
        video clock if it was waiting on audio)."""
        self._audio_extracting = False
        if self._closing:
            # Tear-down raced the worker: drop every temp it may have made.
            for p in ({temp} if temp else set()) | set(self._temp_files):
                if p:
                    try:
                        os.remove(p)
                    except OSError:
                        pass
            self._temp_files.clear()
            return
        if not playable:
            # extraction failed: silent video (same as before)
            self._qt_deferred_start()
            return
        if temp:
            self._temp_audio = temp
        # NOTE: attach DIRECTLY — the file is already playable (baked, or the
        # original on fallback). Going through _create_audio_player here
        # would re-trigger the async bake loop forever.
        if self._audio_player is None:
            self._create_audio_player_now(playable)
        if self._audio_player is not None:
            self._audio_player.play()
        if (self._end_on_audio_end and self._kind == "image"
                and not self._visual_done
                and self._image_end_timer is not None
                and self._image_end_timer.isActive()):
            # Image clock started before the (slow-baked) audio player
            # existed — switch to the audio-end lifetime now.
            self._image_end_timer.stop()
            self._arm_audio_end_watchdog()
        if self._video_clock_pending:
            # Start now (stops the 2s fallback); _on_audio_baked may fire
            # before OR after the fallback already started the clock.
            self._start_video_clock()
        if self._qt_deferred:
            # video-qt + baked audio: start both together (synced).
            self._qt_deferred = False
            if self._player is not None:
                self._player.play()

    def _on_qt_frame(self, frame) -> None:
        """QVideoSink callback (GUI thread): queue the QVideoFrame only
        (~0.01 ms). toImage + height cap + crop/scale all happen on the
        render worker (_render_frame_worker) — the old GUI-side frame
        conversion was the main steady-state cost with several overlapping
        videos (NV12->RGBA alone is ~3 ms/frame)."""
        # Latest-wins: drop a stale unprocessed frame instead of queueing up.
        while True:
            try:
                self._render_in.put_nowait(frame)
                break
            except queue.Full:
                try:
                    self._render_in.get_nowait()
                except queue.Empty:
                    pass

    def _render_frame_worker(self) -> None:
        """Worker thread (qt path): crop/scale each raw QVideoSink frame to
        the window size, then publish it for presentation."""
        while not self._decoder_stop.is_set():
            try:
                frame = self._render_in.get(timeout=0.2)
            except queue.Empty:
                continue
            if frame is None:
                return
            try:
                img = frame.toImage()   # NV12->RGBA: off-GUI on purpose
            except RuntimeError:
                continue                # frame/sink already deleted
            if img.isNull():
                continue
            if self._max_pb_h and img.height() > self._max_pb_h:
                # QtMultimedia decodes internally (no OpenCV in this path),
                # so the height cap is applied here, before the layout scale.
                dh = self._max_pb_h
                dw = max(1, int(round(img.width() * dh / img.height())))
                img = img.scaled(QSize(dw, dh), Qt.IgnoreAspectRatio,
                                 Qt.SmoothTransformation)
            self._last_raw = img   # resize fallback uses the capped dims
            built = self._build_display(img, self._layout)
            if built is None:
                # No/first layout or geometry change: rebuild on the GUI.
                self._current = img
                try:
                    QMetaObject.invokeMethod(self, "_repaint_prepare",
                                             Qt.QueuedConnection)
                except RuntimeError:
                    return  # overlay destroyed while rendering
                continue
            self._current = img
            self._render_cache = built
            self._produced += 1
            try:
                QMetaObject.invokeMethod(self, "_repaint", Qt.QueuedConnection)
            except RuntimeError:
                return  # overlay destroyed while rendering

    def _on_qt_status(self, status) -> None:
        if status == QMediaPlayer.MediaStatus.EndOfMedia:
            # For video with its own audio there is no separate audio player,
            # so the video's audio ended together with the video.
            self._audio_done = (self._audio_player is None)
            self._on_visual_finished()
            return
        elif getattr(QMediaPlayer.MediaStatus, "LoadFailed", None) == status:
            # In some Qt6 builds LoadFailed is absent; treat as end/error
            self._on_visual_finished()
            return
        enum = QMediaPlayer.MediaStatus
        invalid = enum.InvalidMedia
        unknown = getattr(enum, "UnknownMediaStatus", None)
        if status == invalid or (unknown is not None and status == unknown):
            log.error("overlay: media error for %s (status %s)", self._path, status)
            self._audio_done = (self._audio_player is None)
            self._on_visual_finished()
            return
        else:
            # any other status - keep playing unless explicitly errored
            pass

    def _on_audio_playback_state(self, state) -> None:
        """Record that the audio player actually started playing. Used by
        _close_if_ready as a safety net: a player that NEVER reached
        PlayingState (e.g. the media failed to load) would otherwise hold
        the overlay open forever."""
        if state == QMediaPlayer.PlaybackState.PlayingState:
            self._audio_started = True
            # The audio clock is now running — release the deferred video
            # clock (audio-first start for the OpenCV-synced kinds).
            if self._video_clock_pending:
                self._start_video_clock()
            # end_on_audio_end: the audio is playing, so the watchdog has
            # nothing left to guard — the real EndOfMedia will end the visual.
            if self._audio_start_watchdog is not None:
                self._audio_start_watchdog.stop()
                self._audio_start_watchdog.deleteLater()
                self._audio_start_watchdog = None

    def _audio_end(self, status) -> None:
        """Callback for the sidecar / extracted audio player (mediaStatusChanged)."""
        ended = (status == QMediaPlayer.MediaStatus.EndOfMedia or
                 getattr(QMediaPlayer.MediaStatus, "LoadFailed", None) == status)
        if ended:
            self._audio_done = True
            self._audio_player = None  # finished/errored - nothing left to wait for
            if (self._end_on_audio_end and self._kind == "image"
                    and not self._visual_done):
                # The audio ended -> end the visual too (fade-out + close).
                # The display timer (if still running) becomes a no-op via
                # _fade_started; if it fires first, this simply never runs.
                self._on_visual_finished()
            # The extracted EMBEDDED track is the media's own audio clock for
            # the OpenCV-decoded video kinds. When IT ends, the media is over
            # (locked decision: videos end instantly at media end) — finish
            # the visual right away instead of grinding through the remaining
            # frames at keying speed. Sidecar audio is NOT the media's end
            # (it may outlive the video), so it doesn't trigger this.
            if (self._cap is not None
                    and self._kind in ("video-av1", "video-chroma", "video-chroma-cached")
                    and (self._temp_audio is not None or self._audio_is_cached)):
                self._on_visual_finished()
        self._close_if_ready()

    def _on_visual_finished(self) -> None:
        """Called when the visual media has finished playing/displaying
        (image display time elapsed, GIF played through, or video EndOfMedia).

        The image/GIF fades out over ``fade_out_seconds``.  Any separate audio
        player (sidecar / extracted) is intentionally LEFT PLAYING - the
        overlay only closes once *both* the visual fade and the audio have
        finished, so an image+audio pair counts as a single occurrence for
        max_concurrent.
        """
        self._visual_done = True
        if self._audio_player is None:
            # No separate audio to keep playing - audio is already done.
            self._audio_done = True
        if not self._fade_started:
            self._start_fade()  # visual fade-out only; audio keeps playing
        self._close_if_ready()

    def _on_fade_in_finished(self) -> None:
        """Fade-in completed — start the display clock (image/GIF end timer).
        With end_on_audio_end the display clock is deliberately skipped
        (visual lasts until the sidecar audio ends); the watchdog (armed at
        start) still guards a never-starting player."""
        if self._image_end_timer is not None and not self._closing:
            if self._end_on_audio_end and self._audio_player is not None:
                self._arm_audio_end_watchdog()
            else:
                self._image_end_timer.start(self._display_ms)

    def _arm_audio_end_watchdog(self) -> None:
        """end_on_audio_end guard: if the sidecar audio never reaches
        PlayingState (load quirk / dead player), end the visual after a warm-up
        window instead of hanging the overlay forever. No-op when already armed
        or when the audio clock is running."""
        if (self._audio_start_watchdog is not None
                or self._audio_started
                or self._closing):
            return
        self._audio_start_watchdog = QTimer(self)
        self._audio_start_watchdog.setSingleShot(True)
        self._audio_start_watchdog.timeout.connect(self._on_audio_end_watchdog)
        self._audio_start_watchdog.start(5000)

    def _on_audio_end_watchdog(self) -> None:
        """The audio never started playing — end the visual as if the audio
        had ended (fade-out + close) so the overlay can't hang. Skips when
        the audio is (or just became) playing, or closing already began."""
        if self._closing or self._visual_done:
            return
        if (self._audio_player is not None and self._audio_started
                and self._audio_player.playbackState()
                    == QMediaPlayer.PlaybackState.PlayingState):
            return  # started late; the real audio clock ends the visual
        log.warning("overlay: end_on_audio_end audio never started for %s - closing",
                    self._path)
        self._on_visual_finished()

    def _start_max_timer(self) -> None:
        """Arm the max_duration hard cap (single-shot). No-op when disabled."""
        if self._max_duration <= 0:
            return
        self._max_timer = QTimer(self)
        self._max_timer.setSingleShot(True)
        self._max_timer.timeout.connect(self._on_max_duration)
        self._max_timer.start(int(self._max_duration * 1000))

    def _on_max_duration(self) -> None:
        """The configured max_duration ran out — stop the video/image/gif AND
        any audio (sidecar / extracted / embedded) immediately and close the
        overlay with NO fade-out (user request: instant disappear)."""
        self._video_timer.stop()
        self._stop_decoder()
        self._fade_in.stop()
        if self._player is not None:
            self._player.stop()
        if self._gif_timer is not None:
            self._gif_timer.stop()
            self._gif_timer.deleteLater()
            self._gif_timer = None
        if self._audio_player is not None:
            self._audio_player.stop()
            self._audio_player = None
        # Nothing left to play and no fade: skip straight to close.
        self._audio_done = True
        self._visual_done = True
        self._fade_started = True
        self._fade_done = True
        self._finish_close()

    def _visual_end(self, gif_timer=None) -> None:
        """Timer callback: the image/GIF display duration has elapsed."""
        self._on_visual_finished()

    def _fade_finished(self) -> None:
        """Fade-out animation has completed (paint_opacity reached 0)."""
        self._fade_done = True
        self._close_if_ready()

    def _close_if_ready(self) -> None:
        """Close the overlay only when both the visual fade and the audio have
        finished.  If the audio is still playing (or the fade is still
        running), wait for it to finish first."""
        if self._audio_done and self._visual_done and self._fade_done:
            self._finish_close()
        elif (self._visual_done and self._fade_done
                and self._audio_player is not None
                and not self._audio_started
                and self._audio_player.playbackState()
                    != QMediaPlayer.PlaybackState.PlayingState):
            # Safety net: the visual is fully done but the audio player never
            # started playing (load failure / silent decode error). Treat the
            # audio as finished instead of hanging the overlay open forever.
            log.warning("overlay: audio player never started for %s - closing anyway", self._path)
            self._audio_player.stop()
            self._audio_player = None
            self._audio_done = True
            self._finish_close()

    def _gif_load_worker(self, path: str, n_frames: int) -> None:
        """Worker thread: decode every GIF frame (PIL), apply the playback
        height cap + chroma key, convert to QImage and PRE-RENDER each frame's
        display cache via _build_display — so the GUI's per-frame job during
        playback is a plain QImage pointer swap."""
        raws: list = []
        disps: list = []
        durations: list = []
        try:
            with Image.open(path) as im:
                for i in range(n_frames):
                    im.seek(i)
                    frame = im.convert("RGBA")
                    if self._max_pb_h and frame.height > self._max_pb_h:
                        # Height cap: downscale BEFORE keying so
                        # the mask/work cost scales with the cap.
                        h2 = self._max_pb_h
                        w2 = max(1, int(round(frame.width * h2 / frame.height)))
                        frame = frame.resize((w2, h2), Image.BILINEAR)
                    if self._chroma_params is not None:
                        frame = chroma_mod.chroma_key_image(frame, self._chroma_params)
                    arr = np.array(frame)
                    h, w = arr.shape[:2]
                    # .copy() so the QImage owns its buffer (numpy memory is reused)
                    qi = QImage(arr.data, w, h, arr.strides[0],
                                QImage.Format_RGBA8888).copy()
                    raws.append(qi)
                    disps.append(self._build_display(qi, self._layout))
                    dur = im.info.get("duration", 100)
                    durations.append(dur if dur > 0 else 100)
        except Exception as exc:
            log.warning("overlay: gif decode failed %s (%s)", path, exc)
            raws, disps, durations = [], [], []
        try:
            self._gif_loaded.emit(raws, disps, durations)
        except RuntimeError:
            pass  # overlay already destroyed while decoding

    def _on_gif_loaded(self, raws, disps, durations) -> None:
        """GUI thread: install the worker-decoded GIF frames, start the frame
        timer, and (if start() already ran) the deferred image clock."""
        self._gif_loading = False
        if self._closing:
            return
        if not raws:
            log.warning("overlay: no gif frames for %s", self._path)
            self._on_visual_finished()
            return
        self._gif_frames = raws
        self._gif_disps = disps
        self._gif_durations = durations
        self._gif_duration_ms = sum(durations)
        self._gif_frame_index = 0
        self._current = raws[0]
        if disps[0] is not None:
            self._render_cache = disps[0]
        else:
            self._prepare_current()
        self.update()
        # Initialize single-shot timer; delay for Frame 0 -> Frame 1.
        self._gif_timer = QTimer(self)
        self._gif_timer.setSingleShot(True)
        self._gif_timer.timeout.connect(self._advance_gif_frame)
        self._gif_timer.start(max(1, int(self._gif_durations[0] / self._speed)))
        if self._gif_start_pending:
            self._gif_start_pending = False
            self._begin_image_clock()

    def _show_gif_frame(self, idx: int) -> None:
        """Present GIF frame idx: pre-rendered display cache when available
        (O(1)), else rebuild on the fly (layout was invalidated by resize)."""
        self._current = self._gif_frames[idx]
        disp = (self._gif_disps[idx]
                if idx < len(self._gif_disps) and self._gif_disps[idx] is not None
                else None)
        if disp is not None:
            self._render_cache = disp
        else:
            self._prepare_current()
        self.update()

    def _advance_gif_frame(self) -> None:
        if not self._gif_frames or not hasattr(self, "_gif_durations"):
            return
        self._gif_frame_index += 1

        if self._gif_frame_index >= len(self._gif_frames):
            self._gif_frame_index = len(self._gif_frames) - 1
            self._show_gif_frame(self._gif_frame_index)
            if self._gif_timer is not None:
                self._gif_timer.stop()
                self._gif_timer.deleteLater()
                self._gif_timer = None
            return
        self._show_gif_frame(self._gif_frame_index)
        if self._gif_timer is not None:
            next_delay = self._gif_durations[self._gif_frame_index] / self._speed
            self._gif_timer.start(max(1, int(next_delay)))

    def _load_image(self, path: str) -> QImage | None:
        try:
            img = Image.open(path).convert("RGBA")
            if self._max_pb_h and img.height > self._max_pb_h:
                # Performance cap: a large still is downscaled ONCE here so
                # the render-cache build (and any chroma key) works on a
                # smaller image instead of a full-resolution source.
                h2 = self._max_pb_h
                w2 = max(1, int(round(img.width * h2 / img.height)))
                img = img.resize((w2, h2), Image.BILINEAR)
            if self._chroma_params is not None:
                img = chroma_mod.chroma_key_image(img, self._chroma_params)
        except Exception as exc:  # Pillow raises several error types
            log.error("overlay: cannot load image %s (%s)", path, exc)
            return None
        arr = np.asarray(img)
        h, w = arr.shape[:2]
        # .copy() so the QImage owns its buffer (numpy memory is reused).
        return QImage(arr.data, w, h, arr.strides[0], QImage.Format_RGBA8888).copy()

    def _frame_to_qimage(self, frame) -> QImage:
        if frame.ndim == 3 and frame.shape[2] == 4:
            # BGRA bytes map directly to Qt's ARGB32 on little-endian
            # (0xAARRGGBB -> B,G,R,A in memory) — no channel swap needed.
            h, w = frame.shape[:2]
            return QImage(frame.data, w, h, frame.strides[0],
                          QImage.Format_ARGB32).copy()
        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        h, w = rgb.shape[:2]
        return QImage(rgb.data, w, h, rgb.strides[0], QImage.Format_RGB888).copy()

    def _paint_frame(self, frame, mask_idx=None) -> QImage:
        """BGR frame -> QImage for display, applying chroma key when set.

        With a pre-processing cache (video-chroma-cached) the cached per-frame
        alpha mask is applied instead of live chroma keying — the expensive
        mask math was already done once by dyst.precache. Optional despill is
        still applied at playback (it is pixel work, not mask work).
        """
        if self._pb_dst is not None:
            # Playback height cap: same downscale precache used to build the
            # masks, so the cache geometry and the decoded frame always match.
            frame = cv2.resize(frame, self._pb_dst, interpolation=cv2.INTER_AREA)
        if self._cached_masks is not None:
            out = cv2.cvtColor(frame, cv2.COLOR_BGR2BGRA)
            # mask_idx = index of the frame being presented right now
            # (0-based); the worker passes its own counter so masks stay
            # aligned even though GUI drain runs on another thread.
            mi = mask_idx if mask_idx is not None else self._presented
            idx = min(max(0, mi), len(self._cached_masks) - 1)
            out[:, :, 3] = self._cached_masks[idx]
            if self._chroma_params is not None and self._chroma_params.get("despill"):
                chroma_mod._despill(out, self._chroma_params)
            return self._frame_to_qimage(out)
        if self._chroma_params is not None:
            frame = chroma_mod.chroma_key_frame(frame, self._chroma_params)
        return self._frame_to_qimage(frame)

    def _decode_one(self) -> tuple[bool, object | None]:
        """Read one source frame sequentially. On end-of-stream: stop the
        video timer and finish the visual (standard teardown). Returns
        (True, frame) on success, (False, None) when playback is over."""
        ok, frame = self._cap.read()
        if not ok:
            self._video_timer.stop()
            self._on_visual_finished()
            return False, None
        self._frame_index += 1
        return True, frame

    def _ensure_decoder(self) -> None:
        """Start the background workers (once per overlay): the render
        worker for video-qt (crop/scale off-GUI), the cv2 decode worker
        for every other video kind."""
        if self._kind == "video-qt":
            if self._render_thread is not None and self._render_thread.is_alive():
                return
            self._decoder_stop.clear()
            t = threading.Thread(target=self._render_frame_worker,
                                 name="dyst-render", daemon=True)
            self._render_thread = t
            t.start()
            return
        if self._decoder_thread is not None and self._decoder_thread.is_alive():
            return
        self._decoder_stop.clear()
        self._frame_queue = queue.Queue(maxsize=self._DECODE_QUEUE_MAX)
        self._decoder_presented = 0  # worker-side presented counter (mask index)
        t = threading.Thread(target=self._decoder_worker, name="dyst-decoder", daemon=True)
        self._decoder_thread = t
        t.start()

    def _stop_decoder(self) -> None:
        """Signal the worker to stop; called on teardown paths."""
        self._decoder_stop.set()
        # Unblock a worker waiting on a full queue.
        try:
            while True:
                self._frame_queue.get_nowait()
        except queue.Empty:
            pass

    def _decoder_worker(self) -> None:
        """Background thread: blocking cv2 read + chroma key + QImage
        convert + display build (crop/scale via _build_display — read-only,
        safe off-GUI). Pushes (needs_present, display_or_raw) frames to
        _frame_queue; the GUI timer only points _current at them + update(),
        so the crop/scale hot path never runs on the GUI thread anymore.
        Never touches widgets directly."""
        try:
            step = max(1, int(self._fps_step))
            while not self._decoder_stop.is_set():
                frame = None
                with self._decoder_lock:
                    cap = self._cap
                    if cap is None:
                        return
                    for _ in range(step):
                        ok, f = cap.read()
                        if not ok:
                            QMetaObject.invokeMethod(self, "_on_decoder_eos", Qt.QueuedConnection)
                            return
                        frame = f
                if frame is None:
                    return
                try:
                    img = self._paint_frame(frame, mask_idx=self._decoder_presented)
                except Exception:
                    continue
                self._decoder_presented += 1
                raw = self._last_raw   # published by _on_qt_frame (qt path only)
                disp = self._build_display(img, self._layout)
                if disp is None:
                    # Layout stale/missing (e.g. geometry change mid-resize):
                    # hand the RAW frame to the GUI, which rebuilds the
                    # layout via _prepare_current.
                    self._last_raw = img
                    disp, need = img, True
                else:
                    # need = first frame ever (paint must happen) or a
                    # geometry change since the last present.
                    need = raw is None or img.cacheKey() != raw.cacheKey()
                # Bounded queue with backpressure: block when GUI lags so
                # decode stays ~presentation rate (no drops, no speed-up).
                # Stop event unblocks via _stop_decoder draining the queue.
                self._produced += 1
                while not self._decoder_stop.is_set():
                    try:
                        self._frame_queue.put((need, disp), timeout=0.2)
                        break
                    except queue.Full:
                        continue
        except Exception:
            import traceback; traceback.print_exc()

    @Slot()
    def _on_decoder_eos(self) -> None:
        """Worker hit end-of-stream: stop timer, finish visual (GUI thread)."""
        if self._closing:
            return
        self._video_timer.stop()
        self._on_visual_finished()

    @staticmethod
    def _frame_of(item) -> tuple[bool, QImage]:
        """Queue payload -> (needs_present, image). Plain QImage payloads
        (no display cache yet) count as needing a present."""
        if isinstance(item, tuple):
            return item[0], item[1]
        return True, item

    @Slot()
    def _repaint(self) -> None:
        """Queued from _render_frame_worker: swap _render_cache in, repaint."""
        if not self._closing:
            self.update()

    @Slot()
    def _repaint_prepare(self) -> None:
        """Queued when the worker had no fresh layout (first frame / geometry
        change): rebuild the layout + display cache on the GUI thread."""
        if self._closing:
            return
        self._prepare_current()
        self.update()

    def _next_frame(self) -> None:
        """GUI tick: point the overlay at worker-built frames paced to the
        audio clock (or the timer rate when no audio). Decode/key/crop/scale
        all run off-GUI; this is only a pointer swap + update(), so a slow
        frame can't block the event loop or stall audio."""
        if self._closing:
            return
        if self._cap is None and self._frame_queue.empty():
            return
        budget = max(1, int(self._decode_budget))
        if (self._kind in ("video-av1", "video-chroma", "video-chroma-cached")
                and self._audio_player is not None
                and self._audio_player.playbackState()
                    == QMediaPlayer.PlaybackState.PlayingState):
            step = max(1, int(self._fps_step))
            eff_fps = self._fps / step
            # Late-join audio (async ffmpeg extract/bake): the video clock
            # may have run ahead while the audio was still preparing —
            # seek the audio to the already-presented position instead of
            # letting the clip play through unsynced.
            if not self._qt_audio_late:
                target_a = int(self._audio_player.position() * eff_fps / 1000.0)
                if self._presented - target_a > 4:
                    self._qt_audio_late = True  # one-shot: position() jumps
                    self._audio_player.setPosition(
                        int(self._presented / eff_fps * 1000.0))
            # Audio-synced pacing: present only up to where the audio clock
            # says we should be (never more than budget/tick, so catch-up
            # is smooth, not a jump). Worker stays ahead via the queue.
            target = int(self._audio_player.position() * eff_fps / 1000.0)
            target = min(target, self._presented + budget)
            painted = False
            last = None
            need_present = False
            while self._presented < target:
                try:
                    item = self._frame_queue.get_nowait()
                except queue.Empty:
                    break  # worker behind: wait, don't freeze on a burst
                need, img = self._frame_of(item)
                self._current = img
                self._presented += 1
                last = img
                need_present = need_present or need
                painted = True
            if painted:
                if need_present and last is not None:
                    self._present(last)  # geometry change: rebuild cache (rare)
                else:
                    self.update()        # display cache already built off-GUI
            return
        # No audio clock: timer already ticks at effective fps x speed, so
        # exactly one queued frame per tick keeps duration exact.
        try:
            item = self._frame_queue.get_nowait()
        except queue.Empty:
            return  # worker behind: skip tick, timer retries
        need, img = self._frame_of(item)
        self._current = img
        self._presented += 1
        if need:
            self._present(img)  # first frame / geometry change: rebuild cache
        else:
            self.update()

    def _start_fade(self) -> None:
        self._video_timer.stop()
        self._stop_decoder()
        self._fade_in.stop()  # fade-in must never overlap the fade-out
        if self._player is not None:
            self._player.stop()
        if self._gif_timer is not None:
            self._gif_timer.stop()
            self._gif_timer.deleteLater()
            self._gif_timer = None
        # NOTE: the audio player is intentionally NOT stopped here - it keeps
        # playing while the visual fades out, so the overlay stays alive (a
        # single max_concurrent occurrence) until the audio finishes too.
        self._fade_started = True
        self._fade_done = False
        # Speeds up the fade along with the rest of the overlay (1/speed).
        self._fade.setDuration(int(self._fade_out_seconds * 1000 / self._speed))
        # Fade out from the media's base opacity (not always 1.0).
        self._fade.setStartValue(self._opacity)
        self._fade.setEndValue(0.0)
        self._fade.start()

    def dismiss(self) -> None:
        """Close the overlay immediately (no fade), stopping its audio too.

        Public entry point for the tray's Pause action; emits `finished` via
        the normal one-shot teardown path.
        """
        self._finish_close()

    def _finish_close(self) -> None:
        # One-shot guard: natural end (fade finished) and max_duration can race
        # (max fires mid-fade); only run teardown + emit `finished` once.
        if self._closing:
            return
        self._closing = True
        self._video_timer.stop()
        self._stop_decoder()
        self._fade_in.stop()
        with self._decoder_lock:
            cap, self._cap = self._cap, None
        if cap is not None:
            try:
                cap.release()
            except Exception:
                pass
        if False:
            self._cap.release()
            self._cap = None
        if self._player is not None:
            self._player.stop()
            self._player = None
        if self._audio_player is not None:
            self._audio_player.stop()
            self._audio_player = None
        if self._audio_output is not None:
            self._audio_output = None
        if self._gif_timer is not None:
            self._gif_timer.stop()
            self._gif_timer.deleteLater()
            self._gif_timer = None
        if self._image_end_timer is not None:
            self._image_end_timer.stop()
            self._image_end_timer.deleteLater()
            self._image_end_timer = None
        if self._max_timer is not None:
            self._max_timer.stop()
            self._max_timer.deleteLater()
            self._max_timer = None
        if self._video_clock_fallback is not None:
            self._video_clock_fallback.stop()
            self._video_clock_fallback.deleteLater()
            self._video_clock_fallback = None
        if self._audio_start_watchdog is not None:
            self._audio_start_watchdog.stop()
            self._audio_start_watchdog.deleteLater()
            self._audio_start_watchdog = None
        self._video_clock_pending = False
        self._render_cache = None
        self._layout = None   # workers stopped; drop the layout snapshot
        self._gif_frames = []
        self._gif_frame_index = 0
        for tmp in self._temp_files:
            try:
                os.remove(tmp)
            except OSError:
                pass
        self._temp_files = []
        if self._temp_audio:
            try:
                os.remove(self._temp_audio)
            except OSError:
                pass
            self._temp_audio = None
        self.finished.emit()
        self.close()
        self.deleteLater()

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt method name
        cache = self._render_cache
        if cache is None or cache.isNull():
            return
        painter = QPainter(self)
        # Base opacity + fades: per-pixel alpha (windowOpacity is a no-op on
        # Windows layered/translucent windows - see the class docstring).
        # The cached image is already at the window size, so this is a 1:1
        # blit instead of a per-repaint scale.
        painter.setOpacity(self._paint_opacity_value)
        painter.drawImage(0, 0, cache)
