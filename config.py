"""Local configuration for the Arduino-driven AGV (no STM32, no LiDAR).

Only the knobs that the camera + detector + Arduino path actually
consume. The original AGV project's `config.py` is left untouched; this
file is a slimmed-down twin that lives in /home/deep/arduino/.

All values can be overridden by environment variables.
"""

from __future__ import annotations

import os
import socket

# ── Camera line-following ───────────────────────────────────────────────
STEER_DEADZONE = 80                   # Pixels from center before steering
MIN_CONTOUR_AREA = 500                # Reject small noise contours
MORPHOLOGY_KERNEL = (5, 5)

# HSV range for the yellow navigation line.
YELLOW_HSV_LOWER = (20, 100, 100)
YELLOW_HSV_UPPER = (35, 255, 255)

# Enhanced-line detector (CLAHE + WB + bilateral + ROI + aspect)
LINE_DETECTOR = os.environ.get('AGV_LINE_DETECTOR', 'enhanced').lower()
CLAHE_CLIP_LIMIT = float(os.environ.get('AGV_CLAHE_CLIP', '3.0'))
CLAHE_TILE_GRID = int(os.environ.get('AGV_CLAHE_GRID', '8'))
WHITE_BALANCE = os.environ.get(
    'AGV_WHITE_BALANCE', '0').lower() in ('1', 'true', 'yes', 'on')
BILATERAL_D = int(os.environ.get('AGV_BILATERAL_D', '3'))
BILATERAL_SIGMA_COLOR = float(
    os.environ.get('AGV_BILATERAL_SIGMA_COLOR', '50.0'))
BILATERAL_SIGMA_SPACE = float(
    os.environ.get('AGV_BILATERAL_SIGMA_SPACE', '50.0'))
LINE_ROI_TOP_FRACTION = float(
    os.environ.get('AGV_LINE_ROI_TOP', '0.4'))
LINE_MIN_ASPECT_RATIO = float(
    os.environ.get('AGV_LINE_MIN_ASPECT', '1.0'))
LINE_BORDER_IGNORE_FRAC = float(
    os.environ.get('AGV_LINE_BORDER_IGNORE', '0.05'))
YELLOW_HSV_LOWER_DARK = (15, 40, 40)
YELLOW_HSV_UPPER_DARK = (40, 255, 255)

# Tier-0 green-dominance guard (basic detector hardening).
TIER0_GREEN_GUARD_RATIO = float(os.environ.get('AGV_TIER0_GUARD', '1.85'))

# Centroid smoothing + LOST debounce.
CENTROID_EMA_ALPHA = float(os.environ.get('AGV_CENTROID_EMA_ALPHA', '0.4'))
LOST_DEBOUNCE_TICKS = int(os.environ.get('AGV_LOST_DEBOUNCE', '6'))
CAUTION_SPEED = os.environ.get('AGV_CAUTION_SPEED', '2')

# ── Camera backend ─────────────────────────────────────────────────────
# AGV_CAMERA_BACKEND ∈ {argus, v4l2, libcamera, fake}.
CAMERA_BACKEND = os.environ.get('AGV_CAMERA_BACKEND', 'argus').lower()
CAMERA_WIDTH = 1280
CAMERA_HEIGHT = 720
CAMERA_FPS = 60
CAMERA_SENSOR_ID = int(os.environ.get('AGV_CAMERA_SENSOR_ID', '0'))

# Dashboard preview camera. Empty (the default) means auto-detect: the web
# dashboard picks the first non-CSI /dev/video* node it can open. The CSI
# IMX219 is always skipped — it belongs to the AGV, and Argus cannot open a
# CaptureSession while another process holds the node. Set this only to pin a
# specific device (AGV_CAM_PREVIEW_DEV=/dev/video1).
CAM_PREVIEW_DEV = os.environ.get('AGV_CAM_PREVIEW_DEV', '').strip()
# Camera-tab face recognition. Kept under face_recognition/ (gitignored), not
# TELEMETRY_DIR, because enrolled faces must survive a reboot of /tmp.
_FACE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'face_recognition')
CAM_FACE_DB = os.environ.get('AGV_CAM_FACE_DB', os.path.join(_FACE_DIR, 'faces_db.pkl'))
CAM_FACE_THUMBS = os.environ.get('AGV_CAM_FACE_THUMBS', os.path.join(_FACE_DIR, 'faces'))
CAM_PROFILES = os.environ.get('AGV_CAM_PROFILES', os.path.join(_FACE_DIR, 'profiles.json'))
CAM_SETTINGS_FILE = os.environ.get('AGV_CAM_SETTINGS_FILE',
                                   os.path.join(_FACE_DIR, 'cam_settings.json'))

# White balance (same ISP knobs as the AGV project).
CAMERA_WB_MODE = int(os.environ.get('AGV_CAMERA_WB_MODE', '9'))
CAMERA_WB_LOCK = os.environ.get(
    'AGV_CAMERA_WB_LOCK', '0').lower() in ('1', 'true', 'yes', 'on')
CAMERA_FLIP = int(os.environ.get('AGV_CAMERA_FLIP', '0'))
# Cap the auto-exposure time (ns) so a moving rover is not smeared by a
# long shutter; the ISP raises gain instead. 0 = leave Argus fully auto.
CAMERA_EXPOSURE_MAX_NS = int(os.environ.get('AGV_CAMERA_EXPOSURE_MAX_NS', '10000000'))
_EXPOSURE_ARG = (
    'exposuretimerange="13000 {}" '.format(CAMERA_EXPOSURE_MAX_NS)
    if CAMERA_EXPOSURE_MAX_NS > 0 else ''
)
CAMERA_GREEN_CAST_FIX = os.environ.get(
    'AGV_CAMERA_GREEN_CAST_FIX', '1').lower() in ('1', 'true', 'yes', 'on')
CAMERA_GREEN_CAST_MAX_RATIO = float(
    os.environ.get('AGV_CAMERA_GREEN_CAST_RATIO', '1.5'))

# GStreamer pipelines (the same three the AGV project ships).
_ARGUS_PIPELINE = (
    "nvarguscamerasrc sensor-id={sensor} "
    "wbmode={wbmode} awblock={awblock} " + _EXPOSURE_ARG + "! "
    "video/x-raw(memory:NVMM), width={w}, height={h}, "
    "format=NV12, framerate={fps}/1 ! "
    "nvvidconv ! video/x-raw, format=BGRx ! "
    "videoconvert ! video/x-raw, format=BGR ! appsink"
).format(
    sensor=CAMERA_SENSOR_ID,
    wbmode=CAMERA_WB_MODE,
    awblock='true' if CAMERA_WB_LOCK else 'false',
    w=CAMERA_WIDTH, h=CAMERA_HEIGHT, fps=CAMERA_FPS,
)
_V4L2_PIPELINE = (
    "nvv4l2camerasrc sensor-id={sensor} "
    "wbmode={wbmode} awblock={awblock} ! "
    "video/x-raw(memory:NVMM), width={w}, height={h}, "
    "format=NV12, framerate={fps}/1 ! "
    "nvvidconv ! video/x-raw, format=BGRx ! "
    "videoconvert ! video/x-raw, format=BGR ! appsink"
).format(
    sensor=CAMERA_SENSOR_ID,
    wbmode=CAMERA_WB_MODE,
    awblock='true' if CAMERA_WB_LOCK else 'false',
    w=CAMERA_WIDTH, h=CAMERA_HEIGHT, fps=CAMERA_FPS,
)
_LIBCAMERA_PIPELINE = (
    "libcamerasrc camera-name={sano} ! "
    "video/x-raw, width={w}, height={h}, framerate={fps}/1 ! "
    "videoconvert ! video/x-raw, format=BGR ! appsink"
).format(sano=CAMERA_SENSOR_ID, w=CAMERA_WIDTH, h=CAMERA_HEIGHT, fps=CAMERA_FPS)

CAMERA_PIPELINES: dict[str, str] = {
    'argus':     _ARGUS_PIPELINE,
    'v4l2':      _V4L2_PIPELINE,
    'libcamera': _LIBCAMERA_PIPELINE,
}


def get_camera_pipeline(backend: str | None = None) -> str | None:
    name = (backend or CAMERA_BACKEND).lower()
    return CAMERA_PIPELINES.get(name)


# ── Arduino motor driver ────────────────────────────────────────────────
ARDUINO_PORT = os.environ.get('AGV_ARDUINO_PORT', '/dev/ttyACM0')
ARDUINO_BAUD = int(os.environ.get('AGV_ARDUINO_BAUD', '115200'))
# Send at most this many direction changes per second. The Arduino is
# happy to receive faster, but the wheels can't react faster than this.
ARDUINO_CMD_HZ = float(os.environ.get('AGV_ARDUINO_CMD_HZ', '20'))
# Watchdog: mark Arduino as disconnected if no ACK within this many ms.
ARDUINO_ACK_TIMEOUT_MS = int(
    os.environ.get('AGV_ARDUINO_ACK_TIMEOUT_MS', '1500'))


# ── Camera geometry (ground-plane / bird's-eye warp) ───────────────────
# The camera looks forward and down. These three numbers define the
# homography; measure them on the rover and override via the environment.
CAM_HEIGHT_M = float(os.environ.get('AGV_CAM_HEIGHT_M', '1.0'))
CAM_TILT_DEG = float(os.environ.get('AGV_CAM_TILT_DEG', '45'))   # below horizontal
CAM_HFOV_DEG = float(os.environ.get('AGV_CAM_HFOV_DEG', '62.2')) # IMX219
ROVER_WIDTH_M = float(os.environ.get('AGV_ROVER_WIDTH_M', '0.35'))

# Bird's-eye view window on the floor (metres, camera at the origin).
VISION_WIDTH = int(os.environ.get('AGV_VISION_WIDTH', '640'))
BEV_X_HALF_M = float(os.environ.get('AGV_BEV_X_HALF_M', '0.8'))
BEV_Y_NEAR_M = float(os.environ.get('AGV_BEV_Y_NEAR_M', '0.55'))
BEV_Y_FAR_M = float(os.environ.get('AGV_BEV_Y_FAR_M', '1.95'))
BEV_PX_PER_M = int(os.environ.get('AGV_BEV_PX_PER_M', '200'))

# ── Yellow detection (CIELAB hue, relative to the floor) ───────────────
# Measured on the real tape (pale masking tape on a wood desk): Lab hue ~102,
# HSV S only ~85, while the wood is Lab hue ~80 / HSV H 18. A fixed
# saturation floor misses the tape, so we use Lab hue + chroma and require
# the pixel to be *yellower than the typical floor pixel*.
LINE_HUE_DEG = float(os.environ.get('AGV_LINE_HUE_DEG', '100'))   # pure yellow ~103
LINE_HUE_TOL_DEG = float(os.environ.get('AGV_LINE_HUE_TOL_DEG', '30'))
LINE_HUE_MARGIN_DEG = float(os.environ.get('AGV_LINE_HUE_MARGIN_DEG', '10'))
LINE_MIN_CHROMA = float(os.environ.get('AGV_LINE_MIN_CHROMA', '16'))
LINE_MIN_L = int(os.environ.get('AGV_LINE_MIN_L', '60'))          # 0-255 scale
LINE_MIN_LENGTH_M = float(os.environ.get('AGV_LINE_MIN_LENGTH_M', '0.15'))
LINE_TAPE_DELTA_E = float(os.environ.get('AGV_LINE_TAPE_DELTA_E', '14'))
TAPE_MODEL_TTL_S = float(os.environ.get('AGV_TAPE_MODEL_TTL_S', '30'))
LINE_MAX_COVERAGE = float(os.environ.get('AGV_LINE_MAX_COVERAGE', '0.2'))   # of the BEV
LINE_MAX_WIDTH_RATIO = float(os.environ.get('AGV_LINE_MAX_WIDTH_RATIO', '2.5'))
LINE_MIN_ELONGATION = float(os.environ.get('AGV_LINE_MIN_ELONGATION', '1.8'))

# ── Line geometry ──────────────────────────────────────────────────────
LINE_STRIPS = int(os.environ.get('AGV_LINE_STRIPS', '5'))
LINE_MIN_WIDTH_M = float(os.environ.get('AGV_LINE_MIN_WIDTH_M', '0.025'))
LINE_MAX_WIDTH_M = float(os.environ.get('AGV_LINE_MAX_WIDTH_M', '0.45'))
LINE_MAX_JUMP_M = float(os.environ.get('AGV_LINE_MAX_JUMP_M', '0.25'))
LINE_REF_Y_M = float(os.environ.get('AGV_LINE_REF_Y_M', '0.7'))
LOOKAHEAD_M = float(os.environ.get('AGV_LOOKAHEAD_M', '1.0'))
LINE_EMA_ALPHA = float(os.environ.get('AGV_LINE_EMA_ALPHA', '0.5'))

# ── Camera obstacle detection (floor-colour model in the BEV) ──────────
OBST_ENABLED = os.environ.get('AGV_OBST', '1').lower() in ('1', 'true', 'yes', 'on')
OBST_MARGIN_M = float(os.environ.get('AGV_OBST_MARGIN_M', '0.10'))
OBST_DELTA_E = float(os.environ.get('AGV_OBST_DELTA_E', '22'))
OBST_MIN_AREA_M2 = float(os.environ.get('AGV_OBST_MIN_AREA_M2', '0.008'))
OBST_STOP_M = float(os.environ.get('AGV_OBST_STOP_M', '0.6'))
OBST_SLOW_M = float(os.environ.get('AGV_OBST_SLOW_M', '1.2'))
OBST_DEBOUNCE = int(os.environ.get('AGV_OBST_DEBOUNCE', '3'))

# ── Steering + speed (PWM units, 0-255) ────────────────────────────────
K_LOOK = float(os.environ.get('AGV_K_LOOK', '1.0'))     # lookahead error (m)
K_OFFSET = float(os.environ.get('AGV_K_OFFSET', '0.5')) # near offset (m)
K_ANGLE = float(os.environ.get('AGV_K_ANGLE', '0.4'))   # heading (m per rad)
# Steering law: 'pursuit' (turn rate proportional to speed, stable at any
# speed) or 'p' (old fixed-gain proportional).
STEER_MODE = os.environ.get('AGV_STEER_MODE', 'pursuit').lower()
WHEEL_BASE_M = float(os.environ.get('AGV_WHEEL_BASE_M', '0.30'))  # centre to centre
PURSUIT_GAIN = float(os.environ.get('AGV_PURSUIT_GAIN', '1.0'))
K_OFFSET_CURV = float(os.environ.get('AGV_K_OFFSET_CURV', '2.0'))   # 1/m per m of near offset
K_CURV_FF = float(os.environ.get('AGV_K_CURV_FF', '0.0'))           # feed-forward on line curvature
KI_OFFSET = float(os.environ.get('AGV_KI_OFFSET', '1.0'))           # trim for motor mismatch
KAPPA_MAX = float(os.environ.get('AGV_KAPPA_MAX', '3.0'))           # 1/m, tightest turn
STEER_GAIN = float(os.environ.get('AGV_STEER_GAIN', '250'))  # PWM per metre
OMEGA_MAX = float(os.environ.get('AGV_OMEGA_MAX', '110'))
CENTER_DEADBAND_M = float(os.environ.get('AGV_CENTER_DEADBAND_M', '0.04'))
V_MAX = float(os.environ.get('AGV_V_MAX', '150'))
V_MIN = float(os.environ.get('AGV_V_MIN', '70'))
ERR_SLOW_M = float(os.environ.get('AGV_ERR_SLOW_M', '0.30'))
K_CURV = float(os.environ.get('AGV_K_CURV', '0.5'))
OBST_SLOW_FRAC = float(os.environ.get('AGV_OBST_SLOW_FRAC', '0.5'))
CAUTION_S = float(os.environ.get('AGV_CAUTION_S', '0.4'))
CAUTION_SPEED_FRAC = float(os.environ.get('AGV_CAUTION_SPEED_FRAC', '0.5'))
RECOVERY = os.environ.get('AGV_RECOVERY', '1').lower() in ('1', 'true', 'yes', 'on')
RECOVER_S = float(os.environ.get('AGV_RECOVER_S', '3.0'))
RECOVER_OMEGA = float(os.environ.get('AGV_RECOVER_OMEGA', '70'))

# ── Turn authority and on-line checking ─────────────────────────────────
# A skid-steer chassis does not turn until the wheels differ by a minimum
# amount, so a tiny steering command does nothing. TURN_FLOOR is the smallest
# wheel-level difference (each side) used while correcting; it is independent
# of the dashboard speed slider.
TURN_FLOOR = float(os.environ.get('AGV_TURN_FLOOR', '40'))
TURN_FLOOR_BASE = float(os.environ.get('AGV_TURN_FLOOR_BASE', '0'))   # always-on part
TURN_FLOOR_FULL_ERR_M = float(os.environ.get('AGV_TURN_FLOOR_FULL_ERR_M', '0.12'))
# Pivot in place to realign when far off the line / pointing away from it.
ALIGN_ENTER_M = float(os.environ.get('AGV_ALIGN_ENTER_M', '0.6'))   # lookahead error
ALIGN_EXIT_M = float(os.environ.get('AGV_ALIGN_EXIT_M', '0.3'))
ALIGN_ENTER_DEG = float(os.environ.get('AGV_ALIGN_ENTER_DEG', '45'))
ALIGN_EXIT_DEG = float(os.environ.get('AGV_ALIGN_EXIT_DEG', '25'))
ALIGN_OMEGA = float(os.environ.get('AGV_ALIGN_OMEGA', '45'))
# Tracking monitor: is the rover actually getting back onto the line?
TRACK_ON_LINE_M = float(os.environ.get('AGV_TRACK_ON_LINE_M', '0.06'))
TRACK_STUCK_S = float(os.environ.get('AGV_TRACK_STUCK_S', '0.8'))
TRACK_STUCK_MIN_M = float(os.environ.get('AGV_TRACK_STUCK_MIN_M', '0.10'))  # smaller errors are 'settling'
TRACK_STRAIGHT_CURV = float(os.environ.get('AGV_TRACK_STRAIGHT_CURV', '0.3'))  # |curvature| below this = straight
TRACK_BOOST_MAX = float(os.environ.get('AGV_TRACK_BOOST_MAX', '3.0'))
TRACK_OFFLINE_M = float(os.environ.get('AGV_TRACK_OFFLINE_M', '0.45'))
TRACK_OFFLINE_S = float(os.environ.get('AGV_TRACK_OFFLINE_S', '2.5'))

# Master speed limit from the dashboard (percent of full PWM). The tuned
# speeds above correspond to DEFAULT_SPEED_PCT; the dashboard value scales all
# of them proportionally.
SPEED_MIN_PCT = int(os.environ.get('AGV_SPEED_MIN_PCT', '10'))
SPEED_MAX_PCT = int(os.environ.get('AGV_SPEED_MAX_PCT', '100'))
DEFAULT_SPEED_PCT = int(round(100.0 * V_MAX / 255.0))

# Manual driving from the dashboard: pivot / arc turn strength relative to the
# max-speed cap.
MANUAL_TURN_FRAC = float(os.environ.get('AGV_MANUAL_TURN_FRAC', '0.6'))

# ── Motion shaping (jerk-limited S-curve per wheel) ────────────────────
MOTION_HZ = float(os.environ.get('AGV_MOTION_HZ', '50'))
MAX_ACCEL = float(os.environ.get('AGV_MAX_ACCEL', '300'))     # PWM / s^2
MAX_JERK = float(os.environ.get('AGV_MAX_JERK', '1500'))      # PWM / s^3
STOP_ACCEL = float(os.environ.get('AGV_STOP_ACCEL', '800'))   # emergency
STOP_JERK = float(os.environ.get('AGV_STOP_JERK', '8000'))
OMEGA_TAU_S = float(os.environ.get('AGV_OMEGA_TAU_S', '0.12'))
MIN_PWM = float(os.environ.get('AGV_MIN_PWM', '40'))          # wheel stiction
TARGET_STALE_S = float(os.environ.get('AGV_TARGET_STALE_S', '0.4'))

# ── Wheel encoders + closed-loop speed (firmware v3) ───────────────────
# One encoder per side. The firmware counts edges of channel A (2 per pulse)
# and reports ticks/s; everything below converts between ticks and metres.
# CALIBRATE THESE ON THE REAL ROVER: the defaults are guesses.
CLOSED_LOOP = os.environ.get('AGV_CLOSED_LOOP', '1').lower() in ('1', 'true', 'yes', 'on')
ENC_PPR = float(os.environ.get('AGV_ENC_PPR', '20'))            # pulses / encoder rev, per channel
ENC_GEAR = float(os.environ.get('AGV_ENC_GEAR', '1.0'))         # encoder revs per wheel rev
ENC_EDGES = 2                                                   # firmware counts CHANGE of A
WHEEL_DIAMETER_M = float(os.environ.get('AGV_WHEEL_DIAMETER_M', '0.065'))
TICKS_PER_M = ENC_PPR * ENC_EDGES * ENC_GEAR / (3.141592653589793 * WHEEL_DIAMETER_M)
# Wheel speed reached at full PWM (level 255). Level -> target speed is linear.
WHEEL_MAX_MPS = float(os.environ.get('AGV_WHEEL_MAX_MPS', '0.5'))
# PID, in PWM per (m/s): error 0.1 m/s -> KP*0.1 PWM. Feed-forward is derived
# from WHEEL_MAX_MPS and MIN_PWM so the PID only trims the remaining error.
PID_KP = float(os.environ.get('AGV_PID_KP', '120'))
PID_KI = float(os.environ.get('AGV_PID_KI', '250'))
PID_KD = float(os.environ.get('AGV_PID_KD', '0'))

# ── Test recordings (dashboard Record tab) ─────────────────────────────
REC_DIR = os.environ.get('AGV_REC_DIR', os.path.join(os.path.dirname(os.path.abspath(__file__)), 'recordings'))
REC_DEFAULT_RATE_HZ = float(os.environ.get('AGV_REC_RATE_HZ', '5'))    # frames saved per second
REC_MAX_GB = float(os.environ.get('AGV_REC_MAX_GB', '3'))              # per session
REC_MIN_FREE_GB = float(os.environ.get('AGV_REC_MIN_FREE_GB', '3'))    # stop when the disk gets this low
REC_MAX_MIN = float(os.environ.get('AGV_REC_MAX_MIN', '60'))           # per session
REC_JPEG_QUALITY = int(os.environ.get('AGV_REC_JPEG_QUALITY', '88'))

# Recorder v1 knobs (nothing reads these until the v1 recorder lands).
_TRUE = ('1', 'true', 'yes', 'on')
REC_FORMAT = os.environ.get('AGV_REC_FORMAT', 'v1').lower()            # v1 (default) | v0 (rollback)
REC_MODE_DEFAULT = os.environ.get('AGV_REC_MODE_DEFAULT', 'continuous').lower()
REC_SAVE_WIDTH = int(os.environ.get('AGV_REC_SAVE_WIDTH', '0'))        # 0 = full size
REC_SHARD_S = float(os.environ.get('AGV_REC_SHARD_S', '300'))
REC_SHARD_MAX_FRAMES = int(os.environ.get('AGV_REC_SHARD_MAX_FRAMES', '3000'))
REC_FSYNC_S = float(os.environ.get('AGV_REC_FSYNC_S', '2.0'))
REC_QUEUE_MAX = int(os.environ.get('AGV_REC_QUEUE_MAX', '24'))
REC_PRE_S = float(os.environ.get('AGV_REC_PRE_S', '8'))
REC_RING_HZ = float(os.environ.get('AGV_REC_RING_HZ', '10'))
REC_RING_MAX_MB = float(os.environ.get('AGV_REC_RING_MAX_MB', '48'))
REC_POST_S = float(os.environ.get('AGV_REC_POST_S', '3'))
REC_BASELINE_HZ = float(os.environ.get('AGV_REC_BASELINE_HZ', '0.5'))
REC_TELEM_HZ = float(os.environ.get('AGV_REC_TELEM_HZ', '10'))
REC_TRIG_COOLDOWN_S = float(os.environ.get('AGV_REC_TRIG_COOLDOWN_S', '1.0'))
REC_TRIG_PERSIST_S = float(os.environ.get('AGV_REC_TRIG_PERSIST_S', '30.0'))   # a condition that stays true re-fires only this often
REC_TRIG_REARM_S = float(os.environ.get('AGV_REC_TRIG_REARM_S', '1.0'))
REC_TRIG_MAX_OFFSET_M = float(
    os.environ.get('AGV_REC_TRIG_MAX_OFFSET_M', str(BEV_X_HALF_M + 0.2)))
REC_TRIG_MAX_ANGLE_DEG = float(os.environ.get('AGV_REC_TRIG_MAX_ANGLE_DEG', '75'))
REC_TRIG_MIN_STRIPS = int(os.environ.get('AGV_REC_TRIG_MIN_STRIPS', '2'))
REC_TRIG_JUMP_M = float(os.environ.get('AGV_REC_TRIG_JUMP_M', '0.15'))
REC_TRIG_JUMP_DEG = float(os.environ.get('AGV_REC_TRIG_JUMP_DEG', '25'))
REC_REQUIRE_AUTH = os.environ.get('AGV_REC_REQUIRE_AUTH', '1').lower() in _TRUE
REC_LIST_REQUIRE_AUTH = os.environ.get('AGV_REC_LIST_REQUIRE_AUTH', '0').lower() in _TRUE
REC_FILE_TOKEN_TTL_S = float(os.environ.get('AGV_REC_FILE_TOKEN_TTL_S', '600'))
REC_DERIVED_CACHE = int(os.environ.get('AGV_REC_DERIVED_CACHE', '64'))
REC_DERIVED_WHILE_DRIVING = os.environ.get('AGV_REC_DERIVED_WHILE_DRIVING', '0').lower() in _TRUE
ROBOT_ID = os.environ.get('AGV_ROBOT_ID') or socket.gethostname()

# ── Telemetry / dashboard ──────────────────────────────────────────────
TELEMETRY_DIR = os.environ.get('AGV_DASHBOARD_DIR', '/tmp/agv-dashboard')
LOG_LIMIT = 500
SHOW_WINDOW = os.environ.get(
    'AGV_SHOW_WINDOW', '1' if os.environ.get('DISPLAY') else '0'
).lower() in ('1', 'true', 'yes', 'on')
DASHBOARD_HOST = os.environ.get('AGV_DASHBOARD_HOST', '0.0.0.0')
DASHBOARD_HTTP_PORT = int(os.environ.get('AGV_DASHBOARD_PORT', '5000'))
DASHBOARD_WS_PORT = int(os.environ.get('AGV_DASHBOARD_WS_PORT', '5001'))

# ── LiDAR obstacle safety (RPLIDAR A1/A2 / clones, 360° on /dev/ttyUSB0) ─
# Defaults give a 0.50 m hard e-stop, 1.00 m slow-down, 0.20 m/s slow speed.
# Server-side clamps in the API narrow these further; the dashboard can tune
# the values in the LiDAR tab but cannot take them outside the safe envelope.
LIDAR_ENABLED = os.environ.get('AGV_LIDAR_ENABLED', '1').lower() in _TRUE
LIDAR_PORT = os.environ.get('AGV_LIDAR_PORT', '/dev/ttyUSB0')
LIDAR_BAUD = int(os.environ.get('AGV_LIDAR_BAUD', '115200'))
# Rolling window of points kept in memory. Must be ≥ 1 motor revolution so
# the sector min is meaningful (clone spins at ~5 Hz, so 0.5 s is the floor;
# 1.0 s gives some headroom).
LIDAR_WINDOW_SEC = float(os.environ.get('AGV_LIDAR_WINDOW_SEC', '1.0'))
# Range filter: ignore points closer than the housing / lens returns
# (0.10 m) and farther than the real A2 max (12 m).
LIDAR_FILTER_MIN_M = float(os.environ.get('AGV_LIDAR_FILTER_MIN_M', '0.10'))
LIDAR_FILTER_MAX_M = float(os.environ.get('AGV_LIDAR_FILTER_MAX_M', '12.0'))
# Operator-tunable (clamped in the API):
LIDAR_STOP_DIST_M = float(os.environ.get('AGV_LIDAR_STOP_DIST_M', '0.50'))
LIDAR_SLOW_DIST_M = float(os.environ.get('AGV_LIDAR_SLOW_DIST_M', '1.00'))
LIDAR_SLOW_V_MPS = float(os.environ.get('AGV_LIDAR_SLOW_V_MPS', '0.20'))
LIDAR_STALE_MS = int(os.environ.get('AGV_LIDAR_STALE_MS', '200'))
LIDAR_SECTOR_DEG = int(os.environ.get('AGV_LIDAR_SECTOR_DEG', '30'))


# ── Logging ─────────────────────────────────────────────────────────────
LOG_FILE = os.environ.get(
    'AGV_LOG_FILE', os.path.join(TELEMETRY_DIR, 'agv.log'))
# Per-tick live JSONL stream that feeds the dashboard "Logs" tab live panel.
# Written by frame_log.FrameLog, one JSON object per line. Daily rotation is
# the FrameLog writer's job; these constants only pick the directory + rate.
LIVE_FRAMES_ENABLED = os.environ.get('AGV_LIVE_FRAMES_ENABLED', '1').lower() in _TRUE
LIVE_FRAMES_HZ = float(os.environ.get('AGV_LIVE_FRAMES_HZ', '5'))
LIVE_FRAMES_DIR = os.environ.get('AGV_LIVE_FRAMES_DIR',
                                 os.path.join(os.path.dirname(os.path.abspath(__file__)), 'logs'))
