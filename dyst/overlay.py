"""DYST (did you see that? 👀) — overlay window.

One frameless, always-on-top, click-through, taskbar-less window that shows
one image or plays one video, then fades out and emits `finished`.

Playback kinds:
  "image"       — still image for image_seconds, then fade
  "video"       — OpenCV frame decode (no audio; used by future chroma)
  "video-qt"    — QtMultimedia (audio + modern codecs) for non-AV1 videos
  "video-av1"   — AV1: OpenCV software video (no hwaccel errors) + ffmpeg-
                   extracted temp audio played by Qt (see ffmpeg_util)

Monitor selection is NOT implemented yet (always primary).
"""

from __future__ import annotations

import logging
import os
import random

import cv2
import numpy as np
from PIL import Image
from PySide6.QtCore import QPropertyAnimation, QRect, QRectF, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QImage, QPainter
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer, QVideoSink
from PySide6.QtWidgets import QApplication, QWidget

from dyst import ffmpeg_util

log = logging.getLogger("dyst.overlay")


class OverlayWindow(QWidget):
    """Plays one media item on top of everything, then fades out.

    kind: "image" (shown for image_seconds)
          "video" (played to the end)
          "video-qt" (video via QtMultimedia: has AUDIO, supports AV1)

    Video playback routes through QtMultimedia (QMediaPlayer) so audio plays
    and modern codecs (AV1) work via system codecs. The OpenCV path remains
    for the future chroma-key frame pipeline (Phase 3) which needs
    frame-by-frame access.

    Fades windowOpacity 1→0 over fade_out_seconds, then emits `finished`.
    """

    finished = Signal()
    FRAME_MS = 33  # ≈30 fps video playback

    def __init__(self, parent=None):
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
        self._fade_started = False  # True once the fade-out animation has begun
        self._fade_done = False     # True once the fade-out animation has completed

        self._video_timer = QTimer(self)
        self._video_timer.setInterval(self.FRAME_MS)
        self._video_timer.timeout.connect(self._next_frame)

        self._fade = QPropertyAnimation(self, b"windowOpacity", self)
        self._fade.finished.connect(self._fade_finished)

        self._fade_in = QPropertyAnimation(self, b"windowOpacity", self)
        self._fade_in.finished.connect(self._on_fade_in_finished)

        self.setWindowFlags(
            Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool | Qt.WindowTransparentForInput
        )
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)

        screen = QApplication.primaryScreen()
        if screen is not None:
            g = screen.geometry()
            # Center window on screen
            self.setGeometry(g.center().x() - g.width() // 2, g.center().y() - g.height() // 2, g.width(), g.height())
        # Re-assert topmost after geometry is set (spec: re-assert on show).
        self.setWindowFlags(self.windowFlags() | Qt.WindowStaysOnTopHint)
        self.raise_()
        self.setCursor(Qt.BlankCursor)

    def showEvent(self, event):
        super().showEvent(event)
        # Ensure mouse transparency is retained after show (some platforms may reset)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)

    # -- public -----------------------------------------------------------

    def load(self, path: str, kind: str, image_seconds: float = 1.0,
             fade_out_seconds: float = 0.2, volume: float = 1.0,
             mode: str = "fit", sidecar_audio: str | None = None,
             custom: dict | None = None,
             max_duration: float = 0.0,
             speed: float = 1.0, pitch: float = 1.0,
             fade_in_seconds: float = 0.0,
             opacity: float = 1.0) -> bool:
        """Prepare media for display. Returns False on load failure.

        Pass kind="video" for OpenCV playback (no audio, for future chroma),
        kind="video-qt" for QtMultimedia playback, or kind="video-av1" for
        AV1 (OpenCV video + ffmpeg-extracted audio).
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
        self._volume = max(0.0, min(1.0, volume))
        self._mode = mode if mode in ("fit", "cover-height", "cover-width", "stretch", "custom") else "fit"
        self._apply_custom(custom or {})
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
        self._display_ms = 0  # display-phase duration in ms (set in start())
        if self._fade_in_seconds > 0:
            # Be invisible from the moment the window is shown (start() then
            # animates opacity 0->base opacity).
            self.setWindowOpacity(0.0)
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
                if self._sidecar:
                    self._create_audio_player(self._sidecar)
                return True


        if kind in ("video", "video-qt", "video-av1"):
            if kind == "video-qt":
                return self._load_video_qt(path)
            if kind == "video-av1":
                return self._load_video_av1(path)
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

    def _custom_target(self, img_w: float, img_h: float):
        """Custom-mode layout for a source of size img_w x img_h.

        Scale is relative to the aspect-preserving "fit" size, so
        (scale_x, scale_y) == (1, 1) shows the whole media with nothing
        cropped off screen. Position is normalized edge-pinning:
        x = (screen_w - disp_w) * position_x -> 0 pins the left edge to the
        screen's left edge, 1 pins the right edge to the screen's right edge,
        0.5 centers. Values outside 0..1 are allowed (clamped to -1..2) so
        media can peek in from the edges or be pushed off-screen and cropped
        at the screen boundary (Qt clips painting to the widget).
        Returns (x, y, disp_w, disp_h).
        """
        if not img_w or not img_h:
            return 0.0, 0.0, 0.0, 0.0
        fit = min(self.width() / img_w, self.height() / img_h)
        disp_w = img_w * fit * self._scale_x
        disp_h = img_h * fit * self._scale_y
        x = (self.width() - disp_w) * self._position_x
        y = (self.height() - disp_h) * self._position_y
        return x, y, disp_w, disp_h

    def start(self) -> None:
        """Begin playback: image timer, Qt video, or OpenCV frame loop."""
        # Base opacity for the whole overlay (fades compose on top of it).
        self.setWindowOpacity(self._opacity)
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
            if self._fade_in_seconds > 0:
                # Fade in FIRST (opacity 0 -> base opacity); the display clock
                # starts when it completes, so lifetime = fade_in + display +
                # fade_out.
                self.setWindowOpacity(0.0)
                self._fade_in.setDuration(int(self._fade_in_seconds * 1000 / self._speed))
                self._fade_in.setStartValue(0.0)
                self._fade_in.setEndValue(self._opacity)
                self._fade_in.start()
            else:
                self._image_end_timer.start(self._display_ms)
                
        elif self._kind == "video-qt":
            if self._player is not None:
                self._player.play()
            if self._audio_player is not None:
                self._audio_player.play()  # sidecar
        else:  # "video" / "video-av1"
            if self._cap is not None:
                self._video_timer.start()
                if self._kind == "video-av1" and self._audio_player is not None:
                    self._audio_player.play()

    # -- internals --------------------------------------------------------

    def _load_video_cv(self, path: str) -> bool:
        """OpenCV video path (frames only, no audio) — used by future chroma."""
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
        # Play at the video's real frame rate (fallback ~30fps).
        self._fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
        if self._fps > 1:
            # Speed multiplier: interval = base / speed (faster = shorter).
            self._video_timer.setInterval(max(1, int(1000.0 / (self._fps * self._speed))))
        self._frame_index = 0  # frame 0 already consumed above
        self._current = self._frame_to_qimage(frame)
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

    def _load_video_av1(self, path: str) -> bool:
        """AV1 path: OpenCV software-decodes the video (no AV1 hwaccel spam).
        Audio: sidecar wins; else extract the embedded track to a temp file
        played via Qt (audio-only playback -> no video decode -> no errors).
        Speed/pitch are baked into whatever audio plays."""
        if not self._load_video_cv(path):
            return False
        if self._sidecar:
            self._create_audio_player(self._sidecar)  # sidecar wins; no temp file
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
            self._current = img
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

    def _audio_end(self, status) -> None:
        """Callback for the sidecar / extracted audio player (mediaStatusChanged)."""
        ended = (status == QMediaPlayer.MediaStatus.EndOfMedia or
                 getattr(QMediaPlayer.MediaStatus, "LoadFailed", None) == status)
        if ended:
            self._audio_done = True
            self._audio_player = None  # finished/errored - nothing left to wait for
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
        """Fade-in completed — start the display clock (image/GIF end timer)."""
        if self._image_end_timer is not None and not self._closing:
            self._image_end_timer.start(self._display_ms)

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
        """Fade-out animation (windowOpacity 1->0) has completed."""
        self._fade_done = True
        self._close_if_ready()

    def _close_if_ready(self) -> None:
        """Close the overlay only when both the visual fade and the audio have
        finished.  If the audio is still playing (or the fade is still
        running), wait for it to finish first."""
        if self._audio_done and self._visual_done and self._fade_done:
            self._finish_close()

    def _advance_gif_frame(self) -> None:
        if not self._gif_frames or not hasattr(self, "_gif_durations"):
            return
        self._gif_frame_index += 1

        if self._gif_frame_index >= len(self._gif_frames):
            self._gif_frame_index = len(self._gif_frames) - 1
            self._current = self._gif_frames[self._gif_frame_index]
            self.update()
            if self._gif_timer is not None:
                self._gif_timer.stop()
                self._gif_timer.deleteLater()
                self._gif_timer = None
            return
        self._current = self._gif_frames[self._gif_frame_index]
        self.update()
        if self._gif_timer is not None:
            next_delay = self._gif_durations[self._gif_frame_index] / self._speed
            self._gif_timer.start(max(1, int(next_delay)))

    def _load_image(self, path: str) -> QImage | None:
        try:
            img = Image.open(path).convert("RGBA")
        except Exception as exc:  # Pillow raises several error types
            log.error("overlay: cannot load image %s (%s)", path, exc)
            return None
        arr = np.asarray(img)
        h, w = arr.shape[:2]
        # .copy() so the QImage owns its buffer (numpy memory is reused).
        return QImage(arr.data, w, h, arr.strides[0], QImage.Format_RGBA8888).copy()

    def _frame_to_qimage(self, bgr) -> QImage:
        rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
        h, w = rgb.shape[:2]
        return QImage(rgb.data, w, h, rgb.strides[0], QImage.Format_RGB888).copy()

    def _next_frame(self) -> None:
        if self._cap is None:
            return
        if (self._kind == "video-av1" and self._audio_player is not None
                and self._audio_player.playbackState()
                == QMediaPlayer.PlaybackState.PlayingState):
            # AV1 sync WITHOUT per-frame seeks (those re-decode from the
            # nearest keyframe — very slow for AV1). Instead: decode
            # sequentially (fast) and use the audio clock only to correct
            # drift — drop frames when behind, hold still when ahead.
            # Audio is baked to tempo speed, so position already advances at
            # speed x wall-clock; frame index target stays position*fps/1000.
            target = int(self._audio_player.position() * self._fps / 1000.0)
            if target > self._frame_index:
                # We're behind the audio: advance / drop frames to catch up.
                while self._frame_index < target:
                    ok, frame = self._cap.read()
                    if not ok:
                        self._video_timer.stop()
                        self._on_visual_finished()
                        return
                    self._frame_index += 1
                    self._current = self._frame_to_qimage(frame)
                self.update()
            # else: ahead or exactly on time -> keep showing the current frame
            # (the timer naturally paces us; no read, no seek).
            return
        ok, frame = self._cap.read()
        if not ok:
            self._video_timer.stop()
            self._on_visual_finished()
            return
        self._current = self._frame_to_qimage(frame)
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
        if self._current is None or self._current.isNull():
            return
        painter = QPainter(self)
        img = self._current
        if self._mode == "custom":
            # Custom layout: position/scale/flip/rotate. Scale is relative to
            # the aspect-preserving "fit" size (1,1 = whole media visible,
            # nothing cropped); position pins edges at 0/1 and centers at 0.5;
            # rotation is around the placed rect's center.
            x, y, dw, dh = self._custom_target(img.width(), img.height())
            dimg = img
            if self._flip_h:
                dimg = dimg.mirrored(True, False)
            if self._flip_v:
                dimg = dimg.mirrored(False, True)
            painter.setRenderHint(QPainter.SmoothPixmapTransform)
            painter.translate(x + dw / 2.0, y + dh / 2.0)
            painter.rotate(self._rotation)
            painter.drawImage(QRectF(-dw / 2.0, -dh / 2.0, dw, dh), dimg)
        elif self._mode == "stretch":
            painter.setRenderHint(QPainter.SmoothPixmapTransform)
            painter.drawImage(self.rect(), img)
        elif self._mode == "cover-height":
            # "Fit the entire screen horizontally": scale so the media WIDTH
            # matches the screen width. A media proportionally taller than the
            # screen overflows vertically -> cropped at top/bottom (Qt clips to
            # the widget, so we can just center the target rect); a media wider
            # than the screen aspect letterboxes top/bottom (transparent bars
            # because the widget background is translucent).
            painter.setRenderHint(QPainter.SmoothPixmapTransform)
            scale = self.width() / img.width()
            dw, dh = self.width(), int(img.height() * scale)
            painter.drawImage(
                QRect(0, (self.height() - dh) // 2, dw, dh), img)
        elif self._mode == "cover-width":
            # "Fit the entire screen vertically": scale so the media HEIGHT
            # matches the screen height. A media proportionally wider than the
            # screen overflows horizontally -> cropped at left/right; a media
            # taller than the screen aspect letterboxes left/right.
            painter.setRenderHint(QPainter.SmoothPixmapTransform)
            scale = self.height() / img.height()
            dw, dh = int(img.width() * scale), self.height()
            painter.drawImage(
                QRect((self.width() - dw) // 2, 0, dw, dh), img)
        else:  # fit (default): preserve aspect ratio, letterbox, centered
            scaled = img.scaled(
                self.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation
            )
            painter.drawImage(
                (self.width() - scaled.width()) // 2,
                (self.height() - scaled.height()) // 2,
                scaled,
            )