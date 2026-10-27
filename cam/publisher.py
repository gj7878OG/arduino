"""Background capture thread for the C270 USB webcam.

Holds the latest JPEG-encoded frame in a thread-safe buffer so the
dashboard's MJPEG endpoint always serves the freshest frame at full
client-side speed, regardless of how many tabs are watching.

Lifespan mirrors ``lidar.publisher.PointsPublisher``:

* ``start()`` opens the device and spawns the worker thread;
* ``stop()`` is idempotent and joins within ~2 s;
* ``latest_jpeg()`` and ``status()`` are safe to call from any thread.

The C270 is negotiated to MJPEG @ 640x480 once at open(). If the camera
disappears mid-run (USB hot-unplug) the worker logs at most one warning
every 10 s and keeps trying to reopen — the dashboard reports
``connected=false`` so the operator can replug without restarting
``web.py``.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Optional

import cv2

log = logging.getLogger('agv.cam')

# Reasonable default. The C270 streams at 30fps native; throttling to
# ~12fps cuts CPU by ~2.5x with no perceptible lag in a 480p feed.
DEFAULT_FPS = 12
JPEG_QUALITY = 80
# The C270 returns ~25 black frames (mean brightness ~2) for ~1.1 s after
# every open, then jumps to a normal exposure. Skipping a fixed few frames let
# that black through, so wait for real light instead, with a cap so a lens cap
# or a dark room still goes live.
WARMUP_MIN_BRIGHTNESS = 12.0
WARMUP_MAX_FRAMES = 60

_RETRY_S = 5.0         # reopen cadence when the device is missing
_WARN_EVERY_S = 10.0   # cap for repeated error spam


class CamPublisher:
    def __init__(self,
                 device: str = '/dev/video0',
                 width: int = 640,
                 height: int = 480,
                 target_fps: float = DEFAULT_FPS,
                 jpeg_quality: int = JPEG_QUALITY) -> None:
        self._device = device
        self._width = width
        self._height = height
        self._period_s = 1.0 / max(1.0, target_fps)
        self._jpeg_quality = jpeg_quality

        self._lock = threading.Lock()
        self._jpeg: Optional[bytes] = None
        # Latest decoded frame, shared read-only with the analytics worker.
        # cap.read() hands back a fresh array every call and nothing mutates
        # it after publish, so consumers need no copy.
        self._frame = None
        self._frame_seq = 0
        self._frame_ts = 0.0
        self._fps = 0.0
        self._connected = False
        self._last_error = ''
        self._last_warn = 0.0

        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # ── lifecycle ────────────────────────────────────────────────────
    def start(self) -> 'CamPublisher':
        if self._thread is not None and self._thread.is_alive():
            return self
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._loop, name='cam-publisher', daemon=True)
        self._thread.start()
        log.info('cam publisher started -> %s @ %dx%d, target %.1f fps',
                 self._device, self._width, self._height, 1.0 / self._period_s)
        return self

    def stop(self) -> None:
        if self._stop.is_set():
            return
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None and thread.is_alive():
            thread.join(timeout=2.0)

    # ── consumer API ─────────────────────────────────────────────────
    def latest_jpeg(self) -> Optional[bytes]:
        with self._lock:
            return self._jpeg

    def latest_frame(self):
        """(frame_seq, BGR ndarray) of the newest published frame, or (0, None).

        The second consumer of the camera: uvcvideo lets only one process open
        the node, so analytics must read frames from here, never reopen it.
        Treat the array as read-only.
        """
        with self._lock:
            return self._frame_seq, self._frame

    def status(self) -> dict:
        with self._lock:
            age_ms = int(1000 * (time.time() - self._frame_ts)) if self._frame_ts else None
            return {
                'connected': self._connected,
                'fps': round(self._fps, 1),
                'age_ms': age_ms,
                'frame_seq': self._frame_seq,
                'device': self._device,
                'width': self._width,
                'height': self._height,
                'last_error': self._last_error,
            }

    # ── internals ────────────────────────────────────────────────────
    def _open(self) -> Optional[cv2.VideoCapture]:
        cap = cv2.VideoCapture(self._device, cv2.CAP_V4L2)
        if not cap.isOpened():
            return None
        cap.set(cv2.CAP_PROP_FOURCC,     cv2.VideoWriter_fourcc(*'MJPG'))
        cap.set(cv2.CAP_PROP_FRAME_WIDTH,  self._width)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self._height)
        cap.set(cv2.CAP_PROP_FPS,         30)
        cap.set(cv2.CAP_PROP_BUFFERSIZE,  1)
        return cap

    def _publish_error(self, msg: str) -> None:
        """Cap warning spam; keep the most-recent error visible to the UI."""
        with self._lock:
            self._connected = False
            self._last_error = msg
        now = time.monotonic()
        if now - self._last_warn >= _WARN_EVERY_S:
            self._last_warn = now
            log.warning('cam: %s', msg)

    def _loop(self) -> None:
        cap: Optional[cv2.VideoCapture] = self._open()
        if cap is None:
            self._publish_error(f'could not open {self._device}; will retry every {_RETRY_S}s')

        drop = 0
        last_open_try = 0.0
        last_tick = 0.0
        fps_smooth = 0.0

        while not self._stop.is_set():
            now = time.monotonic()
            if cap is None:
                if now - last_open_try >= _RETRY_S:
                    last_open_try = now
                    cap = self._open()
                    if cap is not None:
                        log.info('cam: reopened %s', self._device)
                        drop = 0
                self._stop.wait(min(_RETRY_S, 0.5))
                continue

            ok, frame = cap.read()
            if not ok or frame is None:
                cap.release()
                self._publish_error('read() returned no frame')
                cap = None
                continue

            # Set connected ONLY after a usable frame has come through.
            with self._lock:
                self._connected = True
                self._last_error = ''

            # Hold back the C270's black auto-exposure ramp (see the
            # WARMUP_* constants) so the dashboard never shows it.
            if drop < WARMUP_MAX_FRAMES and frame.mean() < WARMUP_MIN_BRIGHTNESS:
                drop += 1
                self._stop.wait(self._period_s / 4)
                continue
            drop = WARMUP_MAX_FRAMES

            jpeg = cv2.imencode(
                '.jpg', frame,
                [int(cv2.IMWRITE_JPEG_QUALITY), self._jpeg_quality])[1].tobytes()
            ts = time.time()
            with self._lock:
                self._jpeg = jpeg
                self._frame = frame
                self._frame_ts = ts
                self._frame_seq += 1

            # FPS smoothing (warm-up reset so the first live window shows
            # the realistic number, not the load-time spurt).
            dt = time.monotonic() - last_tick
            last_tick = time.monotonic()
            if dt > 0:
                instant = 1.0 / dt
                # First post-warmup sample kicks the filter cleanly.
                fps_smooth = instant if fps_smooth == 0 else 0.8 * fps_smooth + 0.2 * instant
                with self._lock:
                    self._fps = fps_smooth

            # Pace the loop. wait() responds immediately to stop() instead
            # of being forced to sleep through the period.
            self._stop.wait(self._period_s)

        if cap is not None:
            cap.release()
        log.info('cam publisher stopped')


__all__ = ['CamPublisher']
