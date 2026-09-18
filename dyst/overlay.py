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

import cv2
import numpy as np
from PIL import Image
from PySide6.QtCore import Property, QPropertyAnimation, QRect, QRectF, QSize, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QImage, QPainter
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer, QVideoSink
from PySide6.QtWidgets import QApplication, QWidget

from dyst import chroma as chroma_mod
from dyst import ffmpeg_util

log = logging.getLogger("dyst.overlay")


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
    FRAME_MS = 33  # ≈30 fps video playback

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

        # Paint opacity (0..1): the ACTUAL rendering opacity. Animated by the
        # fade animations instead of windowOpacity (see the class docstring).
        self._paint_opacity_value = 1.0
        self._opacity = 1.0
        # Pre-rendered frame at the window size (scale/crop/rotate/flip done
        # ONCE per frame, not on every repaint). paintEvent blits this 1:1.
        self._render_cache: QImage | None = None
        self._preparing = False

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
        if not self._preparing:
            self._prepare_current()

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
        self._image_seconds = max(0.05, image_seconds)
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
            # Animated GIF support via manual frame extraction.
            if path.lower().endswith(".gif"):
                try:
                    with Image.open(path) as im:
                        if getattr(im, "is_animated", False):
                            frames = []
                            durations = []

                            for i in range(im.n_frames):
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

                                # Create a clean QImage copy so memory isn't reclaimed by Python
                                qi = QImage(arr.data, w, h, arr.strides[0], QImage.Format_RGBA8888).copy()
                                frames.append(qi)

                                # Extract frame duration (default to 100ms if 0 or missing)
                                dur = im.info.get("duration", 100)
                                durations.append(dur if dur > 0 else 100)

                            self._gif_frames = frames
                            self._gif_durations = durations
                            self._gif_frame_index = 0
                            self._current = frames[0] if frames else None
                            self._gif_duration_ms = sum(durations)
                            self._prepare_current()

                            # Initialize single-shot timer
                            self._gif_timer = QTimer(self)
                            self._gif_timer.setSingleShot(True)
                            self._gif_timer.timeout.connect(self._advance_gif_frame)

                            # Start initial delay timer for Frame 0 -> Frame 1
                            self._gif_timer.start(max(1, int(self._gif_durations[0] / self._speed)))

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
        flip_h = custom.get("flip_h", False)
        flip_v = custom.get("flip_v", False)
        self._flip_h = random.choice([True, False]) if flip_h == "random" else bool(flip_h)
        self._flip_v = random.choice([True, False]) if flip_v == "random" else bool(flip_v)
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

    def _prepare_current(self) -> None:
        """Build self._render_cache: the current frame scaled/cropped to the
        window size, with custom-mode flip/rotation applied. Called only when
        the frame or geometry changes, so paintEvent can blit it 1:1 (the old
        code re-scaled the full image on every repaint)."""
        if self._preparing:
            return
        img = self._current
        if img is None or img.isNull():
            self._render_cache = None
            return
        self._preparing = True
        try:
            iw, ih = img.width(), img.height()
            dx, dy, dw, dh = self._media_display_rect(iw, ih)
            if dw <= 0 or dh <= 0:
                self._render_cache = None
                return
            wx, wy, ww, wh = self._window_rect(dx, dy, dw, dh)
            # _window_rect() is in screen-local coords; add the selected
            # monitor's origin so the window lands on the right screen.
            ox, oy = self._screen_rect.x(), self._screen_rect.y()
            cur = self.geometry()
            if (cur.x(), cur.y(), cur.width(), cur.height()) != (wx + ox, wy + oy, ww, wh):
                self.setGeometry(wx + ox, wy + oy, ww, wh)

            if self._mode == "custom" and (self._rotation or self._flip_h or self._flip_v):
                cache = QImage(ww, wh, QImage.Format_ARGB32_Premultiplied)
                cache.fill(Qt.transparent)
                p = QPainter(cache)
                p.setRenderHint(QPainter.SmoothPixmapTransform)
                p.translate((dx + dw * 0.5) - wx, (dy + dh * 0.5) - wy)
                if self._flip_h or self._flip_v:
                    p.scale(-1.0 if self._flip_h else 1.0,
                            -1.0 if self._flip_v else 1.0)
                if self._rotation:
                    p.rotate(self._rotation)
                p.drawImage(QRectF(-dw * 0.5, -dh * 0.5, dw, dh), img)
                p.end()
                self._render_cache = cache
                return

            # Non-rotated: crop the visible slice of the source, then scale it
            # to the window size (one scale per frame, not per repaint).
            inv_x = iw / dw
            inv_y = ih / dh
            sx0 = int(min(iw - 1, max(0, round((wx - dx) * inv_x))))
            sy0 = int(min(ih - 1, max(0, round((wy - dy) * inv_y))))
            sx1 = int(min(iw, max(sx0 + 1, round((wx + ww - dx) * inv_x))))
            sy1 = int(min(ih, max(sy0 + 1, round((wy + wh - dy) * inv_y))))
            crop = img.copy(sx0, sy0, sx1 - sx0, sy1 - sy0)
            if crop.width() != ww or crop.height() != wh:
                crop = crop.scaled(ww, wh, Qt.IgnoreAspectRatio,
                                   Qt.SmoothTransformation)
            self._render_cache = crop
        finally:
            self._preparing = False

    def start(self) -> None:
        """Begin playback: image timer, Qt video, or OpenCV frame loop."""
        # Base opacity for the whole overlay (fades compose on top of it).
        self.paint_opacity = self._opacity
        self._start_max_timer()
        if self._kind == "image":
            if self._audio_player is not None:
                self._audio_player.play()  # sidecar audio over the image
                        
            if self._gif_frames:
                # DO NOT call self._gif_timer.start() here without args; load() already started frame 0.
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
                
        elif self._kind == "video-qt":
            if self._player is not None:
                self._player.play()
            if self._audio_player is not None:
                self._audio_player.play()  # sidecar
        else:  # "video" / "video-av1"
            if self._cap is not None:
                if self._audio_player is not None and self._kind != "video":
                    # AUDIO-FIRST START: QMediaPlayer has startup latency
                    # (media warm-up + Windows audio session setup). If the
                    # frame timer ran freely meanwhile, the video would run
                    # AHEAD of the audio clock — and the sync loop only
                    # corrects being behind, so the visuals would permanently
                    # lead the audio. The video clock therefore starts only
                    # once the audio ACTUALLY reaches PlayingState (its clock
                    # is authoritative), with a short fallback in case the
                    # audio never starts.
                    self._video_clock_pending = True
                    self._video_clock_fallback = QTimer(self)
                    self._video_clock_fallback.setSingleShot(True)
                    self._video_clock_fallback.timeout.connect(self._start_video_clock)
                    self._video_clock_fallback.start(2000)
                else:
                    self._video_timer.start()
            if self._audio_player is not None:
                self._audio_player.play()

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
        if (self._audio_player is not None and not self._audio_started
                and self._audio_player.playbackState()
                    != QMediaPlayer.PlaybackState.PlayingState):
            log.warning("overlay: audio never started — running the video "
                        "clock alone for %s", self._path)
        self._video_timer.start()

    # -- internals --------------------------------------------------------

    def _load_video_cv(self, path: str) -> bool:
        """OpenCV video path (frames only, no audio)."""
        cap = cv2.VideoCapture(path)
        if not cap.isOpened():
            log.error("overlay: cannot open video %s", path)
            return False
        ok, frame = cap.read()
        if not ok:
            log.error("overlay: no frames in video %s", path)
            cap.release()
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
        # Height cap: remember the downscale target; _paint_frame applies it.
        if self._max_pb_h and frame.shape[0] > self._max_pb_h:
            h2 = self._max_pb_h
            w2 = max(2, int(round(frame.shape[1] * h2 / frame.shape[0])))
            self._pb_dst = (w2, h2)
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
        self._frame_index = 0  # frame 0 already consumed above
        if self._fps_step == 1:
            # Frame 0 is presented immediately: _presented is still 0, so
            # _paint_frame indexes mask 0, then we mark it as shown.
            self._current = self._paint_frame(frame)
            self._presented = 1
        else:
            # Frame 0 is NOT a sampled frame (sampling presents every Nth
            # source frame), so nothing is painted until the first one —
            # the window stays transparent for <step/fps (~1 frame).
            self._current = None
            self._presented = 0
        self._prepare_current()
        return True

    def _create_audio_player(self, path: str) -> None:
        """Dedicated audio player (sidecar or extracted track). Speed/pitch
        are baked into a temp file when either != 1.0 (QtMultimedia has no
        pitch API), so the player runs at rate 1.0."""
        src = self._prepare_audio_file(path)
        audio_out = QAudioOutput(self)
        audio_out.setVolume(self._volume)
        self._audio_output = audio_out
        player = QMediaPlayer(self)
        player.setAudioOutput(audio_out)
        player.setSource(QUrl.fromLocalFile(src))
        player.mediaStatusChanged.connect(self._audio_end)
        player.playbackStateChanged.connect(self._on_audio_playback_state)
        self._audio_player = player

    def _prepare_audio_file(self, path: str) -> str:
        """Return a playable version of *path* honouring speed/pitch.
        If both are 1.0 (default) returns the original. Otherwise bakes the
        changes into a temp file via ffmpeg and tracks it for cleanup.
        Falls back to the original if ffmpeg is unavailable / fails."""
        if self._speed == 1.0 and self._pitch == 1.0:
            return path
        baked = ffmpeg_util.pitch_shift(path, self._pitch, self._speed)
        if baked is None:
            return path
        self._temp_files.append(baked)
        return baked

    def _load_video_av1(self, path: str, embedded_audio: str = "") -> bool:
        """OpenCV-decoded video path (AV1 / chroma): frames via OpenCV.
        Audio precedence: sidecar wins; else the one-time CACHED extraction
        (from precache, when present — no ffmpeg run at spawn time); else
        extract the embedded track to a temp file played via Qt.
        Speed/pitch are baked into whatever audio plays."""
        if not self._load_video_cv(path):
            return False
        if self._sidecar:
            self._create_audio_player(self._sidecar)  # sidecar wins; no temp file
            return True
        audio_path = embedded_audio
        if audio_path:
            # Cached one-time extraction — plays from the cache dir and is
            # NEVER deleted on close (unlike _temp_audio).
            self._create_audio_player(audio_path)  # may bake speed/pitch into a temp copy
            return True
        audio_path = ffmpeg_util.extract_audio(path)
        if audio_path is None:
            return True  # video only (ffmpeg missing / extraction failed)
        self._temp_audio = audio_path
        self._create_audio_player(audio_path)  # may bake speed/pitch into a second temp
        return True

    def _load_video_qt(self, path: str) -> bool:
        """QtMultimedia video path: audio plays, modern codecs decode.
        If a sidecar exists, the video's own audio is muted and the sidecar
        is played on a second player (sidecar wins per spec).
        speed: setPlaybackRate (tape style; when pitch == 1 the embedded
        audio speeds up naturally). pitch: the embedded track is extracted
        and baked (dual-player pattern) so pitch is independent of speed."""
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
            audio_path = ffmpeg_util.extract_audio(path)
            if audio_path is not None:
                self._temp_audio = audio_path
                self._create_audio_player(audio_path)  # bakes speed+pitch
        return True

    def _on_qt_frame(self, frame) -> None:
        img = frame.toImage()
        if not img.isNull():
            if self._max_pb_h and img.height() > self._max_pb_h:
                # QtMultimedia decodes internally (no OpenCV in this path),
                # so the height cap is applied at paint time instead.
                dh = self._max_pb_h
                dw = max(1, int(round(img.width() * dh / img.height())))
                img = img.scaled(QSize(dw, dh), Qt.IgnoreAspectRatio,
                                 Qt.SmoothTransformation)
            self._current = img
            self._prepare_current()
            self.update()

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

    def _advance_gif_frame(self) -> None:
        if not self._gif_frames or not hasattr(self, "_gif_durations"):
            return
        self._gif_frame_index += 1

        if self._gif_frame_index >= len(self._gif_frames):
            self._gif_frame_index = len(self._gif_frames) - 1
            self._current = self._gif_frames[self._gif_frame_index]
            self._prepare_current()
            self.update()
            if self._gif_timer is not None:
                self._gif_timer.stop()
                self._gif_timer.deleteLater()
                self._gif_timer = None
            return
        self._current = self._gif_frames[self._gif_frame_index]
        self._prepare_current()
        self.update()
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

    def _paint_frame(self, frame) -> QImage:
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
            # _presented = index of the frame being presented right now
            # (0-based), so the mask that matches this frame is _presented
            # itself — both precache and playback present every Nth source
            # frame in the same order.
            idx = min(max(0, self._presented), len(self._cached_masks) - 1)
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

    def _next_frame(self) -> None:
        if self._cap is None:
            return
        step = self._fps_step
        if (self._kind in ("video-av1", "video-chroma", "video-chroma-cached")
                and self._audio_player is not None
                and self._audio_player.playbackState()
                == QMediaPlayer.PlaybackState.PlayingState):
            # Audio-synced pacing WITHOUT per-frame seeks (those re-decode
            # from the nearest keyframe — very slow for AV1). Decode
            # sequentially and use the audio clock only to decide whether to
            # advance: never decode more than _decode_budget frames per tick
            # (the old code chased the audio position with bursts up to 30
            # frames — freezing the GUI then jumping = visible stutter).
            # EVERY decoded frame is painted; the newest one is what the
            # screen shows. With the fps cap (step > 1) `step` source frames
            # are decoded per presented frame and only every Nth is painted,
            # so the presented rate stays at the capped fps while the audio
            # clock (untouched) keeps the duration exact.
            target = int(self._audio_player.position() * (self._fps / step) / 1000.0)
            painted = False
            if step == 1:
                target = min(target, self._frame_index + self._decode_budget)
                while self._frame_index < target:
                    ok, frame = self._decode_one()
                    if not ok:
                        return
                    self._current = self._paint_frame(frame)
                    self._presented += 1
                    painted = True
            else:
                target = min(target, self._presented + self._decode_budget)
                while self._presented < target:
                    frame = None
                    for _ in range(step):
                        ok, frame = self._decode_one()
                        if not ok:
                            return
                    self._current = self._paint_frame(frame)
                    self._presented += 1
                    painted = True
            if painted:
                self._prepare_current()
                self.update()
            return
        # Non-audio-synced kinds (plain "video"): the timer already ticks at
        # the effective fps; decode `step` source frames per tick and show
        # the last one so the presentation rate matches the duration.
        ok, frame = self._decode_one()
        if not ok:
            return
        for _ in range(step - 1):
            ok, frame = self._decode_one()
            if not ok:
                return
        self._current = self._paint_frame(frame)
        self._presented += 1
        self._prepare_current()
        self.update()

    def _start_fade(self) -> None:
        self._video_timer.stop()
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
        self._fade_in.stop()
        if self._cap is not None:
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
