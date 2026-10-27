"""Tests for cam/publisher.py — frame hand-off and the C270 warm-up.

The C270 returns ~25 black frames (mean brightness ~2) right after open and
then jumps to a normal exposure. The publisher must not show that black, and
must still go live in a genuinely dark room. ``_open`` is the seam: a fake
capture replaces the V4L2 device.
"""
import _env  # noqa: F401  (must be first)
import threading
import time

import numpy as np

from cam import publisher
from cam.publisher import CamPublisher

BLACK = np.full((48, 64, 3), 2, np.uint8)
BRIGHT = np.full((48, 64, 3), 140, np.uint8)
DARK_ROOM = np.full((48, 64, 3), 5, np.uint8)


class FakeCap:
    def __init__(self, frames, hold=None):
        self._frames = iter(frames)
        self._hold = hold            # while unset, repeat the last frame instead of failing
        self._last = None
        self.released = False

    def read(self):
        time.sleep(0.002)
        try:
            self._last = next(self._frames)
        except StopIteration:
            if self._hold is None or self._hold.is_set() or self._last is None:
                return False, None
        return True, self._last.copy()

    def release(self):
        self.released = True


def _pub(frames):
    pub = CamPublisher(device='/dev/fake', target_fps=500)
    pub._open = lambda: FakeCap(frames)
    return pub


def test_black_warmup_frames_are_never_published():
    pub = _pub([BLACK] * 25 + [BRIGHT] * 40)
    pub.start()
    try:
        assert _env.poll(lambda: pub.status()['frame_seq'] > 0, 3)
        seq, frame = pub.latest_frame()
        assert frame.mean() > 100, f'published a dark frame (mean {frame.mean():.0f})'
        assert pub.latest_jpeg()
    finally:
        pub.stop()


def test_a_dark_room_still_goes_live_after_the_cap():
    pub = _pub([DARK_ROOM] * (publisher.WARMUP_MAX_FRAMES + 30))
    pub.start()
    try:
        assert _env.poll(lambda: pub.status()['frame_seq'] > 0, 5), 'dark room never went live'
        assert pub.latest_frame()[1].mean() < publisher.WARMUP_MIN_BRIGHTNESS
    finally:
        pub.stop()


def test_latest_frame_is_empty_before_the_first_frame():
    assert CamPublisher(device='/dev/fake').latest_frame() == (0, None)


def test_latest_frame_seq_matches_status_seq():
    pub = _pub([BRIGHT] * 30)
    pub.start()
    try:
        assert _env.poll(lambda: pub.status()['frame_seq'] >= 5, 3)
        seq, frame = pub.latest_frame()
        assert frame is not None and seq <= pub.status()['frame_seq']
    finally:
        pub.stop()


def test_connected_is_reported_once_frames_flow_and_dropped_on_read_failure():
    unplug = threading.Event()
    pub = CamPublisher(device='/dev/fake', target_fps=50)
    pub._open = lambda: FakeCap([BRIGHT] * 3, hold=unplug)
    pub.start()
    try:
        assert _env.poll(lambda: pub.status()['connected'], 3)
        unplug.set()                 # USB pulled: read() starts failing
        assert _env.poll(lambda: not pub.status()['connected'], 3), 'stayed connected after read() failed'
        assert 'no frame' in pub.status()['last_error']
    finally:
        pub.stop()


def test_stop_is_idempotent_and_safe_before_start():
    pub = CamPublisher(device='/dev/fake')
    pub.stop()
    pub.stop()


if __name__ == '__main__':
    _env.run_tests(globals())
