"""Web dashboard for AGV telemetry.

Two surfaces, both reading the same telemetry directory:

  - HTTP / Flask (default port 5000):
      /            →  static dashboard page
      /status      →  JSON snapshot (used for first paint)
      /video_feed  →  MJPEG stream of the latest annotated frame
  - WebSocket (default port 5001):
      /            →  push the live status every ~500 ms
        Clients (a single browser tab) connect once and receive updates
        instead of polling. Lower CPU on the Jetson and smoother UI.

The HTTP layer is also a tiny server for the WebSocket — `flask run`
is not involved.
"""

from __future__ import annotations

import asyncio
import base64
import glob
import hmac
import json
import logging
import os
import re
import secrets
import shutil
import subprocess
import threading
import time
from pathlib import Path

from flask import Flask, Response, jsonify, make_response, render_template, request, send_file

from config import (
    CAM_FACE_DB, CAM_FACE_THUMBS, CAM_PREVIEW_DEV, CAM_PROFILES, CAM_SETTINGS_FILE,
    DASHBOARD_HOST, DASHBOARD_HTTP_PORT, DASHBOARD_WS_PORT,
    TELEMETRY_DIR,
)
from control_state import (
    LIDAR_CLAMPS, LIDAR_LIMITS, LIDAR_SLOW_GAP_M, MANUAL_STALE_S,
    TURN_FLOOR_BASE_MAX, TURN_FLOOR_BASE_MIN, TURN_FLOOR_BASE_DEFAULT,
    LOOKAHEAD_MAX_M, LOOKAHEAD_MIN_M, STEER_CLAMPS,
    ControlReader,
    clamp_speed, read_state, write_state,
)
from config import TURN_FLOOR, TURN_FLOOR_FULL_ERR_M
import recording_io
from config import REC_DEFAULT_RATE_HZ, REC_DIR, SPEED_MAX_PCT, SPEED_MIN_PCT
from config import LIVE_FRAMES_DIR
from cam.analytics import GREETER_KEYS as CAM_GREETER_KEYS, LIMITS as CAM_LIMITS, CamAnalytics
from cam.facefile import FacesFilePublisher
from cam.faces import FaceStore, valid_name as _cam_valid_name
from cam.profiles import ProfileStore, public_view as _cam_profile_view
from cam.publisher import CamPublisher

log = logging.getLogger('agv.web')
# web.py never configures logging — the AGV process does, for its own logger,
# on its own stdout. Without a handler every message logged here is silently
# dropped: sign-ins, motor toggles, mode changes, the generated-password
# warning, and the camera decision below. Attach one, and stop propagation so
# it is emitted exactly once.
if not log.handlers:
    _web_log_handler = logging.StreamHandler()
    _web_log_handler.setFormatter(logging.Formatter(
        '%(asctime)s %(levelname)s [%(name)s] %(message)s'))
    log.addHandler(_web_log_handler)
    log.propagate = False
# Without this the logger inherits WARNING from the unconfigured root and
# every log.info() below is discarded before the handler ever sees it.
log.setLevel(logging.INFO)

STATUS_PATH = Path(TELEMETRY_DIR) / 'status.json'
FRAME_PATH = Path(TELEMETRY_DIR) / 'frame.jpg'
POINTS_PATH = Path(TELEMETRY_DIR) / 'points.json'

# Background capture thread for a non-CSI camera (the C270 USB webcam),
# started by the dashboard on import so /cam/mjpeg is hot whenever Flask is
# up. The publisher survives an unplugged camera and resurfaces the feed the
# moment the device re-enumerates.
def _video_index(dev: str) -> int:
    m = re.search(r'(\d+)$', dev)
    return int(m.group(1)) if m else 0


def _detect_preview_cam() -> str:
    """First non-CSI camera node that actually delivers frames.

    Never returns a tegra-capture-vi node. That sensor belongs to the AGV:
    Argus cannot create a CaptureSession while another process holds the node,
    and the AGV then reports NO_FRAMES with a dead line follower. Device
    numbering moves with whatever is plugged in — the IMX219 claims /dev/video0
    and pushes the USB webcam to /dev/video1 — so resolve by identity, not by
    a hardcoded path.

    Returns '' when nothing suitable is present; CamPublisher then reports the
    preview offline and retries every 5 s rather than stealing the CSI sensor.
    """
    if CAM_PREVIEW_DEV:
        log.info('preview camera pinned to %s', CAM_PREVIEW_DEV)
        return CAM_PREVIEW_DEV
    import cv2
    for dev in sorted(glob.glob('/dev/video*'), key=_video_index):
        try:
            name = Path('/sys/class/video4linux') / Path(dev).name / 'name'
            name = name.read_text().strip()
        except OSError:
            continue
        if any(k in name for k in ('tegra-capture-vi', 'vi-output', 'imx219')):
            continue                       # CSI — the AGV owns it
        cap = cv2.VideoCapture(dev, cv2.CAP_V4L2)
        if not cap.isOpened():
            continue
        # A webcam exposes a capture node plus a metadata node under the same
        # name; only the capture node yields frames, so verify before claiming it.
        ok, deadline = False, time.monotonic() + 1.0
        while time.monotonic() < deadline:
            got, _ = cap.read()
            if got:
                ok = True
                break
        cap.release()
        if ok:
            log.info('preview camera: %s (%s)', dev, name)
            return dev
    log.warning('no non-CSI camera found; dashboard preview stays offline')
    return ''


CAM_DEV = _detect_preview_cam()
CAM_PUBLISHER = CamPublisher(device=CAM_DEV, width=640, height=480, target_fps=12)
CAM_PUBLISHER.start()
# Face/object analytics reads frames from CAM_PUBLISHER (uvcvideo allows one
# opener), loads its models lazily, and idles until the Camera tab is watched.
# The voice process is separate and reads files, so the rover's view of the
# camera is cam_faces.json (written here) and it can wake the camera with
# cam_watch.json (written there). Neither side imports the other.
CAM_FACES_PATH = Path(TELEMETRY_DIR) / 'cam_faces.json'
CAM_WATCH_PATH = Path(TELEMETRY_DIR) / 'cam_watch.json'
CAM_PROFILES_STORE = ProfileStore(CAM_PROFILES)
CAM_ANALYTICS = CamAnalytics(
    CAM_PUBLISHER,
    store=FaceStore(CAM_FACE_DB, CAM_FACE_THUMBS),
    settings_path=CAM_SETTINGS_FILE,
    profiles=CAM_PROFILES_STORE,
    watch_path=str(CAM_WATCH_PATH),
).start()
CAM_FACES_FILE = FacesFilePublisher(CAM_ANALYTICS, CAM_PUBLISHER, str(CAM_FACES_PATH)).start()

# ─────────────────────────────────────────────────────────────────
# HTTP layer (Flask)
# ─────────────────────────────────────────────────────────────────

# `Flask('agv-dashboard')` has no module to locate: it falls back to the working
# directory and every template 500s unless the process happens to be started from
# the repo root. Pin the root to this file, like PASSWORD_FILE below.
flask_app = Flask('agv-dashboard', root_path=str(Path(__file__).resolve().parent))


@flask_app.context_processor
def inject_project_config():
    """Make the project's `config.py` available to every template as
    `project_config`. Without this, `{{ config.X }}` in the template would
    resolve to Flask's own config object, not the AGV config module.
    """
    import config as _project_config
    return {'project_config': _project_config}


def _read_status() -> dict:
    fallback = {
        'running': False,
        'camera_connected': None,
        'camera_backend': 'argus',
        'stm32_connected': None,
        'stm32_ack_age_ms': 0.0,
        'stm32_encoder_left': 0,
        'stm32_encoder_right': 0,
        'stm32_encoder_delta_left': 0,
        'stm32_encoder_delta_right': 0,
        'stm32_firmware_version': '',
        'camera_direction': 'UNAVAILABLE',
        'camera_command': 'S',
        'line_centroid_x': -1,
        'line_centroid_y': -1,
        'line_area': 0,
        'line_error_px': 0,
        'lidar_status': 'UNKNOWN',
        'lidar_connected': None,
        'lidar_error': None,
        'lidar_distance_mm': 0,
        'lidar_min_front_m': None,
        'lidar_sector_mins': None,
        'lidar_age_ms': None,
        'lidar_estop_reason': '',
        'lidar_action': 'OFF',
        'lidar_stop_dist_m': 0.0,
        'lidar_slow_dist_m': 0.0,
        'lidar_slow_v_mps': 0.0,
        'lidar_enabled': False,
        'lidar_port': '',
        'lidar_baud': 0,
        'lidar_sector_deg': 0,
        'final_command': 'S',
        'decision_reason': '',
        'updated_at': None,
        'logs': [],
        'sensor_probes': [],
        'last_error': None,
        'enabled': False,
        'max_speed_pct': 0,
        'turn_floor_base': TURN_FLOOR_BASE_DEFAULT,
        'mode': 'auto',
        'obstacle_enforced': True,
        'track_state': 'NO_LINE',
        'steer_boost': 1.0,
        'line_offset_m': 0.0,
        'line_angle_deg': 0.0,
        'line_lookahead_m': 0.0,
        'line_curvature': 0.0,
        'line_strips': 0,
        'obstacle_state': 'CLEAR',
        'obstacle_distance_m': 0.0,
        'speed_target': 0.0,
        'omega_target': 0.0,
        'left_pwm': 0,
        'right_pwm': 0,
        'vision_ms': 0.0,
    }
    try:
        with STATUS_PATH.open('r', encoding='utf-8') as fh:
            payload = json.load(fh)
        return _fill_control_defaults(payload, fallback)
    except (OSError, ValueError):
        return _fill_control_defaults(dict(fallback), fallback)


def _fill_control_defaults(payload: dict, fallback: dict) -> dict:
    """Fill in the fields only the AGV publishes, from the control file.

    ``closed_loop_wanted`` is the wheel-control tick box. While the AGV runs
    it reports the choice itself; with the dashboard alone there is nothing to
    report it, and the box would fall back to "true" and re-tick itself — so
    read the saved choice out of control.json instead.

    ``record_auto`` is the same case for a different reason: the AGV has no
    opinion about it (nothing on its side changes), so it is never in
    status.json and must come from control.json or the switch would untick
    itself the moment the AGV next published.
    """
    for key, default in fallback.items():
        payload.setdefault(key, default)
    ctl = None
    if payload.get('closed_loop_wanted') is None or payload.get('record_auto') is None:
        ctl = read_state()
    if payload.get('closed_loop_wanted') is None:
        payload['closed_loop_wanted'] = bool(ctl['closed_loop'])
    if payload.get('record_auto') is None:
        payload['record_auto'] = bool(ctl['record_auto'])
    return payload


@flask_app.route('/points.json', methods=['GET'])
def points_json():
    """Raw 2 s rolling window of LiDAR points for the dashboard's
    polar overlay. Same atomic-write convention as status.json: the
    writer publishes ``points.json.tmp`` and then ``os.replace()``s it,
    so readers never see a partial file."""
    try:
        with POINTS_PATH.open('r', encoding='utf-8') as fh:
            payload = json.load(fh)
    except (OSError, ValueError):
        payload = {'updated_at': 0.0, 'window_s': 2.0, 'n': 0, 'points': []}
    body = json.dumps(payload).encode('utf-8')
    resp = make_response(body)
    resp.headers['Content-Type'] = 'application/json'
    resp.headers['Cache-Control'] = 'no-store'
    return resp


# ── Voice tab ───────────────────────────────────────────────────────
# The voice stack is a separate process (python -m voice.run_phase0): it owns
# the C270's ALSA device, which is exclusive, so web.py never imports ``voice``.
# It publishes voice_status.json; we re-serve it. The only thing that goes the
# other way is voice_command.json, a two-verb diagnostic inbox.
VOICE_STATUS_PATH = Path(TELEMETRY_DIR) / 'voice_status.json'
VOICE_COMMAND_PATH = Path(TELEMETRY_DIR) / 'voice_command.json'
VOICE_OUTPUT_PATH = Path(TELEMETRY_DIR) / 'voice_output.json'
VOICE_VERBS = ('listen', 'speak_test', 'announce_who', 'call_person', 'ask', 'report')
VOICE_NOTES_DIR = Path(TELEMETRY_DIR) / 'voice_notes'
# Ids are minted by the voice process: epoch-ms + 4 hex. Anything else is a
# path-traversal attempt or a typo, never a note.
VOICE_NOTE_ID_RE = re.compile(r'^[0-9]{10,14}-[0-9a-f]{4}$')
VOICE_STALE_DEFAULT_S = 10.0


def _voice_payload():
    """(payload, age_s) from voice_status.json, or (None, None)."""
    try:
        with VOICE_STATUS_PATH.open('r', encoding='utf-8') as fh:
            payload = json.load(fh)
    except (OSError, ValueError):
        return None, None
    if not isinstance(payload, dict):
        return None, None
    try:
        age = max(0.0, time.time() - float(payload.get('updated_at', 0)))
    except (TypeError, ValueError):
        return None, None
    return payload, age


def _voice_running(payload, age) -> bool:
    if payload is None:
        return False
    try:
        limit = float(payload.get('stale_after_s', VOICE_STALE_DEFAULT_S))
    except (TypeError, ValueError):
        limit = VOICE_STALE_DEFAULT_S
    return age is not None and age <= limit


def _voice_reader_signed_in(req) -> bool:
    """Side-effect-free sign-in check (never feeds the wrong-password
    throttle, so a polling tab cannot lock anyone out)."""
    auth = req.headers.get('Authorization', '')
    if auth.startswith('Bearer '):
        _payload, reason = _check_token(auth[7:].strip(), 'a')
        return not reason
    given = req.headers.get('X-AGV-Password', '')
    return bool(given) and hmac.compare_digest(given.encode(), _PASSWORD.encode())


@flask_app.route('/api/voice/status', methods=['GET'])
def api_voice_status():
    """Voice pipeline health + recent activity. Read-only, never 500s.

    Lane health is open (like points.json). Transcripts, narration text and
    parse rows are operator speech, so they are only included for a signed-in
    session. A stale file is reported as ``running: false`` rather than shown
    as if it were current.
    """
    payload, age = _voice_payload()
    running = _voice_running(payload, age)
    if payload is None or not running:
        body = {'running': False, 'stale': True,
                'age_s': None if age is None else round(age, 1),
                'hint': 'python -m voice.run_phase0'}
    else:
        body = dict(payload)
        body['running'] = True
        body['stale'] = False
        body['age_s'] = round(age, 1)
        if _voice_reader_signed_in(request):
            body['redacted'] = False
        else:
            body['redacted'] = True
            for key in ('transcripts', 'narration', 'parses', 'notes'):
                body[key + '_n'] = len(body.pop(key, None) or [])
            cmd = dict(body.get('command') or {})
            cmd['detail'] = ''
            body['command'] = cmd
    resp = make_response(json.dumps(body))
    resp.headers['Content-Type'] = 'application/json'
    resp.headers['Cache-Control'] = 'no-store'
    return resp


@flask_app.route('/api/voice/notes/<note_id>.wav', methods=['GET'])
def api_voice_note(note_id):
    """Play back a recent microphone capture (a "voice note").

    Room audio, so it is signed-in only — and the browser's <audio> tag
    cannot send an Authorization header, so the page fetches this as a blob.
    """
    if not _voice_reader_signed_in(request):
        return _rec_auth_fail()
    if not VOICE_NOTE_ID_RE.match(note_id):
        return jsonify({'error': 'not found'}), 404
    path = VOICE_NOTES_DIR / (note_id + '.wav')
    if not path.is_file():
        return jsonify({'error': 'not found'}), 404
    resp = send_file(path, mimetype='audio/wav')
    resp.headers['Cache-Control'] = 'no-store'
    return resp


@flask_app.route('/api/voice/command', methods=['POST'])
def api_voice_command():
    """Ask the voice process to run a whitelisted verb.

    ``listen`` / ``speak_test`` are diagnostics; ``announce_who`` / ``call_person``
    make the rover say who the camera sees (fixed templates, enrolled names only).

    Password-gated (it opens a microphone), whitelisted to four verbs, and it
    only drops a file the voice process polls. It cannot reach the motors: a
    diagnostics tab must not become a third path to them.
    """
    ok, status = _password_ok(request)
    if not ok:
        return _auth_fail(status)
    body = request.get_json(silent=True) or {}
    verb = body.get('verb')
    if verb not in VOICE_VERBS:
        return jsonify({'error': 'verb must be one of: ' + ', '.join(VOICE_VERBS)}), 400
    payload, age = _voice_payload()
    if not _voice_running(payload, age):
        return jsonify({'error': 'voice process is not running '
                                 '(start: python -m voice.run_phase0)'}), 503
    cmd = {'id': secrets.token_hex(6), 'verb': verb, 'ts': time.time()}
    if verb == 'call_person':
        # The only text from the browser that gets spoken is a name, and only an
        # enrolled one: nothing else can ride in on this verb.
        name = body.get('name')
        if not isinstance(name, str) or not _cam_valid_name(name) \
                or name not in [p['name'] for p in CAM_ANALYTICS.store.people()]:
            return jsonify({'error': 'name must be an enrolled person'}), 400
        cmd['name'] = name
    if verb in ('listen', 'ask'):
        try:
            seconds = float(body.get('seconds', 3))
        except (TypeError, ValueError):
            return jsonify({'error': 'seconds must be a number'}), 400
        if seconds != seconds or seconds in (float('inf'), float('-inf')):
            return jsonify({'error': 'seconds must be a finite number'}), 400
        cmd['seconds'] = max(1.0, min(10.0, seconds))
    tmp = VOICE_COMMAND_PATH.with_suffix('.json.tmp')
    try:
        tmp.write_text(json.dumps(cmd), encoding='utf-8')
        os.replace(tmp, VOICE_COMMAND_PATH)
    except OSError as exc:
        log.warning('[API] voice command not written: %s', exc)
        return jsonify({'error': 'could not queue command'}), 500
    log.info('[API] voice %s queued from %s', verb, request.remote_addr)
    return jsonify({'id': cmd['id'], 'verb': verb}), 202


# Microphone check. Unlike the rest of the Voice tab this does not need the
# voice process: connection is read from sysfs (no device opened), and the
# working-test is one short capture. ``voice.mic`` is imported lazily and only
# for these two functions, so a broken voice package cannot take the dashboard
# down. If the voice process happens to be mid-listen the capture reports
# "busy" instead of fighting it for the device.
_MIC_LAST: dict = {}
_MIC_LOCK = threading.Lock()
# Separate lock: a speaker test and a mic test are different devices and must
# not block or cancel each other, but two speaker tests at once would overlap
# two clips on one sink and make both verdicts meaningless.
_OUTPUT_LOCK = threading.Lock()


@flask_app.route('/api/voice/mic', methods=['GET'])
def api_voice_mic():
    """Which microphone is connected, plus the result of the last test."""
    try:
        from voice.mic import mic_inventory
        inv = mic_inventory()
    except Exception as exc:                       # never break the dashboard
        log.warning('[API] mic inventory failed: %s', exc)
        inv = {'connected': None, 'error': 'could not inspect audio devices',
               'cards': [], 'device': None}
    inv['last_test'] = dict(_MIC_LAST) or None
    resp = make_response(json.dumps(inv))
    resp.headers['Content-Type'] = 'application/json'
    resp.headers['Cache-Control'] = 'no-store'
    return resp


@flask_app.route('/api/voice/mic/test', methods=['POST'])
def api_voice_mic_test():
    """Capture ~1.5 s and report whether audio is actually arriving.

    Password-gated: it opens the microphone. One test at a time.
    """
    ok, status = _password_ok(request)
    if not ok:
        return _auth_fail(status)
    if not _MIC_LOCK.acquire(blocking=False):
        return jsonify({'error': 'a microphone test is already running'}), 409
    try:
        from voice.mic import probe_microphone
        result = probe_microphone(1.5)
    except Exception as exc:
        log.warning('[API] mic test failed: %s', exc)
        result = {'ok': False, 'state': 'error', 'message': 'test could not run',
                  'peak_rms': 0.0, 't': time.time()}
    finally:
        _MIC_LOCK.release()
    _MIC_LAST.clear()
    _MIC_LAST.update(result)
    log.info('[API] mic test from %s: %s', request.remote_addr, result.get('state'))
    return jsonify(result)


@flask_app.route('/api/voice/output', methods=['GET'])
def api_voice_output():
    """Every output device, and which one Tarka is pointed at.

    Read-only and open to any signed-in reader: it enumerates sinks with
    ``pactl``/``aplay`` and opens no device. The list comes from the voice
    process's own published payload when it is running, because that process
    is the only one that should know about audio; this endpoint falls back to
    enumerating directly when the voice process is down.
    """
    payload, age = _voice_payload()
    block = payload.get('output') if isinstance(payload, dict) else None
    if block and block.get('outputs'):
        return jsonify(block)
    try:
        from voice.output import OutputSelector, list_outputs
        block = OutputSelector(str(TELEMETRY_DIR)).describe(list_outputs())
    except Exception as exc:
        log.warning('[API] output enumeration failed: %s', exc)
        return jsonify({'error': 'could not list audio outputs', 'outputs': []}), 503
    return jsonify(block)


@flask_app.route('/api/voice/output', methods=['POST'])
def api_voice_output_set():
    """Point Tarka's speech at a specific output device.

    Password-gated, like the other voice routes, because it changes what the
    rover does unattended. It writes a file the voice process polls — it never
    touches the mixer itself, and it cannot reach the motors.

    The device name is validated against the sinks that exist right now before
    it is written, so a hand-edited file cannot push an arbitrary string into
    a subprocess environment.
    """
    ok, status = _password_ok(request)
    if not ok:
        return _auth_fail(status)
    body = request.get_json(silent=True) or {}
    device = body.get('device', 'auto')
    try:
        from voice.output import OutputSelector, list_outputs
        listing = list_outputs()
        selector = OutputSelector(str(TELEMETRY_DIR))
        written, message = selector.write(
            device, allowed=[o['id'] for o in listing['outputs']])
    except Exception as exc:
        log.warning('[API] output change failed: %s', exc)
        return jsonify({'error': 'could not change the audio output'}), 503
    if not written:
        return jsonify({'error': message}), 400
    log.info('[API] voice output set to %s from %s', message, request.remote_addr)
    return jsonify(selector.describe(listing))


@flask_app.route('/api/voice/output/test', methods=['POST'])
def api_voice_output_test():
    """Play a real cached phrase on one output and report whether it was heard.

    Separate from ``/api/voice/output`` because the question it answers is
    different: not "which device is selected" but "is audio actually coming
    out of it". ``aplay`` exiting 0 is not that answer — a paired Bluetooth
    speaker that is powered off accepts the stream and produces silence, so
    the verdict comes from watching whether the sink ever left ``SUSPENDED``.

    Password-gated (it makes noise) and one at a time. It cannot reach the
    motors; it only starts a player for at most a few seconds.
    """
    ok, status = _password_ok(request)
    if not ok:
        return _auth_fail(status)
    if not _OUTPUT_LOCK.acquire(blocking=False):
        return jsonify({'error': 'a speaker test is already running'}), 409
    try:
        body = request.get_json(silent=True) or {}
        device = body.get('device') or ''
        try:
            from voice.output import list_outputs, probe_playback
            # Validate against the live list, same as the write path: a name
            # that is not a real sink never reaches a subprocess environment.
            allowed = {o['id'] for o in list_outputs()['outputs']}
            if device and device != 'auto' and device not in allowed:
                return jsonify({'error': 'that is not one of the current outputs',
                                'audible': False}), 400
            result = probe_playback(device or 'auto')
        except Exception as exc:
            log.warning('[API] speaker test failed: %s', exc)
            result = {'audible': False, 'played': False, 'reason': 'test could not run'}
    finally:
        _OUTPUT_LOCK.release()
    log.info('[API] speaker test on %s: %s (%s)', result.get('device'),
             result.get('audible'), result.get('reason'))
    return jsonify(result)


@flask_app.route('/', methods=['GET'])
def index():
    return render_template('dashboard.html')


_CONTROL = ControlReader()
PASSWORD_FILE = Path(__file__).resolve().parent / '.dashboard_password'


def _load_password() -> str:
    """Start password: env var, else .dashboard_password, else generate one."""
    env = os.environ.get('AGV_DASHBOARD_PASSWORD') or os.environ.get('AGV_DASHBOARD_TOKEN')
    if env:
        return env
    try:
        text = PASSWORD_FILE.read_text(encoding='utf-8').strip()
        if text:
            return text
    except OSError:
        pass
    generated = secrets.token_urlsafe(6)
    fd = os.open(PASSWORD_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as fh:
        fh.write(generated + '\n')
    log.warning('[API] generated a dashboard start password in %s', PASSWORD_FILE)
    return generated


_PASSWORD = _load_password()
_fail_times: list = []          # recent wrong-password timestamps

# ── Sessions: short-lived access token + longer refresh token ───────
# Both are HMAC-signed, self-contained tokens, so they survive page reloads
# (the browser keeps them) and dashboard restarts. The signing key is derived
# from a random secret AND the password: changing the password signs everybody
# out. The refresh token has a fixed absolute lifetime (it is not extended by
# using it), so a session can never outlive REFRESH_TTL_S.
ACCESS_TTL_S = int(os.environ.get('AGV_ACCESS_TTL_S', '900'))        # 15 min
REFRESH_TTL_S = int(os.environ.get('AGV_REFRESH_TTL_S', '28800'))    # 8 h
SECRET_FILE = Path(__file__).resolve().parent / '.dashboard_secret'
REVOKED_FILE = Path(TELEMETRY_DIR) / 'revoked_sessions.json'


def _load_secret() -> bytes:
    try:
        text = SECRET_FILE.read_text(encoding='utf-8').strip()
        if len(text) >= 32:
            return text.encode()
    except OSError:
        pass
    value = secrets.token_hex(32)
    fd = os.open(SECRET_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as fh:
        fh.write(value + '\n')
    return value.encode()


_KEY = hmac.new(_load_secret(), _PASSWORD.encode(), 'sha256').digest()
_revoked: dict = {}             # sid -> expiry (epoch); pruned when expired
_revoked_lock = threading.Lock()


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b'=').decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + '=' * (-len(text) % 4))


def _make_token(kind: str, sid: str, iat: float, exp: float, extra: dict = None) -> str:
    payload = {'t': kind, 'sid': sid, 'iat': int(iat), 'exp': int(exp)}
    if extra:
        payload.update({k: v for k, v in extra.items() if k not in payload})
    body = _b64(json.dumps(payload, separators=(',', ':')).encode())
    sig = _b64(hmac.new(_KEY, body.encode(), 'sha256').digest())
    return f'{body}.{sig}'


def _check_token(token: str, kind: str):
    """(payload, reason): reason is '' when valid, else 'expired'|'revoked'|'invalid'."""
    try:
        body, sig = token.split('.', 1)
        good = _b64(hmac.new(_KEY, body.encode(), 'sha256').digest())
        if not hmac.compare_digest(sig.encode(), good.encode()):
            return None, 'invalid'
        payload = json.loads(_unb64(body))
        if payload.get('t') != kind:
            return None, 'invalid'
    except (ValueError, TypeError, KeyError, UnicodeError):
        return None, 'invalid'
    if payload['exp'] <= time.time():
        return payload, 'expired'
    with _revoked_lock:
        if payload['sid'] in _revoked:
            return payload, 'revoked'
    return payload, ''


def _load_revoked() -> None:
    try:
        data = json.loads(REVOKED_FILE.read_text(encoding='utf-8'))
        now = time.time()
        with _revoked_lock:
            _revoked.update({k: v for k, v in data.items() if v > now})
    except (OSError, ValueError, AttributeError):
        pass


def _save_revoked() -> None:
    try:
        now = time.time()
        with _revoked_lock:
            for k in [k for k, v in _revoked.items() if v <= now]:
                del _revoked[k]
            data = dict(_revoked)
        REVOKED_FILE.parent.mkdir(parents=True, exist_ok=True)
        REVOKED_FILE.write_text(json.dumps(data), encoding='utf-8')
    except OSError:
        pass


_load_revoked()


def _throttled() -> bool:
    now = time.monotonic()
    recent = [t for t in _fail_times if now - t < 60.0]
    _fail_times[:] = recent
    return len(recent) >= 5 and now - recent[-1] < 30.0


def _password_ok(req) -> tuple:
    """(ok, http_status).

    Accepts ``Authorization: Bearer <access token>`` (401 when it is expired /
    invalid, so the page knows to refresh it) or, for scripts, the legacy
    ``X-AGV-Password`` header. Wrong passwords are throttled: 5 in 60 s ->
    30 s lockout.
    """
    auth = req.headers.get('Authorization', '')
    if auth.startswith('Bearer '):
        _payload, reason = _check_token(auth[7:].strip(), 'a')
        return (True, 200) if not reason else (False, 401)
    if _throttled():
        return False, 429
    given = req.headers.get('X-AGV-Password', '')
    if hmac.compare_digest(given.encode(), _PASSWORD.encode()):
        return True, 200
    _fail_times.append(time.monotonic())
    return False, 403


def _auth_fail(status: int):
    msg = {401: 'session expired - sign in again', 429: 'too many wrong passwords - wait 30 s'}
    return jsonify({'error': msg.get(status, 'wrong password')}), status


def _rec_auth_fail():
    return jsonify({'error': 'sign in required'}), 401


def _rec_read_ok(req, name: str = None, force: bool = False) -> tuple:
    """(ok, status) for READING recordings. Side-effect free: never touches
    _fail_times and never returns 429, so unauthenticated image loads cannot
    lock out the START button. Not enforced unless config.REC_REQUIRE_AUTH."""
    import config as _cfg
    if not (_cfg.REC_REQUIRE_AUTH or force):
        return True, 200
    ft = req.args.get('ft', '')
    if ft:
        payload, reason = _check_token(ft, 'f')
        if not reason and (name is None or payload.get('n') == name):
            return True, 200
    auth = req.headers.get('Authorization', '')
    if auth.startswith('Bearer '):
        _payload, reason = _check_token(auth[7:].strip(), 'a')
        if not reason:
            return True, 200
    given = req.headers.get('X-AGV-Password', '')
    if given and hmac.compare_digest(given.encode(), _PASSWORD.encode()):
        return True, 200
    return False, 401


def _session_json(sid: str, iat: float, exp: float, refresh: bool = True) -> dict:
    now = time.time()
    out = {'access_token': _make_token('a', sid, now, min(now + ACCESS_TTL_S, exp)),
           'access_expires_in': int(min(ACCESS_TTL_S, exp - now)),
           'refresh_expires_in': int(exp - now), 'server_time': now}
    if refresh:
        out['refresh_token'] = _make_token('r', sid, iat, exp)
    return out


@flask_app.route('/api/login', methods=['POST'])
def api_login():
    """{"password": ...} -> access + refresh tokens (also stored by the page)."""
    if _throttled():
        return _auth_fail(429)
    given = str((request.get_json(silent=True) or {}).get('password', ''))
    if not hmac.compare_digest(given.encode(), _PASSWORD.encode()):
        _fail_times.append(time.monotonic())
        return _auth_fail(403)
    now = time.time()
    log.info('[API] sign-in from %s', request.remote_addr)
    return jsonify(_session_json(secrets.token_hex(8), now, now + REFRESH_TTL_S))


@flask_app.route('/api/refresh', methods=['POST'])
def api_refresh():
    """{"refresh_token": ...} -> a fresh access token. The refresh token keeps
    its original expiry: it is not extended by use."""
    token = str((request.get_json(silent=True) or {}).get('refresh_token', ''))
    payload, reason = _check_token(token, 'r')
    if reason:
        return jsonify({'error': f'refresh token {reason} - sign in again',
                        'reason': reason}), 401
    return jsonify(_session_json(payload['sid'], payload['iat'], payload['exp'],
                                 refresh=False))


@flask_app.route('/api/auth', methods=['GET'])
def api_auth():
    """Is this access token still good, and for how long?"""
    auth = request.headers.get('Authorization', '')
    payload, reason = _check_token(auth[7:].strip(), 'a') if auth.startswith('Bearer ') else (None, 'invalid')
    if reason:
        return jsonify({'authenticated': False, 'reason': reason}), 401
    return jsonify({'authenticated': True, 'access_expires_in': int(payload['exp'] - time.time()),
                    'server_time': time.time()})


@flask_app.route('/api/logout', methods=['POST'])
def api_logout():
    """Revoke the session of the given refresh token (or of the bearer token)."""
    body = request.get_json(silent=True) or {}
    payload, _ = _check_token(str(body.get('refresh_token', '')), 'r')
    if payload is None:
        auth = request.headers.get('Authorization', '')
        if auth.startswith('Bearer '):
            payload, _ = _check_token(auth[7:].strip(), 'a')
    if payload is not None:
        with _revoked_lock:
            _revoked[payload['sid']] = payload['exp']
        _save_revoked()
    return jsonify({'ok': True})


def _control_json():
    state = read_state()
    state.pop('ts', None)
    state.pop('manual_ts', None)
    state.pop('manual_l', None)
    state.pop('manual_r', None)
    state.pop('manual_raw', None)
    state.update(speed_min_pct=SPEED_MIN_PCT, speed_max_pct=SPEED_MAX_PCT,
                 password_required_to_start=True,
                 manual_stale_s=MANUAL_STALE_S)
    return jsonify(state)


@flask_app.route('/api/enable', methods=['GET', 'POST'])
def api_enable():
    """Motor switch. GET reads it; POST {"enabled": bool} sets it.

    START needs the dashboard password (``X-AGV-Password`` header). STOP never
    does, so the rover can always be switched off.
    """
    if request.method == 'POST':
        body = request.get_json(silent=True) or {}
        want = bool(body.get('enabled', False))
        if want:
            ok, status = _password_ok(request)
            if not ok:
                return _auth_fail(status)
        write_state(enabled=want)          # STOP also zeroes any manual command
        log.info('[API] motors %s from %s', 'ENABLED' if want else 'DISABLED',
                 request.remote_addr)
    return _control_json()


@flask_app.route('/api/mode', methods=['GET', 'POST'])
def api_mode():
    """Switch between automatic line following and manual driving.

    Going to MANUAL needs the dashboard password (it lets you drive the rover
    yourself). Going back to AUTO never does, and it stops the motors.
    """
    if request.method == 'POST':
        body = request.get_json(silent=True) or {}
        want = 'manual' if body.get('mode') == 'manual' else 'auto'
        if want == 'manual':
            ok, status = _password_ok(request)
            if not ok:
                return _auth_fail(status)
        # Leaving manual also stops the motors: otherwise the rover would start
        # line-following the instant AUTO is chosen. START (password) runs auto.
        if want == 'auto':
            write_state(mode='auto', enabled=False)
        else:
            write_state(mode='manual')
        log.info('[API] mode %s from %s', want, request.remote_addr)
    return _control_json()


@flask_app.route('/api/manual', methods=['POST'])
def api_manual():
    """Manual drive command. Two payload shapes, not one:

    ``{"v": -1..1, "w": -1..1}`` — forward / turn right. The d-pad and the
    W A S D / arrow keys. This is the whole-vehicle vector, mixed by the
    motion layer.

    ``{"left": -1..1, "right": -1..1}`` — per-wheel, as a fraction of the
    speed cap. The Bench tab's chassis diagram and its per-side sliders, for
    testing one driver against the other. ``raw: true`` additionally bypasses
    the encoder PID, so the wheels take raw PWM for that one command.

    When ``left``/``right`` are present they win and ``v``/``w`` are ignored,
    so a payload carrying both is not an error. The two shapes are not
    interchangeable: they clear different state in ``control.json``, and the
    dashboard has a call site for each.

    Hold-to-drive either way: the browser repeats this ~10 times a second; the
    AGV treats a command older than MANUAL_STALE_S as released and stops. Needs
    the password and MANUAL mode with the motors ON.
    """
    ok, status = _password_ok(request)
    if not ok:
        return _auth_fail(status)
    body = request.get_json(silent=True) or {}
    try:
        v, w = float(body.get('v', 0.0)), float(body.get('w', 0.0))
    except (TypeError, ValueError):
        return jsonify({'error': 'v and w must be numbers'}), 400
    state = read_state()
    if state['mode'] != 'manual' or not state['enabled']:
        return jsonify({'error': 'switch to MANUAL mode and press START first'}), 409
    if body.get('left') is not None or body.get('right') is not None:
        # Per-wheel test (chassis diagram): left / right as -1..1 of the cap.
        try:
            lft, rgt = float(body.get('left', 0.0)), float(body.get('right', 0.0))
        except (TypeError, ValueError):
            return jsonify({'error': 'left and right must be numbers'}), 400
        write_state(manual_lr=(lft, rgt), raw=bool(body.get('raw', False)))
    else:
        write_state(manual=(v, w))
    return jsonify({'ok': True})


@flask_app.route('/api/encoders/zero', methods=['POST'])
def api_encoders_zero():
    """Zero the dashboard's "ticks since zero" counters (needs sign-in)."""
    ok, status = _password_ok(request)
    if not ok:
        return _auth_fail(status)
    write_state(enc_zero=True)
    return jsonify({'ok': True})


# ── Test recordings (see recorder.py) ───────────────────────────────
_SESSION_RE = re.compile(r'^\d{8}_\d{6}$')
_REC_FILES = re.compile(r'^(frames/\d{6}\.jpg|views/\d{6}\.jpg|log\.jsonl|events\.jsonl|meta\.json|summary\.json'
                        r'|manifest\.json|shards/s\d{4}/(frames/\d{6}\.jpg|log\.jsonl|events\.jsonl|telemetry\.jsonl|manifest\.json))$')


def _session_path(name: str):
    if not _SESSION_RE.match(name or ''):
        return None
    path = Path(REC_DIR) / name
    return path if path.is_dir() else None


def _rec_status() -> dict:
    st = _read_status()
    return {k: st.get(k) for k in ('rec_on', 'rec_session', 'rec_frames', 'rec_problems', 'rec_marks',
                                    'rec_dropped', 'rec_mb', 'rec_elapsed_s', 'rec_free_gb',
                                    'rec_stop_reason', 'rec_format', 'rec_mode', 'rec_shard', 'rec_ring_s')}


@flask_app.route('/api/record', methods=['GET', 'POST'])
def api_record():
    """Recording control.

    POST {"action": "start"|"stop"|"mark"|"auto", "rate": fps, "views": bool,
          "mode": "continuous"|"triggered", "note": text, "enabled": bool}

    "auto" is the one that is not about the current session: it sets the
    standing preference that makes the *dashboard* start a recording when the
    rover begins driving in AUTO and stop it when the drive ends. The trigger
    and the save-or-delete prompt are front-end, so this route only has to
    carry the flag.

    No sign-in needed for any of it: it never moves the rover and is capped by
    size, time and free disk (see recorder.py).
    """
    if request.method == 'POST':
        body = request.get_json(silent=True) or {}
        action = str(body.get('action', ''))
        if action == 'start':
            try:
                rate = float(body.get('rate', REC_DEFAULT_RATE_HZ))
            except (TypeError, ValueError):
                return jsonify({'error': 'rate must be a number'}), 400
            # 'mode' optional: absent keeps the stored mode; invalid coerces to the default
            write_state(record=True, record_rate=rate, record_views=bool(body.get('views', False)),
                        record_mode=body.get('mode'))
        elif action == 'stop':
            write_state(record=False)
        elif action == 'mark':
            write_state(record_mark=str(body.get('note', '')) or 'marked')
        elif action == 'auto':
            # The standing "record every AUTO drive" preference. No sign-in:
            # like the rest of this route it cannot move the rover, and all it
            # does is make the dashboard start writing frames to a directory
            # that already exists. Turning it off stops future recordings; it
            # never touches one already running (the caller stops that first).
            write_state(record_auto=bool(body.get('enabled', False)))
        else:
            return jsonify({'error': 'action must be start, stop, mark or auto'}), 400
        log.info('[API] record %s from %s', action, request.remote_addr)
    state = read_state()
    return jsonify({'record': state['record'], 'rate': state['record_rate'],
                    'views': state['record_views'], 'mode': state['record_mode'],
                    'auto': state['record_auto'], **_rec_status()})


_MODE_CACHE: dict = {}


def _session_mode(path: Path, schema: int) -> str:
    """Recording mode from meta.json (v1); v0 is always continuous. Cached by meta mtime."""
    if schema == 0:
        return 'continuous'
    meta = path / 'meta.json'
    try:
        mt = meta.stat().st_mtime_ns
    except OSError:
        return 'continuous'
    hit = _MODE_CACHE.get(str(meta))
    if hit and hit[0] == mt:
        return hit[1]
    mode = recording_io.load_meta(path).get('mode')
    mode = mode if isinstance(mode, str) and mode else 'continuous'
    if len(_MODE_CACHE) > 500:
        _MODE_CACHE.clear()
    _MODE_CACHE[str(meta)] = (mt, mode)
    return mode


def _dir_stats(path: Path) -> dict:
    info = recording_io.session_stats(path)
    schema = recording_io.detect_schema(path)
    info['schema'] = 1 if schema >= 1 else 0
    info['mode'] = _session_mode(path, info['schema'])
    return info


@flask_app.route('/api/recordings', methods=['GET'])
def api_recordings():
    import config as _cfg
    if _cfg.REC_LIST_REQUIRE_AUTH:
        ok, _st = _rec_read_ok(request, force=True)
        if not ok:
            return _rec_auth_fail()
    root = Path(REC_DIR)
    out = []
    if root.is_dir():
        st = _rec_status()
        rec_on, rec_session = st.get('rec_on'), st.get('rec_session')
        for entry in sorted(root.iterdir(), reverse=True):
            if entry.is_dir() and _SESSION_RE.match(entry.name):
                info = _dir_stats(entry)
                info['name'] = entry.name
                info['active'] = bool(rec_on and rec_session == entry.name)
                out.append(info)
    return jsonify({'sessions': out[:100]})


@flask_app.route('/api/recordings/<name>/token', methods=['POST'])
def api_recording_token(name):
    """Short-lived file token (?ft=) for <img>/download links of one recording."""
    import config as _cfg
    ok, status = _password_ok(request)
    if not ok:
        return _auth_fail(status)
    if _session_path(name) is None:
        return jsonify({'error': 'not found'}), 404
    sid = 'pw'
    auth = request.headers.get('Authorization', '')
    if auth.startswith('Bearer '):
        payload, _ = _check_token(auth[7:].strip(), 'a')
        if payload is not None:
            sid = payload['sid']
    now = time.time()
    ttl = _cfg.REC_FILE_TOKEN_TTL_S
    ft = _make_token('f', sid, now, now + ttl, {'n': name})
    return jsonify({'ft': ft, 'expires_in': ttl})


@flask_app.route('/api/recordings/<name>/log.jsonl')
def api_recording_log(name):
    ok, _st = _rec_read_ok(request, name)
    if not ok:
        return _rec_auth_fail()
    path = _session_path(name)
    if path is None:
        return jsonify({'error': 'not found'}), 404
    rows = recording_io.load_rows(path)
    body = (json.dumps(r, separators=(',', ':')) + '\n' for r in rows)
    return Response(body, mimetype='application/x-ndjson', headers={'Cache-Control': 'no-cache'})


@flask_app.route('/api/recordings/<name>/events.jsonl')
def api_recording_events(name):
    ok, _st = _rec_read_ok(request, name)
    if not ok:
        return _rec_auth_fail()
    path = _session_path(name)
    if path is None:
        return jsonify({'error': 'not found'}), 404
    evs = recording_io.load_events(path)
    body = (json.dumps(e, separators=(',', ':')) + '\n' for e in evs)
    return Response(body, mimetype='application/x-ndjson', headers={'Cache-Control': 'no-cache'})


@flask_app.route('/api/recordings/<name>/meta')
def api_recording_meta(name):
    ok, _st = _rec_read_ok(request, name)
    if not ok:
        return _rec_auth_fail()
    path = _session_path(name)
    if path is None:
        return jsonify({'error': 'not found'}), 404

    def jload(p):
        try:
            data = json.loads(p.read_text(encoding='utf-8'))
            return data if isinstance(data, dict) else None
        except (OSError, ValueError):
            return None

    try:
        schema = recording_io.detect_schema(path)
    except Exception:
        schema = -1
    man = jload(path / 'manifest.json')
    totals = man.get('totals') if man else None
    resp = jsonify({'schema': schema, 'meta': jload(path / 'meta.json'),
                    'summary': jload(path / 'summary.json'),
                    'manifest_totals': totals if isinstance(totals, dict) else None})
    resp.headers['Cache-Control'] = 'no-cache'
    return resp


@flask_app.route('/api/recordings/<name>', methods=['DELETE'])
def api_recording_delete(name):
    ok, status = _password_ok(request)
    if not ok:
        return _auth_fail(status)
    path = _session_path(name)
    if path is None:
        return jsonify({'error': 'no such recording'}), 404
    st = _rec_status()
    if st.get('rec_on') and st.get('rec_session') == name:
        return jsonify({'error': 'stop the recording first'}), 409
    shutil.rmtree(path, ignore_errors=True)
    log.info('[API] deleted recording %s from %s', name, request.remote_addr)
    return jsonify({'ok': True})


@flask_app.route('/recordings/<name>/<path:rel>')
def recording_file(name, rel):
    ok, _st = _rec_read_ok(request, name)
    if not ok:
        return _rec_auth_fail()
    path = _session_path(name)
    if path is None or not _REC_FILES.match(rel):
        return jsonify({'error': 'not found'}), 404
    target = path / rel
    try:
        inside = target.resolve().is_relative_to(path.resolve())
    except (OSError, RuntimeError, ValueError):
        inside = False
    if not inside or not target.is_file():
        return jsonify({'error': 'not found'}), 404
    resp = send_file(target, conditional=True)
    resp.headers['Cache-Control'] = 'no-cache' if rel.endswith(('.jsonl', '.json')) else 'max-age=3600'
    return resp


@flask_app.route('/recordings/<name>.tar')
def recording_download(name):
    ok, _st = _rec_read_ok(request, name)
    if not ok:
        return _rec_auth_fail()
    """The whole session as a tar stream (no temp file, no memory spike)."""
    path = _session_path(name)
    if path is None:
        return jsonify({'error': 'not found'}), 404
    proc = subprocess.Popen(['tar', '-cf', '-', '--exclude=*.tmp', '--exclude=.*.tmp', '-C', str(REC_DIR), name], stdout=subprocess.PIPE)

    def stream():
        try:
            while True:
                chunk = proc.stdout.read(1 << 20)
                if not chunk:
                    break
                yield chunk
        finally:
            proc.stdout.close()
            proc.wait()

    return Response(stream(), mimetype='application/x-tar',
                    headers={'Content-Disposition': f'attachment; filename={name}.tar'})


# ── Derived views (re-run the current vision.py on a saved frame) ───
import collections as _collections

_DERIVED_LOCK = threading.Semaphore(1)
_DERIVED_ACQUIRE_S = 10.0
_DERIVED_CACHE_D: '_collections.OrderedDict' = _collections.OrderedDict()
_DERIVED_CACHE_LOCK = threading.Lock()
_ROW_INDEX: dict = {}


def _row_index(path: Path):
    """({i: row}, sorted [i]) for a session, cached by the log files' mtimes."""
    files = [path / 'log.jsonl'] + sorted(path.glob('shards/s*/log.jsonl'))
    sig = []
    for f in files:
        try:
            st = f.stat()
            sig.append((str(f), st.st_mtime_ns, st.st_size))
        except OSError:
            pass
    sig = tuple(sig)
    hit = _ROW_INDEX.get(str(path))
    if hit and hit[0] == sig:
        return hit[1], hit[2], hit[3]
    by_i = {}
    for r in recording_io.load_rows(path):
        i = r.get('i')
        if isinstance(i, int) and not isinstance(i, bool):
            by_i[i] = r
    order = sorted(by_i)
    if len(_ROW_INDEX) > 50:
        _ROW_INDEX.clear()
    mt = max((s[1] for s in sig), default=0)
    _ROW_INDEX[str(path)] = (sig, by_i, order, mt)
    return by_i, order, mt


@flask_app.route('/api/build')
def api_build():
    ok, _st = _rec_read_ok(request)
    if not ok:
        return _rec_auth_fail()
    from build_info import get_build_info
    b = get_build_info()
    return jsonify({k: b.get(k) for k in ('git_commit', 'git_dirty', 'src_fingerprint')})


@flask_app.route('/api/recordings/<name>/derived/<int:i>.jpg')
def api_recording_derived(name, i):
    ok, _st = _rec_read_ok(request, name)
    if not ok:
        return _rec_auth_fail()
    import config as _cfg
    kind = request.args.get('kind', 'views')
    if kind not in ('views', 'annotated'):
        return jsonify({'error': 'kind must be views or annotated'}), 400
    try:
        warm = int(request.args.get('warm', '0'))
    except (TypeError, ValueError):
        return jsonify({'error': 'warm must be an integer 0..10'}), 400
    warm = max(0, min(10, warm))
    if not _cfg.REC_DERIVED_WHILE_DRIVING and read_state().get('enabled'):
        return jsonify({'error': 'busy: rover driving'}), 503
    path = _session_path(name)
    if path is None:
        return jsonify({'error': 'not found'}), 404
    by_i, order, log_mt = _row_index(path)
    row = by_i.get(i)
    if row is None:
        return jsonify({'error': 'no such frame'}), 404
    from build_info import get_build_info
    b = get_build_info()
    fp = b.get('src_fingerprint') or ''
    code = (b.get('git_commit') or 'nogit')[:7] + ('+dirty' if b.get('git_dirty') else '') + '+' + fp[:8]
    key = (name, i, kind, warm if kind == 'views' else 0, fp, log_mt)
    with _DERIVED_CACHE_LOCK:
        data = _DERIVED_CACHE_D.get(key)
        if data is not None:
            _DERIVED_CACHE_D.move_to_end(key)
    cache = 'hit'
    if data is None:
        cache = 'miss'
        if not _DERIVED_LOCK.acquire(timeout=_DERIVED_ACQUIRE_S):
            return jsonify({'error': 'busy'}), 503
        try:
            import cv2
            import derived_views
            fpth = recording_io.frame_path(path, row)
            frame = cv2.imread(str(fpth)) if fpth else None
            if frame is None:
                return jsonify({'error': 'frame unreadable'}), 422
            try:
                if kind == 'annotated':
                    img = derived_views.render_annotated(frame)
                else:
                    warm_frames = []
                    if warm:
                        dt = row.get('dt')
                        prev = [j for j in order if j < i][-warm:]
                        for j in prev:
                            pdt = by_i[j].get('dt')
                            if (isinstance(dt, (int, float)) and isinstance(pdt, (int, float))
                                    and dt - pdt > 3.0):
                                continue
                            pp = recording_io.frame_path(path, by_i[j])
                            pf = cv2.imread(str(pp)) if pp else None
                            if pf is not None:
                                warm_frames.append(pf)
                    img = derived_views.render_views(frame, warm_frames, label=f'{name} #{i}')
            except ValueError:
                return jsonify({'error': 'invalid frame'}), 422
            ok, buf = cv2.imencode('.jpg', img, [cv2.IMWRITE_JPEG_QUALITY, 85])
            if not ok:
                return jsonify({'error': 'encode failed'}), 500
            data = buf.tobytes()
        finally:
            _DERIVED_LOCK.release()
        with _DERIVED_CACHE_LOCK:
            _DERIVED_CACHE_D[key] = data
            while len(_DERIVED_CACHE_D) > max(1, _cfg.REC_DERIVED_CACHE):
                _DERIVED_CACHE_D.popitem(last=False)
    return Response(data, mimetype='image/jpeg',
                    headers={'X-Derived-Code': code, 'X-Cache': cache, 'Cache-Control': 'no-cache'})


@flask_app.route('/review/<name>')
def review(name):
    if _session_path(name) is None:
        return 'No such recording', 404
    return render_template('review.html', name=name)


@flask_app.route('/api/loop', methods=['GET', 'POST'])
def api_loop():
    """Wheel speed control: {"closed_loop": true} = encoder PID on the Arduino,
    false = raw PWM (open loop). Useful to compare both on the bench."""
    if request.method == 'POST':
        body = request.get_json(silent=True) or {}
        want = bool(body.get('closed_loop', True))
        write_state(closed_loop=want)
        log.info('[API] wheel control %s from %s',
                 'CLOSED LOOP (PID)' if want else 'OPEN LOOP (PWM)', request.remote_addr)
    return _control_json()


@flask_app.route('/api/obstacle', methods=['GET', 'POST'])
def api_obstacle():
    """Enforce (default) or ignore the camera obstacle stop.

    Switching it OFF removes a safety layer, so it needs the password;
    switching it back ON never does.
    """
    if request.method == 'POST':
        body = request.get_json(silent=True) or {}
        want = bool(body.get('enabled', True))
        if not want:
            ok, status = _password_ok(request)
            if not ok:
                return _auth_fail(status)
        write_state(obstacle_stop=want)
        log.info('[API] obstacle stop %s from %s', 'ON' if want else 'OFF', request.remote_addr)
    return _control_json()


@flask_app.route('/api/speed', methods=['GET', 'POST'])
def api_speed():
    """Master max-speed limit in percent. Every other speed scales with it."""
    if request.method == 'POST':
        body = request.get_json(silent=True) or {}
        try:
            pct = clamp_speed(float(body.get('max_speed_pct')))
        except (TypeError, ValueError):
            return jsonify({'error': 'max_speed_pct must be a number'}), 400
        write_state(max_speed_pct=pct)
        log.info('[API] max speed %d%% from %s', pct, request.remote_addr)
    return _control_json()


@flask_app.route('/api/steer', methods=['GET', 'POST'])
def api_steer():
    """Line-following steering tuning (dashboard Drive tab).

    POST takes any subset of ``{turn_floor_base, lookahead_m}``:
      turn_floor_base  minimum turn authority, as a wheel-level PWM
                       difference, the follower may use the moment it starts
                       correcting. Pure pursuit alone asks for only a few PWM
                       when the line is 10-20 cm off centre, which a
                       skid-steer chassis cannot turn with, so this floor is
                       what actually makes it steer.
      lookahead_m      pure-pursuit lookahead, metres ahead of the rover.
                       Short tracks the centre of the line tightly but is
                       noisier; long is smooth but rides off-centre.

    Clamped on the server: the slider ranges are a hint, not the authority, so
    a hand-crafted request cannot demand more turning than TURN_FLOOR (the
    fully-boosted maximum the tracking monitor already allows) nor a lookahead
    so short the controller loses a usable sample. No password -- these can
    only make the rover steer more, never less, so they cannot remove a
    safety layer.
    """
    if request.method == 'POST':
        body = request.get_json(silent=True) or {}
        patch = {}
        for key in ('turn_floor_base', 'lookahead_m'):
            value = body.get(key)
            if value is None:
                continue
            try:
                patch[key] = STEER_CLAMPS[key](value)
            except (TypeError, ValueError):
                return jsonify({'error': f'{key} must be a number'}), 400
        if patch:
            write_state(**patch)
            log.info('[API] steering tuning from %s: %s', request.remote_addr,
                     {k: read_state()[k] for k in patch})
    state = read_state()
    return jsonify({
        'turn_floor_base': state['turn_floor_base'],
        'turn_floor_base_min': TURN_FLOOR_BASE_MIN,
        'turn_floor_base_max': TURN_FLOOR_BASE_MAX,
        'turn_floor_full': TURN_FLOOR,
        'turn_floor_full_err_m': TURN_FLOOR_FULL_ERR_M,
        'lookahead_m': state['lookahead_m'],
        'lookahead_min_m': LOOKAHEAD_MIN_M,
        'lookahead_max_m': LOOKAHEAD_MAX_M,
    })


@flask_app.route('/api/lidar', methods=['GET', 'POST'])
def api_lidar():
    """Obstacle-safety zones for the rotating LiDAR (dashboard LiDAR tab).

    POST takes any subset of ``{enabled, lidar_stop_dist_m, lidar_slow_dist_m,
    lidar_slow_v_mps, lidar_stale_ms, lidar_sector_deg}``. Every numeric
    value is clamped here on the server: the slider ranges are a hint, not
    the authority, so a hand-crafted request cannot set a stop distance the
    rover cannot react to.

    Turning the LiDAR safety OFF removes a safety layer, so it needs the
    password; turning it back ON never does.
    """
    if request.method == 'POST':
        body = request.get_json(silent=True) or {}
        patch = {}
        if 'enabled' in body:
            want = bool(body['enabled'])
            if not want:
                ok, status = _password_ok(request)
                if not ok:
                    return _auth_fail(status)
            patch['lidar_enabled'] = want
        for key, clamp in LIDAR_CLAMPS.items():
            if body.get(key) is None:
                continue
            try:
                patch[key] = clamp(body[key])
            except (TypeError, ValueError):
                return jsonify({'error': f'{key} must be a number'}), 400
        if patch:
            before = {k: read_state().get(k) for k in patch}
            write_state(lidar=patch)
            after = {k: read_state()[k] for k in patch}
            changed = {k: (before[k], after[k]) for k in patch if before[k] != after[k]}
            log.info('[API] lidar settings from %s: %s', request.remote_addr, changed or 'no-op')
    state = read_state()
    return jsonify({
        **{k: state[k] for k in ('lidar_enabled', *LIDAR_CLAMPS)},
        'limits': LIDAR_LIMITS,
        'slow_gap_m': LIDAR_SLOW_GAP_M,
    })


@flask_app.route('/status', methods=['GET'])
def status():
    payload = _read_status()
    try:
        after_id = int(request.args.get('after', '0'))
    except ValueError:
        after_id = 0
    filtered = []
    for entry in payload.get('logs', []):
        if entry.get('id', 0) <= after_id:
            continue
        entry.setdefault('category', 'SYSTEM')
        filtered.append(entry)
    payload['logs'] = filtered
    return jsonify(payload)


def _generate_frames():
    while True:
        try:
            with FRAME_PATH.open('rb') as fh:
                frame = fh.read()
            if frame:
                yield (b'--frame\r\n'
                       b'Content-Type: image/jpeg\r\n'
                       b'Cache-Control: no-cache\r\n\r\n' + frame + b'\r\n')
        except OSError:
            pass
        time.sleep(0.1)


@flask_app.route('/video_feed', methods=['GET'])
def video_feed():
    return Response(
        _generate_frames(),
        mimetype='multipart/x-mixed-replace; boundary=frame',
        headers={'Cache-Control': 'no-cache, no-store, must-revalidate'},
    )


@flask_app.route('/frame.jpg', methods=['GET'])
def frame_jpg():
    """Serve the latest annotated frame as a plain JPEG (Playwright-friendly).

    The MJPEG stream at /video_feed is infinite, which makes it impossible
    for headless browsers to fetch it as a single asset. /frame.jpg is the
    one-shot version: it returns whatever bytes are currently in frame.jpg.
    """
    try:
        data = FRAME_PATH.read_bytes()
    except OSError:
        return Response(b'', status=204)
    return Response(
        data,
        mimetype='image/jpeg',
        headers={'Cache-Control': 'no-cache, no-store, must-revalidate'},
    )


# ─────────────────────────────────────────────────────────────────
# USB camera feed (Logitech C270) — separate from the AGV pipeline.
# ─────────────────────────────────────────────────────────────────
#
# Why its own publisher: the AGV pipeline (``/video_feed`` above) reads
# JPEGs the line-follower writes to disk and only updates them at ~5 Hz
# with the overlay baked in. The new C270 tab wants the raw, unannotated
# stream at the camera's native 12 fps so the operator can inspect the
# scene directly. Keeping it independent also means the line follower
# can crash or be stopped without losing the USB feed.
def _cam_mjpeg_iter():
    boundary = b'frame'
    last_seq = -1
    while True:
        jpeg = CAM_PUBLISHER.latest_jpeg()
        status = CAM_PUBLISHER.status()
        if jpeg and status['frame_seq'] != last_seq:
            last_seq = status['frame_seq']
            yield (b'--' + boundary + b'\r\n'
                   b'Content-Type: image/jpeg\r\n'
                   b'Content-Length: ' + str(len(jpeg)).encode('ascii') + b'\r\n'
                   b'Cache-Control: no-cache\r\n\r\n') + jpeg + b'\r\n'
        time.sleep(0.05)


@flask_app.route('/cam/mjpeg', methods=['GET'])
def cam_mjpeg():
    return Response(
        _cam_mjpeg_iter(),
        mimetype='multipart/x-mixed-replace; boundary=frame',
        headers={'Cache-Control': 'no-cache, no-store, must-revalidate'},
    )


@flask_app.route('/cam/status', methods=['GET'])
def cam_status():
    return jsonify(CAM_PUBLISHER.status())


@flask_app.route('/cam/detections', methods=['GET'])
def cam_detections():
    """Boxes, names, enrolment progress and events for the Camera-tab overlay.

    Polling this is what wakes the analytics worker; it idles ~10 s after the
    last poll. Public like /cam/status: it exposes names only, no images.
    """
    CAM_ANALYTICS.touch()
    resp = jsonify(CAM_ANALYTICS.latest())
    resp.headers['Cache-Control'] = 'no-store'
    return resp


@flask_app.route('/api/cam/settings', methods=['GET', 'POST'])
def api_cam_settings():
    """GET and POST return the same body; POST echoes the *clamped* values."""
    if request.method == 'POST':
        body = request.get_json(silent=True) or {}
        # Turning the greeter on keeps the camera analysing with nobody at the
        # dashboard and lets the rover say names aloud, so it is not an open knob.
        if any(k in body for k in CAM_GREETER_KEYS):
            ok, status = _password_ok(request)
            if not ok:
                return _auth_fail(status)
        log.info('[API] cam settings %s from %s', sorted(body), request.remote_addr)
        CAM_ANALYTICS.update_settings(body)
    return jsonify({**CAM_ANALYTICS.settings(), 'limits': CAM_LIMITS,
                    'classes_available': CAM_ANALYTICS.object_classes()})


@flask_app.route('/api/cam/people', methods=['GET'])
def api_cam_people():
    """The roster with each person's profile; notes only for a signed-in reader."""
    signed_in = _voice_reader_signed_in(request)
    people = [dict(p, profile=_cam_profile_view(CAM_PROFILES_STORE.get(p['name']), signed_in))
              for p in CAM_ANALYTICS.store.people()]
    resp = jsonify({'people': people})
    resp.headers['Cache-Control'] = 'no-store'
    return resp


@flask_app.route('/api/cam/people/<name>/profile', methods=['PUT'])
def api_cam_person_profile(name):
    ok, status = _password_ok(request)
    if not ok:
        return _auth_fail(status)
    if not _cam_valid_name(name) or name not in [p['name'] for p in CAM_ANALYTICS.store.people()]:
        return jsonify({'error': 'no such person'}), 404
    body = request.get_json(silent=True) or {}
    try:
        prof = CAM_PROFILES_STORE.set(name, body)
    except ValueError as exc:
        return jsonify({'error': str(exc)}), 400
    log.info('[API] cam profile %r updated from %s', name, request.remote_addr)
    return jsonify(prof)


@flask_app.route('/api/cam/people/<name>/thumb', methods=['GET'])
def api_cam_person_thumb(name):
    jpg = CAM_ANALYTICS.store.thumb(name)
    if jpg is None:
        return jsonify({'error': 'no such person'}), 404
    return Response(jpg, mimetype='image/jpeg', headers={'Cache-Control': 'no-cache'})


@flask_app.route('/api/cam/people/<name>', methods=['DELETE'])
def api_cam_person_delete(name):
    # Auth: this edits who the system recognises, so it is not an open route.
    ok, status = _password_ok(request)
    if not ok:
        return _auth_fail(status)
    if not CAM_ANALYTICS.remove_person(name):
        return jsonify({'error': 'no such person'}), 404
    log.info('[API] cam person removed %r from %s', name, request.remote_addr)
    return jsonify({'removed': name, 'people': CAM_ANALYTICS.store.people()})


@flask_app.route('/api/cam/enroll', methods=['POST'])
def api_cam_enroll():
    ok, status = _password_ok(request)
    if not ok:
        return _auth_fail(status)
    body = request.get_json(silent=True) or {}
    name = str(body.get('name', '')).strip()
    if not _cam_valid_name(name):
        return jsonify({'error': 'name must be 1-32 chars: letters, digits, space, _ . -'}), 400
    try:
        samples = int(body.get('samples', 8))
    except (TypeError, ValueError):
        return jsonify({'error': 'samples must be a whole number'}), 400
    if CAM_PUBLISHER.status()['frame_seq'] == 0:
        return jsonify({'error': 'camera is not streaming'}), 409
    log.info('[API] cam enrol %r from %s', name, request.remote_addr)
    return jsonify(CAM_ANALYTICS.start_enroll(name, samples))


@flask_app.route('/api/cam/enroll/cancel', methods=['POST'])
def api_cam_enroll_cancel():
    CAM_ANALYTICS.cancel_enroll()
    return jsonify({'active': False})


@flask_app.route('/detections', methods=['GET'])
def detections():
    """Read-only detection viewer.

    Shows every signal agv_main.py reports: the annotated camera feed,
    the live centroid (cx, cy), the line-following verdict, the final motor
    command, the LiDAR safety state, the STM32 link health, and a running
    histogram of recent directions. All data is sourced from status.json.
    """
    return render_template('detections.html')


# ── Per-tick data for the dashboard Logs tab ───────────────────
# Public routes (same sensitivity class as /status and /frame.jpg).
# /api/livelog tails the AGV control loop's per-tick JSONL stream;
# /api/sessions/* lets the dashboard show a scrubber over the latest
# recording's row timeline.

_LIVELOG_KNOWN_FILTERS = {'errors', 'obstacle_slow', 'obstacle_stop',
                           'enc_fault_l', 'enc_fault_r', 'obstacle'}


def _livelog_path(today=None):
    from datetime import date as _date
    d = today or _date.today()
    return Path(LIVE_FRAMES_DIR) / f'agv-frames-{d.isoformat()}.jsonl'


def _row_matches_filter(row: dict, flt: str) -> bool:
    if flt == 'errors':
        return bool(row.get('errors'))
    if flt == 'obstacle_slow':
        return row.get('obst_raw') == 'SLOW'
    if flt == 'obstacle_stop':
        return row.get('obst_raw') == 'STOP'
    if flt == 'obstacle':
        return row.get('obst_raw') in ('SLOW', 'STOP')
    if flt == 'enc_fault_l':
        return bool((row.get('fault_bits') or 0) & 1)
    if flt == 'enc_fault_r':
        return bool((row.get('fault_bits') or 0) & 2)
    return False


@flask_app.route('/api/livelog', methods=['GET'])
def api_livelog():
    """Stream today's per-tick rows as NDJSON, advancing the client cursor.

    Query params:
      since=<epoch_ms>   emit only rows with t > since (default 0 = all)
      filter=<csv>       restrict to rows matching any of the named filters
    Returns 200 with one JSON object per line. Missing file returns 200 [].
    """
    since_raw = request.args.get('since', '0')
    try:
        since = float(since_raw) / 1000.0 if since_raw else 0.0
    except (TypeError, ValueError):
        return jsonify({'error': 'bad_since'}), 400
    flt_raw = (request.args.get('filter') or '').strip()
    filters = [f for f in flt_raw.split(',') if f in _LIVELOG_KNOWN_FILTERS] \
        if flt_raw else []

    path = _livelog_path()
    if not path.is_file():
        return Response('', mimetype='application/x-ndjson')

    def stream_filtered():
        try:
            with open(path, 'r', encoding='utf-8', errors='replace') as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except ValueError:
                        continue
                    if not isinstance(obj, dict):
                        continue
                    t_val = obj.get('t')
                    if isinstance(t_val, (int, float)) and not isinstance(t_val, bool) \
                            and t_val < since:
                        continue
                    if filters and not any(_row_matches_filter(obj, f) for f in filters):
                        continue
                    yield json.dumps(obj, separators=(',', ':')) + '\n'
        except OSError:
            return

    return Response(stream_filtered(), mimetype='application/x-ndjson',
                    headers={'Cache-Control': 'no-cache, no-store, must-revalidate'})


@flask_app.route('/api/sessions/latest', methods=['GET'])
def api_sessions_latest():
    """Return the newest recording session's metadata, or 404 if none exist."""
    root = Path(REC_DIR)
    if not root.is_dir():
        return jsonify({'error': 'no_sessions'}), 404
    newest = None
    for entry in sorted(root.iterdir(), reverse=True):
        if entry.is_dir() and _SESSION_RE.match(entry.name):
            newest = entry
            break
    if newest is None:
        return jsonify({'error': 'no_sessions'}), 404
    info = _dir_stats(newest)
    info['name'] = newest.name
    info['folder'] = str(newest)
    started_mono = (info.get('started_mono') if isinstance(info.get('started_mono'), (int, float))
                    else None)
    duration_s = info.get('duration_s') if isinstance(info.get('duration_s'), (int, float)) else 0.0
    info['started_at_ms'] = int((started_mono - duration_s) * 1000) if started_mono else 0
    info['n'] = info.get('frames') or 0
    return jsonify(info)


@flask_app.route('/api/sessions/<name>/timeline', methods=['GET'])
def api_session_timeline(name):
    """Return a slice of the named session's rows as JSON.

    Query params:
      from=<int>   first row index (default 0)
      to=<int>     last row index inclusive (default -1 = last)
    Errors:
      404 unknown session, 416 out-of-range.
    """
    path = _session_path(name)
    if path is None:
        return jsonify({'error': 'unknown_session'}), 404
    all_rows = recording_io.load_rows(path, sort=True)
    total = len(all_rows)
    try:
        rf = int(request.args.get('from', '0'))
        rt = int(request.args.get('to', str(total - 1)))
    except (TypeError, ValueError):
        return jsonify({'error': 'bad_range'}), 400
    if rf < 0:
        rf = max(0, total + rf)
    if rt < 0:
        rt = max(0, total + rt)
    if rf > rt or rf >= total:
        return jsonify({'error': 'range_out_of_bounds', 'total': total}), 416
    rt = min(rt, total - 1)
    rows = all_rows[rf:rt + 1]
    fields_raw = (request.args.get('fields') or '').strip()
    fields = [f for f in fields_raw.split(',') if f] if fields_raw else []
    if fields:
        rows = [{k: r.get(k) for k in fields} for r in rows]
    return jsonify({
        'session': _dir_stats(path),
        'rows': rows,
        'total': total,
        'from': rf,
        'to': rt,
    })


@flask_app.route('/api/sessions/<name>/timeline.csv', methods=['GET'])
def api_session_timeline_csv(name):
    """Stream the same row slice as CSV for download."""
    import csv as _csv
    from io import StringIO as _StringIO
    path = _session_path(name)
    if path is None:
        return jsonify({'error': 'unknown_session'}), 404
    rows = recording_io.load_rows(path, sort=True)
    total = len(rows)
    try:
        rf = int(request.args.get('from', '0'))
        rt = int(request.args.get('to', str(total - 1)))
    except (TypeError, ValueError):
        return jsonify({'error': 'bad_range'}), 400
    if rf < 0:
        rf = max(0, total + rf)
    if rt < 0:
        rt = max(0, total + rt)
    if rf > rt or rf >= total:
        return jsonify({'error': 'range_out_of_bounds', 'total': total}), 416
    rt = min(rt, total - 1)
    rows = rows[rf:rt + 1]

    # Flatten the nested row dict to a stable header list. New top-level keys
    # get appended at the end; existing column order is preserved.
    flat = [_flatten_row(r) for r in rows]
    header = []
    seen = set()
    for f in flat:
        for k in f:
            if k not in seen:
                seen.add(k)
                header.append(k)

    buf = _StringIO()
    w = _csv.DictWriter(buf, fieldnames=header, extrasaction='ignore')
    w.writeheader()
    for f in flat:
        w.writerow(f)
    return Response(buf.getvalue(), mimetype='text/csv',
                    headers={'Content-Disposition': f'attachment; filename="{name}-timeline.csv"',
                             'Cache-Control': 'no-cache, no-store, must-revalidate'})


def _flatten_row(row: dict) -> dict:
    """Flatten a row dict to top-level keys with dotted names for nested."""
    out: dict = {}
    for k, v in row.items():
        if isinstance(v, dict):
            for kk, vv in v.items():
                out[f'{k}.{kk}'] = vv
        else:
            out[k] = v
    return out


# ─────────────────────────────────────────────────────────────────
# WebSocket layer (push instead of polling)
# ─────────────────────────────────────────────────────────────────

class WSHub:
    """Tracks active WebSocket clients and broadcasts status snapshots."""

    def __init__(self) -> None:
        self._clients: set = set()
        self._lock = asyncio.Lock()

    async def register(self, ws) -> None:
        async with self._lock:
            self._clients.add(ws)

    async def unregister(self, ws) -> None:
        async with self._lock:
            self._clients.discard(ws)

    async def broadcast(self, message: dict) -> None:
        async with self._lock:
            clients = list(self._clients)
        if not clients:
            return
        encoded = json.dumps(message)
        dead = []
        for ws in clients:
            try:
                await ws.send(encoded)
            except Exception:
                dead.append(ws)
        if dead:
            async with self._lock:
                for ws in dead:
                    self._clients.discard(ws)


HUB = WSHub()


async def _ws_handler(ws) -> None:
    await HUB.register(ws)
    try:
        # Send first snapshot immediately, then await incoming pings.
        snapshot = _read_status()
        await ws.send(json.dumps(snapshot))
        async for _ in ws:
            pass  # client messages are unused; the server pushes only
    finally:
        await HUB.unregister(ws)


async def _publisher_loop() -> None:
    """Push status.json snapshots to every connected WS client at ~2 Hz."""
    last_id = 0
    while True:
        try:
            payload = _read_status()
            for entry in payload.get('logs', []):
                if entry.get('id', 0) > last_id:
                    last_id = entry['id']
            await HUB.broadcast(payload)
        except Exception as exc:
            log.warning('[WS] publish error: %s', exc)
        await asyncio.sleep(0.5)


async def _ws_main() -> None:
    import websockets
    async with websockets.serve(_ws_handler, DASHBOARD_HOST,
                                 DASHBOARD_WS_PORT,
                                 ping_interval=20, ping_timeout=20):
        await _publisher_loop()


def _start_ws() -> None:
    """Run the asyncio WebSocket loop on a dedicated daemon thread."""
    def _run():
        try:
            asyncio.run(_ws_main())
        except Exception as exc:
            log.warning('[WS] server stopped: %s', exc)
    threading.Thread(target=_run, name='ws-hub', daemon=True).start()


# ─────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────

def main() -> None:
    _start_ws()
    flask_app.run(
        host=DASHBOARD_HOST,
        port=DASHBOARD_HTTP_PORT,
        threaded=True,
        debug=False,
        use_reloader=False,
    )


if __name__ == '__main__':
    main()