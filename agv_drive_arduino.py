#!/usr/bin/env python3
"""AGV orchestrator: camera-only line following that drives an Arduino.

  camera  -> Vision (bird's-eye warp, line geometry, floor obstacles)
          -> LineController (targets: forward v, turn omega, emergency flag)
          -> MotionThread (fixed 50 Hz, jerk-limited S-curve per wheel)
          -> Arduino  ``M<left>,<right>``  (firmware v2, 1 s command timeout)
  telemetry -> status.json / frame.jpg for the web dashboard

The dashboard ON/OFF switch (control.json) gates the motors. The AGV always
starts OFF. See README_AGV.md for tuning.
"""

from __future__ import annotations

import dataclasses
import logging
import os
import signal
import time
import traceback
from typing import Optional

import cv2
import numpy as np

from arduino import Arduino, ArduinoReport
from camera import Camera, CameraReport
from config import (
    LOG_FILE, MANUAL_TURN_FRAC, OBST_ENABLED, SHOW_WINDOW, TICKS_PER_M, V_MAX,
    WHEEL_DIAMETER_M, WHEEL_MAX_MPS,
    LIVE_FRAMES_DIR, LIVE_FRAMES_ENABLED, LIVE_FRAMES_HZ,
)
from control_state import MANUAL_STALE_S, ControlReader, write_enabled, write_state
from frame_log import FrameLog
from lidar import LidarSafety, LidarSnapshot, PointsPublisher
from rec_row import build_log_row
import build_info
import config
from rec_row import build_telem_row
from recorder import make_recorder
from triggers import TriggerEngine, legacy_problem
from decision import Decision, Direction, LineController
from manual import manual_command
from motion import MotionThread
from telemetry import (
    LOG_CAT_CAMERA, LOG_CAT_STM32, LOG_CAT_SYSTEM,
    SensorProbe, TelemetryPublisher, TelemetryState,
)
from vision import ObstacleResult, Vision, VisionResult

# ── Logging ──────────────────────────────────────────────────────────────
os.makedirs(os.path.dirname(LOG_FILE) or '.', exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger('agv')
logging.getLogger('werkzeug').setLevel(logging.WARNING)

FRAME_STALE_S = 0.5          # older than this = camera is not delivering
PUBLISH_PERIOD_S = 0.12      # dashboard refresh (frame + state)
FIRMWARE_WAIT_S = 3.0


# ── Helpers ────────────────────────────────────────────────────────────

def probe_to_dict(report, note: Optional[str] = None) -> dict:
    if isinstance(report, CameraReport):
        return SensorProbe(
            device='camera',
            port='CSI (IMX219 ribbon)' if report.pipeline else 'fake',
            status='OK' if report.frame_ok else ('NO_FRAMES' if report.opened else 'FAILED'),
            error=report.error,
            latency_ms=report.latency_ms,
        ).as_dict()
    if isinstance(report, ArduinoReport):
        return SensorProbe(
            device='arduino', port=report.port,
            status=('OK' if report.opened and not note
                    else 'ERROR' if report.opened else 'FAILED'),
            error=report.error or note, latency_ms=report.latency_ms,
        ).as_dict()
    return {}


# ── Orchestrator ────────────────────────────────────────────────────────

class AGV:
    """camera -> vision -> controller -> motion thread -> arduino."""

    def __init__(self) -> None:
        self.camera = Camera()
        self.arduino = Arduino()
        self.telemetry = TelemetryPublisher()
        self.vision = Vision()
        self.controller = LineController()
        self.motion = MotionThread(self.arduino)
        self.control = ControlReader()
        # Rotating LiDAR obstacle safety. Constructing it opens nothing;
        # probe() owns the connect so startup stays in one place.
        self.lidar = LidarSafety()
        self._lid: LidarSnapshot = self.lidar.snapshot()
        self._lid_action = 'OFF'
        self._lid_points_writer: Optional[PointsPublisher] = None
        self._lid_zones_cache: dict = self.control.state()
        self._lid_zones_t = 0.0
        self.probes: list[dict] = []
        self.last_error: Optional[str] = None
        self.stop_requested = False
        self.stop_reason = 'main loop ended'
        self._last_seq = -1
        self._last_publish = 0.0
        self._enabled = False
        self._closed_wanted = True
        self.recorder = make_recorder()
        self.trig = TriggerEngine()
        self._rec_warned = False
        self._rec_req = False                   # we started a recording for the current request
        self._rec_mark_seen = time.time()
        self._enc_zero_seen = time.time()       # ignore zero requests from before this run
        self._last: dict = {}
        self._vis = VisionResult()
        self._snap_t = 0.0
        self._dw: dict = {}
        self._snap_n = 0
        self._last_row: Optional[dict] = None  # last build_log_row dict (consumed by frame_log)
        self.frame_log: Optional[FrameLog] = None
        if LIVE_FRAMES_ENABLED:
            self.frame_log = FrameLog(base_dir=LIVE_FRAMES_DIR, hz=LIVE_FRAMES_HZ)
            try:
                self.frame_log.start()
            except Exception:
                log.exception('frame_log start failed (continuing without it)')
                self.frame_log = None

    # ── startup ──────────────────────────────────────────────────────
    def probe(self) -> None:
        cam = self.camera.open()
        ard = self.arduino.connect()
        note = None
        if ard.opened:
            t_end = time.monotonic() + FIRMWARE_WAIT_S
            while time.monotonic() < t_end and not self.arduino.snapshot().protocol_ok:
                time.sleep(0.05)
            if not self.arduino.snapshot().protocol_ok:
                note = ('firmware v2 not detected - flash MotorControl.ino '
                        '(motors stay off)')
                log.error('[Arduino] %s', note)
        self.probes = [probe_to_dict(cam), probe_to_dict(ard, note)]
        # LiDAR last: it blocks ~9 s while the head spins up, and the
        # rover must not start moving until the sensor is either live or
        # known-missing. A missing port fails fast, so an unplugged
        # sensor costs nothing here.
        self.probes.append(self._probe_lidar())

    def _probe_lidar(self) -> dict:
        """LiDAR row for the same "Sensor probes" table."""
        if not self.lidar.enabled:
            return {'name': 'LiDAR', 'ok': False, 'detail': 'disabled in config',
                    'note': 'obstacle safety OFF'}
        opened = self.lidar.connect()
        if not opened:
            return {'name': 'LiDAR', 'ok': False,
                    'detail': self.lidar.port,
                    'note': 'obstacle safety ARMED but sensor unreachable '
                            '- the rover will refuse to move'}
        # Start the points.json writer once so the dashboard can render
        # the raw-points polar overlay. Out dir is the same one the
        # telemetry publisher writes status.json into; the dashboard's
        # web.py serves /points.json from that location.
        #
        # zones_source is re-read on every publish tick, so moving a zone
        # slider on the dashboard moves the canvas's stop/slow rings
        # within ~200 ms instead of at the next restart.
        try:
            from config import TELEMETRY_DIR
            self._lid_points_writer = PointsPublisher(
                self.lidar,
                TELEMETRY_DIR,
                zones_source=lambda: (
                    self.control.state().get('lidar_stop_dist_m', 0.50),
                    self.control.state().get('lidar_slow_dist_m', 1.00),
                ),
            ).start()
        except Exception:
            log.exception('LiDAR points publisher failed to start')
            self._lid_points_writer = None
        snap = self.lidar.snapshot()
        return {'name': 'LiDAR', 'ok': snap.ok,
                'detail': f'{self.lidar.port} @ {self.lidar.baud} baud',
                'note': f'{self.lidar.n_sectors} sectors of '
                        f'{self.lidar.sector_deg} deg'}

    # ── LiDAR obstacle safety ────────────────────────────────────────
    def _lid_zones(self) -> dict:
        """The LiDAR settings in force, re-read at most a few times a
        second. _publish() needs them for the dashboard and does not
        otherwise hold the tick's control dict."""
        now = time.monotonic()
        if now - self._lid_zones_t > 0.25:
            self._lid_zones_cache = self.control.state()
            self._lid_zones_t = now
        return self._lid_zones_cache

    def _lidar_override(self, ctl) -> Optional[tuple]:
        """Decide what the LiDAR allows this tick.

        Returns ``(action, v_cap, reason)`` or None when the layer is off.
        ``action`` is CLEAR / SLOW / ESTOP; ``v_cap`` is the ceiling on the
        commanded forward speed in m/s (None = no ceiling).

        The zones come from control.json so the operator can retune from
        the dashboard without SSH. This runs in every mode — a safety kill
        is a safety kill — and sits *after* the line follower's own
        obstacle check and *before* the motor command, so it can only ever
        reduce motion, never add any.
        """
        if not self.lidar.enabled or not ctl.get('lidar_enabled', True):
            self._lid_action = 'OFF'
            return None
        snap = self.lidar.snapshot()
        self._lid = snap
        stop_d = ctl['lidar_stop_dist_m']
        slow_d = ctl['lidar_slow_dist_m']

        # No fresh samples: we cannot see, so we must not move. This is
        # the fail-safe direction — a sensor fault stops the rover rather
        # than silently removing the layer.
        if not snap.ok:
            self._lid_action = 'ESTOP'
            return ('ESTOP', None, f'lidar {snap.reason}')

        front = snap.min_front_m
        if front == float('inf'):
            self._lid_action = 'CLEAR'
            return None                       # nothing in the front sector
        if front < stop_d:
            self._lid_action = 'ESTOP'
            return ('ESTOP', None, f'lidar estop front={front:.2f}m')
        if front < slow_d:
            self._lid_action = 'SLOW'
            return ('SLOW', ctl['lidar_slow_v_mps'],
                    f'lidar slow front={front:.2f}m')
        self._lid_action = 'CLEAR'
        return None

    def start(self) -> None:
        write_enabled(False)                    # never start driving on boot (speed kept)
        write_state(record=False)               # ... nor start recording by itself
        try:
            build_info.get_build_info()          # cached; code version for the recordings
        except Exception:
            log.exception('build_info failed (continuing)')
        self.telemetry.restore_previous()
        self.telemetry.start()
        log.info('=' * 55)
        log.info('  AGV - camera line follower -> Arduino (dashboard OFF)')
        log.info('=' * 55)
        self.probe()
        log.info('[Probe] Camera:  %s', self.probes[0])
        log.info('[Probe] Arduino: %s', self.probes[1])
        self.motion.start()
        log.info('Running %s - Ctrl+C to stop',
                 'headlessly' if not SHOW_WINDOW else 'with local window')

    # ── shutdown ─────────────────────────────────────────────────────
    def stop(self) -> None:
        log.warning('Shutting down AGV (%s)...', self.stop_reason)
        self.recorder.stop('AGV shutting down')
        self.recorder.close(5.0)
        fl = getattr(self, 'frame_log', None)
        if fl is not None:
            try:
                fl.close()
            except Exception:
                log.exception('frame_log close failed (continuing)')
            self.frame_log = None
        self.motion.request_stop()                 # ramps to 0, then returns
        if self.motion.is_alive():
            self.motion.join(timeout=2.0)
        if self.arduino.isOpen():
            self.arduino.send('S', force=True)
            log.info('   Motors stopped.')
        self.camera.release()
        self.arduino.close()
        # Reap the LiDAR threads explicitly. Without this the points
        # publisher keeps the sensor's buffer alive and the reader
        # thread keeps /dev/ttyUSB0 open, so a restart in the same
        # process (or a supervisor's restart loop) finds the port busy.
        pub, self._lid_points_writer = self._lid_points_writer, None
        if pub is not None:
            pub.stop()
        self.lidar.close()
        log.info('AGV shutdown complete.')
        self.telemetry.publish(
            TelemetryState(
                running=False, enabled=False,
                sensor_probes=self.probes, last_error=self.last_error,
                final_command='S',
            ),
            log_message=[
                (LOG_CAT_CAMERA, 'Camera disconnected (capture closed)'),
                (LOG_CAT_SYSTEM, f'AGV stopped ({self.stop_reason})'),
            ],
        )
        self.telemetry.stop()

    # ── main loop ────────────────────────────────────────────────────
    def tick(self) -> None:
        now = time.monotonic()
        info = self.camera.latest()
        frame_ok = info is not None and (now - info.monotonic) < FRAME_STALE_S
        if frame_ok and info.sequence == self._last_seq:
            time.sleep(0.004)                      # wait for a new frame
            return
        if not frame_ok:
            time.sleep(0.02)
        frame = info.image if frame_ok else None
        if frame_ok:
            self._last_seq = info.sequence

        vis = self.vision.analyze(frame, debug=True) if frame_ok else VisionResult()
        self._vis = vis

        ctl = self.control.state()
        if ctl['enc_zero_ts'] > self._enc_zero_seen:
            self._enc_zero_seen = ctl['enc_zero_ts']
            self.arduino.zero_encoders()
        self._snapshot_if_obstacle(now, frame, vis)
        enabled = ctl['enabled']
        speed_pct = ctl['max_speed_pct']
        # Dashboard master limit: every speed and turn target scales with it.
        scale = (speed_pct / 100.0) * 255.0 / V_MAX
        if enabled and not self._enabled:
            self.controller.reset()
            self.vision.reset_floor()       # operator re-armed: relearn the floor
        link_ok = self.arduino.link_alive()
        self.controller.speed_scale = scale
        self._closed_wanted = ctl['closed_loop']
        _w = self.motion.snapshot()
        self.controller.moving = (enabled and ctl['mode'] == 'auto'
                                  and (abs(_w.left) + abs(_w.right)) > 80)
        enforce = ctl['obstacle_stop']
        # A raw driver test bypasses the encoder PID for as long as it is fresh.
        raw_test = (ctl['mode'] == 'manual' and ctl.get('manual_l') is not None
                    and ctl.get('manual_raw')
                    and time.time() - ctl['manual_ts'] < MANUAL_STALE_S)
        self.motion.set_closed_loop(ctl['closed_loop'] and not raw_test)
        self.motion.set_raw_pwm(raw_test)
        manual = ctl['mode'] == 'manual'
        if manual:
            # Manual owns the wheels: the line follower is not consulted at all
            # and its state is cleared so AUTO starts fresh.
            self.controller.reset()
            d = Decision(Direction.CENTER, 'S', 'S', 0.0, 0.0, True,
                         'MANUAL', (0, 200, 255))
            d, v_t, w_t = self._manual_target(d, ctl, speed_pct, enabled)
        else:
            d = self.controller.decide(vis.line, frame_ok,
                                       vis.obstacle if enforce else ObstacleResult(),
                                       link_ok, now)
            if not enforce and vis.obstacle.state != 'CLEAR':
                d = dataclasses.replace(
                    d, status_text=d.status_text + f' [obstacle {vis.obstacle.state} ignored]')
            v_t, w_t = d.v * scale, d.omega * scale
        # LiDAR obstacle safety — the last word before the motor command,
        # in every mode. It can only subtract speed.
        lid = self._lidar_override(ctl)
        if lid is not None:
            action, v_cap, reason = lid
            if action == 'ESTOP':
                v_t = w_t = 0.0
                if not d.emergency:
                    d = dataclasses.replace(d, emergency=True,
                                            status_text=reason)
            elif action == 'SLOW' and v_cap is not None:
                # v_t is PWM-scaled (see `scale` above); convert the cap
                # back into the same units so both sides agree.
                v_limit = v_cap * 255.0 / V_MAX
                if v_t > v_limit:
                    v_t = v_limit
                d = dataclasses.replace(d, status_text=d.status_text + f' [{reason}]')
        self.motion.set_target(v_t, w_t, d.emergency, enabled)
        wheels = self.motion.snapshot()

        events = self._transitions(d, vis, enabled, frame_ok, ctl['mode'])
        self._record(now, frame, info, vis, d, ctl, enabled, speed_pct, v_t, w_t, wheels, link_ok, events)
        # Live per-tick JSONL stream for the dashboard "Logs" tab. The writer
        # rate-limits internally; we just feed it every tick.
        if self.frame_log is not None and self._last_row is not None:
            self.frame_log.write(build_telem_row(self._last_row))
        self._enabled = enabled
        self._publish(now, frame, vis, d, wheels, enabled, link_ok, events,
                      speed_pct, v_t, w_t, ctl['mode'], enforce)

    # ── test recording ───────────────────────────────────────────────
    def _record(self, now, frame, info, vis, d, ctl, enabled, speed_pct, v_t, w_t,
                wheels, link_ok, events) -> None:
        rec = self.recorder
        want = ctl['record']
        if want and not rec.active:
            if self._rec_req:                  # it ended by itself (disk / size / time limit)
                self._rec_req = False
                write_state(record=False)
            else:
                self._rec_req = True
                name = rec.start(ctl['record_rate'], ctl['record_views'],
                                 self._rec_meta(frame), mode=ctl['record_mode'])
                if not name:
                    self._rec_req = False
                    write_state(record=False)
        elif not want and rec.active:
            rec.stop()
            self._rec_req = False
        elif not want:
            self._rec_req = False
        if not rec.active:
            return
        if ctl['record_mark_ts'] > self._rec_mark_seen:
            self._rec_mark_seen = ctl['record_mark_ts']
            rec.mark(ctl['record_mark_note'])
        for cat, text in events:
            rec.event(cat.lower(), text)
        line, obst = vis.line, vis.obstacle
        snap = self.arduino.snapshot()
        h, w = (frame.shape[:2] if frame is not None else (0, 0))
        curve_px = [[int(x), int(y)] for x, y in
                    self.vision.world_to_frame(list(line.curve)[::max(1, len(line.curve) // 14)], w, h)] if frame is not None and line.found else []
        look_px = None
        if frame is not None and line.found and line.look_point is not None:
            lp = self.vision.world_to_frame([line.look_point], w, h)
            look_px = [int(lp[0][0]), int(lp[0][1])] if lp else None
        row = build_log_row(
            seq=getattr(info, 'sequence', -1), frame_mono=getattr(info, 'monotonic', None),
            t_mono=now, ctl=ctl, enabled=enabled, speed_pct=speed_pct, line=line, obst=obst,
            enforce=ctl['obstacle_stop'], d=d, v_t=v_t, w_t=w_t, wheels=wheels, snap=snap,
            link_ok=link_ok, vision_ms=vis.ms, curve_px=curve_px, look_px=look_px,
            closed_wanted=ctl['closed_loop'], fw_ready=self.arduino.closed_loop_ready(),
            req_mono=getattr(info, 'monotonic', None))
        # Stash for the live frame-log writer regardless of recorder activity, so
        # the dashboard Logs tab gets per-tick data even between recordings.
        self._last_row = row
        if not rec.active:
            return
        problem = legacy_problem(row)
        triggers, telem = (), None
        try:
            triggers = self.trig.evaluate(row, now)
            telem = build_telem_row(row)
        except Exception:
            if not self._rec_warned:
                self._rec_warned = True
                log.exception('recording trigger/telemetry failed (continuing)')
        rec.tick(time.time(), frame, row, problem, self.vision.debug_image,
                 t_mono=now, triggers=triggers, telem=telem)

    def _rec_meta(self, frame) -> dict:
        """Extra session metadata (every access guarded; never raises)."""
        try:
            fh, fw = (frame.shape[:2] if frame is not None else (None, None))
        except Exception:
            fh = fw = None
        fw_ver, ready = None, None
        try:
            snap = self.arduino.snapshot()
            fw_ver = snap.firmware_version or None
            for ln in reversed(list(snap.recent_replies or [])):
                if 'READY' in ln:
                    ready = ln
                    break
        except Exception:
            pass
        return {'camera': {'backend': getattr(self.camera, 'backend', None)},
                'image': {'format': 'jpeg', 'quality': config.REC_JPEG_QUALITY,
                          'save_width': config.REC_SAVE_WIDTH,
                          'frame_w': fw, 'frame_h': fh, 'post_flip': True,
                          'green_cast_fix': config.CAMERA_GREEN_CAST_FIX},
                'firmware': {'version': fw_ver, 'ready_line': ready}}

    def _publish(self, now, frame, vis, d, wheels, enabled, link_ok, events,
                 speed_pct, v_t, w_t, mode, enforce=True) -> None:
        if now - self._last_publish < PUBLISH_PERIOD_S and not events:
            return
        self._last_publish = now
        if frame is not None:
            out = frame.copy()
            self._draw_hud(out, vis, d, wheels, enabled, mode)
        else:
            out = np.zeros((720, 1280, 3), dtype=np.uint8)
            cv2.putText(out, 'NO CAMERA FRAME', (400, 360),
                        cv2.FONT_HERSHEY_SIMPLEX, 1.2, (0, 0, 255), 3)
        line, obst = vis.line, vis.obstacle
        snap = self.arduino.snapshot()
        state = TelemetryState(
            running=True,
            camera_connected=self.camera.connected,
            camera_backend=self.camera.backend,
            stm32_connected=link_ok,
            stm32_ack_age_ms=self.arduino.ack_age_ms(),
            stm32_firmware_version=snap.firmware_version or 'unknown',
            camera_direction=d.direction.value,
            camera_command=d.camera_cmd,
            line_centroid_x=-1, line_centroid_y=-1,
            line_area=0, line_error_px=0,
            lidar_status=obst.state,                  # camera obstacle state
            lidar_connected=OBST_ENABLED,
            lidar_error=None if OBST_ENABLED else 'obstacle detection disabled',
            lidar_distance_mm=obst.distance_m * 1000.0,
            # Rotating-LiDAR safety. The snapshot was taken by
            # _lidar_override at 50 Hz this tick; only the zones (which
            # change rarely, from the dashboard) are re-read here.
            # `lidar_connected` above is the camera obstacle detector's
            # enable flag, so the sensor's own health needs its own field —
            # without it the UI reports "connected" for an unplugged LiDAR.
            lidar_sensor_connected=bool(self._lid.ok),
            lidar_min_front_m=(None if self._lid.min_front_m == float('inf')
                               else round(self._lid.min_front_m, 3)),
            lidar_sector_mins=[(None if v == float('inf') else round(v, 3))
                               for v in self._lid.min_per_sector.values()],
            lidar_age_ms=self._lid.age_ms,
            lidar_estop_reason=self._lid.reason,
            lidar_action=self._lid_action,
            lidar_stop_dist_m=self._lid_zones()['lidar_stop_dist_m'],
            lidar_slow_dist_m=self._lid_zones()['lidar_slow_dist_m'],
            lidar_slow_v_mps=self._lid_zones()['lidar_slow_v_mps'],
            lidar_enabled=bool(self.lidar.enabled
                               and self._lid_zones()['lidar_enabled']),
            # Live LiDAR connection details — the dashboard's status bar
            # surfaces these so an operator can confirm where the feed
            # is wired without SSH'ing in.
            lidar_port=self.lidar.port,
            lidar_baud=self.lidar.baud,
            lidar_sector_deg=self.lidar.sector_deg,
            final_command=d.final_cmd,
            decision_reason=d.status_text,
            sensor_probes=self.probes,
            last_error=self.last_error,
            enabled=enabled,
            max_speed_pct=speed_pct,
            mode=mode,
            obstacle_enforced=enforce,
            track_state=d.track,
            steer_boost=d.boost,
            line_offset_m=line.offset_m,
            line_angle_deg=line.angle_deg,
            line_lookahead_m=line.lookahead_m,
            line_curvature=line.curvature,
            line_strips=line.strips_used,
            obstacle_state=obst.state,
            obstacle_distance_m=obst.distance_m,
            speed_target=v_t,
            omega_target=w_t,
            left_pwm=wheels.left,
            right_pwm=wheels.right,
            vision_ms=vis.ms,
            stm32_encoder_left=snap.encoder_left,
            stm32_encoder_right=snap.encoder_right,
            wheel_closed_loop=wheels.closed,
            closed_loop_wanted=self._closed_wanted,
            fw_closed_ready=self.arduino.closed_loop_ready(),
            wheel_tgt_l_mps=wheels.left_mps,
            wheel_tgt_r_mps=wheels.right_mps,
            wheel_meas_l_mps=snap.meas_ticks_left / TICKS_PER_M,
            wheel_meas_r_mps=snap.meas_ticks_right / TICKS_PER_M,
            wheel_applied_l=snap.applied_left,
            wheel_applied_r=snap.applied_right,
            enc_fault_l=bool(snap.fault_bits & 1),
            enc_fault_r=bool(snap.fault_bits & 2),
            wheel_max_mps=WHEEL_MAX_MPS,
            wheel_diameter_m=WHEEL_DIAMETER_M,
            enc_rel_l=snap.encoder_left - snap.enc_zero_left,
            enc_rel_r=snap.encoder_right - snap.enc_zero_right,
            ticks_per_m=TICKS_PER_M,
            **self._rec_telemetry(),
        )
        self.telemetry.publish(state, frame=out, log_message=events or None)

    def _rec_telemetry(self) -> dict:
        s = self.recorder.status()
        return dict(rec_on=s['on'], rec_session=s['session'], rec_frames=s['frames'],
                    rec_problems=s['problems'], rec_marks=s['marks'], rec_dropped=s['dropped'],
                    rec_mb=s['mb'], rec_elapsed_s=s['elapsed_s'], rec_free_gb=s['free_gb'],
                    rec_stop_reason=s['stop_reason'],
                    rec_format=s.get('format', 'v0'), rec_mode=s.get('mode', 'continuous'),
                    rec_shard=str(s.get('shard', '')), rec_ring_s=float(s.get('ring_s', 0.0)))

    # ── HUD rendering ────────────────────────────────────────────────
    def _draw_hud(self, frame, vis: VisionResult, d, wheels, enabled, mode='auto') -> None:
        h, w = frame.shape[:2]
        line = vis.line
        if line.found:
            pts = self.vision.world_to_frame(list(line.curve), w, h)
            if len(pts) > 1:
                cv2.polylines(frame, [np.array(pts, np.int32)], False,
                              (0, 255, 0), 4, cv2.LINE_AA)
            if line.look_point is not None:
                lp = self.vision.world_to_frame([line.look_point], w, h)
                if lp:
                    cv2.circle(frame, lp[0], 14, (0, 0, 255), -1)
                    cv2.circle(frame, lp[0], 16, (255, 255, 255), 2)
        cv2.line(frame, (w // 2, 0), (w // 2, h), (255, 200, 0), 1)

        cv2.rectangle(frame, (0, 0), (w, 60), (0, 0, 0), -1)
        cv2.putText(frame, f'{d.final_cmd} | {d.status_text}', (10, 40),
                    cv2.FONT_HERSHEY_SIMPLEX, 1.0, d.status_color, 2)
        tag, tag_col = (('OFF', (0, 0, 255)) if not enabled
                        else ('MANUAL', (0, 200, 255)) if mode == 'manual'
                        else ('ON', (0, 220, 0)))
        cv2.putText(frame, tag, (w - 40 - 28 * len(tag) - 100, 42), cv2.FONT_HERSHEY_SIMPLEX,
                    1.2, tag_col, 3, cv2.LINE_AA)
        ard_col = (0, 255, 0) if self.arduino.link_alive() else (0, 0, 255)
        cv2.circle(frame, (w - 40, 30), 14, ard_col, -1)

        info = (f'[{d.track} x{d.boost:.1f}]  off {line.offset_m:+.2f}m  ang {line.angle_deg:+.0f}deg  '
                f'look {line.lookahead_m:+.2f}m  curv {line.curvature:+.2f}  '
                f'strips {line.strips_used}   PWM L{wheels.left:+4d} R{wheels.right:+4d}')
        cv2.rectangle(frame, (0, 62), (w, 96), (0, 0, 0), -1)
        cv2.putText(frame, info, (10, 88), cv2.FONT_HERSHEY_SIMPLEX, 0.7,
                    (255, 255, 255), 2, cv2.LINE_AA)

        if vis.bev_debug is not None:
            bev = cv2.resize(vis.bev_debug, None, fx=0.8, fy=0.8)
            bh, bw = bev.shape[:2]
            frame[h - bh - 10:h - 10, w - bw - 10:w - 10] = bev
            cv2.rectangle(frame, (w - bw - 10, h - bh - 10), (w - 10, h - 10),
                          (255, 255, 255), 1)
            obst = vis.obstacle
            if obst.state != 'CLEAR':
                cv2.putText(frame, f'{obst.state} {obst.distance_m:.2f}m',
                            (w - bw - 10, h - bh - 18), cv2.FONT_HERSHEY_SIMPLEX,
                            0.8, (0, 0, 255) if obst.state == 'STOP' else (0, 200, 255),
                            2, cv2.LINE_AA)

    def _snapshot_if_obstacle(self, now, frame, vis) -> None:
        """Save the raw frame + detector views when an obstacle is flagged, so
        false stops can be diagnosed from real camera data (max 12, 3 s apart)."""
        if (frame is None or vis.obstacle.raw_state == 'CLEAR'
                or now - self._snap_t < 3.0 or self._snap_n >= 12):
            return
        self._snap_t, self._snap_n = now, self._snap_n + 1
        folder = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'logs', 'snapshots')
        os.makedirs(folder, exist_ok=True)
        stamp = time.strftime('%H%M%S')
        tag = f'{stamp}_{vis.obstacle.raw_state}_{vis.obstacle.distance_m:.2f}m'
        cv2.imwrite(os.path.join(folder, tag + '_frame.jpg'), frame)
        dbg = self.vision.debug_image()
        if dbg is not None:
            cv2.imwrite(os.path.join(folder, tag + '_views.png'), dbg)
        log.info('[Snapshot] obstacle %s saved to %s', vis.obstacle.raw_state, folder)

    def _manual_target(self, d, ctl, speed_pct, enabled):
        """Dashboard driving: hold-to-drive (deadman). The command goes through
        the same jerk limiter and the max-speed cap as automatic driving.
        Camera obstacle detection is NOT enforced here: the operator is."""
        m = manual_command(ctl, speed_pct)
        v_t, w_t = m.v, m.omega
        d = dataclasses.replace(
            d, camera_cmd=m.cmd, final_cmd=m.cmd, v=v_t, omega=w_t,
            emergency=(m.idle or not m.fresh), status_text=m.text,
            status_color=(0, 200, 255))
        return d, v_t, w_t

    # ── state-transition events for the log ───────────────────────────
    def _stable(self, key: str, value, now: float, dwell: float = 0.6):
        """Return (previous_logged, value) once ``value`` has held for ``dwell``
        seconds and differs from what was last logged; else None. Stops a
        value that hovers at a threshold from flooding the log."""
        cur = self._dw.setdefault(key, {'v': value, 'since': now, 'logged': value})
        if value != cur['v']:
            cur['v'], cur['since'] = value, now
        if cur['v'] != cur['logged'] and now - cur['since'] >= dwell:
            prev, cur['logged'] = cur['logged'], cur['v']
            return prev, cur['v']
        return None

    def _transitions(self, d, vis, enabled, frame_ok, mode='auto') -> list[tuple[str, str]]:
        now = time.monotonic()
        events: list[tuple[str, str]] = []
        link = self.arduino.link_alive()
        for key, val, cat, fmt, dwell in (
            ('enabled', enabled, LOG_CAT_SYSTEM, lambda p, c: 'Motors ENABLED from dashboard' if c else 'Motors DISABLED from dashboard', 0.0),
            ('mode', mode, LOG_CAT_SYSTEM, lambda p, c: f'Mode: {p} -> {c}', 0.0),
            ('cam', frame_ok, LOG_CAT_CAMERA, lambda p, c: 'Camera frames OK' if c else 'Camera frames stale', 0.5),
            ('loop', self.motion.snapshot().closed, LOG_CAT_STM32,
             lambda p, c: 'Wheel control: closed loop (encoder PID)' if c
             else 'Wheel control: open loop (raw PWM)', 0.8),
            ('encfault', self.arduino.snapshot().fault_bits, LOG_CAT_STM32,
             lambda p, c: ('Encoder fault (wheel commanded but not moving): '
                           f'L={bool(c & 1)} R={bool(c & 2)}') if c
             else 'Encoder fault cleared', 0.5),
            ('link', link, LOG_CAT_SYSTEM, lambda p, c: 'Arduino link up' if c else 'Arduino link DOWN', 0.5),
            ('obst', vis.obstacle.state, LOG_CAT_CAMERA, lambda p, c: f'Obstacle {p} -> {c} ({vis.obstacle.distance_m:.2f} m)', 0.5),
            ('dir', d.direction.value, LOG_CAT_CAMERA, lambda p, c: f'Line: {p} -> {c}', 0.8),
            ('cmd', d.final_cmd, LOG_CAT_SYSTEM, lambda p, c: f'Final command: {p} -> {c}', 0.8),
        ):
            ch = self._stable(key, val, now, dwell)
            if ch:
                events.append((cat, fmt(*ch)))
        # Tracking: only report real problems and recovery, not ON_LINE/CORRECTING.
        bad = d.track in ('STUCK', 'DRIFTING', 'OFF_LINE', 'ALIGNING')
        ch = self._stable('trackbad', d.track if bad else 'OK', now, 0.5)
        if ch:
            events.append((LOG_CAT_CAMERA,
                           f'Tracking: {ch[1]} (boost x{d.boost:.1f})' if ch[1] != 'OK'
                           else 'Tracking: back on the line / correcting normally'))
        return events


# ── Entry point ─────────────────────────────────────────────────────────

def main() -> None:
    agv = AGV()

    def _request_stop(signum, _frame) -> None:
        agv.stop_requested = True
        agv.stop_reason = 'stop requested (signal %d)' % signum

    signal.signal(signal.SIGINT, _request_stop)
    signal.signal(signal.SIGTERM, _request_stop)
    try:
        agv.start()
        while not agv.stop_requested:
            try:
                agv.tick()
            except Exception as exc:
                agv.last_error = f'{type(exc).__name__}: {exc}'
                log.error('[MainLoop] Unhandled exception - staying alive\n%s',
                          traceback.format_exc())
                # The motion thread sees stale targets and stops the wheels.
                time.sleep(0.5)
    finally:
        agv.stop()


if __name__ == '__main__':
    main()
