"""Tests for camera.py — rebuilding the capture after a disconnect.

A dropped camera leaves the existing ``cv2.VideoCapture`` handle returning
False forever. Retrying ``read()`` on that dead handle can never recover, so
the handle itself has to be rebuilt; the reader thread owns that loop.
``cv2.VideoCapture`` is the seam here — a fake device flips between absent
and present, which is exactly what an unplug/replug does to the hardware.
"""
import _env  # noqa: F401  (must be first)
import time

import numpy as np

import camera as camera_mod
from camera import Camera

GOOD = np.full((16, 16, 3), 120, np.uint8)


class FakeCap:
    def __init__(self, device):
        self._d = device
        self.released = False

    def isOpened(self):
        return self._d.present

    def read(self):
        if not self._d.present:
            return False, None
        time.sleep(0.002)
        return True, GOOD.copy()

    def release(self):
        self.released = True


class FakeDevice:
    """Stands in for cv2.VideoCapture. ``present`` is the plug state."""

    def __init__(self):
        self.present = False
        self.built = 0          # how many handles were constructed
        self.released = 0

    def open(self, pipeline, api):
        self.built += 1
        return FakeCap(self)


def _cam(monkeypatch, device, **kw):
    """Camera wired to a FakeDevice, with the retry policy made fast."""
    monkeypatch.setattr(camera_mod.cv2, 'VideoCapture', device.open)
    monkeypatch.setattr(camera_mod, 'REOPEN_AFTER_FAILS', 3)
    monkeypatch.setattr(camera_mod, 'REOPEN_BACKOFF_S', 0.1)
    monkeypatch.setattr(camera_mod, 'REOPEN_WARN_EVERY_S', 0.0)
    cam = Camera(kw.pop('backend', 'v4l2'))
    return cam


def test_reader_keeps_retrying_even_when_the_camera_is_absent_at_startup(monkeypatch):
    """The bug: open() failed, so no thread existed and nothing ever retried."""
    dev = FakeDevice()
    cam = _cam(monkeypatch, dev)
    try:
        rep = cam.open()
        assert not rep.frame_ok, 'expected the probe to report no frames'
        assert cam.connected is False
        assert cam._thread is not None, 'no reader thread -> no reconnect possible'

        dev.present = True                       # plug it in
        assert _env.poll(lambda: cam.connected, 5), \
            'camera came up but the reader never reacquired the pipeline'
        assert dev.built > 1, 'handle was never rebuilt'
        assert cam.latest() is not None
    finally:
        cam.release()


def test_midstream_disconnect_recovers_without_a_restart(monkeypatch):
    dev = FakeDevice()
    dev.present = True
    cam = _cam(monkeypatch, dev)
    try:
        assert cam.open().frame_ok
        assert _env.poll(lambda: cam.connected, 5)

        dev.present = False                      # unplug
        assert _env.poll(lambda: not cam.connected, 5), 'drop was never noticed'

        dev.present = True                       # replug
        assert _env.poll(lambda: cam.connected, 5), \
            'did not reconnect after the camera came back'
    finally:
        cam.release()


def test_a_brief_glitch_does_not_tear_down_a_working_pipeline(monkeypatch):
    """One dropped frame is backoff, not a reconnect."""
    dev = FakeDevice()
    dev.present = True
    cam = _cam(monkeypatch, dev)
    try:
        assert cam.open().frame_ok
        assert _env.poll(lambda: cam.connected, 5)
        built = dev.built

        dev.present = False                      # shorter than REOPEN_AFTER_FAILS
        time.sleep(0.05)
        dev.present = True
        assert _env.poll(lambda: cam.connected, 5)

        assert dev.built == built, \
            'a momentary glitch rebuilt the pipeline unnecessarily'
    finally:
        cam.release()


def test_connected_only_flips_true_once_a_frame_is_published(monkeypatch):
    """`connected` and `latest()` are read together; they must agree."""
    dev = FakeDevice()
    dev.present = True
    cam = _cam(monkeypatch, dev)
    try:
        assert cam.open().frame_ok
        for _ in range(200):
            if cam.connected:
                break
            time.sleep(0.005)
        assert cam.connected
        assert cam.latest() is not None, \
            'camera reported connected with an empty frame buffer'
    finally:
        cam.release()


def test_last_error_is_cleared_once_frames_flow_again(monkeypatch):
    dev = FakeDevice()
    cam = _cam(monkeypatch, dev)
    try:
        cam.open()
        assert cam.last_error, 'an absent camera should explain itself'

        dev.present = True
        assert _env.poll(lambda: cam.connected, 5)
        assert cam.last_error in (None, ''), \
            f'stale error survived recovery: {cam.last_error!r}'
    finally:
        cam.release()


def test_release_stops_the_retry_loop(monkeypatch):
    """A camera that is absent forever must not leave a thread spinning."""
    dev = FakeDevice()                          # never present
    cam = _cam(monkeypatch, dev)
    cam.open()
    cam.release()
    assert cam._thread is None
    assert cam._running is False