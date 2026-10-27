"""Dashboard controls shared between web.py and the AGV process.

web.py writes ``control.json`` into the telemetry directory; the AGV reads
it every tick. Two settings live there:

  enabled        motors ON/OFF. The AGV resets this to OFF on startup, so a
                 restart can never start driving by itself.
  mode           'auto' (line following) or 'manual' (dashboard driving).
  manual_v/_w    manual drive command, -1..1 (forward / turn right) plus its
                 wall-clock timestamp; the AGV treats it as stale after
                 MANUAL_STALE_S, so releasing a button (or losing the browser)
                 stops the rover.
  manual_l/_r    optional per-wheel test command, -1..1 of the speed cap
                 (overrides manual_v/_w while fresh). Used by the dashboard's
                 chassis diagram to spin one side at a time.
  manual_raw     True = the per-wheel test is a raw driver command (PWM =
                 fraction x 255, no speed cap, no PID). False = a speed
                 command (fraction of the max-speed cap, PID holds it).
  enc_zero_ts    wall-clock time of the last "zero encoder counts" request; the
                 running process zeroes its tick display when this changes.
  record         True = the AGV is saving test frames + a detection log (see
                 recorder.py). record_rate / record_views set how; record_mark_ts +
                 record_mark_note flag "this frame is wrong". Reset to OFF at startup.
                 record_mode is 'continuous' (every frame at record_rate) or
                 'triggered' (ring buffer + event clips); anything else, or a
                 missing key in an old file, reads as config.REC_MODE_DEFAULT.
  closed_loop    True = wheel speed PID on the Arduino (encoders), False = raw
                 PWM. Only has effect while the firmware reports support.
                 A standing preference, not a command: STOP, a mode change
                 and a process restart all leave it as the operator left it.
                 (The per-wheel test's raw flag, manual_raw, IS a command and
                 is cleared every time the manual command goes stale.)
  turn_floor_base  minimum turn authority, as a wheel-level PWM difference, the
                 follower may use the moment it starts correcting; the
                 tracking monitor's boost ramp adds on top of this up to
                 TURN_FLOOR. Tuned from the dashboard Drive tab, because a
                 skid-steer chassis needs a real differential before it yaws at
                 all, and the pure-pursuit term alone asks for far less than
                 that. Clamped on the way in *and* out, so a hand-edited
                 control.json cannot exceed TURN_FLOOR. Standing preference,
                 survives restarts, and only ever makes the rover steer MORE,
                 never less, so it needs no password.
  lookahead_m   pure-pursuit lookahead, metres ahead of the rover, 0.30-1.00.
                 Short tracks the centre of the line tightly but is noisier;
                 long is smooth but rides off-centre. This is the 'sit on the
                 centre of it' knob, where turn_floor_base is the 'get back on
                 the line' one. Clamped on the way in *and* out; the low end is
                 held clear of the controller's 0.2 m no-usable-sample cut-off.
                 A standing preference, survives restarts, no password.
  obstacle_stop  True = camera obstacle STOP/SLOW is enforced (default).
                 Turning it off needs the password; for bench testing only.
  max_speed_pct  master speed limit, 10-100 % of full PWM. Every other speed
                 (line following, caution, obstacle slow-down, turning,
                 search) is scaled by it. Survives restarts.
  lidar_*        obstacle-safety zones for the rotating LiDAR, tuned from the
                 dashboard's LiDAR tab. lidar_enabled is the master switch
                 (disabling it needs the password, so a stray browser tab
                 can't quietly disarm the rover); lidar_stop_dist_m hard-stops
                 below that range, lidar_slow_dist_m caps speed at
                 lidar_slow_v_mps between the two; lidar_stale_ms is the
                 sensor watchdog (no fresh sample in that time = stop);
                 lidar_sector_deg is the sector-map resolution. Every value
                 is clamped on the way in *and* on the way out, and the slow
                 zone is always held clear of the stop zone.
  record_auto   False (the default) = the dashboard starts a recording by
                 itself whenever the rover is actually driving in AUTO
                 (mode auto AND motors enabled), and stops it again when
                 the drive ends. A standing preference like closed_loop,
                 not a command: it never moves the rover and never starts
                 a recording on its own, so it needs no password. Off by
                 default because every drive it turns on fills disk.
"""

from __future__ import annotations

import fcntl
import json
import os
import tempfile
import time

from config import (
    CLOSED_LOOP, DEFAULT_SPEED_PCT, LIDAR_SECTOR_DEG, LIDAR_SLOW_DIST_M,
    LIDAR_SLOW_V_MPS, LIDAR_STOP_DIST_M, LIDAR_STALE_MS, REC_DEFAULT_RATE_HZ,
    REC_MODE_DEFAULT, SPEED_MAX_PCT, SPEED_MIN_PCT, TELEMETRY_DIR,
    TURN_FLOOR, TURN_FLOOR_BASE, LOOKAHEAD_M,
)

CONTROL_PATH = os.path.join(TELEMETRY_DIR, 'control.json')
MANUAL_STALE_S = 0.4
REC_MODES = ('continuous', 'triggered')
_DEFAULT_REC_MODE = REC_MODE_DEFAULT if REC_MODE_DEFAULT in REC_MODES else 'continuous'

# ── LiDAR safety knobs ──────────────────────────────────────────────
# Hard limits. A typo in the dashboard (or a hand-edited control.json)
# must never be able to set a stop distance the rover cannot react to.
LIDAR_STOP_MIN_M, LIDAR_STOP_MAX_M = 0.20, 1.50
LIDAR_SLOW_MIN_M, LIDAR_SLOW_MAX_M = 0.30, 3.00
LIDAR_SLOW_V_MIN, LIDAR_SLOW_V_MAX = 0.05, 0.50
LIDAR_STALE_MIN_MS, LIDAR_STALE_MAX_MS = 100, 1000
LIDAR_SECTOR_MIN_DEG, LIDAR_SECTOR_MAX_DEG = 15, 90
# The slow zone must stay clear of the stop zone, whatever was sent.
LIDAR_SLOW_GAP_M = 0.10
# sector_deg must divide 360 evenly or the last sector is a sliver.
LIDAR_SECTOR_CHOICES = (15, 20, 30, 45, 60, 90)


def _rec_mode(value) -> str:
    return value if value in REC_MODES else _DEFAULT_REC_MODE


def clamp_speed(pct: float) -> int:
    return int(round(max(SPEED_MIN_PCT, min(SPEED_MAX_PCT, float(pct)))))


def clamp_lidar_stop_dist_m(v) -> float:
    """Distance in front that triggers a hard stop, in metres."""
    return round(max(LIDAR_STOP_MIN_M, min(LIDAR_STOP_MAX_M, float(v))), 3)


def clamp_lidar_slow_dist_m(v) -> float:
    """Distance in front that triggers the slow-down, in metres."""
    return round(max(LIDAR_SLOW_MIN_M, min(LIDAR_SLOW_MAX_M, float(v))), 3)


def clamp_lidar_slow_v_mps(v) -> float:
    """Speed cap while inside the slow zone, in m/s."""
    return round(max(LIDAR_SLOW_V_MIN, min(LIDAR_SLOW_V_MAX, float(v))), 3)


def clamp_lidar_stale_ms(v) -> int:
    """No fresh sample within this many ms = sensor fault = stop."""
    return int(round(max(LIDAR_STALE_MIN_MS, min(LIDAR_STALE_MAX_MS, float(v)))))


def clamp_lidar_sector_deg(v) -> int:
    """Sector width. Snapped to an exact divisor of 360 so the sector map
    has no sliver sector and ``round(360 / deg)`` is exact."""
    v = max(LIDAR_SECTOR_MIN_DEG, min(LIDAR_SECTOR_MAX_DEG, int(round(float(v)))))
    return min(LIDAR_SECTOR_CHOICES, key=lambda c: (abs(c - v), c))


# key -> clamp, for write_state(lidar={...}) and read_state()
LIDAR_CLAMPS = {
    'lidar_stop_dist_m': clamp_lidar_stop_dist_m,
    'lidar_slow_dist_m': clamp_lidar_slow_dist_m,
    'lidar_slow_v_mps': clamp_lidar_slow_v_mps,
    'lidar_stale_ms': clamp_lidar_stale_ms,
    'lidar_sector_deg': clamp_lidar_sector_deg,
}
# Factory defaults for the same keys (from config.py).
_LIDAR_DEFAULTS = {
    'lidar_stop_dist_m': LIDAR_STOP_DIST_M,
    'lidar_slow_dist_m': LIDAR_SLOW_DIST_M,
    'lidar_slow_v_mps': LIDAR_SLOW_V_MPS,
    'lidar_stale_ms': LIDAR_STALE_MS,
    'lidar_sector_deg': LIDAR_SECTOR_DEG,
}
# What the dashboard renders its sliders from. The server clamps anyway;
# these only keep the UI honest about what a value will actually become.
LIDAR_LIMITS = {
    'lidar_stop_dist_m': {'min': LIDAR_STOP_MIN_M, 'max': LIDAR_STOP_MAX_M,
                          'step': 0.05, 'unit': 'm',
                          'label': 'Stop distance'},
    'lidar_slow_dist_m': {'min': LIDAR_SLOW_MIN_M, 'max': LIDAR_SLOW_MAX_M,
                          'step': 0.05, 'unit': 'm',
                          'label': 'Slow-down distance'},
    'lidar_slow_v_mps': {'min': LIDAR_SLOW_V_MIN, 'max': LIDAR_SLOW_V_MAX,
                         'step': 0.01, 'unit': 'm/s',
                         'label': 'Slow-down speed'},
    'lidar_stale_ms': {'min': LIDAR_STALE_MIN_MS, 'max': LIDAR_STALE_MAX_MS,
                       'step': 10, 'unit': 'ms',
                       'label': 'Stale watchdog'},
    'lidar_sector_deg': {'min': LIDAR_SECTOR_MIN_DEG, 'max': LIDAR_SECTOR_MAX_DEG,
                         'step': 5, 'unit': '°',
                         'label': 'Sector width',
                         'choices': list(LIDAR_SECTOR_CHOICES)},
}


def _unit(x) -> float:
    return max(-1.0, min(1.0, float(x)))


# ── Steering tuning ────────────────────────────────────────────────
# Minimum turn authority, as a wheel-level PWM difference, available the
# instant the rover starts correcting. Capped at TURN_FLOOR so the slider can
# only ever hand the tracking monitor's boost ramp less headroom, never more
# turning authority than the fully-boosted case already allows.
TURN_FLOOR_BASE_MIN = 0.0
TURN_FLOOR_BASE_MAX = float(TURN_FLOOR)
# Factory default (from config.py): unchanged behaviour until the operator
# moves the slider.
TURN_FLOOR_BASE_DEFAULT = float(TURN_FLOOR_BASE)


def clamp_turn_floor_base(v) -> float:
    """Smallest wheel difference (PWM, per side) the follower may command."""
    return round(max(TURN_FLOOR_BASE_MIN,
                     min(TURN_FLOOR_BASE_MAX, float(v))), 1)


# Pure-pursuit lookahead, metres ahead of the rover. Short tracks the centre of
# the line tightly but is noisier; long is smooth but rides off-centre. The low
# end is floored well above the controller's 0.2 m "no usable sample" cut-off,
# so a tuned value can never put the fallback path in play.
LOOKAHEAD_MIN_M = 0.30
LOOKAHEAD_MAX_M = 1.00
LOOKAHEAD_DEFAULT_M = float(LOOKAHEAD_M)


def clamp_lookahead_m(v) -> float:
    """Lookahead distance in metres, clamped to the tunable band."""
    return round(max(LOOKAHEAD_MIN_M, min(LOOKAHEAD_MAX_M, float(v))), 2)


# key -> clamp, for write_state(turn_floor_base=... / lookahead_m=...) and
# read_state(). Both are steering tuning, so they share one table.
STEER_CLAMPS = {
    'turn_floor_base': clamp_turn_floor_base,
    'lookahead_m': clamp_lookahead_m,
}
# Older name, kept so nothing that referenced it breaks.
TURN_FLOOR_CLAMPS = STEER_CLAMPS


def read_state(path: str = CONTROL_PATH) -> dict:
    """Current controls; unreadable / missing file means OFF + default speed."""
    state = {'enabled': False, 'max_speed_pct': DEFAULT_SPEED_PCT,
             'obstacle_stop': True, 'mode': 'auto', 'manual_v': 0.0, 'manual_w': 0.0, 'manual_ts': 0.0,
             'manual_l': None, 'manual_r': None, 'manual_raw': False, 'enc_zero_ts': 0.0,
             'record': False, 'record_rate': REC_DEFAULT_RATE_HZ, 'record_views': False,
             'record_mark_ts': 0.0, 'record_mark_note': '', 'record_mode': _DEFAULT_REC_MODE,
             'record_auto': False,
             'closed_loop': CLOSED_LOOP,
             'turn_floor_base': TURN_FLOOR_BASE,
             'lookahead_m': LOOKAHEAD_DEFAULT_M,
             'lidar_enabled': True,
             'lidar_stop_dist_m': LIDAR_STOP_DIST_M,
             'lidar_slow_dist_m': LIDAR_SLOW_DIST_M,
             'lidar_slow_v_mps': LIDAR_SLOW_V_MPS,
             'lidar_stale_ms': LIDAR_STALE_MS,
             'lidar_sector_deg': LIDAR_SECTOR_DEG}
    try:
        with open(path, 'r', encoding='utf-8') as fh:
            raw = json.load(fh)
        state['enabled'] = bool(raw.get('enabled', False))
        state['max_speed_pct'] = clamp_speed(raw.get('max_speed_pct', DEFAULT_SPEED_PCT))
        state['mode'] = 'manual' if raw.get('mode') == 'manual' else 'auto'
        state['obstacle_stop'] = bool(raw.get('obstacle_stop', True))
        state['manual_v'] = _unit(raw.get('manual_v', 0.0))
        state['manual_w'] = _unit(raw.get('manual_w', 0.0))
        state['manual_ts'] = float(raw.get('manual_ts', 0.0))
        state['closed_loop'] = bool(raw.get('closed_loop', CLOSED_LOOP))
        # Read back through the same clamp write_state uses, so a hand-edited
        # control.json cannot ask for more turn authority than TURN_FLOOR.
        state['turn_floor_base'] = clamp_turn_floor_base(
            raw.get('turn_floor_base', TURN_FLOOR_BASE))
        state['lookahead_m'] = clamp_lookahead_m(
            raw.get('lookahead_m', LOOKAHEAD_DEFAULT_M))
        state['enc_zero_ts'] = float(raw.get('enc_zero_ts', 0.0))
        state['record'] = bool(raw.get('record', False))
        state['record_rate'] = max(0.2, min(60.0, float(raw.get('record_rate', REC_DEFAULT_RATE_HZ))))
        state['record_views'] = bool(raw.get('record_views', False))
        state['record_mode'] = _rec_mode(raw.get('record_mode'))
        state['record_mark_ts'] = float(raw.get('record_mark_ts', 0.0))
        state['record_mark_note'] = str(raw.get('record_mark_note', ''))[:200]
        state['record_auto'] = bool(raw.get('record_auto', False))
        # LiDAR zone knobs. Read back through the same clamps that
        # write_state uses, so a hand-edited file can't widen the ranges.
        state['lidar_enabled'] = bool(raw.get('lidar_enabled', True))
        for key, clamp in LIDAR_CLAMPS.items():
            state[key] = clamp(raw.get(key, _LIDAR_DEFAULTS[key]))
        # The slow zone has to sit clear of the stop zone, or the rover
        # would brake to a crawl inside the distance it must stop at.
        floor = min(LIDAR_SLOW_MAX_M, state['lidar_stop_dist_m'] + LIDAR_SLOW_GAP_M)
        state['lidar_slow_dist_m'] = max(state['lidar_slow_dist_m'], floor)
        if raw.get('manual_l') is not None and raw.get('manual_r') is not None:
            state['manual_l'], state['manual_r'] = _unit(raw['manual_l']), _unit(raw['manual_r'])
            state['manual_raw'] = bool(raw.get('manual_raw', False))
    except (OSError, ValueError, AttributeError, TypeError):
        pass
    return state


def write_state(enabled=None, max_speed_pct=None, mode=None, manual=None,
                obstacle_stop=None, manual_lr=None, closed_loop=None, raw=False, enc_zero=False,
                record=None, record_rate=None, record_views=None, record_mark=None,
                record_mode=None, record_auto=None, lidar=None, turn_floor_base=None,
                lookahead_m=None, path: str = CONTROL_PATH) -> dict:
    """Update only the given fields, atomically; returns the new state.

    ``manual`` is a (v, w) pair in -1..1; ``manual_lr`` a (left, right) pair. Stopping (enabled=False) or leaving
    manual mode always zeroes the manual command. A file lock keeps the
    read-modify-write safe against the 10 Hz manual updates.
    """
    os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
    with open(path + '.lock', 'w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        state = read_state(path)
        if enabled is not None:
            state['enabled'] = bool(enabled)
            if not enabled:
                state['manual_v'] = state['manual_w'] = 0.0
                state['manual_l'] = state['manual_r'] = None
                state['manual_raw'] = False
                # closed_loop survives: it is a standing preference the
                # operator set, not a live command. Stopping the motors
                # must not silently re-arm the encoder PID under them.
        if max_speed_pct is not None:
            state['max_speed_pct'] = clamp_speed(max_speed_pct)
        if obstacle_stop is not None:
            state['obstacle_stop'] = bool(obstacle_stop)
        if mode is not None:
            state['mode'] = 'manual' if mode == 'manual' else 'auto'
            if state['mode'] != 'manual':
                state['manual_v'] = state['manual_w'] = 0.0
                state['manual_l'] = state['manual_r'] = None
                state['manual_raw'] = False
        if manual is not None:
            state['manual_v'], state['manual_w'] = _unit(manual[0]), _unit(manual[1])
            state['manual_l'] = state['manual_r'] = None
            state['manual_raw'] = False
            state['manual_ts'] = time.time()
        if manual_lr is not None:
            state['manual_l'], state['manual_r'] = _unit(manual_lr[0]), _unit(manual_lr[1])
            state['manual_v'] = state['manual_w'] = 0.0
            state['manual_raw'] = bool(raw)
            state['manual_ts'] = time.time()
        if closed_loop is not None:
            state['closed_loop'] = bool(closed_loop)
        if turn_floor_base is not None:
            state['turn_floor_base'] = clamp_turn_floor_base(turn_floor_base)
        if lookahead_m is not None:
            state['lookahead_m'] = clamp_lookahead_m(lookahead_m)
        if enc_zero:
            state['enc_zero_ts'] = time.time()
        if record is not None:
            state['record'] = bool(record)
        if record_rate is not None:
            state['record_rate'] = max(0.2, min(60.0, float(record_rate)))
        if record_views is not None:
            state['record_views'] = bool(record_views)
        if record_mode is not None:
            state['record_mode'] = _rec_mode(record_mode)   # invalid -> default
        if record_mark is not None:
            state['record_mark_ts'] = time.time()
            state['record_mark_note'] = str(record_mark)[:200]
        if record_auto is not None:
            state['record_auto'] = bool(record_auto)
        if lidar:
            for key, value in lidar.items():
                if value is None:
                    continue
                if key == 'lidar_enabled':
                    state['lidar_enabled'] = bool(value)
                elif key in LIDAR_CLAMPS:
                    state[key] = LIDAR_CLAMPS[key](value)
                else:
                    raise ValueError(f'unknown lidar setting: {key!r}')
            floor = min(LIDAR_SLOW_MAX_M,
                        state['lidar_stop_dist_m'] + LIDAR_SLOW_GAP_M)
            state['lidar_slow_dist_m'] = max(state['lidar_slow_dist_m'], floor)
        state['ts'] = time.time()
        fd, tmp = tempfile.mkstemp(prefix='.ctl-', dir=os.path.dirname(path) or '.')
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as fh:
                json.dump(state, fh)
            os.replace(tmp, path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
    return state


def write_enabled(enabled: bool, path: str = CONTROL_PATH) -> None:
    """Used at AGV startup: OFF, automatic mode, no manual command."""
    write_state(enabled=enabled, mode='auto' if not enabled else None, path=path)


class ControlReader:
    """Reads the tiny control file on every call (no mtime caching: file
    timestamps are coarse, so two quick writes could look unchanged)."""

    def __init__(self, path: str = CONTROL_PATH) -> None:
        self.path = path

    def state(self) -> dict:
        return read_state(self.path)

    def enabled(self) -> bool:
        return self.state()['enabled']
