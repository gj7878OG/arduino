"""Typed telemetry payloads and atomic on-disk publisher.

Two layers — public payload model (TelemetryState, SensorProbe, etc.) and
the TelemetryPublisher that writes status.json + frame.jpg atomically on
a background thread. The control loop never blocks on disk I/O.

Log entries are categorised: SYSTEM / CAMERA / LIDAR / STM32.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import time
from collections import deque
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Iterable, Optional, Union

from config import TELEMETRY_DIR, LOG_LIMIT

log = logging.getLogger('agv.telemetry')


class LogCategory(str, Enum):
    SYSTEM = 'SYSTEM'
    CAMERA = 'CAMERA'
    LIDAR = 'LIDAR'
    STM32 = 'STM32'


# Categories tuple kept for backward compatibility with old code that
# imported a constant.
LOG_CATEGORIES: tuple[str, ...] = tuple(c.value for c in LogCategory)

# Constants matching the previous module's names.
LOG_CAT_SYSTEM = LogCategory.SYSTEM.value
LOG_CAT_CAMERA = LogCategory.CAMERA.value
LOG_CAT_LIDAR = LogCategory.LIDAR.value
LOG_CAT_STM32 = LogCategory.STM32.value


@dataclass
class SensorProbe:
    device: str
    port: str
    status: str         # OK / FAILED / ERROR / NO_FRAMES
    error: Optional[str] = None
    latency_ms: float = 0.0
    serial: Optional[str] = None

    def as_dict(self) -> dict:
        return {k: v for k, v in asdict(self).items() if v is not None}


@dataclass
class TelemetryState:
    running: bool = False
    camera_connected: bool = False
    camera_backend: str = 'argus'
    stm32_connected: bool = False
    stm32_ack_age_ms: float = 0.0
    stm32_encoder_left: int = 0
    stm32_encoder_right: int = 0
    stm32_encoder_delta_left: int = 0
    stm32_encoder_delta_right: int = 0
    stm32_firmware_version: str = ''
    camera_direction: str = 'UNAVAILABLE'
    camera_command: str = 'S'
    # Raw detection output from observe_frame(); -1 / 0 when the line
    # is lost or the camera is unavailable, so the detections page can
    # show "no detection" instead of (0, 0).
    line_centroid_x: int = -1
    line_centroid_y: int = -1
    line_area: int = 0
    line_error_px: int = 0
    lidar_status: str = 'UNKNOWN'
    lidar_connected: bool = False
    lidar_error: Optional[str] = None
    lidar_distance_mm: float = 0.0
    # Rotating-LiDAR obstacle safety (lidar/safety.py). None means "no
    # reading" — the dashboard shows "—" rather than a misleading 0.0.
    #
    # NOTE the collision above: `lidar_status` / `lidar_connected` /
    # `lidar_error` / `lidar_distance_mm` are the *camera* obstacle
    # detector (agv_drive_arduino binds them to OBST_ENABLED and vis.obstacle),
    # not this sensor. `lidar_sensor_connected` below is the only field that
    # reports whether the rotating LiDAR is actually producing a healthy
    # snapshot — use it for the LiDAR's own up/down indicator.
    lidar_sensor_connected: bool = False         # rotating LiDAR healthy
    lidar_min_front_m: Optional[float] = None    # nearest obstacle ahead
    lidar_sector_mins: Optional[list] = None     # per-sector minimum, metres
    lidar_age_ms: Optional[int] = None           # age of the newest sample
    lidar_estop_reason: str = ''                 # why safety is engaged
    lidar_action: str = 'CLEAR'                  # CLEAR / SLOW / ESTOP
    lidar_stop_dist_m: float = 0.0               # the zones in force
    lidar_slow_dist_m: float = 0.0
    lidar_slow_v_mps: float = 0.0
    lidar_enabled: bool = False
    # LiDAR connection details (lidar_safety.py). Surfaced to the
    # dashboard so the operator can confirm which port / baud / sector
    # width the live feed is using without SSH.
    lidar_port: str = ''
    lidar_baud: int = 0
    lidar_sector_deg: int = 0
    final_command: str = 'S'
    decision_reason: str = ''
    sensor_probes: list[dict] = field(default_factory=list)
    last_error: Optional[str] = None
    # Camera-only perception + motion (all optional so old readers work).
    enabled: bool = False
    mode: str = 'auto'
    obstacle_enforced: bool = True
    track_state: str = 'NO_LINE'
    steer_boost: float = 1.0
    max_speed_pct: int = 0
    line_offset_m: float = 0.0
    line_angle_deg: float = 0.0
    line_lookahead_m: float = 0.0
    line_curvature: float = 0.0
    line_strips: int = 0
    obstacle_state: str = 'CLEAR'
    obstacle_distance_m: float = 0.0
    speed_target: float = 0.0
    omega_target: float = 0.0
    left_pwm: int = 0
    right_pwm: int = 0
    vision_ms: float = 0.0
    # Encoder speed feedback (firmware v3). Speeds in m/s, + = forward.
    wheel_closed_loop: bool = False        # the Arduino PID is holding speeds
    closed_loop_wanted: bool = True        # dashboard switch
    fw_closed_ready: bool = False          # firmware answered + took the gains
    wheel_tgt_l_mps: float = 0.0
    wheel_tgt_r_mps: float = 0.0
    wheel_meas_l_mps: float = 0.0
    wheel_meas_r_mps: float = 0.0
    wheel_applied_l: int = 0               # PWM really applied by the Arduino
    wheel_applied_r: int = 0
    enc_fault_l: bool = False              # commanded but encoder not moving
    enc_fault_r: bool = False
    wheel_max_mps: float = 0.0
    wheel_diameter_m: float = 0.0
    enc_rel_l: int = 0                     # ticks since the last "zero"
    enc_rel_r: int = 0
    ticks_per_m: float = 0.0
    # Test recording (dashboard Record tab)
    rec_on: bool = False
    rec_session: str = ''
    rec_frames: int = 0
    rec_problems: int = 0
    rec_marks: int = 0
    rec_dropped: int = 0
    rec_mb: float = 0.0
    rec_elapsed_s: float = 0.0
    rec_free_gb: float = 0.0
    rec_stop_reason: str = ''
    rec_format: str = 'v0'
    rec_mode: str = 'continuous'
    rec_shard: str = ''
    rec_ring_s: float = 0.0
    updated_at: float = field(default_factory=time.time)


@dataclass
class LogEntry:
    id: int
    timestamp: float
    category: str
    message: str

    def as_dict(self) -> dict:
        return asdict(self)


def _normalize(
    message: Optional[Union[str, Iterable]],
    category: str,
) -> list[tuple[str, str]]:
    """Convert a message arg into a list of (category, text) tuples."""
    if message is None:
        return []
    if isinstance(message, str):
        return [(category, message)]
    if isinstance(message, dict):
        items = [message]
    else:
        items = list(message)
    out: list[tuple[str, str]] = []
    for item in items:
        if item is None:
            continue
        if isinstance(item, tuple) and len(item) == 2 and item[0] in LOG_CATEGORIES:
            cat, text = item
            out.append((cat, str(text)))
        elif isinstance(item, dict):
            cat = item.get('category', category)
            out.append((cat, str(item.get('message', ''))))
        else:
            out.append((category, str(item)))
    return out


class TelemetryPublisher:
    """Atomic writer for status.json + frame.jpg on a background thread."""

    def __init__(self, telemetry_dir: Optional[str] = None,
                 log_limit: Optional[int] = None) -> None:
        self.dir = telemetry_dir or TELEMETRY_DIR
        self.limit = log_limit or LOG_LIMIT
        os.makedirs(self.dir, exist_ok=True)
        self._lock = threading.Lock()
        self._logs: deque[LogEntry] = deque(maxlen=self.limit)
        self._next_id = 1
        self._condition = threading.Condition()
        self._pending: Optional[tuple[dict, Optional[bytes]]] = None
        self._running = False
        self._thread: Optional[threading.Thread] = None

    # ── lifecycle ─────────────────────────────────────────────────────
    def start(self) -> None:
        if self._thread is not None:
            return
        self._running = True
        self._thread = threading.Thread(
            target=self._run, name='dashboard-telemetry', daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        with self._condition:
            self._running = False
            self._condition.notify_all()
        if self._thread:
            self._thread.join(timeout=2)
            self._thread = None

    # ── log handling ─────────────────────────────────────────────────
    def restore_previous(self) -> None:
        """Backfill log IDs from the on-disk file (cross-restart continuity)."""
        try:
            with open(os.path.join(self.dir, 'status.json'), 'r', encoding='utf-8') as fh:
                payload = json.load(fh)
            with self._lock:
                for entry in payload.get('logs', [])[-self.limit:]:
                    if 'category' not in entry:
                        entry['category'] = LOG_CAT_SYSTEM
                    log_entry = LogEntry(
                        id=int(entry.get('id', 0)),
                        timestamp=float(entry.get('timestamp', 0.0)),
                        category=entry['category'],
                        message=str(entry.get('message', '')),
                    )
                    self._logs.append(log_entry)
                    if log_entry.id >= self._next_id:
                        self._next_id = log_entry.id + 1
        except (OSError, ValueError, TypeError):
            pass

    # ── publish API ──────────────────────────────────────────────────
    def publish(self,
                state: Union[TelemetryState, dict],
                frame: Optional[Any] = None,
                log_message: Optional[Union[str, Iterable]] = None,
                log_category: str = LOG_CAT_SYSTEM) -> None:
        """Queue state + log lines + (optional) frame for the background writer."""
        if isinstance(state, TelemetryState):
            state_dict: dict[str, Any] = asdict(state)
        else:
            state_dict = dict(state)
        # Apply any new log lines.
        with self._lock:
            for cat, text in _normalize(log_message, log_category):
                self._logs.append(LogEntry(
                    id=self._next_id, timestamp=time.time(),
                    category=cat, message=text,
                ))
                self._next_id += 1
            # Snapshot logs into payload.
            logs_snapshot = [entry.as_dict() for entry in self._logs]
        state_dict['updated_at'] = time.time()
        state_dict['logs'] = logs_snapshot

        # Encode frame (if given) on the caller thread so cv2 stays
        # off the writer thread. The payload byte buffer is small.
        frame_bytes: Optional[bytes] = None
        if frame is not None:
            try:
                import cv2
                encoded, buf = cv2.imencode('.jpg', frame,
                                            [cv2.IMWRITE_JPEG_QUALITY, 80])
                if encoded:
                    frame_bytes = buf.tobytes()
            except Exception as exc:
                log.info('[Telemetry] frame encode failed: %s', exc)

        with self._condition:
            self._pending = (state_dict, frame_bytes)
            self._condition.notify()

    # ── background writer ────────────────────────────────────────────
    def _run(self) -> None:
        import cv2  # only the writer thread imports cv2
        while True:
            with self._condition:
                while self._pending is None and self._running:
                    self._condition.wait()
                if self._pending is None and not self._running:
                    return
                payload, frame_bytes = self._pending
                self._pending = None
            try:
                if frame_bytes:
                    _atomic_write(os.path.join(self.dir, 'frame.jpg'), frame_bytes)
                _atomic_write(os.path.join(self.dir, 'status.json'),
                              json.dumps(payload).encode('utf-8'))
            except (OSError, TypeError, ValueError) as exc:
                log.info('[Telemetry] publish failed: %s', exc)
            if frame_bytes is not None:
                time.sleep(0.2)


def _atomic_write(path: str, data: bytes) -> None:
    """Replace `path` with `data` atomically; readers never see partials."""
    os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
    fd, temp_path = tempfile.mkstemp(prefix='.agv-',
                                      dir=os.path.dirname(path) or '.')
    try:
        with os.fdopen(fd, 'wb') as fh:
            fh.write(data)
        os.replace(temp_path, path)
    finally:
        if os.path.exists(temp_path):
            try:
                os.unlink(temp_path)
            except OSError:
                pass