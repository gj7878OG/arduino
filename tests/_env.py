"""Import this FIRST in every test: isolates all state in a temp folder.

Points the recorder / dashboard / log at a temp dir, uses the fake camera and a
serial port that does not exist, so a test can never move the rover or touch
the real recordings, /tmp/agv-dashboard or ports 5000/5001.
"""
import os
import sys
import tempfile
import time
import traceback

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TESTS = os.path.join(REPO, 'tests')
for _p in (REPO, TESTS):
    if _p not in sys.path:
        sys.path.insert(0, _p)

TMP = tempfile.mkdtemp(prefix='agv-test-')
import atexit, shutil  # noqa: E402
atexit.register(shutil.rmtree, TMP, ignore_errors=True)   # do not leave temp dirs behind
REC_DIR = os.path.join(TMP, 'recordings')
DASH_DIR = os.path.join(TMP, 'dashboard')
os.makedirs(REC_DIR, exist_ok=True)
os.makedirs(DASH_DIR, exist_ok=True)

os.environ['AGV_REC_DIR'] = REC_DIR
os.environ['AGV_DASHBOARD_DIR'] = DASH_DIR
os.environ['AGV_LOG_FILE'] = os.path.join(DASH_DIR, 'agv.log')
os.environ['AGV_DASHBOARD_PASSWORD'] = 'testpw'
os.environ['AGV_CAMERA_BACKEND'] = 'fake'
os.environ['AGV_ARDUINO_PORT'] = '/dev/agv-test-none'
# The LiDAR is real hardware on /dev/ttyUSB0; point it at a port that
# cannot exist so no test can ever spin up the sensor head.
os.environ['AGV_LIDAR_PORT'] = '/dev/agv-test-none'
# Default is auth ON; plain-server tests assume open endpoints. Tests that exercise
# the real defaults (test_e2e, enforced test_review_ui) pop this from their spawn env.
# web.py opens the USB camera at import. Pin it to a node that cannot exist so a
# test never grabs the C270 (uvcvideo has one opener) or probes /dev/video*.
os.environ['AGV_CAM_PREVIEW_DEV'] = '/dev/agv-test-none'
# Enrolled faces are biometric data: keep tests off the real db and settings.
os.environ['AGV_CAM_FACE_DB'] = os.path.join(DASH_DIR, 'faces_db.pkl')
os.environ['AGV_CAM_FACE_THUMBS'] = os.path.join(DASH_DIR, 'faces')
os.environ['AGV_CAM_PROFILES'] = os.path.join(DASH_DIR, 'profiles.json')
os.environ['AGV_CAM_SETTINGS_FILE'] = os.path.join(DASH_DIR, 'cam_settings.json')
os.environ.setdefault('AGV_REC_REQUIRE_AUTH', '0')
os.environ.setdefault('AGV_CONTROL_FILE', os.path.join(DASH_DIR, 'control.json'))


def poll(fn, timeout: float = 5.0, interval: float = 0.15):
    """Call ``fn`` until it returns something truthy or ``timeout`` passes.

    Returns the first truthy value, or None on timeout. Use this instead
    of a bare sleep when waiting on something the server or the browser
    will eventually produce.
    """
    end = time.monotonic() + timeout
    while True:
        value = fn()
        if value:
            return value
        if time.monotonic() >= end:
            return None
        time.sleep(interval)


def run_tests(ns: dict) -> None:
    """Run every ``test_*`` callable in ``ns``; print ``OK: N tests`` or exit 1."""
    tests = [(k, v) for k, v in sorted(ns.items()) if k.startswith('test_') and callable(v)]
    failed = 0
    for name, fn in tests:
        try:
            fn()
        except Exception:
            failed += 1
            print(f'FAIL: {name}')
            traceback.print_exc()
    if failed:
        print(f'FAILED: {failed} of {len(tests)} tests')
        sys.exit(1)
    print(f'OK: {len(tests)} tests')
