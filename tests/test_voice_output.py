"""Tests for voice.output — choosing which speaker Tarka talks out of.

Enumeration and playback are stubbed: these tests never open an audio device
and never produce sound. What they protect is the part that can go wrong
quietly — a device name that reaches a subprocess environment without being
checked, and a "selected" speaker that is no longer plugged in.
"""

import _env  # noqa: F401

import atexit
import json
import shutil
import tempfile
from pathlib import Path

import voice.output as out_mod
from voice.output import AUTO, DEVICE_RE, OutputSelector, list_outputs

PACTL_SINKS = """Sink #56
\tState: SUSPENDED
\tName: alsa_output.platform-3510000.hda.HiFi__hw_HDA_3__sink
\tDescription: Built-in Audio HDMI / DisplayPort 1 Output
Sink #165
\tState: RUNNING
\tName: bluez_output.1E_D2_C5_C4_A1_6E.1
\tDescription: ZEB-COUNTY
"""

APLAY_L = """**** List of PLAYBACK Hardware Devices ****
card 1: HDA [NVIDIA Jetson Orin Nano HDA], device 3: HDMI 0 [DELL IN1930]
  Subdevices: 1/1
  Subdevice #0: subdevice #0
"""


class _Runner:
    """Stands in for subprocess.run and shutil.which."""

    def __init__(self, answers):
        self.answers = answers
        self.calls = []

    def which(self, name):
        return '/usr/bin/' + name if name in self.answers else None

    def run(self, args, **kw):
        self.calls.append(args)
        key = ' '.join(args[:2])          # 'pactl get-default-sink'
        text = self.answers.get(key, self.answers.get(args[0]))
        if text is None:
            raise FileNotFoundError(args[0])
        return type('R', (), {'returncode': 0, 'stdout': text, 'stderr': ''})()


def _with(answers, fn):
    import shutil
    real_run, real_which = out_mod.subprocess.run, shutil.which
    runner = _Runner(answers)
    shutil.which = runner.which
    out_mod.subprocess.run = runner.run
    try:
        return fn(runner)
    finally:
        out_mod.subprocess.run, shutil.which = real_run, real_which


# ── enumeration ─────────────────────────────────────────────────────


def test_pactl_sinks_are_parsed_with_kind_and_state():
    got = _with({'pactl': PACTL_SINKS,
                 'pactl get-default-sink': 'bluez_output.1E_D2_C5_C4_A1_6E.1\n'},
                lambda r: list_outputs())
    assert got['source'] == 'pactl'
    ids = [o['id'] for o in got['outputs']]
    assert 'bluez_output.1E_D2_C5_C4_A1_6E.1' in ids
    bt = [o for o in got['outputs'] if o['kind'] == 'bluetooth'][0]
    assert bt['label'] == 'ZEB-COUNTY' and bt['state'] == 'RUNNING'
    hdmi = [o for o in got['outputs'] if o['kind'] == 'hdmi'][0]
    assert 'HDMI' in hdmi['label']


def test_aplay_is_the_fallback_when_pactl_is_missing():
    import shutil
    real_run, real_which = out_mod.subprocess.run, shutil.which
    shutil.which = lambda n: '/usr/bin/aplay' if n == 'aplay' else None
    out_mod.subprocess.run = lambda args, **kw: type(
        'R', (), {'returncode': 0, 'stdout': APLAY_L, 'stderr': ''})()
    try:
        got = list_outputs()
    finally:
        out_mod.subprocess.run, shutil.which = real_run, real_which
    assert got['source'] == 'aplay'
    assert got['outputs'][0]['id'] == 'plughw:1,3'


def test_enumeration_failure_is_empty_not_an_exception():
    import shutil
    real_run, real_which = out_mod.subprocess.run, shutil.which
    shutil.which = lambda n: None
    out_mod.subprocess.run = real_run
    try:
        got = list_outputs()
    finally:
        out_mod.subprocess.run, shutil.which = real_run, real_which
    assert got == {'outputs': [], 'default': '', 'source': 'none'}


# ── the selection ───────────────────────────────────────────────────


def test_default_selection_is_auto_and_yields_no_override():
    with tempfile.TemporaryDirectory() as d:
        sel = OutputSelector(d)
        assert sel.read() == AUTO
        # No override: the machine's own default is left alone.
        assert sel.env_for() == {}


def test_a_saved_device_becomes_a_pulse_sink_override():
    with tempfile.TemporaryDirectory() as d:
        sel = OutputSelector(d)
        ok, _ = sel.write('bluez_output.1E_D2_C5_C4_A1_6E.1')
        assert ok
        assert sel.env_for() == {'PULSE_SINK': 'bluez_output.1E_D2_C5_C4_A1_6E.1'}
        # Explicit override wins over the file, for a one-off play.
        assert sel.env_for('plughw:1,3') == {'PULSE_SINK': 'plughw:1,3'}


def test_a_device_that_is_not_a_real_output_is_refused():
    """The allowlist is the point: an unknown name never reaches a subprocess."""
    with tempfile.TemporaryDirectory() as d:
        sel = OutputSelector(d)
        allowed = ['plughw:1,3']
        ok, msg = sel.write('bluez_output.evil.1', allowed=allowed)
        assert not ok and 'not one of the current outputs' in msg
        assert sel.read() == AUTO, 'a refused device must not be written'
        ok, _ = sel.write('plughw:1,3', allowed=allowed)
        assert ok


def test_shell_metacharacters_are_refused_even_as_the_only_check():
    with tempfile.TemporaryDirectory() as d:
        sel = OutputSelector(d)
        for bad in ('a; rm -rf /', 'a b', 'a$(id)', 'a|b', 'a\nb', '../etc/passwd',
                    'x' * 200, 'a>b', 'a&b'):
            ok, _ = sel.write(bad)
            assert not ok, bad
            assert not DEVICE_RE.match(bad), bad
        # A comma is legal: ALSA hardware names are 'plughw:1,3'.
        assert DEVICE_RE.match('plughw:1,3')
        assert sel.write('plughw:1,3')[0]


def test_a_corrupt_or_missing_file_reads_as_auto():
    with tempfile.TemporaryDirectory() as d:
        sel = OutputSelector(d)
        sel.path.write_text('{not json')
        assert sel.read() == AUTO
        sel.path.write_text(json.dumps({'device': 42}))
        assert sel.read() == AUTO


def test_auto_can_be_written_back_explicitly():
    with tempfile.TemporaryDirectory() as d:
        sel = OutputSelector(d)
        sel.write('plughw:1,3')
        ok, msg = sel.write('')
        assert ok and msg == AUTO
        assert sel.env_for() == {}


def test_a_selected_device_that_disappeared_is_reported_missing():
    """The case that bites: the speaker drops off after being chosen."""
    with tempfile.TemporaryDirectory() as d:
        sel = OutputSelector(d)
        sel.write('bluez_output.GONE.1')
        described = sel.describe({'outputs': [{'id': 'plughw:1,3', 'label': 'HDMI',
                                               'kind': 'hdmi', 'state': 'SUSPENDED'}],
                                  'default': 'plughw:1,3', 'source': 'pactl'})
    assert described['state'] == 'missing'


def test_a_phantom_default_is_flagged_even_on_auto():
    """The default names a speaker that is not in the list — audio goes nowhere
    while aplay still reports success."""
    with tempfile.TemporaryDirectory() as d:
        sel = OutputSelector(d)
        described = sel.describe({'outputs': [{'id': 'plughw:1,3', 'label': 'HDMI',
                                               'kind': 'hdmi', 'state': 'SUSPENDED'}],
                                  'default': 'bluez_output.GONE.1', 'source': 'pactl'})
    assert described['selected'] == AUTO
    assert described['default_present'] is False


# ── the publisher contract ──────────────────────────────────────────


def test_the_publisher_reports_a_missing_default_not_a_healthy_lane():
    from voice.status import VoiceStatusPublisher
    from voice.telemetry import VoiceTelemetry

    GONE_LISTING = {'outputs': [{'id': 'plughw:1,3', 'label': 'HDMI',
                                 'kind': 'hdmi', 'state': 'SUSPENDED'}],
                    'default': 'bluez_output.GONE.1', 'source': 'pactl'}

    class Sel:
        """A real selector with the machine's device list frozen."""

        def __init__(self, directory):
            self._inner = OutputSelector(directory)

        def describe(self):
            return self._inner.describe(GONE_LISTING)

    with tempfile.TemporaryDirectory() as d:
        block = VoiceStatusPublisher(VoiceTelemetry(), d, outputs=Sel(d)) \
            .publish_once()['output']
    assert block['default_present'] is False
    assert block['ok'] is False, 'a phantom default must not read as healthy'


def test_the_publisher_omits_the_block_when_there_is_no_selector():
    from voice.status import VoiceStatusPublisher
    from voice.telemetry import VoiceTelemetry
    with tempfile.TemporaryDirectory() as d:
        assert 'output' not in VoiceStatusPublisher(VoiceTelemetry(), d).publish_once()


def test_a_broken_selector_never_stops_the_status_tick():
    from voice.status import VoiceStatusPublisher
    from voice.telemetry import VoiceTelemetry

    class Broken:
        def describe(self):
            raise RuntimeError('pactl exploded')

    with tempfile.TemporaryDirectory() as d:
        block = VoiceStatusPublisher(VoiceTelemetry(), d, outputs=Broken()) \
            .publish_once()['output']
    assert block['source'] == 'error' and block['outputs'] == []


# ── is anything actually coming out? ────────────────────────────────


class _Probe:
    """Stands in for aplay/pactl during a probe. Opens no device.

    The fake player stays alive until the scripted sink states have been
    consumed, then exits — otherwise it would finish before the first poll
    and the probe would never get to look at the sink, which is the thing
    under test.
    """

    def __init__(self, states, exit_code=0, finish=True, default_sink='sink.a'):
        # `states` is what `pactl list sinks` reports each time it is polled.
        self.states = list(states)
        self.default_sink = default_sink
        self.exit_code = exit_code
        self.finish = finish                 # does aplay exit on its own?
        self.killed = False
        self._polls = 0

    def sinks(self):
        if self.states:
            state = self.states[min(self._polls, len(self.states) - 1)]
            self._polls += 1
            return ('Sink #0\n\tState: %s\n\tName: sink.a\n'
                    '\tDescription: Test Sink\n' % state)
        return ''

    def popen(self, args, **kw):
        self.args = args
        self.env = kw.get('env') or {}
        real = out_mod.subprocess.Popen

        class P:
            def __init__(self):
                self.returncode = None

            def poll(self):
                if outer.finish and self.polls >= len(outer.states):
                    self.returncode = outer.exit_code
                self.polls += 1
                return self.returncode

            def wait(self, timeout=None):
                if self.returncode is None:
                    raise out_mod.subprocess.TimeoutExpired(args[0], timeout or 0)
                return self.returncode

            def kill(self):
                outer.killed = True
                self.returncode = -9

        outer = self
        p = P()
        p.polls = 0
        return p

    def run(self, args, **kw):
        key = ' '.join(args[:2])
        text = self.sinks() if key == 'pactl list' else {
            'pactl get-default-sink': self.default_sink + '\n',
            'pactl get-sink-mute': 'Mute: no\n',
            'pactl get-sink-volume': 'Volume: front-left: 65536 /  100% / -0.00 dB\n',
        }.get(key, '')
        return type('R', (), {'returncode': 0, 'stdout': text, 'stderr': ''})()


def _probe(states, exit_code=0, finish=True, device='sink.a', clip=None,
           muted='no', vol='100', timeout_s=1.0, default_sink='sink.a'):
    import shutil
    real_run, real_which, real_popen = out_mod.subprocess.run, shutil.which, out_mod.subprocess.Popen
    probe = _Probe(states, exit_code, finish, default_sink)
    shutil.which = lambda n: '/usr/bin/aplay' if n == 'aplay' else None
    out_mod.subprocess.run = probe.run
    out_mod.subprocess.Popen = probe.popen
    real_sink = out_mod.sink_status

    def fake_status(dev):
        st = real_sink(dev)
        st.update({'muted': muted == 'yes', 'volume_pct': int(vol)})
        return st
    out_mod.sink_status = fake_status
    try:
        return probe, out_mod.probe_playback(device, clip, timeout_s=timeout_s)
    finally:
        out_mod.sink_status = real_sink
        out_mod.subprocess.run, out_mod.subprocess.Popen = real_run, real_popen
        shutil.which = real_which


_CLIP_DIR = tempfile.mkdtemp(prefix='voice-output-test-')


def _clip():
    """A one-second silent WAV that survives the call (mkdtemp, not `with`)."""
    import atexit
    import wave
    atexit.register(shutil.rmtree, _CLIP_DIR, True)
    p = Path(_CLIP_DIR) / 'x.wav'
    with wave.open(str(p), 'wb') as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(b'\0\0' * 1600)
    return p


def test_a_sink_that_starts_is_reported_audible():
    _, r = _probe(['SUSPENDED', 'RUNNING'], clip=_clip())
    assert r['audible'] is True and r['reason'] == 'the speaker took the audio'
    assert r['became_active'] is True


def test_a_sink_that_never_starts_is_not_called_played():
    """The bug the button hid: aplay exits 0 and nobody hears anything."""
    _, r = _probe(['SUSPENDED'], clip=_clip())
    assert r['became_active'] is False
    assert r['audible'] is False
    assert r['reason'] == 'the speaker never started playing'


def test_an_off_bluetooth_speaker_says_so_instead_of_a_generic_failure():
    """A paired-but-disconnected BT speaker is the actual cause on this bench."""
    _, r = _probe(['SUSPENDED'], device='bluez_output.X.1', clip=_clip())
    assert 'not connected' in r['reason']


def test_a_muted_or_zero_volume_speaker_is_named_as_the_cause():
    _, r = _probe(['SUSPENDED'], clip=_clip(), muted='yes')
    assert r['reason'] == 'that speaker is muted'
    _, r = _probe(['SUSPENDED'], clip=_clip(), vol='0')
    assert r['reason'] == 'that speaker is at 0% volume'


def test_a_real_aplay_failure_is_still_reported_as_a_failure():
    _, r = _probe(['SUSPENDED'], exit_code=1, clip=_clip())
    assert r['played'] is False and r['reason'] == '/usr/bin/aplay failed on this clip'


def test_killing_a_long_clip_is_not_read_as_a_failure():
    """The probe stops the clip itself once the deadline passes; a SIGKILL
    from our own hand must not be mistaken for the player failing."""
    probe, r = _probe(['SUSPENDED'], finish=False, clip=_clip(), timeout_s=0.3)
    assert probe.killed is True, 'the test should have exercised the kill path'
    assert r['played'] is True, 'we killed it, so the exit code proves nothing'
    assert r['audible'] is False


def test_no_player_or_no_clip_is_reported_without_raising():
    import shutil
    real_which = shutil.which
    shutil.which = lambda n: None
    try:
        r = out_mod.probe_playback('sink.a', _clip())
    finally:
        shutil.which = real_which
    assert r['reason'] == 'no audio player is installed'

    r = out_mod.probe_playback('sink.a', Path('/nonexistent/clip.wav'))
    assert r['reason'] in ('no test clip available', 'no audio player is installed')


def test_the_probe_defaults_to_a_real_cached_phrase():
    """Not a generated tone: a green probe should mean Tarka's own audio works."""
    clip = out_mod.default_clip()
    assert clip is not None and clip.is_file(), 'no cached phrase to test with'
    assert clip.parent.name == 'phrases'


def test_auto_is_probed_as_the_default_sink_by_name():
    """Without this the probe looks for a sink called 'auto', finds nothing,
    and reports every healthy default speaker as silent."""
    _, r = _probe(['SUSPENDED', 'RUNNING'], device='auto', clip=_clip())
    assert r['requested'] == 'auto'
    assert r['device'] == 'sink.a', 'auto must resolve to the default sink'
    assert r['audible'] is True


def test_a_machine_with_no_default_speaker_says_so():
    """Distinct from 'never started playing': nothing to play through at all."""
    _, r = _probe([], device='', clip=_clip(), default_sink='')
    assert r['audible'] is False
    assert r['reason'] in ('no default speaker is set on this machine',
                           'no audio player is installed')


if __name__ == '__main__':
    _env.run_tests(globals())