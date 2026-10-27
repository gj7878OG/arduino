"""Line-following decision engine (camera + Arduino, no LiDAR).

Pure logic, no I/O. Feed it the vision result and link health, get back the
*targets* for the motion layer: forward speed ``v`` and turn ``omega`` in PWM
units (omega > 0 = turn right), plus whether the stop must be an emergency
one. The wheels never see these numbers directly - motion.py shapes them
into jerk-limited PWM.

Priority, highest first:
  1. no camera frame / Arduino link lost          -> emergency stop
  2. camera obstacle STOP                         -> emergency stop
  3. line visible                                 -> steer + speed from geometry
       obstacle SLOW                              -> speed scaled down
  4. line just lost (< CAUTION_S)                 -> hold heading, slower
  5. line lost, recovery enabled (< RECOVER_S)    -> pivot slowly to last side
  6. otherwise                                    -> smooth stop
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum
from typing import Optional, Tuple

from config import (
    CAUTION_S, CAUTION_SPEED_FRAC, ERR_SLOW_M, K_ANGLE,
    K_CURV, K_CURV_FF, K_LOOK, K_OFFSET, K_OFFSET_CURV, KAPPA_MAX, KI_OFFSET,
    LINE_EMA_ALPHA, LOOKAHEAD_M, OBST_SLOW_FRAC, OMEGA_MAX, PURSUIT_GAIN,
    RECOVERY, RECOVER_OMEGA, RECOVER_S, STEER_GAIN, STEER_MODE, V_MAX, V_MIN,
    WHEEL_BASE_M, ALIGN_ENTER_DEG, ALIGN_ENTER_M, ALIGN_EXIT_DEG, ALIGN_EXIT_M,
    ALIGN_OMEGA, TRACK_BOOST_MAX, TRACK_OFFLINE_M, TRACK_OFFLINE_S,
    TRACK_ON_LINE_M, TRACK_STRAIGHT_CURV, TRACK_STUCK_MIN_M, TRACK_STUCK_S, TURN_FLOOR, TURN_FLOOR_BASE, TURN_FLOOR_FULL_ERR_M,
)
from vision import LineGeometry, ObstacleResult


class Direction(str, Enum):
    """Camera-side line-following verdict (kept for the dashboard)."""
    LEFT = 'LEFT'
    CENTER = 'CENTER'
    RIGHT = 'RIGHT'
    CAUTION = 'CAUTION'          # line briefly lost; holding heading
    LOST = 'LOST'                # line gone (stopped or searching)
    UNAVAILABLE = 'UNAVAILABLE'  # no camera frame


_GREEN = (0, 255, 0)
_AMBER = (0, 255, 255)
_RED = (0, 0, 255)


@dataclass(frozen=True)
class Decision:
    direction: Direction
    camera_cmd: str          # compact F / L / R / S for telemetry
    final_cmd: str
    v: float                 # forward target, PWM units
    omega: float             # turn target, PWM units (+ = right)
    emergency: bool          # use the harder (still bounded) stop ramp
    status_text: str
    status_color: Tuple[int, int, int]   # BGR for the HUD
    err_m: float = 0.0       # combined steering error, for display
    track: str = 'NO_LINE'   # ON_LINE | CORRECTING | STUCK | DRIFTING | OFF_LINE | ALIGNING
    boost: float = 1.0       # steering boost applied by the tracking monitor


def _clamp(x: float, lo: float, hi: float) -> float:
    return lo if x < lo else hi if x > hi else x


class TrackMonitor:
    """Checks that steering is actually bringing the rover back to the line.

    There is no wheel feedback, so the only proof is the camera: if the error
    is not shrinking while we are steering, the turn is too weak (friction,
    low battery, low speed slider). The monitor then raises ``boost``, which
    multiplies the turn command (and slows the forward speed), until the error
    starts to fall again.
    """

    def __init__(self) -> None:
        self.state = 'NO_LINE'
        self.boost = 1.0
        self._hist: list = []             # (t, |err|) over ~1.2 s
        self._bad_since: Optional[float] = None
        self._far_since: Optional[float] = None
        self._t_prev: Optional[float] = None
        self._last_sign = 0.0
        self._crossed_t = -99.0

    def update(self, now: float, err: float, curv: float = 0.0,
               moving: bool = True, mag: Optional[float] = None) -> Tuple[str, float]:
        # ``mag`` is how far we really are from the line (worst of offset,
        # lookahead and heading); ``err`` only supplies the side for overshoot
        # detection. The combined steering error can cancel to ~0 while the
        # rover is far off, so it must not decide 'on the line'.
        e = abs(err) if mag is None else mag
        dt = 0.0 if self._t_prev is None else min(now - self._t_prev, 0.2)
        self._t_prev = now
        self._hist.append((now, e))
        while self._hist and now - self._hist[0][0] > 1.2:
            self._hist.pop(0)
        if not moving:
            # Parked / ramping up: nothing to judge yet. Forget the history so
            # the error trend only counts once the wheels are really driving.
            self._hist = [(now, e)]
            self._bad_since = None
            self.boost = max(1.0, self.boost - 1.0 * dt)
            self.state = ('ON_LINE' if e < (TRACK_ON_LINE_M * 1.6 if self.state == 'ON_LINE'
                                            else TRACK_ON_LINE_M) else 'CORRECTING')
            return self.state, self.boost
        span = self._hist[-1][0] - self._hist[0][0]
        slope = ((self._hist[-1][1] - self._hist[0][1]) / span) if span > 0.5 else None

        if e > 0.02 and err * self._last_sign < 0:       # crossed the line: steering works
            self._crossed_t = now
        if e > 0.02:
            self._last_sign = 1.0 if err > 0 else -1.0
        recently_crossed = (now - self._crossed_t) < 2.0

        on_line_limit = (TRACK_ON_LINE_M * 1.6 if self.state == 'ON_LINE'
                         else TRACK_ON_LINE_M)          # hysteresis: no flicker at the edge
        if e < on_line_limit:
            self.state, self._bad_since = 'ON_LINE', None
            self.boost = max(1.0, self.boost - 1.5 * dt)
        elif recently_crossed:
            # Overshooting means the turn is strong enough: back the boost off.
            self.state, self._bad_since = 'CORRECTING', None
            self.boost = max(1.0, self.boost - 1.0 * dt)
        elif slope is not None and slope < -0.02:
            self.state, self._bad_since = 'CORRECTING', None
            self.boost = max(1.0, self.boost - 0.6 * dt)
        elif e < TRACK_STUCK_MIN_M or abs(curv) > TRACK_STRAIGHT_CURV:
            # small and settling, or a curved line where a steady lag is normal
            self.state, self._bad_since = 'CORRECTING', None
        else:
            if self._bad_since is None:
                self._bad_since = now
            worsening = slope is not None and slope > 0.03
            if now - self._bad_since >= (0.5 if worsening else TRACK_STUCK_S):
                self.state = 'DRIFTING' if worsening else 'STUCK'
                self.boost = min(TRACK_BOOST_MAX, self.boost + 1.2 * dt)
            else:
                self.state = 'CORRECTING'
        if e > TRACK_OFFLINE_M:
            self._far_since = self._far_since or now
            if now - self._far_since >= TRACK_OFFLINE_S:
                self.state = 'OFF_LINE'
        else:
            self._far_since = None
        return self.state, self.boost

    def reset(self) -> None:
        self.__init__()


class LineController:
    def __init__(self) -> None:
        self._off: Optional[float] = None
        self._ang = 0.0
        self._look = 0.0
        self._curv = 0.0
        self._last_seen: Optional[float] = None
        self._last_side = 0
        self._last_v = 0.0
        self._last_omega = 0.0
        self._dir = Direction.CENTER
        self._integ = 0.0
        self._t_prev: Optional[float] = None
        # Dashboard max-speed scale (set by the AGV each tick). The wheels run
        # at v * speed_scale, so loop gain grows with it.
        self.speed_scale = 1.0
        # Minimum turn authority, as a wheel-level PWM difference, that is
        # available the moment we start correcting. The tracking monitor adds
        # on top of this up to TURN_FLOOR. Set by the AGV each tick from the
        # dashboard slider; a skid-steer chassis that needs ~40 PWM to yaw at
        # all will otherwise be handed only a few PWM here and drive straight.
        self.turn_floor_base = TURN_FLOOR_BASE
        # Pure-pursuit lookahead (metres ahead of the rover). Normally the
        # vision result carries the real sampled distance; this is the fallback
        # for a line too short to give one. Mirrors Vision.lookahead_m so both
        # agree when the operator tunes it from the dashboard.
        self.lookahead_m = LOOKAHEAD_M
        # Set by the AGV: True only while the wheels are really driving in AUTO
        # mode (the tracking monitor judges steering only then).
        self.moving = False
        self._monitor = TrackMonitor()
        self._align = False

    def reset(self) -> None:
        # The dashboard tuning knobs are operator settings, not run state:
        # re-arming the rover (or dropping out of MANUAL) must not undo them.
        floor_base = self.turn_floor_base
        look = self.lookahead_m
        self.__init__()
        self.turn_floor_base = floor_base
        self.lookahead_m = look

    def _smooth(self, line: LineGeometry) -> None:
        a = LINE_EMA_ALPHA
        if self._off is None:
            self._off, self._ang = line.offset_m, math.radians(line.angle_deg)
            self._look, self._curv = line.lookahead_m, line.curvature
            return
        self._off += a * (line.offset_m - self._off)
        self._ang += a * (math.radians(line.angle_deg) - self._ang)
        self._look += a * (line.lookahead_m - self._look)
        self._curv += a * (line.curvature - self._curv)

    def _steer(self, line: LineGeometry, err: float, v: float, now: float) -> float:
        """Turn target (PWM units, + = right) for forward target ``v``."""
        dt = 0.0 if self._t_prev is None else min(now - self._t_prev, 0.2)
        self._t_prev = now
        if STEER_MODE != 'pursuit':
            return _clamp(STEER_GAIN * err, -OMEGA_MAX, OMEGA_MAX)
        # Pure pursuit: curvature that reaches the lookahead point. The yaw
        # rate needed is curvature * speed, and in PWM units that is
        # omega = kappa * v * wheel_base / 2, so the turn scales with speed
        # and the loop stays stable from creeping to full speed.
        dist = line.look_dist_m if line.look_dist_m > 0.2 else self.lookahead_m
        y = self._look
        kappa = PURSUIT_GAIN * 2.0 * y / (dist * dist + y * y)
        # The near-offset and trim terms add loop gain; the camera delay is
        # fixed, so back them off (quadratically) once the real speed exceeds
        # the speed they were tuned at.
        fast = max(1.0, v * self.speed_scale / V_MAX)
        damp = 1.0 / (fast * fast)
        kappa += damp * K_OFFSET_CURV * self._off + K_CURV_FF * self._curv
        # Slow trim for a weaker motor / drifting bias: integrate the near
        # offset only while close to the line, with a hard limit.
        if abs(self._off) < 0.15:
            self._integ = _clamp(self._integ + self._off * dt, -0.5, 0.5)
        else:
            self._integ *= max(0.0, 1.0 - dt)
        kappa += damp * KI_OFFSET * self._integ
        kappa = _clamp(kappa, -KAPPA_MAX, KAPPA_MAX)
        return _clamp(kappa * v * WHEEL_BASE_M / 2.0, -OMEGA_MAX, OMEGA_MAX)

    def decide(self, line: LineGeometry, frame_ok: bool,
               obstacle: ObstacleResult, link_alive: bool,
               now: float) -> Decision:
        if not frame_ok:
            return Decision(Direction.UNAVAILABLE, 'S', 'S', 0, 0, True,
                            'NO CAMERA', _RED)
        if not link_alive:
            return Decision(Direction.LOST, 'S', 'S', 0, 0, True,
                            'ARDUINO LINK DOWN', _RED)
        if obstacle.state == 'STOP':
            return Decision(
                Direction.CAUTION, 'S', 'S', 0, 0, True,
                f'OBSTACLE STOP {obstacle.distance_m:.2f} m', _RED)

        if line.found:
            self._smooth(line)
            self._last_seen = now
            err = (K_LOOK * self._look + K_OFFSET * self._off
                   + K_ANGLE * self._ang)
            scale = max(self.speed_scale, 0.1)
            cap = scale * V_MAX                      # absolute speed ceiling, levels
            ang_deg = math.degrees(self._ang)
            dist_off = max(abs(self._off), abs(self._look), 0.5 * abs(self._ang))
            track, boost = self._monitor.update(now, err, self._curv, self.moving, dist_off)

            # Pivot in place when far off the line / pointing away from it;
            # leave only once well back (hysteresis).
            # Triggered by heading or a far-off lookahead point, NOT by plain
            # sideways offset (the arc steering handles that without weaving).
            if self._align:
                if abs(self._look) < ALIGN_EXIT_M and abs(ang_deg) < ALIGN_EXIT_DEG:
                    self._align = False
            elif abs(self._look) > ALIGN_ENTER_M or abs(ang_deg) > ALIGN_ENTER_DEG:
                self._align = True

            if self._align:
                # A pivot fixes the HEADING, so turn toward the line's direction
                # (the sign of its angle); only when the heading is fine and the
                # lookahead point is far off does the sideways error decide.
                if abs(ang_deg) > ALIGN_EXIT_DEG:
                    side = 1 if ang_deg > 0 else -1
                else:
                    side = 1 if self._look > 0 else -1
                w_final = min(ALIGN_OMEGA, max(TURN_FLOOR, 0.5 * cap)) * boost
                w_final = min(w_final, 255.0)
                self._last_side = side
                self._last_v, self._last_omega = 0.0, side * w_final / scale
                direction, cmd = ((Direction.RIGHT, 'R') if side > 0
                                  else (Direction.LEFT, 'L'))
                self._dir = direction
                return Decision(direction, cmd, cmd, 0.0, side * w_final / scale,
                                False, f'ALIGNING to line err={err:+.2f}m',
                                _AMBER, err, 'ALIGNING', boost)

            v = V_MAX * (1.0 - 0.6 * min(1.0, abs(err) / ERR_SLOW_M))
            v /= 1.0 + K_CURV * abs(self._curv)
            v /= 1.0 + 0.6 * (boost - 1.0)          # slow down while correcting
            v = max(v, V_MIN)
            text, color = 'FOLLOWING', _GREEN
            if track in ('STUCK', 'DRIFTING', 'OFF_LINE'):
                text, color = f'CORRECTING ({track.lower()}) x{boost:.1f}', _AMBER
            if obstacle.state == 'SLOW':
                v = max(v * OBST_SLOW_FRAC, V_MIN)
                text = f'OBSTACLE SLOW {obstacle.distance_m:.2f} m'
                color = _AMBER
            omega = self._steer(line, err, v, now) * boost

            # Minimum turn authority: a real chassis needs a minimum wheel
            # difference before it turns, whatever the speed slider says.
            w_final = omega * scale
            # The floor starts small and grows only while the tracking monitor
            # sees the error NOT shrinking (steering too weak for this chassis).
            k = (boost - 1.0) / max(TRACK_BOOST_MAX - 1.0, 1e-6)
            floor = (self.turn_floor_base
                     + (TURN_FLOOR - self.turn_floor_base) * min(1.0, k))
            cap_v = max(8.0, v * scale) if k < 0.25 else TURN_FLOOR   # a stuck rover may pivot
            floor = (min(floor, cap_v)
                     * min(1.0, abs(err) / TURN_FLOOR_FULL_ERR_M))
            if abs(err) > 0.02 and abs(w_final) < floor:
                # Strengthen the turn the controller already wants; only fall
                # back to the error's sign when it asks for (almost) no turn.
                sgn = (1 if w_final > 0 else -1) if abs(w_final) > 0.5 else (1 if err > 0 else -1)
                w_final = sgn * floor
            omega = _clamp(w_final / scale, -255.0 / scale, 255.0 / scale)

            if abs(err) > 0.02:
                self._last_side = 1 if err > 0 else -1
            self._last_v, self._last_omega = v, omega
            # The label follows the turn we are actually commanding (not the
            # raw error, whose terms can cancel): leave CENTER above a small
            # turn, return to it only well below, so it does not flicker.
            turn = omega * scale
            enter, leave = 6.0, 3.0
            limit = leave if self._dir is not Direction.CENTER else enter
            if abs(turn) <= limit:
                direction, cmd = Direction.CENTER, 'F'
            elif turn > 0:
                direction, cmd = Direction.RIGHT, 'R'
            else:
                direction, cmd = Direction.LEFT, 'L'
            self._dir = direction
            return Decision(direction, cmd, cmd, v, omega, False,
                            f'{text} err={err:+.2f}m', color, err, track, boost)

        lost_for = (now - self._last_seen) if self._last_seen is not None else None
        if lost_for is None or lost_for > CAUTION_S:
            self._off = None            # re-acquire starts from fresh values
            self._monitor.reset()
            self._align = False
        if lost_for is not None and lost_for <= CAUTION_S:
            return Decision(
                Direction.CAUTION, 'F', 'F',
                max(self._last_v * CAUTION_SPEED_FRAC, V_MIN * 0.8),
                self._last_omega, False,
                'CAUTION holding heading', _AMBER)
        if (RECOVERY and lost_for is not None and self._last_side != 0
                and lost_for <= CAUTION_S + RECOVER_S
                and obstacle.state != 'SLOW'):
            side = self._last_side
            return Decision(
                Direction.LOST, 'R' if side > 0 else 'L',
                'R' if side > 0 else 'L', 0, side * RECOVER_OMEGA, False,
                f'SEARCHING {"right" if side > 0 else "left"}', _AMBER)
        return Decision(Direction.LOST, 'S', 'S', 0, 0, False,
                        f'LINE LOST ({line.reason or "no line"})', _RED)
