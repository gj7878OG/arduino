"""Camera abstraction for the AGV.

Supports four backends:
  - argus    : NVIDIA Argus via nvarguscamerasrc (best ISP, JetPack ≤ 6)
  - v4l2     : NVIDIA V4L2 via nvv4l2camerasrc (lower latency)
  - libcamera: libcamera via libcamerasrc (no NVIDIA stack needed)
  - fake     : synthetic yellow line for offline / CI tests

The module exposes a single Camera class that owns the capture and
publishes the latest frame through a thread-safe accessor. The decision
loop never touches OpenCV capture objects directly — it asks the camera
for the next frame.
"""

from __future__ import annotations

import functools
import logging
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Optional

import cv2
import numpy as np

from config import (
    CAMERA_BACKEND, CAMERA_FLIP, CAMERA_GREEN_CAST_FIX,
    CAMERA_GREEN_CAST_MAX_RATIO, get_camera_pipeline,
    LINE_MIN_ASPECT_RATIO, LINE_BORDER_IGNORE_FRAC,
    TIER0_GREEN_GUARD_RATIO,
)

log = logging.getLogger('agv.camera')

# Reconnect policy. A dropped USB camera leaves the existing
# cv2.VideoCapture handle returning False forever — retrying read() on that
# dead handle can never recover, so the handle itself has to be rebuilt. The
# counter keeps a momentary glitch (a single bad read, an ISP hiccup) from
# tearing down a working pipeline.
REOPEN_AFTER_FAILS = 15
REOPEN_BACKOFF_S = 2.0
REOPEN_WARN_EVERY_S = 15.0


@dataclass
class CameraReport:
    """Result of probing the camera at startup."""
    backend: str
    pipeline: Optional[str]
    opened: bool
    frame_ok: bool
    error: Optional[str] = None
    latency_ms: float = 0.0


@dataclass
class FrameInfo:
    """A captured frame plus its acquisition timestamp (monotonic)."""
    image: np.ndarray
    monotonic: float
    sequence: int


class FakeCamera:
    """Synthesises a 1280x720 BGR frame with a wobbling yellow line."""

    def __init__(self, width: int = 1280, height: int = 720) -> None:
        self.width = width
        self.height = height
        self._t0 = time.monotonic()
        self._open = True

    def isOpened(self) -> bool: return self._open
    def release(self) -> None: self._open = False

    def read(self) -> tuple[bool, Optional[np.ndarray]]:
        if not self._open:
            return False, None
        frame = np.full((self.height, self.width, 3), 80, dtype=np.uint8)  # gray floor
        cx = self.width // 2 + int(120 * np.sin(time.monotonic() - self._t0))
        cv2.line(frame, (cx, 0), (cx, self.height), (0, 255, 255), 12)
        cv2.putText(frame, "FAKE CAMERA — synthetic yellow line",
                    (20, 40), cv2.FONT_HERSHEY_SIMPLEX, 1.0, (255, 255, 255), 2)
        return True, frame


class Camera:
    """Single-thread capture + thread-safe latest-frame buffer.

    The capture loop runs on a daemon thread so the decision loop is
    never blocked by GStreamer / V4L2 / libcamera latency.
    """

    def __init__(self, backend: Optional[str] = None) -> None:
        self.backend = (backend or CAMERA_BACKEND).lower()
        self.pipeline = get_camera_pipeline(self.backend)
        self._cap: Optional[cv2.VideoCapture] = None
        self._fake: Optional[FakeCamera] = None
        self._lock = threading.Lock()
        self._latest: Optional[FrameInfo] = None
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._seq = 0
        self._connected = False
        self._last_error: Optional[str] = None
        self._next_reopen = 0.0
        self._last_reopen_warn = 0.0

    # ── lifecycle ─────────────────────────────────────────────────────
    def _acquire(self) -> tuple[bool, str]:
        """Build a fresh capture handle into ``self._cap``.

        Returns ``(ok, reason)``; ``reason`` is empty on success. Used both
        by :meth:`open` and by the reader's reconnect path so the two can
        never drift apart.
        """
        if not self.pipeline:
            return False, f'unknown backend {self.backend!r}'
        try:
            cap = cv2.VideoCapture(self.pipeline, cv2.CAP_GSTREAMER)
        except Exception as e:
            return False, f'{type(e).__name__}: {e}'
        if not cap.isOpened():
            cap.release()
            return False, 'cv2.VideoCapture.isOpened() returned False'
        ret, frame = cap.read()
        if not ret or frame is None:
            cap.release()
            return False, 'pipeline opened but produced no frames'
        self._cap = cap
        return True, ''

    def _reacquire(self) -> bool:
        """Drop the current handle and rebuild the pipeline.

        Called from the reader thread whenever the capture goes away for
        good. Failures are throttled in the log because a genuinely absent
        camera retries forever.
        """
        cap, self._cap = self._cap, None
        if cap is not None:
            try:
                cap.release()
            except Exception:
                pass
        ok, reason = self._acquire()
        if ok:
            self._last_error = ''
            log.info('[Camera] pipeline reacquired')
            return True
        self._last_error = reason
        now = time.monotonic()
        if now - self._last_reopen_warn >= REOPEN_WARN_EVERY_S:
            self._last_reopen_warn = now
            log.warning('[Camera] reconnect failed: %s (retrying every %.0fs)',
                        reason, REOPEN_BACKOFF_S)
        return False

    def open(self) -> CameraReport:
        t0 = time.monotonic()
        if self.backend == 'fake':
            self._fake = FakeCamera()
            self._start_reader()
            return CameraReport(
                backend='fake', pipeline=None, opened=True, frame_ok=True,
                latency_ms=round((time.monotonic() - t0) * 1000, 1),
            )
        ok, reason = self._acquire()
        if not ok:
            self._last_error = reason
            # Start the reader anyway: it owns the retry loop, so a camera
            # that is merely not plugged in yet (or still booting) comes
            # up on its own instead of needing the whole AGV restarted.
            self._start_reader()
            return CameraReport(
                self.backend, self.pipeline,
                # The distinction the probe reports: the pipeline itself
                # opened, it just had nothing to show.
                reason != 'pipeline opened but produced no frames',
                False, reason,
            )
        # `connected` is not set here: the reader flips it once a frame is
        # genuinely published, so callers that read `connected` and
        # `latest()` together never see "live" with an empty buffer.
        self._start_reader()
        return CameraReport(
            backend=self.backend, pipeline=self.pipeline, opened=True, frame_ok=True,
            latency_ms=round((time.monotonic() - t0) * 1000, 1),
        )

    def release(self) -> None:
        self._running = False
        if self._thread:
            # The reader may be mid-backoff (up to 2s), so allow a little
            # more than that or the thread outlives release().
            self._thread.join(timeout=3)
            self._thread = None
        if self._cap:
            self._cap.release()
            self._cap = None
        if self._fake:
            self._fake.release()
            self._fake = None
        self._connected = False

    def isOpened(self) -> bool:
        return self._connected

    # ── latest frame ──────────────────────────────────────────────────
    def latest(self) -> Optional[FrameInfo]:
        with self._lock:
            return self._latest

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def last_error(self) -> Optional[str]:
        return self._last_error

    # ── internal reader ──────────────────────────────────────────────
    def _start_reader(self) -> None:
        self._running = True
        self._thread = threading.Thread(
            target=self._reader, name='camera-reader', daemon=True,
        )
        self._thread.start()

    def _reader(self) -> None:
        backoff = 0.05
        fails = 0
        while self._running:
            if self._fake is not None:
                ret, frame = self._fake.read()
            elif self._cap is None:
                # Never opened, or the handle was dropped. Rebuild the
                # pipeline; this is the branch that makes a camera that is
                # absent at startup come up on its own once it is plugged
                # in or finishes booting.
                if time.monotonic() >= self._next_reopen:
                    self._next_reopen = time.monotonic() + REOPEN_BACKOFF_S
                    self._reacquire()
                time.sleep(0.2)
                continue
            else:
                try:
                    ret, frame = self._cap.read()
                except cv2.error as exc:
                    ret, frame = False, None
                    self._last_error = f'cv2.error during read: {exc}'
                    log.error('[Camera] %s', self._last_error)
                except Exception as exc:
                    ret, frame = False, None
                    self._last_error = f'{type(exc).__name__}: {exc}'
                    log.error('[Camera] %s', self._last_error)

            if not ret or frame is None:
                self._connected = False
                self._last_error = self._last_error or 'read() returned False'
                fails += 1
                # Only rebuild once the handle is consistently dead — a
                # single dropped frame is just backoff.
                if fails >= REOPEN_AFTER_FAILS and time.monotonic() >= self._next_reopen:
                    fails = 0
                    self._next_reopen = time.monotonic() + REOPEN_BACKOFF_S
                    log.warning('[Camera] no frames for %d reads; reconnecting',
                                REOPEN_AFTER_FAILS)
                    self._reacquire()
                time.sleep(backoff)
                backoff = min(backoff * 2, 2.0)
                continue
            fails = 0
            backoff = 0.05
            self._seq += 1
            # Apply configured software flip. CAMERA_FLIP=0 is a no-op
            # so the default behaviour is unchanged. flipCode -1 is
            # "rotate 180°" (both axes) — use that when the camera is
            # physically mounted upside-down.
            flip_code = CAMERA_FLIP
            if flip_code not in (-1, 0, 1):
                flip_code = 0
            if flip_code != 0:
                frame = cv2.flip(frame, flip_code)
            # Display-side green-cast correction. The detector still
            # uses gray-world WB inside its own pipeline; this is
            # purely so the dashboard image looks right when the ISP
            # produces a green-tinted scene.
            if CAMERA_GREEN_CAST_FIX:
                frame = _green_cast_correction(
                    frame, max_ratio=CAMERA_GREEN_CAST_MAX_RATIO,
                )
            with self._lock:
                self._latest = FrameInfo(image=frame, monotonic=time.monotonic(),
                                         sequence=self._seq)
            # Only claim the camera is live once the frame is actually
            # published: consumers read `connected` and `latest()` together,
            # and flipping it first would advertise a live camera whose
            # buffer is still empty (the window right after a reconnect).
            self._connected = True
            self._last_error = ''


# Convenience functions kept for backward compatibility with code that
# used the old module pattern.

def detect_yellow_centroid(
    frame: np.ndarray,
    lower: tuple[int, int, int],
    upper: tuple[int, int, int],
    min_area: int,
    kernel: tuple[int, int] = (5, 5),
) -> Optional[tuple[int, int, int]]:
    """Return (cx, cy, area) of the largest yellow contour, or None.

    Multi-tier detection. Tries increasingly permissive colour ranges
    so the line stays locked under different lighting and tape colours:

      Tier 0: gray-world WB + BGR "yellow" tier (G > threshold AND R > B).
              After gray-world normalisation, real yellow tape reads as
              a high-G, slightly-warm pixel regardless of the room's
              colour cast. This is the primary tier and handles the
              "blue room turns yellow tape green" problem by ignoring
              hue entirely and just measuring the *shape* of the yellow
              signal in BGR. Works on light-blue / wood / gray floors
              alike. Lives above the floor (lower 2/3 of the frame).
      Tier 1: configured tight yellow range  (H ~20-35, saturated)
      Tier 2: same hue, relaxed S/V          (catches washed-out tape)
      Tier 3: wider warm range               (yellow → chartreuse)
      Tier 4: orange + yellow + yellow-green (H 0-70)
      Tier 5: green range                    (catches "yellow" tape that
                                              the camera has rendered green
                                              under bluish room lighting)
      Tier 6: "warm cream" BGR tier          (fallback for the case where
                                              the camera's ISP is still on
                                              and pushes everything to red;
                                              keeps the old logic available
                                              if a future build re-enables
                                              auto-WB.)

    Tiers 0, 3, 4, 5, 6 prefer contours in the lower 2/3 of the frame
    (the floor) unless the contour is very large (clearly the line).
    """
    h, w = frame.shape[:2]
    floor_top = h // 3   # below this Y is treated as "floor"
    k = np.ones(kernel, np.uint8)

    # ── Green-dominance guard (added by tuning plan) ─────────────────
    # Under a fluorescent room the IMX219 ISP at wbmode=0 renders the
    # whole frame green-tinted; the green maps to HSV H≈52 which also
    # satisfies the basic detector's broad warm tiers (4: H 0-70; 5:
    # H 35-90). Rather than chasing the bias through every tier, short-
    # circuit the whole basic detector to None and force the caller to
    # use the enhanced detector (now the default) for green-biased scenes.
    m = cv2.mean(frame)
    b_mean, g_mean, r_mean = float(m[0]), float(m[1]), float(m[2])
    skip_tier0 = (
        g_mean > TIER0_GREEN_GUARD_RATIO * r_mean
        and g_mean > TIER0_GREEN_GUARD_RATIO * b_mean
    )

    if skip_tier0:
        # Frame is green-biased; Tier 0 and the broad warm HSV tiers
        # (4-5) would all false-positive on the wall. Bail out entirely
        # and trust the caller (now defaulting to LINE_DETECTOR=enhanced)
        # to handle this scene.
        return None

    # Tier 0: gray-world WB + BGR "yellow" test.
    #
    # Yellow is high R + high G + low B. After gray-world normalisation
    # the room's colour cast is removed and the floor's average RGB is
    # approximately equal — the tape's "high G, R > B" signature then
    # becomes a robust discriminator that doesn't care about hue. This
    # is what makes detection work in a pink-fluorescent room (where
    # hue ends up anywhere from 0 to 150) and would equally work in a
    # blue room (where hue gets pushed green).
    wb_frame = _gray_world_white_balance(frame)
    b_w, g_w, r_w = cv2.split(wb_frame)
    r_wi = r_w.astype(np.int16)
    b_wi = b_w.astype(np.int16)
    g_wi = g_w.astype(np.int16)
    # G > 120 catches the high-green of yellow (yellow ≈ R=G high, B low)
    # R > B excludes blue floor (light-blue floor has B > R even after WB)
    yellow_bgr = (g_wi > 120) & (r_wi > b_wi)
    mask_y = np.where(yellow_bgr, 255, 0).astype(np.uint8)
    # Constrain to the floor (lower 2/3 of the frame) before morphology
    # so a bright window/wall above the AGV cannot feed the centroid.
    mask_y[:floor_top, :] = 0
    mask_y = cv2.morphologyEx(mask_y, cv2.MORPH_OPEN, k)
    mask_y = cv2.morphologyEx(mask_y, cv2.MORPH_CLOSE, k)
    contours_y, _ = cv2.findContours(
        mask_y, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE,
    )
    if contours_y:
        contour = max(contours_y, key=cv2.contourArea)
        area = int(cv2.contourArea(contour))
        if area >= min_area:
            moments = cv2.moments(contour)
            if moments['m00'] != 0:
                cx = int(moments['m10'] / moments['m00'])
                cy = int(moments['m01'] / moments['m00'])

                # Aspect-ratio filter (added by tuning plan). Rejects
                # round blobs (e.g. the E&TC cardboard box). Disabled
                # when LINE_MIN_ASPECT_RATIO <= 1.0. If the filter
                # rejects the contour we DO NOT return — we fall through
                # to the HSV tiers so the detector keeps trying.
                passes_filters = True
                if LINE_MIN_ASPECT_RATIO > 1.0:
                    rect = cv2.minAreaRect(contour)
                    bw, bh = rect[1]
                    longer = max(float(bw), float(bh), 1e-6)
                    shorter = max(min(float(bw), float(bh)), 1e-6)
                    if longer / shorter < LINE_MIN_ASPECT_RATIO:
                        passes_filters = False

                # Border filter (added by tuning plan). Rejects contours
                # that touch the frame edge (e.g. tape leaving the FOV).
                if passes_filters and LINE_BORDER_IGNORE_FRAC > 0.0:
                    border = int(w * LINE_BORDER_IGNORE_FRAC)
                    if border > 0 and (cx < border or cx > w - border):
                        passes_filters = False

                if passes_filters:
                    return cx, cy, area

    hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)

    def _scan(low_h: int, high_h: int, min_s: int, min_v: int,
              min_area_override: Optional[int] = None
    ) -> Optional[tuple[int, int, int]]:
        mask = cv2.inRange(
            hsv,
            np.array((low_h, min_s, min_v)),
            np.array((high_h, 255, 255)),
        )
        mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, k)
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, k)
        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        if not contours:
            return None
        contour = max(contours, key=cv2.contourArea)
        area = int(cv2.contourArea(contour))
        threshold = min_area_override if min_area_override is not None else min_area
        if area < threshold:
            return None
        moments = cv2.moments(contour)
        if moments['m00'] == 0:
            return None
        cx = int(moments['m10'] / moments['m00'])
        cy = int(moments['m01'] / moments['m00'])
        return cx, cy, area

    # Tier 1: configured range, anywhere in the frame
    result = _scan(lower[0], upper[0], lower[1], lower[2])
    if result is not None:
        return result

    # Tier 2: same hue, relaxed saturation/value
    result = _scan(lower[0], upper[0], 50, 50)
    if result is not None:
        return result

    # Tier 3: broader warm range — yellow-green / chartreuse
    result = _scan(10, 45, 50, 50)
    if result is not None:
        cx, cy, area = result
        if cy >= floor_top or area >= 4 * min_area:
            return result

    # Tier 4: orange → yellow → chartreuse (H 0-70, slightly stricter)
    result = _scan(0, 70, 60, 60)
    if result is not None:
        cx, cy, area = result
        if cy >= floor_top or area >= 8 * min_area:
            return result

    # Tier 5: GREEN range — last resort. Under very blue room lighting,
    # a yellow tape can render as green-cyan. Catches the line even
    # when the camera's white balance is fighting us.
    # Require a high saturation so we don't pick up a green floor.
    result = _scan(35, 90, 100, 80)
    if result is not None:
        cx, cy, area = result
        if cy >= floor_top or area >= 6 * min_area:
            return result

    # Tier 6: "warm cream" BGR tier. Celo / masking tape whose yellow hue
    # has been completely destroyed by a strong pink/purple room cast ends
    # up almost neutral in HSV (H ~ 130-155, looks blue in colour wheel).
    # What still separates tape from the desk is warmth: tape has R ≥ B
    # while the cool-pink desk has R < B. The brightness band is restricted
    # to mid-bright (V 140-215) so we don't pick up pure white walls.
    b, g, r = cv2.split(frame)
    r_i = r.astype(np.int16)
    b_i = b.astype(np.int16)
    warm = (r_i - b_i >= -2) & (b >= 140) & (b <= 215)
    mask_w = np.where(warm, 255, 0).astype(np.uint8)
    mask_w = cv2.morphologyEx(mask_w, cv2.MORPH_OPEN, k)
    mask_w = cv2.morphologyEx(mask_w, cv2.MORPH_CLOSE, k)
    contours, _ = cv2.findContours(
        mask_w, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE,
    )
    if contours:
        contour = max(contours, key=cv2.contourArea)
        area = int(cv2.contourArea(contour))
        if area >= min_area:
            moments = cv2.moments(contour)
            if moments['m00'] != 0:
                cx = int(moments['m10'] / moments['m00'])
                cy = int(moments['m01'] / moments['m00'])
                # Constrain to the lower 2/3 of the frame unless very large,
                # so a bright wall / paper sheet at the top can't steal it.
                if cy >= floor_top or area >= 6 * min_area:
                    return cx, cy, area

    return None


# ──────────────────────────────────────────────────────────────────────────────
# Enhanced detector — yellow tape on a blue surface in low ambient light
# ──────────────────────────────────────────────────────────────────────────────

@functools.lru_cache(maxsize=8)
def _make_clahe(clip_limit: float, tile_grid: int) -> cv2.CLAHE:
    """Cache one CLAHE object per (clip, grid) pair; the GPU/CPU cost of
    recreating cv2.CLAHE on every frame is wasteful at 60 fps."""
    return cv2.createCLAHE(
        clipLimit=float(clip_limit),
        tileGridSize=(int(tile_grid), int(tile_grid)),
    )


def _gray_world_white_balance(frame: np.ndarray) -> np.ndarray:
    """Gray-world white balance: scale each BGR channel so the channel means
    match the overall mean. Counters strong color casts from the floor
    surface (blue floor tints everything toward blue) and from cheap
    fluorescent / LED room lighting.

    Implemented with ``cv2.mean`` (per-channel mean in 1.3 ms) and a
    256-entry per-channel LUT applied with ``cv2.LUT`` (~1.5 ms/channel).
    End-to-end ~4 ms at 720p on Jetson, vs 41 ms for the obvious
    ``frame.astype(np.float32) * scale`` implementation.
    """
    # cv2.mean returns (b, g, r, a) for BGR(A); take the first three.
    # This is ~18× faster than frame.mean(axis=(0,1)) because it does not
    # materialise a 64 MB float64 intermediate.
    m = cv2.mean(frame)
    avg_bgr = np.array(m[:3], dtype=np.float32)
    avg_gray = float(avg_bgr.mean())
    if avg_gray < 1.0:
        return frame
    scale = (avg_gray / np.maximum(avg_bgr, 1.0)).astype(np.float32)
    # Build a (256, 3) LUT: lut[v, c] = clip(v * scale[c])
    lut = np.clip(
        np.arange(256, dtype=np.float32)[:, None] * scale[None, :],
        0, 255,
    ).astype(np.uint8)
    b, g, r = cv2.split(frame)
    b = cv2.LUT(b, lut[:, 0])
    g = cv2.LUT(g, lut[:, 1])
    r = cv2.LUT(r, lut[:, 2])
    return cv2.merge([b, g, r])


def _green_cast_correction(
    frame: np.ndarray,
    *,
    max_ratio: float = 1.5,
    min_mean: float = 20.0,
) -> np.ndarray:
    """Counteract a strong monochromatic cast (e.g. a green-tinted room
    under cheap fluorescent light) by rescaling each BGR channel toward
    the global mean, with a `max_ratio` cap so the correction never
    amplifies noise on a legitimately-monochrome scene (green floor,
    sunset, etc.).

    Cheaper than gray-world WB: same `cv2.mean` + LUT pattern, no
    bilateral/CLAHE downstream needed. Apply this as a *display*
    pre-pass to make the dashboard image look right; the detector
    continues to use the full gray-world WB inside its pipeline.

    Args:
        frame:      BGR uint8 image.
        max_ratio:  Per-channel scale factor cap. 1.5 = "scale each
                    channel by at most 1.5× toward the global mean."
                    Lower = more conservative; the 2026-10-16 incident
                    needed 1.7× to neutralise the cast, so 1.5 is the
                    default safe value.
        min_mean:   Skip the correction when the global mean is below
                    this (very dark frames amplify noise).
    """
    m = cv2.mean(frame)
    avg_bgr = np.array(m[:3], dtype=np.float32)
    avg_gray = float(avg_bgr.mean())
    if avg_gray < min_mean:
        return frame
    # Per-channel scale: avg_gray / mean_c, capped to [1/max_ratio, max_ratio].
    scale = (avg_gray / np.maximum(avg_bgr, 1.0)).astype(np.float32)
    scale = np.clip(scale, 1.0 / max_ratio, max_ratio)
    if np.allclose(scale, 1.0, atol=1e-3):
        return frame  # already neutral; skip the LUT
    lut = np.clip(
        np.arange(256, dtype=np.float32)[:, None] * scale[None, :],
        0, 255,
    ).astype(np.uint8)
    b, g, r = cv2.split(frame)
    b = cv2.LUT(b, lut[:, 0])
    g = cv2.LUT(g, lut[:, 1])
    r = cv2.LUT(r, lut[:, 2])
    return cv2.merge([b, g, r])


def detect_yellow_centroid_enhanced(
    frame: np.ndarray,
    *,
    lower: tuple[int, int, int],
    upper: tuple[int, int, int],
    min_area: int,
    kernel: tuple[int, int] = (5, 5),
    clahe_clip_limit: float = 3.0,
    clahe_tile_grid: int = 8,
    white_balance: bool = True,
    bilateral_d: int = 5,
    bilateral_sigma_color: float = 50.0,
    bilateral_sigma_space: float = 50.0,
    roi_top_fraction: float = 0.4,
    min_aspect_ratio: float = 1.0,
    border_ignore_frac: float = 0.05,
    lower_dark: Optional[tuple[int, int, int]] = (15, 40, 40),
    upper_dark: Optional[tuple[int, int, int]] = (40, 255, 255),
) -> Optional[tuple[int, int, int]]:
    """Enhanced yellow-line centroid detection.

    Pipeline (each step is independently toggleable):

      1. Gray-world white balance        — counters blue color cast
      2. Bilateral denoise (edge-preserving) — prevents CLAHE amplifying
                                              sensor noise in low light
      3. CLAHE on LAB L-channel          — lifts shadow detail without
                                              blowing out highlights
      4. ROI crop to floor region        — drops the upper portion of the
                                              frame (walls / ceiling / sky)
      5. Multi-range HSV inRange         — configured range + dim low-light
                                              range + relaxed S/V
      6. Morphological open + close      — drops speckle, fills tape gaps
      7. Aspect-ratio filter             — rejects round blobs (set to 1.0
                                              to disable)
      8. Border filter                   — rejects centroids hugging the
                                              frame edge
      9. Largest qualifying contour → (cx, cy, area)

    Returns ``(cx, cy, area)`` of the largest qualifying contour in
    full-frame coordinates, or ``None`` if nothing qualified.
    """
    h, w = frame.shape[:2]
    if h < 8 or w < 8:
        return None

    # ── Green-dominance guard (added by tuning plan) ─────────────────
    # On 2026-10-16 the IMX219 at wbmode=0 (default) returned a
    # green-tinted frame with mean BGR ≈ (68, 117, 69). After the
    # gray-world WB the relative green bias is reduced but local
    # regions still satisfy the H≈20-35 "yellow" range and Tier 1-2
    # of the multi-range HSV pick the wall (44 % false-positive rate
    # on the live dashboard). Short-circuit the whole enhanced
    # detector to None when the scene is green-biased; the rover
    # emits CAUTION and holds its last-known heading instead of
    # steering into the wall.
    m = cv2.mean(frame)
    b_mean, g_mean, r_mean = float(m[0]), float(m[1]), float(m[2])
    if (g_mean > TIER0_GREEN_GUARD_RATIO * r_mean
            and g_mean > TIER0_GREEN_GUARD_RATIO * b_mean):
        return None

    # 1. White balance (full frame so the mean reflects the whole scene)
    work = _gray_world_white_balance(frame) if white_balance else frame

    # 2. Bilateral denoise (edge-preserving; applied BEFORE CLAHE so the
    #    adaptive equalisation doesn't amplify sensor noise). At d=3 on a
    #    720p frame this costs ~7 ms — cheaper than re-allocating the ROI.
    if bilateral_d > 0:
        work = cv2.bilateralFilter(
            work, int(bilateral_d),
            float(bilateral_sigma_color), float(bilateral_sigma_space),
        )

    # 4. ROI crop — skip the upper portion of the frame (walls / ceiling).
    #    HSV and morphology run on the smaller region.
    roi_y = int(h * roi_top_fraction)
    if roi_y >= h - 4:
        return None
    roi = work[roi_y:, :]
    roi_h, roi_w = roi.shape[:2]

    # 3. CLAHE on L channel of LAB (preserves chroma while boosting L)
    clahe = _make_clahe(clahe_clip_limit, clahe_tile_grid)
    lab = cv2.cvtColor(roi, cv2.COLOR_BGR2LAB)
    l_ch, a_ch, b_ch = cv2.split(lab)
    l_ch = clahe.apply(l_ch)
    roi = cv2.cvtColor(cv2.merge([l_ch, a_ch, b_ch]), cv2.COLOR_LAB2BGR)

    # 5. Multi-range HSV
    hsv = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)
    k = np.ones(kernel, np.uint8)
    ranges: list[tuple[np.ndarray, np.ndarray]] = [
        (np.array(lower, dtype=np.uint8), np.array(upper, dtype=np.uint8)),
    ]
    if lower_dark is not None and upper_dark is not None:
        ranges.append((
            np.array(lower_dark, dtype=np.uint8),
            np.array(upper_dark, dtype=np.uint8),
        ))
    # A relaxed range covers washed-out tape
    ranges.append((
        np.array((lower[0], 50, 50), dtype=np.uint8),
        np.array((upper[0], 255, 255), dtype=np.uint8),
    ))

    border = int(roi_w * border_ignore_frac) if border_ignore_frac > 0 else 0
    best: Optional[tuple[int, int, int]] = None

    for lo, hi in ranges:
        mask = cv2.inRange(hsv, lo, hi)
        mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, k)
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, k)
        contours, _ = cv2.findContours(
            mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE,
        )
        if not contours:
            continue
        contour = max(contours, key=cv2.contourArea)
        area = int(cv2.contourArea(contour))
        if area < min_area:
            continue

        # 7. Aspect ratio filter
        if min_aspect_ratio > 1.0:
            rect = cv2.minAreaRect(contour)
            bw, bh = rect[1]
            longer = max(float(bw), float(bh), 1e-6)
            shorter = max(min(float(bw), float(bh)), 1e-6)
            if longer / shorter < min_aspect_ratio:
                continue

        # 8. Border filter
        moments = cv2.moments(contour)
        if moments['m00'] == 0:
            continue
        cx = int(moments['m10'] / moments['m00'])
        cy = int(moments['m01'] / moments['m00'])
        if border > 0 and (cx < border or cx > roi_w - border):
            continue

        # 9. Pick the largest qualifying contour
        if best is None or area > best[2]:
            best = (cx, cy + roi_y, area)

    return best