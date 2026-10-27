"""Tests for voice.mic — C270 capture and the level gate.

No audio hardware is touched. Device discovery runs against a synthetic
sysfs tree, and capture runs against a stubbed ``subprocess.run``, so the
suite passes on a laptop with no webcam.
"""

import _env  # noqa: F401

import contextlib
import io
import struct
import wave
from pathlib import Path

import voice.mic as mic
from voice._constants import (
    C270_USB_ID,
    MIC_CHANNELS,
    MIC_MAX_SECONDS,
    MIC_SAMPLE_RATE,
    MIC_SPEECH_RMS,
)
from voice.mic import (
    LevelMeter,
    MicCapture,
    MicDevice,
    _duration_arg,
    _usb_id_for_card,
    find_alsa_card,
    frame_rms,
    read_wav_pcm,
)


# ── helpers ─────────────────────────────────────────────────────────


def _pcm(rms: float, samples: int) -> bytes:
    """S16_LE mono samples approximating a given RMS.

    A square wave at the requested amplitude is close enough: RMS of a
    ±A square is A, and it survives the int16 conversion the WAV writer
    does. Real speech is compared only against a threshold, so shape
    doesn't matter here.
    """
    if samples <= 0:
        return b''
    amp = max(0, min(int(rms), 32767))
    half = samples // 2
    body = struct.pack('<h', amp) * half + struct.pack('<h', -amp) * (samples - half)
    return body


def _wav(pcm: bytes, rate: int = MIC_SAMPLE_RATE, channels: int = MIC_CHANNELS) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, 'wb') as w:
        w.setnchannels(channels)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)
    return buf.getvalue()


@contextlib.contextmanager
def _patched(module, name, value):
    """Temporarily set ``module.name = value``. No monkeypatch fixture in
    this project's runner, so do it by hand and always restore."""
    original = getattr(module, name)
    setattr(module, name, value)
    try:
        yield value
    finally:
        setattr(module, name, original)


@contextlib.contextmanager
def _fixture_sysfs(root: Path):
    """Point discovery at a temp dir standing in for /sys/class/sound."""
    with _patched(mic, '_SYS_CLASS_SOUND', root):
        yield root


def _make_card(root: Path, name: str, vid=None, pid=None, product=None) -> Path:
    """Build a card dir whose ``device`` symlink walks up to a USB node
    carrying idVendor/idProduct."""
    card = root / name
    card.mkdir(parents=True)
    if vid is None:
        return card
    # Chain: card/device -> .../usb_interface -> .../usb_device
    usb_device = root / f'{name}-usbdev'
    usb_interface = root / f'{name}-if'
    usb_device.mkdir(parents=True)
    usb_interface.mkdir(parents=True)
    (usb_device / 'idVendor').write_text(f'{vid}\n')
    (usb_device / 'idProduct').write_text(f'{pid}\n')
    if product:
        (usb_device / 'product').write_text(f'{product}\n')
    link = usb_interface / 'device'
    link.symlink_to(usb_device)
    (card / 'device').symlink_to(link)
    return card


# ── device discovery ────────────────────────────────────────────────


def test_finds_c270_by_usb_id(tmp=None):
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        _make_card(root, 'card0', vid='0b10', pid='1234', product='other')
        _make_card(root, 'card2', vid='046d', pid='0825', product='C270 HD WEBCAM')
        with _fixture_sysfs(root):
            dev = find_alsa_card()
    assert dev is not None, 'C270 should be discovered'
    assert dev.card == 2
    assert dev.alsa_device == 'hw:2,0'
    assert dev.plughw == 'plughw:2,0'


def test_discovery_ignores_non_c270_devices():
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        _make_card(root, 'card0', vid='0b10', pid='1234')
        with _fixture_sysfs(root):
            assert find_alsa_card() is None


def test_discovery_returns_none_for_missing_sysfs():
    with _fixture_sysfs(Path('/no/such/sys/class/sound')):
        assert find_alsa_card() is None


def test_usb_id_for_card_is_case_normalised():
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        card = _make_card(root, 'card1', vid='046D', pid='0825')
        assert _usb_id_for_card(card) == C270_USB_ID


def test_usb_id_for_missing_device_is_none():
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        card = Path(td) / 'card9'
        card.mkdir()
        assert _usb_id_for_card(card) is None


def test_custom_usb_id_is_honoured():
    """The same code path finds a different webcam by its own VID:PID."""
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        _make_card(root, 'card0', vid='046d', pid='0825', product='C270')
        _make_card(root, 'card3', vid='1533', pid='0a37', product='PlayStation Eye')
        with _fixture_sysfs(root):
            dev = find_alsa_card('1533:0a37')
    assert dev is not None
    assert dev.card == 3
    assert dev.usb_id == '1533:0a37'


# ── duration formatting ─────────────────────────────────────────────


def test_duration_rejects_fractional():
    """arecord errors out on '3.0'; we must pass an integer string."""
    assert _duration_arg(3.0) == '3'
    assert _duration_arg(3.7) == '4'


def test_duration_clamps_to_max():
    assert _duration_arg(999) == str(int(MIC_MAX_SECONDS))


def test_duration_clamps_to_at_least_one():
    assert _duration_arg(0.05) == '1'


# ── signal level ────────────────────────────────────────────────────


def test_frame_rms_of_silence_is_zero():
    assert frame_rms(b'\x00\x00' * 100) == 0.0


def test_frame_rms_of_empty_is_zero():
    assert frame_rms(b'') == 0.0


def test_frame_rms_matches_amplitude():
    pcm = _pcm(1000, 2000)
    assert abs(frame_rms(pcm) - 1000) < 1.0


def test_read_wav_pcm_strips_header():
    pcm = _pcm(500, 1600)
    assert read_wav_pcm(_wav(pcm)) == pcm


def test_read_wav_pcm_on_garbage_is_empty():
    assert read_wav_pcm(b'not a wav file at all') == b''


def test_read_wav_pcm_on_empty_is_empty():
    assert read_wav_pcm(b'') == b''


def test_meter_detects_sustained_speech():
    # 40 frames of loud audio at 50 ms each = 2 s.
    pcm = _pcm(MIC_SPEECH_RMS * 4, MIC_SAMPLE_RATE * 2)
    meter = LevelMeter.for_defaults()
    assert meter.feed(_wav(pcm)) is True


def test_meter_rejects_room_tone():
    pcm = _pcm(MIC_SPEECH_RMS / 4, MIC_SAMPLE_RATE * 2)
    meter = LevelMeter.for_defaults()
    assert meter.feed(_wav(pcm)) is False


def test_meter_rejects_burst_shorter_than_required():
    """A cough — 2 loud frames, but MIC_SPEECH_FRAMES is 6."""
    meter = LevelMeter.for_defaults()
    loud = _pcm(MIC_SPEECH_RMS * 4, meter.frame_bytes * 2)
    assert meter.feed(_wav(loud)) is False


def test_meter_ignores_trailing_partial_frame():
    meter = LevelMeter.for_defaults()
    pcm = _pcm(MIC_SPEECH_RMS * 4, meter.frame_bytes * 10 + 7)
    assert meter.feed(_wav(pcm)) is True


def test_meter_tracks_peak_across_frames():
    meter = LevelMeter.for_defaults()
    pcm = _pcm(MIC_SPEECH_RMS * 8, meter.frame_bytes * 4)
    meter.feed(_wav(pcm))
    assert meter.peak_rms > MIC_SPEECH_RMS


def test_meter_reset_clears_state():
    meter = LevelMeter.for_defaults()
    meter.feed(_wav(_pcm(MIC_SPEECH_RMS * 4, meter.frame_bytes * 10)))
    meter.reset()
    assert meter.loud_streak == 0
    assert meter.quiet_streak == 0
    assert meter.peak_rms == 0.0


def test_meter_default_frame_is_50ms():
    meter = LevelMeter.for_defaults()
    assert meter.frame_bytes == MIC_SAMPLE_RATE * 50 // 1000 * 2


# ── capture (stubbed subprocess) ────────────────────────────────────


class _Completed:
    def __init__(self, stdout=b'', stderr=b''):
        self.stdout = stdout
        self.stderr = stderr


def test_record_builds_correct_arecord_argv():
    seen = {}

    def fake_run(argv, **kwargs):
        seen['argv'] = argv
        return _Completed(stdout=_wav(_pcm(500, 16000)))

    cap = MicCapture(device=MicDevice(card=2, alsa_device='hw:2,0'))
    with _patched(mic.shutil, 'which', lambda name: '/usr/bin/arecord'):
        with _patched(mic.subprocess, 'run', fake_run):
            cap.record(2)
    argv = seen['argv']
    assert argv[0] == 'arecord'
    assert argv[argv.index('-D') + 1] == 'plughw:2,0'
    assert argv[argv.index('-f') + 1] == 'S16_LE'
    assert argv[argv.index('-r') + 1] == str(MIC_SAMPLE_RATE)
    assert argv[argv.index('-c') + 1] == str(MIC_CHANNELS)
    assert argv[argv.index('-d') + 1] == '2'
    assert argv[-1] == '-'          # wav to stdout, not a file


def test_record_returns_none_when_arecord_missing():
    def fake_run(argv, **kwargs):
        raise FileNotFoundError('arecord')

    cap = MicCapture(device=MicDevice(card=2, alsa_device='hw:2,0'))
    with _patched(mic.shutil, 'which', lambda name: None):
        assert cap.record(1) is None
        assert cap.available is False


def test_record_returns_none_on_arecord_failure():
    import subprocess as sp

    def fake_run(argv, **kwargs):
        raise sp.CalledProcessError(1, argv, stderr=b'alsa lib error')

    cap = MicCapture(device=MicDevice(card=2, alsa_device='hw:2,0'))
    with _patched(mic.shutil, 'which', lambda name: '/usr/bin/arecord'):
        with _patched(mic.subprocess, 'run', fake_run):
            assert cap.record(1) is None


def test_record_returns_none_on_timeout():
    import subprocess as sp

    def fake_run(argv, **kwargs):
        raise sp.TimeoutExpired(argv, 5)

    cap = MicCapture(device=MicDevice(card=2, alsa_device='hw:2,0'))
    with _patched(mic.shutil, 'which', lambda name: '/usr/bin/arecord'):
        with _patched(mic.subprocess, 'run', fake_run):
            assert cap.record(1) is None


def test_record_returns_none_when_no_device():
    cap = MicCapture(device=MicDevice(card=2, alsa_device='hw:2,0'))
    cap.device = None
    assert cap.record(1) is None


def test_record_returns_none_on_empty_output():
    cap = MicCapture(device=MicDevice(card=2, alsa_device='hw:2,0'))
    with _patched(mic.shutil, 'which', lambda name: '/usr/bin/arecord'):
        with _patched(mic.subprocess, 'run', lambda a, **k: _Completed(b'')):
            assert cap.record(1) is None


def test_listen_returns_none_when_only_silence():
    cap = MicCapture(device=MicDevice(card=2, alsa_device='hw:2,0'))
    quiet = _wav(_pcm(MIC_SPEECH_RMS / 5, MIC_SAMPLE_RATE))
    with _patched(mic.shutil, 'which', lambda name: '/usr/bin/arecord'):
        with _patched(mic.subprocess, 'run', lambda a, **k: _Completed(quiet)):
            assert cap.listen(1) is None


def test_listen_returns_audio_when_speech_present():
    cap = MicCapture(device=MicDevice(card=2, alsa_device='hw:2,0'))
    loud = _wav(_pcm(MIC_SPEECH_RMS * 4, MIC_SAMPLE_RATE))
    with _patched(mic.shutil, 'which', lambda name: '/usr/bin/arecord'):
        with _patched(mic.subprocess, 'run', lambda a, **k: _Completed(loud)):
            assert cap.listen(1) == loud


def test_capture_without_device_is_unavailable():
    """A MicCapture built on a machine with no C270 logs once and goes
    quietly unavailable — it must never raise at construction."""
    with _patched(mic, 'find_alsa_card', lambda *a, **k: None):
        with _patched(mic, 'list_capture_cards', lambda: []):
            cap = MicCapture()
    assert cap.device is None
    assert cap.available is False


# ── the "no mic" warning must stay readable ──────────────────────────

# The real output of `arecord -l` on this Jetson: a banner, then 20 onboard
# ADMAIF devices each with two subdevice lines. Passing that through
# unfiltered produced a single ~5000-character log line.
ARECORD_L = '\n'.join(
    ['**** List of CAPTURE Hardware Devices ****'] +
    ['card 1: APE [NVIDIA Jetson Orin Nano APE], device %d: '
     'fe.admaif@290f000.ADMAIF%d (*) []\n  Subdevices: 1/1\n'
     '  Subdevice #0: subdevice #0' % (i, i + 1) for i in range(20)])


def test_capture_cards_are_devices_not_subdevice_furniture():
    """The banner and subdevice lines are not capture devices."""
    with _patched(mic.shutil, 'which', lambda n: '/usr/bin/arecord'):
        with _patched(mic.subprocess, 'run',
                      lambda a, **k: _Completed(ARECORD_L)):
            cards = mic.list_capture_cards()
    assert len(cards) == 20
    assert all(c.startswith('card ') for c in cards), cards[:2]
    assert not any('Subdevice' in c for c in cards)
    assert not any(c.startswith('****') for c in cards)
    # ~2.7 kB of real `arecord -l` output; the summary must be orders
    # of magnitude shorter than what it replaces.
    assert len(ARECORD_L) > 2500, 'the fixture must stay realistically noisy'


def test_the_mic_warning_is_one_short_line():
    """The bug: a single log line carrying the whole of `arecord -l`."""
    with _patched(mic.shutil, 'which', lambda n: '/usr/bin/arecord'):
        with _patched(mic.subprocess, 'run',
                      lambda a, **k: _Completed(ARECORD_L)):
            summary = mic.summarise_capture_cards()
    assert len(summary) < 200, summary
    assert 'more' in summary, 'a truncated list must say so: %r' % summary


def test_a_summary_with_nothing_to_report_says_so():
    with _patched(mic.shutil, 'which', lambda n: None):
        assert mic.summarise_capture_cards() == 'none detected'


def test_losing_the_mic_leaves_audio_output_working():
    """Input and output are separate; a dead webcam must not mute the rover."""
    from voice.tts import SystemPlayback

    with _patched(mic, 'find_alsa_card', lambda *a, **k: None):
        with _patched(mic.shutil, 'which', lambda n: '/usr/bin/arecord'):
            with _patched(mic.subprocess, 'run',
                          lambda a, **k: _Completed(ARECORD_L)):
                cap = MicCapture()
    assert cap.available is False
    assert SystemPlayback().timeout_s > 0


if __name__ == '__main__':
    _env.run_tests(globals())