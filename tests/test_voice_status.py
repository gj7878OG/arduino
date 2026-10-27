"""Tests for voice.status — the voice_status.json publisher."""

import _env  # noqa: F401

import json
import os
import tempfile

from voice.status import SCHEMA, STATUS_FILENAME, VoiceStatusPublisher
from voice.telemetry import VoiceTelemetry

SECRET = 'dg_SUPER_SECRET_VALUE_0123456789abcdef0123456789'


def _pub(d, **kw):
    return VoiceStatusPublisher(VoiceTelemetry(), d,
                                environ={'DEEPGRAM_API_KEY': SECRET}, **kw)


def test_publish_writes_valid_json_atomically():
    with tempfile.TemporaryDirectory() as d:
        _pub(d).publish_once()
        assert not os.path.exists(os.path.join(d, STATUS_FILENAME + '.tmp'))
        data = json.load(open(os.path.join(d, STATUS_FILENAME)))
    assert data['schema'] == SCHEMA and data['pid'] == os.getpid()


def test_key_values_never_published():
    with tempfile.TemporaryDirectory() as d:
        _pub(d).publish_once()
        raw = open(os.path.join(d, STATUS_FILENAME)).read()
    assert SECRET not in raw and 'SUPER_SECRET' not in raw
    assert json.loads(raw)['keys'] == {'DEEPGRAM_API_KEY': True,
                                       'GEMINI_API_KEY': False,
                                       'GOOGLE_API_KEY': False}


def test_unconfigured_lanes_report_down_not_crash():
    with tempfile.TemporaryDirectory() as d:
        lanes = _pub(d).publish_once()['lanes']
    assert set(lanes) == {'mic', 'stt', 'tts', 'narrator', 'parser'}
    assert all(not l['ok'] and l['reason'] for l in lanes.values())


def test_payload_has_all_cards():
    with tempfile.TemporaryDirectory() as d:
        p = _pub(d).publish_once()
    for k in ('transcripts', 'narration', 'parses', 'meter', 'latency'):
        assert k in p


def test_start_stop_is_idempotent_and_publishes():
    with tempfile.TemporaryDirectory() as d:
        pub = _pub(d, interval_s=0.05).start()
        import time; time.sleep(0.2)
        pub.stop(); pub.stop()
        assert os.path.exists(os.path.join(d, STATUS_FILENAME))


def test_unwritable_dir_does_not_kill_loop():
    pub = _pub('/proc/nope', interval_s=0.05)
    pub._out_dir = '/proc/nope'
    pub._stop.clear()
    import threading, time
    t = threading.Thread(target=pub._loop, daemon=True); t.start()
    time.sleep(0.15); assert t.is_alive(); pub._stop.set(); t.join(1)


def test_lane_state_is_down_degraded_or_ok():
    from voice.status import _lane
    assert _lane(False, 'x')['state'] == 'down'
    assert _lane(True, '0/20 cached')['state'] == 'degraded'
    assert _lane(True)['state'] == 'ok'


def test_every_published_lane_has_state():
    with tempfile.TemporaryDirectory() as d:
        lanes = _pub(d).publish_once()['lanes']
    assert all(l['state'] in ('ok', 'degraded', 'down') for l in lanes.values())


# ── camera link lane ────────────────────────────────────────────────
class _Faces:
    def __init__(self, **st): self._st = st
    def state(self): return dict(self._st)


def test_no_faces_lane_unless_a_face_watcher_is_configured():
    with tempfile.TemporaryDirectory() as d:
        assert 'faces' not in _pub(d).publish_once()['lanes']


def test_faces_lane_reports_a_live_link():
    with tempfile.TemporaryDirectory() as d:
        lane = _pub(d, faces=_Faces(state='running', enabled=True, greeted=2,
                                    last_greeted_at=5.0)).publish_once()['lanes']['faces']
    assert lane['state'] == 'ok' and lane['greeted'] == 2 and lane['greeter'] is True


def test_faces_lane_is_down_when_the_dashboard_is_not_publishing():
    with tempfile.TemporaryDirectory() as d:
        lane = _pub(d, faces=_Faces(state='no_data', enabled=False)).publish_once()['lanes']['faces']
    assert lane['state'] == 'down' and 'dashboard' in lane['reason']


def test_faces_lane_is_down_when_the_camera_is_offline():
    with tempfile.TemporaryDirectory() as d:
        lane = _pub(d, faces=_Faces(state='camera_offline', enabled=True)).publish_once()['lanes']['faces']
    assert lane['state'] == 'down' and 'offline' in lane['reason']


def test_faces_lane_is_not_a_fault_while_the_greeter_is_simply_off():
    with tempfile.TemporaryDirectory() as d:
        lane = _pub(d, faces=_Faces(state='idle', enabled=False)).publish_once()['lanes']['faces']
    assert lane['state'] == 'ok'


def test_faces_lane_never_publishes_names():
    with tempfile.TemporaryDirectory() as d:
        raw = json.dumps(_pub(d, faces=_Faces(state='running', enabled=True, greeted=1,
                                              last_greeted_at=1.0, name='Rushabh')).publish_once())
    assert 'Rushabh' not in raw


def test_tts_lane_does_not_count_name_events_as_missing_clips():
    from voice._constants import DYNAMIC_EVENTS, PERSONA_EVENTS
    class Engine:
        cache = {}
        synth_available = True
        _playback = type('P', (), {'_tool': 'aplay'})()
    with tempfile.TemporaryDirectory() as d:
        lane = _pub(d, tts=Engine()).publish_once()['lanes']['tts']
    from voice.record_phrases import phrase_text, plan
    planned = plan()
    # Count exactly what the recorder can produce, so a full cache never
    # reads as missing clips. The plan also holds vocabulary stems
    # (num_4, unit_meters), so it is no longer bounded by PERSONA_EVENTS.
    assert lane['expected'] == len(planned)
    # Name-carrying events cannot be pre-recorded — a clip would play
    # without the name — so they must never enter the expected count.
    assert not (DYNAMIC_EVENTS & planned.keys())
    # And every persona event that *can* be pre-recorded is counted.
    recordable = {e for e in PERSONA_EVENTS - DYNAMIC_EVENTS
                  if phrase_text(e) is not None}
    assert recordable and recordable <= planned.keys()


if __name__ == '__main__':
    _env.run_tests(globals())
