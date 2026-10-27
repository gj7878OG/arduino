"""mic_inventory() and probe_microphone(): every state, no hardware needed."""
import _env  # noqa: F401

import io
import struct
import subprocess
import wave
from types import SimpleNamespace

import voice.mic as m

DEV = m.MicDevice(card=0, alsa_device='hw:0,0', product='WEBCAM')
LINES = ['**** List of CAPTURE Hardware Devices ****',
         'card 0: WEBCAM [C270 HD WEBCAM], device 0: USB Audio [USB Audio]',
         'card 2: APE [NVIDIA Jetson Orin Nano APE], device 0: x [] ',
         'card 2: APE [NVIDIA Jetson Orin Nano APE], device 1: y []']


def _wav(sample):
    b = io.BytesIO()
    with wave.open(b, 'wb') as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(16000)
        w.writeframes(struct.pack('<h', sample) * 16000)
    return b.getvalue()


class _Patch:
    def __init__(self, device=DEV, which='/usr/bin/arecord', run=None, lines=LINES):
        self.device, self.which, self.run, self.lines = device, which, run, lines

    def __enter__(self):
        self.o = (m.find_alsa_card, m.shutil.which, m.subprocess.run, m.list_capture_cards)
        m.find_alsa_card = lambda *a, **k: self.device
        m.shutil.which = lambda n: self.which
        m.list_capture_cards = lambda: self.lines
        if self.run:
            m.subprocess.run = self.run

    def __exit__(self, *a):
        m.find_alsa_card, m.shutil.which, m.subprocess.run, m.list_capture_cards = self.o


def _done(stdout=b'', stderr=b'', rc=0):
    return lambda *a, **k: SimpleNamespace(stdout=stdout, stderr=stderr, returncode=rc)


def test_inventory_marks_the_selected_card_and_dedupes():
    with _Patch():
        inv = m.mic_inventory()
    assert inv['connected'] is True and inv['device']['plughw'] == 'plughw:0,0'
    assert [c['card'] for c in inv['cards']] == [0, 2]
    assert [c['selected'] for c in inv['cards']] == [True, False]


def test_inventory_when_unplugged():
    with _Patch(device=None, lines=LINES[:1] + LINES[2:]):
        inv = m.mic_inventory()
    assert inv['connected'] is False and inv['device'] is None
    assert not any(c['selected'] for c in inv['cards'])


def test_probe_missing_device_never_runs_arecord():
    def boom(*a, **k): raise AssertionError('must not capture')
    with _Patch(device=None, run=boom):
        r = m.probe_microphone()
    assert r['state'] == 'missing' and r['ok'] is False and '046d:0825' in r['message']


def test_probe_without_arecord():
    with _Patch(which=None):
        assert m.probe_microphone()['state'] == 'no_tool'


def test_probe_working():
    with _Patch(run=_done(stdout=_wav(900))):
        r = m.probe_microphone()
    assert r['state'] == 'working' and r['ok'] is True and r['peak_rms'] > 800


def test_probe_flat_zero_is_silent_not_working():
    with _Patch(run=_done(stdout=_wav(0))):
        r = m.probe_microphone()
    assert r['state'] == 'silent' and r['ok'] is False
    assert 'muted or dead' in r['message']


def test_probe_busy_device():
    with _Patch(run=_done(stderr=b'arecord: main:830: audio open error: Device or resource busy', rc=1)):
        r = m.probe_microphone()
    assert r['state'] == 'busy' and 'in use' in r['message']


def test_probe_other_failure_surfaces_the_error():
    with _Patch(run=_done(stderr=b'arecord: no such card', rc=1)):
        r = m.probe_microphone()
    assert r['state'] == 'error' and 'no such card' in r['message']


def test_probe_timeout_and_oserror_do_not_raise():
    def slow(*a, **k): raise subprocess.TimeoutExpired('arecord', 1)
    with _Patch(run=slow):
        assert m.probe_microphone()['state'] == 'error'
    def gone(*a, **k): raise OSError('exec failed')
    with _Patch(run=gone):
        assert m.probe_microphone()['state'] == 'error'


def test_probe_empty_output_is_an_error():
    with _Patch(run=_done(stdout=b'')):
        assert m.probe_microphone()['state'] == 'error'


if __name__ == '__main__':
    _env.run_tests(globals())
