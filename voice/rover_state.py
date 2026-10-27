"""Read the rover's published state — read-only, never raises.

The AGV process writes ``status.json`` (sensors, link health) and the
dashboard writes ``control.json`` (enabled, mode). Voice reads both exactly
the way the dashboard and ``event_watcher`` do: plain JSON, no import of the
main project, no write.

The important property is **staleness**. A status file nobody is updating
describes a rover that may no longer exist, so a state older than
``FRESH_S`` is reported as ``fresh=False`` and the report says "I can't
reach the controller" instead of reading out old numbers as if they were
current.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional

STATUS_FILENAME = 'status.json'
CONTROL_FILENAME = 'control.json'
FRESH_S = 5.0                 # status.json older than this = controller gone
LIDAR_FRESH_MS = 2000         # a LiDAR sample older than this = sensor stalled


@dataclass(frozen=True)
class RoverState:
    fresh: bool = False                 # status.json is recent
    age_s: Optional[float] = None
    enabled: Optional[bool] = None      # motors on?  (control.json)
    mode: Optional[str] = None          # 'auto' | 'manual'
    camera_connected: Optional[bool] = None
    stm32_connected: Optional[bool] = None
    lidar_enabled: Optional[bool] = None
    lidar_connected: Optional[bool] = None
    lidar_age_ms: Optional[float] = None
    lidar_min_front_m: Optional[float] = None
    lidar_action: Optional[str] = None  # CLEAR / SLOW / ESTOP

    @property
    def lidar_live(self) -> bool:
        """A connected sensor delivering fresh samples."""
        return bool(self.lidar_connected) and self.lidar_age_ms is not None \
            and self.lidar_age_ms <= LIDAR_FRESH_MS


def _read(path: str) -> Optional[dict]:
    try:
        with open(path, 'r', encoding='utf-8') as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def _num(v: Any) -> Optional[float]:
    if isinstance(v, bool) or not isinstance(v, (int, float)) or v != v:
        return None
    return float(v)


def _bool(v: Any) -> Optional[bool]:
    return v if isinstance(v, bool) else None


def read_rover_state(telemetry_dir: str,
                     clock: Callable[[], float] = time.time,
                     fresh_s: float = FRESH_S) -> RoverState:
    """Snapshot of what the rover last reported. Missing files = not fresh."""
    status = _read(os.path.join(telemetry_dir, STATUS_FILENAME)) or {}
    control = _read(os.path.join(telemetry_dir, CONTROL_FILENAME)) or {}

    updated = _num(status.get('updated_at'))
    age = None if updated is None else max(0.0, clock() - updated)
    fresh = age is not None and age <= fresh_s and status.get('running') is not False

    mode = control.get('mode', status.get('mode'))
    return RoverState(
        fresh=fresh, age_s=age,
        enabled=_bool(control.get('enabled', status.get('enabled'))),
        mode=mode if mode in ('auto', 'manual') else None,
        camera_connected=_bool(status.get('camera_connected')),
        stm32_connected=_bool(status.get('stm32_connected')),
        lidar_enabled=_bool(status.get('lidar_enabled')),
        lidar_connected=_bool(status.get('lidar_connected')),
        lidar_age_ms=_num(status.get('lidar_age_ms')),
        lidar_min_front_m=_num(status.get('lidar_min_front_m')),
        lidar_action=status.get('lidar_action') if status.get('lidar_action') in
        ('CLEAR', 'SLOW', 'ESTOP') else None,
    )


__all__ = ['RoverState', 'read_rover_state', 'FRESH_S']
