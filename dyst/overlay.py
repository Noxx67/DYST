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

import cv2
import numpy as np
from PIL import Image
from PySide6.QtCore import QPropertyAnimation, QRect, Qt, QTimer, QUrl, Signal
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

    Fades windowOpacity 1→0 over fade_seconds, then emits `finished`.
    """

    finished = Signal()
    FRAME_MS = 33  # ≈30 fps video playback

    def __init__(self, parent=None):
        super().__init__(parent)
        self._path: str | None = None
        self._kind: str | None = None
        self._image_seconds = 1.0
        self._fade_seconds = 0.2
        self._audio_volume = 1.0
        self._current: QImage | None = None
        self._cap: cv2.VideoCapture | None = None
        self._player: QMediaPlayer | None = None       # video (+own audio) player
        self._audio_player: QMediaPlayer | None = None  # sidecar / extracted audio
        self._audio_output: QAudioOutput | None = None
        self._temp_audio: str | None = None
        self._sidecar: str | None = None
        self._mode = "fit"
        self._fps = 30.0
        self._frame_index = 0
        self._gif_frames: list[QImage] = []
        self._gif_frame_index = 0
        self._gif_timer: QTimer | None = None

        self._video_timer = QTimer(self)
        self._video_timer.setInterval(self.FRAME_MS)
        self._video_timer.timeout.connect(self._next_frame)

        self._fade = QPropertyAnimation(self, b"windowOpacity", self)
        self._fade.finished.connect(self._finish_close)

        self.setWindowFlags(
            Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool
        )
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)

        screen = QApplication.primaryScreen()
        if screen is not None:
            self.setGeometry(screen.geometry())
        # Re-assert topmost after geometry is set (spec: re-assert on show).
        self.setWindowFlags(self.windowFlags() | Qt.WindowStaysOnTopHint)
        self.raise_()

    # -- public -----------------------------------------------------------

    def load(self, path: str, kind: str, image_seconds: float = 1.0,
             fade_seconds: float = 0.2, audio_volume: float = 1.0,
             mode: str = "fit", sidecar_audio: str | None = None) -> bool:
        """Prepare media for display. Returns False on load failure.

        Pass kind="video" for OpenCV playback (no audio, for future chroma),
        kind="video-qt" for QtMultimedia playback, or kind="video-av1" for
        AV1 (OpenCV video + ffmpeg-extracted audio).
        mode: "fit" (preserve aspect, letterbox) | "cover" (fill, crop) |
              "stretch" (fill, squish).
        sidecar_audio: paired audio file; if given it WINS over the video's
              own audio (spec: sidecar > embedded > silent) and gives
              images sound too.
        """
        self._path = path
        self._kind = kind
        self._image_seconds = max(0.05, image_seconds)
        self._fade_seconds = max(0.0, fade_seconds)
        self._audio_volume = max(0.0, min(1.0, audio_volume))
        self._mode = mode if mode in ("fit", "cover", "stretch") else "fit"
        self._sidecar = sidecar_audio if (sidecar_audio and os.path.isfile(sidecar_audio)) else None

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
                            self._gif_timer.start(self._gif_durations[0])

                            if self._sidecar:
                                self._create_audio_player(self._sidecar)
                            return True
                except Exception as exc:
                    log.warning("overlay: cannot load gif %s (%s)", path, exc)


        if kind in ("video", "video-qt", "video-av1"):
            if kind == "video-qt":
                return self._load_video_qt(path)
            if kind == "video-av1":
                return self._load_video_av1(path)
            return self._load_video_cv(path)

        log.error("overlay: unknown kind %r", kind)
        return False

    def start(self) -> None:
        """Begin playback: image timer, Qt video, or OpenCV frame loop."""
        if self._kind == "image":
            if self._audio_player is not None:
                self._audio_player.play()  # sidecar audio over the image
                        
            if self._gif_frames:
                # DO NOT call self._gif_timer.start() here without args; load() already started frame 0.
                # GIFs: play through once, then close instantly (no fade).
                gif_duration = getattr(self, "_gif_duration_ms", len(self._gif_frames) * 50)
                delay = max(int(self._image_seconds * 1000), gif_duration)
                QTimer.singleShot(delay, self._finish_close)
            else:
                QTimer.singleShot(int(self._image_seconds * 1000), self._start_fade)
                
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
            self._video_timer.setInterval(max(1, int(1000.0 / self._fps)))
        self._frame_index = 0  # frame 0 already consumed above
        self._current = self._frame_to_qimage(frame)
        return True

    def _create_audio_player(self, path: str) -> None:
        """Dedicated audio player (sidecar or extracted track)."""
        audio_out = QAudioOutput(self)
        audio_out.setVolume(self._audio_volume)
        self._audio_output = audio_out
        player = QMediaPlayer(self)
        player.setAudioOutput(audio_out)
        player.setSource(QUrl.fromLocalFile(path))
        self._audio_player = player

    def _load_video_av1(self, path: str) -> bool:
        """AV1 path: OpenCV software-decodes the video (no AV1 hwaccel spam).
        Audio: sidecar wins; else extract the embedded track to a temp file
        played via Qt (audio-only playback -> no video decode -> no errors).
        """
        if not self._load_video_cv(path):
            return False
        if self._sidecar:
            self._create_audio_player(self._sidecar)  # sidecar wins; no temp file
            return True
        audio_path = ffmpeg_util.extract_audio(path)
        if audio_path is None:
            return True  # video only (ffmpeg missing / extraction failed)
        self._temp_audio = audio_path
        self._create_audio_player(audio_path)
        return True

    def _load_video_qt(self, path: str) -> bool:
        """QtMultimedia video path: audio plays, modern codecs decode.
        If a sidecar exists, the video's own audio is muted and the sidecar
        is played on a second player (sidecar wins per spec)."""
        audio_out = QAudioOutput(self)
        audio_out.setVolume(0.0 if self._sidecar else self._audio_volume)
        self._audio_output = audio_out

        player = QMediaPlayer(self)
        player.setAudioOutput(audio_out)
        player.setVideoSink(QVideoSink(self))
        player.setSource(QUrl.fromLocalFile(path))
        player.videoSink().videoFrameChanged.connect(self._on_qt_frame)
        player.mediaStatusChanged.connect(self._on_qt_status)
        self._player = player
        if self._sidecar:
            self._create_audio_player(self._sidecar)
        return True

    def _on_qt_frame(self, frame) -> None:
        img = frame.toImage()
        if not img.isNull():
            self._current = img
            self.update()

    def _on_qt_status(self, status) -> None:
        if status == QMediaPlayer.MediaStatus.EndOfMedia:
            self._finish_close()  # video ended: end instantly (no fade)
        elif status == QMediaPlayer.MediaStatus.LoadFailed:
            # In Qt6 the LoadFailed member does not exist; treat as end/error
            self._finish_close()
        elif status in (QMediaPlayer.MediaStatus.InvalidMedia,
                        QMediaPlayer.MediaStatus.UnknownMediaStatus):
            log.error("overlay: media error for %s (status %s)", self._path, status)
            self._finish_close()
        else:
            # any other status – keep playing unless explicitly errored
            pass


    def _advance_gif_frame(self) -> None:
        if not self._gif_frames or not hasattr(self, "_gif_durations"):
            return
        self._gif_frame_index = (self._gif_frame_index + 1) % len(self._gif_frames)
        self._current = self._gif_frames[self._gif_frame_index]
        self.update()

        # Re-arm the single-shot timer for the next frame's duration
        if self._gif_timer is not None:
            next_delay = self._gif_durations[self._gif_frame_index]
            self._gif_timer.start(next_delay)

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
            target = int(self._audio_player.position() * self._fps / 1000.0)
            if target > self._frame_index:
                # We're behind the audio: advance / drop frames to catch up.
                while self._frame_index < target:
                    ok, frame = self._cap.read()
                    if not ok:
                        self._finish_close()  # end of video: end instantly
                        return
                    self._frame_index += 1
                    self._current = self._frame_to_qimage(frame)
                self.update()
            # else: ahead or exactly on time -> keep showing the current frame
            # (the timer naturally paces us; no read, no seek).
            return
        ok, frame = self._cap.read()
        if not ok:
            self._finish_close()  # end of video: end instantly (no fade)
            return
        self._current = self._frame_to_qimage(frame)
        self.update()

    def _start_fade(self) -> None:
        self._video_timer.stop()
        if self._player is not None:
            self._player.stop()
        if self._audio_player is not None:
            self._audio_player.stop()
        if self._gif_timer is not None:
            self._gif_timer.stop()
            self._gif_timer.deleteLater()
            self._gif_timer = None
        self._fade.setDuration(int(self._fade_seconds * 1000))
        self._fade.setStartValue(1.0)
        self._fade.setEndValue(0.0)
        self._fade.start()

    def _finish_close(self) -> None:
        self._video_timer.stop()
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
        self._gif_frames = []
        self._gif_frame_index = 0
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
        if self._mode == "stretch":
            painter.drawImage(self.rect(), img)
        elif self._mode == "cover":
            # Scale to fill the window, then crop the overflow (centered).
            sw = max(self.width() / img.width(), self.height() / img.height())
            dw, dh = int(img.width() * sw), int(img.height() * sw)
            src = QRect((dw - self.width()) // 2, (dh - self.height()) // 2,
                        self.width(), self.height())
            painter.drawImage(self.rect(), img, src)
        else:  # fit (default): preserve aspect ratio, letterbox, centered
            scaled = img.scaled(
                self.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation
            )
            painter.drawImage(
                (self.width() - scaled.width()) // 2,
                (self.height() - scaled.height()) // 2,
                scaled,
            )