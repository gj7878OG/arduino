"""LiDAR safety module: owns /dev/ttyUSB0, parses 5-byte RPLIDAR nodes into
sectors, and exposes a thread-safe snapshot for the AGV control loop.

Mirrors the pattern in ``arduino.py``:

  - one writer thread (the reader, here) + one closer (shutdown)
  - a single ``threading.Lock`` for every state mutation
  - ``time.monotonic()`` for every in-buffer timestamp and liveness check
  - ``snapshot()`` returns a dataclass; consumers poll at 50 Hz

All point timestamps come from one clock — ``time.monotonic()`` — so a
wall-clock step (NTP, DST, an operator setting the clock) can neither
resurrect stale points nor age out fresh ones. Wall clock appears only
in ``points.json``'s ``updated_at``, which is display-only.

The safety consumer here is the AGV orchestrator: it polls
``snapshot()`` every tick and decides whether to e-stop, slow, or leave
motion alone. Publishing ``points.json`` for the dashboard is a separate
concern and lives in ``lidar.publisher``.

Detectors:
  - **stale**        no fresh points in ``stale_ms``
  - **stuck**        every sector is reporting the same distance (a flat
                     sweep) for > 2 s — the sensor is replaying a buffer
                     or the head is jammed, not measuring a round room
  - **noisy**        >= 30 % of points are out-of-range (> 12 m or == 0)
  - **motor_stopped**  the angle span over the rolling window is < 60°
                     — the head isn't spinning and we're getting the
                     same direction over and over
  - **usb_disconnected**  ``serial.Serial`` raised on a read
"""

from __future__ import annotations

import collections
import dataclasses
import logging
import threading
import time
from typing import Optional

import serial

from config import (
    LIDAR_BAUD, LIDAR_ENABLED, LIDAR_FILTER_MAX_M, LIDAR_FILTER_MIN_M,
    LIDAR_PORT, LIDAR_SECTOR_DEG, LIDAR_STALE_MS, LIDAR_WINDOW_SEC,
)
from ._constants import (
    MOTOR_SPAN_MIN_DEG, NOISY_FRACTION, NODE_LEN, POINTS_MAX,
    POINTS_WINDOW_S, REOPEN_BACKOFF_S, REOPEN_QUIET_S, SCAN_HEADER,
    SCAN_HEADER_LEN, STUCK_HOLD_S, STUCK_MIN_SAMPLES, STUCK_SPREAD_M,
)
from .parser import parse_nodes

log = logging.getLogger('agv.lidar')


@dataclasses.dataclass
class LidarSnapshot:
    """Thread-safe view of the LiDAR's current state.

    ``min_per_sector`` is keyed by sector index (0 = forward, increasing
    clockwise). The number of sectors is ``round(360 / sector_deg)``.
    Sectors with no points in the window hold ``inf``.
    """
    min_per_sector: dict
    min_front_m: float
    age_ms: int
    ok: bool
    reason: str
    samples_in_window: int
    points_filtered: int
    sector_deg: int
    n_sectors: int = 0

    def __repr__(self) -> str:
        front = self.min_front_m
        front_s = '—' if front == float('inf') else f'{front:.2f}m'
        return (f'<LidarSnapshot {self.reason or "ok"} front={front_s} '
                f'age={self.age_ms}ms n={self.samples_in_window} '
                f'sectors={self.n_sectors}x{self.sector_deg}deg>')

    def as_dict(self) -> dict:
        """JSON-safe view: ``inf`` becomes ``None``, floats are rounded."""
        return {
            'min_per_sector': {str(k): (None if v == float('inf') else round(v, 3))
                               for k, v in self.min_per_sector.items()},
            'min_front_m': (None if self.min_front_m == float('inf')
                            else round(self.min_front_m, 3)),
            'age_ms': self.age_ms,
            'ok': self.ok,
            'reason': self.reason,
            'samples_in_window': self.samples_in_window,
            'points_filtered': self.points_filtered,
            'sector_deg': self.sector_deg,
            'n_sectors': self.n_sectors,
        }


@dataclasses.dataclass
class _LidarInternal:
    connected: bool = False
    running: bool = False
    last_error: str = ''
    # (monotonic_ts, angle_deg, dist_m, dist_q2) tuples in arrival order
    pts: collections.deque = dataclasses.field(default_factory=collections.deque)
    # when the sweep last changed, for the frozen-sweep detector
    _stuck_anchor_ts: float = 0.0


class LidarSafety:
    """Owns the RPLIDAR serial port and a single reader thread.

    The reader keeps a rolling buffer of the last ``window_sec`` seconds
    of points. ``snapshot()`` reduces it to per-sector min distance + a
    health verdict; ``recent_points()`` hands the same buffer to the
    dashboard publisher. Thread-safe; safe to call from the 50 Hz
    control loop.
    """

    def __init__(self, port: Optional[str] = None,
                 baud: Optional[int] = None,
                 sector_deg: Optional[int] = None,
                 window_sec: Optional[float] = None,
                 stale_ms: Optional[int] = None,
                 enabled: Optional[bool] = None) -> None:
        self.port = port or LIDAR_PORT
        self.baud = baud or LIDAR_BAUD
        self.sector_deg = int(sector_deg or LIDAR_SECTOR_DEG)
        self.window_sec = float(window_sec or LIDAR_WINDOW_SEC)
        self.stale_ms = int(stale_ms or LIDAR_STALE_MS)
        self.enabled = bool(LIDAR_ENABLED if enabled is None else enabled)
        self.n_sectors = max(1, round(360 / self.sector_deg))
        self._serial: Optional[serial.Serial] = None
        self._state = _LidarInternal()
        self._lock = threading.Lock()
        self._reader: Optional[threading.Thread] = None
        self._closing = threading.Event()
        # Cached node buffer between reads
        self._node_buf = bytearray()
        self._header_found = False

    # ── lifecycle ────────────────────────────────────────────────────
    def connect(self) -> bool:
        # Reconnecting over a live session would orphan the old reader
        # thread on the old fd; tear the old one down first.
        if self._serial is not None or self._reader is not None:
            self.close()
        self._closing = threading.Event()

        if not self.enabled:
            log.info('LiDAR disabled in config; not opening %s', self.port)
            with self._lock:
                self._state.last_error = 'disabled in config'
            return False
        try:
            self._open_serial()
            with self._lock:
                self._state.connected = True
                self._state.running = True
                self._state.last_error = ''
            self._reader = threading.Thread(
                target=self._reader_loop, name='lidar-reader', daemon=True,
            )
            self._reader.start()
            log.info('LiDAR connected on %s @ %d baud', self.port, self.baud)
            return True
        except Exception as exc:
            with self._lock:
                self._state.connected = False
                self._state.running = False
                self._state.last_error = f'{type(exc).__name__}: {exc}'
            log.warning('LiDAR connect failed: %s', exc)
            return False

    def close(self) -> None:
        """Stop the reader and release the port. Idempotent."""
        if self._closing.is_set():
            return
        self._closing.set()
        # Pull the fd out from under the reader. The attribute write is
        # atomic and the reader reads it unlocked, so the reader either
        # gets the live port (and then sees _closing on the next check)
        # or gets None (and tries a reopen it will abandon on close).
        with self._lock:
            ser, self._serial = self._serial, None
            self._state.running = False
            self._state.connected = False
        if ser is not None:
            try:
                # Stop the spin-down cleanly.
                ser.write(b"\xA5\x25")
            except Exception:
                pass
            try:
                ser.close()
            except Exception:
                pass
        reader, self._reader = self._reader, None
        if reader is not None and reader.is_alive():
            reader.join(timeout=0.5)

    def _open_serial(self) -> None:
        """Open the port with the same dance as ``/tmp/lidar_capture_full.py``:
        DTR=False to enable the motor on this clone, RESET, wait for boot,
        SET_MOTOR_PWM(800), wait for the head to spin up.
        """
        ser = serial.Serial(self.port, self.baud, timeout=0.2)
        # Clone "rtm" / Model 18: DTR is active-LOW motor enable.
        ser.dtr = False
        try:
            ser.reset_input_buffer()
            ser.reset_output_buffer()
        except Exception:
            pass
        # RESET, then drain whatever it says for 3 s.
        ser.write(b"\xA5\x40")
        end = time.monotonic() + 3.0
        while time.monotonic() < end:
            ser.read(256)
        # SET_MOTOR_PWM (Slamtec 6-byte form, PWM=800)
        ser.write(b"\xA5\xF0\x02\x00" + (800).to_bytes(2, "little"))
        time.sleep(0.2)
        try:
            ser.read(64)
        except Exception:
            pass
        time.sleep(6.0)         # motor spin-up
        try:
            ser.read(64)
        except Exception:
            pass
        # SCAN. Without this the head may be spinning but the firmware
        # sends no measurement nodes and the buffer stays empty — the
        # motor PWM alone does not start the scan on this clone.
        ser.write(b"\xA5\x20")
        time.sleep(0.05)
        self._serial = ser
        self._node_buf = bytearray()
        self._header_found = False

    def _reopen_with_backoff(self) -> bool:
        """Best-effort reopen after a USB drop. Returns True on success."""
        try:
            with self._lock:
                old, self._serial = self._serial, None
            if old is not None:
                try:
                    old.close()
                except Exception:
                    pass
            time.sleep(0.5)
            self._open_serial()
            with self._lock:
                self._state.connected = True
                self._state.running = True
                self._state.last_error = ''
            log.info('LiDAR reopened on %s', self.port)
            return True
        except Exception as exc:
            with self._lock:
                self._state.connected = False
                self._state.running = False
                self._state.last_error = f'{type(exc).__name__}: {exc}'
            return False

    # ── reader thread ────────────────────────────────────────────────
    def _reader_loop(self) -> None:
        """One thread, short blocking reads, rolling 5-byte parse.

        Mirrors the streaming code in ``/tmp/lidar_capture_full.py``:
        once we've found the 7-byte header we never re-sync (the bytes
        0xA5 0x5A can appear inside normal data and a mid-stream resync
        would destroy the 5-byte stride).
        """
        backoff = 0.5
        last_data = time.monotonic()
        next_reopen = 0.0
        while not self._closing.is_set():
            # A healthy handle that has gone quiet for REOPEN_QUIET_S is the
            # stall case no exception can report: rebuild the port, which
            # also re-issues SCAN and can restart a stopped motor.
            if (time.monotonic() - last_data > REOPEN_QUIET_S
                    and time.monotonic() >= next_reopen):
                next_reopen = time.monotonic() + REOPEN_BACKOFF_S
                log.warning('LiDAR silent for %.0fs; reopening', REOPEN_QUIET_S)
                with self._lock:
                    self._serial = None
                last_data = time.monotonic()
                continue

            ser = self._serial
            if ser is None:
                if not self._reopen_with_backoff():
                    time.sleep(backoff)
                    backoff = min(backoff * 2, 5.0)
                    continue
                backoff = 0.5
                ser = self._serial

            try:
                chunk = ser.read(4096)
            except (serial.SerialException, OSError, ValueError, TypeError) as exc:
                # close() pulls the port out from under an in-flight
                # read: pyserial sets ``fd`` to None first, so the
                # in-flight os.read() raises TypeError — not a
                # SerialException — and would otherwise kill this thread
                # outright. That is a shutdown, not a fault to recover
                # from.
                if self._closing.is_set():
                    break
                with self._lock:
                    self._state.last_error = f'{type(exc).__name__}: {exc}'
                    self._state.connected = False
                log.warning('LiDAR read failed: %s; will reopen', exc)
                with self._lock:
                    self._serial = None
                continue

            if not chunk:
                # Nothing to do; small sleep keeps CPU sane.
                time.sleep(0.005)
                continue

            self._node_buf.extend(chunk)

            if not self._header_found:
                # USB hands us arbitrary chunk sizes, so the 7-byte
                # header can arrive split across reads. Wait for a
                # candidate that actually has its full SCAN_HEADER_LEN
                # behind it; holding a truncated one costs nothing and
                # avoids resyncing onto an A5 5A that lives inside node
                # data — the failure this loop may risk exactly once,
                # before it locks to the 5-byte stride.
                buf = self._node_buf
                start, hdr = 0, -1
                while True:
                    cand = buf.find(SCAN_HEADER, start)
                    if cand < 0:
                        break
                    if cand + SCAN_HEADER_LEN <= len(buf):
                        hdr = cand          # first complete header wins
                        break
                    start = cand + 1        # truncated; a later one may fit
                if hdr >= 0:
                    del buf[:hdr + SCAN_HEADER_LEN]
                    self._header_found = True
                    # Fall through: this same chunk may already carry
                    # measurement nodes behind the header.
                else:
                    if start:
                        # Keep the truncated candidate at the tail; the
                        # rest of it is already in flight.
                        del buf[:start - 1]
                    elif buf[-1:] == b'\xA5':
                        buf[:] = b'\xA5'       # 5A may be the next byte
                    else:
                        buf.clear()
                    continue

            now = time.monotonic()
            added = 0
            for tup in parse_nodes(bytes(self._node_buf), skip=0, now=now):
                with self._lock:
                    self._state.pts.append(tup)
                added += 1
                # consume the 5 bytes from the front of the buffer
                del self._node_buf[:NODE_LEN]
            if added:
                last_data = time.monotonic()
                self._trim_locked()

    def _trim_locked(self) -> None:
        """Drop points older than ``window_sec``. Caller must hold the lock."""
        cutoff = time.monotonic() - self.window_sec
        s = self._state
        while s.pts and s.pts[0][0] < cutoff:
            s.pts.popleft()

    # ── public API ───────────────────────────────────────────────────
    def recent_points(self, window_s: float = POINTS_WINDOW_S,
                      limit: int = POINTS_MAX) -> list:
        """Thread-safe copy of the last ``window_s`` seconds of raw points,
        newest-last, downsampled by even stride to at most ``limit``.

        Returns ``(monotonic_ts, angle_deg, dist_m, dist_q2)`` tuples —
        the caller's clock is ``time.monotonic()`` too, so it can compute
        ages itself without knowing which clock these came from.
        """
        cutoff = time.monotonic() - window_s
        with self._lock:
            pts = [t for t in self._state.pts if t[0] >= cutoff]
        if len(pts) > limit > 0:
            stride = len(pts) / limit
            pts = [pts[int(i * stride)] for i in range(limit)]
        return pts

    def snapshot(self) -> LidarSnapshot:
        """Thread-safe view. Reduces the rolling buffer to per-sector min
        and a health verdict. Safe to call from the 50 Hz control loop.
        """
        with self._lock:
            self._trim_locked()
            s = self._state
            now = time.monotonic()
            n_raw = len(s.pts)
            cutoff = now - self.window_sec
            fresh = [t for t in s.pts if t[0] >= cutoff]
            # Per-sector min over the window
            mins = {i: float('inf') for i in range(self.n_sectors)}
            n_filtered = 0
            for _ts, ang, dist_m, _q2 in fresh:
                if not (LIDAR_FILTER_MIN_M <= dist_m <= LIDAR_FILTER_MAX_M):
                    continue
                n_filtered += 1
                idx = int((ang % 360) / self.sector_deg) % self.n_sectors
                if dist_m < mins[idx]:
                    mins[idx] = dist_m
            # Front sector: 0 only (we treat 0 = forward; widen via config
            # later if a wider cone is needed)
            front = mins.get(0, float('inf'))
            if s.pts:
                age_ms = int((now - s.pts[-1][0]) * 1000)
            else:
                age_ms = 99999

            # Health verdict
            ok = True
            reason = ''
            if not s.connected:
                ok = False
                # A short, stable code for the dashboard chip; the full
                # exception text stays in last_error for the log.
                reason = 'usb_disconnected'
            elif age_ms > self.stale_ms:
                ok = False
                reason = 'stale'
            else:
                # Frozen sweep: every sector reporting the same distance
                # means the sensor is replaying a buffer or the head is
                # jammed, not that the room happens to be round.
                seen = [v for v in mins.values() if v != float('inf')]
                if len(seen) >= 2 and n_raw >= STUCK_MIN_SAMPLES:
                    flat = (max(seen) - min(seen)) <= STUCK_SPREAD_M
                    if not flat or s._stuck_anchor_ts == 0.0:
                        s._stuck_anchor_ts = now
                    elif (now - s._stuck_anchor_ts) > STUCK_HOLD_S:
                        ok = False
                        reason = 'stuck'
                # Noisy: too many out-of-range points
                if ok and n_raw:
                    bad = sum(1 for t in fresh
                              if not (LIDAR_FILTER_MIN_M <= t[2] <= LIDAR_FILTER_MAX_M))
                    if bad / max(1, n_raw) > NOISY_FRACTION:
                        ok = False
                        reason = 'noisy'
                # Motor stopped: angle span too small
                if ok and fresh:
                    angs = [t[1] for t in fresh if t[2] > 0]
                    if angs:
                        span = max(angs) - min(angs)
                        # wrap-aware span
                        if span > 180:
                            # try a different unwrap
                            unwrapped = []
                            base = angs[0]
                            for a in angs:
                                while a < base - 180:
                                    a += 360
                                while a > base + 180:
                                    a -= 360
                                unwrapped.append(a)
                            span = max(unwrapped) - min(unwrapped)
                        if span < MOTOR_SPAN_MIN_DEG and n_raw > 50:
                            ok = False
                            reason = 'motor_stopped'

        return LidarSnapshot(
            min_per_sector=mins,
            min_front_m=front,
            age_ms=age_ms,
            ok=ok,
            reason=reason,
            samples_in_window=n_raw,
            points_filtered=n_filtered,
            sector_deg=self.sector_deg,
            n_sectors=self.n_sectors,
        )

    def link_alive(self) -> bool:
        with self._lock:
            return bool(self._state.connected and self._state.pts) and \
                (time.monotonic() - self._state.pts[-1][0]) * 1000 <= self.stale_ms


__all__ = ['LidarSafety', 'LidarSnapshot']