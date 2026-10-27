"""Rover state reader, responder, and the Asker — and above all, honesty.

Tarka must never say something untrue: no invented battery level, no old
sensor values presented as current, no dead LiDAR reported as an empty path,
and no "Moving." / "Stopped." for a command that did nothing.
"""
import _env  # noqa: F401

import json
import os
import random
import re
import tempfile
from types import SimpleNamespace

from voice._constants import PERSONA_EVENTS
from voice.ask import Asker
from voice.intents import interpret, parse_rules
from voice.respond import respond, status_report
from voice.rover_state import RoverState, read_rover_state
from voice.speech_plan import PlanSpeaker
from voice.telemetry import VoiceTelemetry
from voice.vocab import VOCAB

LIVE = dict(fresh=True, enabled=False, mode='auto', camera_connected=True, lidar_enabled=True,
            lidar_connected=True, lidar_age_ms=80, lidar_min_front_m=1.4, lidar_action='CLEAR')


def S(**over):
    return RoverState(**{**LIVE, **over})


def say(text, state=None):
    return respond(parse_rules(text), state or S()).text


# ── rover state reader ──────────────────────────────────────────────


def _dir(status=None, control=None):
    d = tempfile.mkdtemp()
    for name, data in (('status.json', status), ('control.json', control)):
        if data is not None:
            open(os.path.join(d, name), 'w').write(data if isinstance(data, str) else json.dumps(data))
    return d


def test_fresh_status_is_read():
    now = 1000.0
    d = _dir({'updated_at': now - 1, 'running': True, 'lidar_enabled': True, 'lidar_connected': True,
              'lidar_age_ms': 50, 'lidar_min_front_m': 2.5, 'lidar_action': 'CLEAR',
              'camera_connected': True}, {'enabled': True, 'mode': 'manual'})
    s = read_rover_state(d, clock=lambda: now)
    assert s.fresh and s.enabled is True and s.mode == 'manual' and s.lidar_live
    assert s.lidar_min_front_m == 2.5 and s.camera_connected is True


def test_stale_missing_or_corrupt_status_is_not_fresh():
    now = 1000.0
    assert not read_rover_state(_dir({'updated_at': now - 60}), clock=lambda: now).fresh
    assert not read_rover_state(_dir(), clock=lambda: now).fresh
    assert not read_rover_state(_dir('{broken'), clock=lambda: now).fresh
    assert not read_rover_state(_dir({'updated_at': 'soon'}), clock=lambda: now).fresh
    assert not read_rover_state(_dir({'updated_at': now, 'running': False}), clock=lambda: now).fresh


def test_garbage_values_become_unknown_not_wrong():
    s = read_rover_state(_dir({'updated_at': 1000.0, 'lidar_min_front_m': 'far', 'lidar_age_ms': True,
                               'camera_connected': 'yes', 'lidar_action': 'EXPLODE'},
                              {'enabled': 'maybe', 'mode': 'warp'}), clock=lambda: 1000.0)
    assert s.lidar_min_front_m is None and s.lidar_age_ms is None and s.camera_connected is None
    assert s.lidar_action is None and s.enabled is None and s.mode is None


def test_a_stalled_lidar_is_not_live():
    assert S().lidar_live
    assert not S(lidar_age_ms=5000).lidar_live
    assert not S(lidar_age_ms=None).lidar_live
    assert not S(lidar_connected=False).lidar_live


# ── honesty ─────────────────────────────────────────────────────────


def test_battery_is_never_a_number():
    for text in ('what is the battery level', 'how much battery is left', 'battery'):
        t = say(text)
        assert t == "I can't measure the battery." and not re.search(r'\d|percent', t), t


def test_a_motion_or_stop_command_is_never_answered_as_if_it_happened():
    for text in ('go forward two meters', 'stop', 'turn left ninety degrees', 'emergency stop',
                 'back up one meter'):
        t = say(text)
        assert t == "Voice control isn't switched on. Use the dashboard.", (text, t)
        assert not re.search(r'moving|stopped|done|confirmed|going', t, re.I), t


def test_stale_telemetry_is_never_read_out_as_current():
    stale = S(fresh=False, lidar_min_front_m=0.3, enabled=True)
    for text in ('status', 'how far is the nearest obstacle', 'what mode are you in'):
        assert say(text, stale) == "I can't reach the controller.", text


def test_a_dead_lidar_is_offline_not_clear():
    for dead in (S(lidar_age_ms=9000, lidar_min_front_m=None), S(lidar_connected=False, lidar_min_front_m=None),
                 S(lidar_age_ms=None, lidar_min_front_m=None)):
        for text in ('how far is the nearest obstacle', 'status'):
            t = say(text, dead)
            assert 'offline' in t and 'clear' not in t.lower() and 'Nothing in front' not in t, (text, t)


def test_lidar_safety_switched_off_says_so():
    t = say('how far is the nearest obstacle', S(lidar_enabled=False))
    assert t == 'LiDAR safety is off.'
    assert 'LiDAR safety is off.' in say('status', S(lidar_enabled=False))


def test_position_is_not_invented():
    assert say('where are you') == "I don't track my position."


# ── answers ─────────────────────────────────────────────────────────


def test_status_report_reads_the_real_state():
    assert say('status') == 'Motors off. Auto mode. Obstacle ahead at one point four meters. Camera is online.'
    assert say('status', S(enabled=True, mode='manual', lidar_min_front_m=None, camera_connected=False)) \
        == 'Motors are on. Manual mode. LiDAR is clear. Camera is offline.'


def test_obstacle_answers():
    assert say('how far is the nearest obstacle') == 'Obstacle ahead at one point four meters.'
    assert say('how far is the nearest obstacle', S(lidar_min_front_m=0.31)) == \
        'Obstacle ahead at thirty centimeters.'
    assert say('is anything in front of you', S(lidar_min_front_m=None)) == 'Nothing in front of me.'
    assert say('is anything in front of you', S(lidar_action='ESTOP', lidar_min_front_m=0.31)) == \
        'Obstacle ahead. Stopping. Obstacle ahead at thirty centimeters.'
    assert say('is anything in front of you', S(lidar_action='SLOW', lidar_min_front_m=0.8)).startswith(
        'Slowing down. Obstacle nearby.')


def test_mode_help_and_clarifications():
    assert say('what mode are you in') == 'Motors off. Auto mode.'
    assert say('who are you') == "I'm Tarka. Say stop at any time. I can move, stop, and report status."
    assert say('go forward') == 'Distance?'
    assert say('disable lidar') == "I can't do that from voice. Please use the dashboard."
    assert say('sing me a song') == "Sorry, I didn't catch that."


def test_unknown_action_is_a_clarification_not_a_crash():
    from voice.intent import Intent
    from voice.intents import Interpretation
    r = Interpretation('intent', Intent('unknown', {}, 0.0))
    assert respond(r, S()).text == "Sorry, I didn't catch that."


def test_every_clip_a_response_can_need_is_a_known_clip():
    """So the offline path is always available: every key is a persona event or vocab."""
    rng = random.Random(7)
    for _ in range(400):
        s = RoverState(fresh=rng.random() < .8, enabled=rng.choice([None, True, False]),
                       mode=rng.choice([None, 'auto', 'manual']),
                       camera_connected=rng.choice([None, True, False]),
                       lidar_enabled=rng.choice([None, True, False]),
                       lidar_connected=rng.choice([None, True, False]),
                       lidar_age_ms=rng.choice([None, 10, 5000]),
                       lidar_min_front_m=rng.choice([None, 0.0, 0.05, 0.4, 1.0, 3.3, 55.0, 150.0]),
                       lidar_action=rng.choice([None, 'CLEAR', 'SLOW', 'ESTOP']))
        for text in ('status', 'how far is the nearest obstacle', 'what mode are you in', 'who are you'):
            plan = respond(parse_rules(text), s)
            assert plan, (text, s)
            for key in plan.keys:
                assert key in PERSONA_EVENTS or key in VOCAB, key


def test_status_report_never_raises_on_any_state():
    assert status_report(RoverState()).text == "I can't reach the controller."


# ── the Asker ───────────────────────────────────────────────────────


class FakeSpeaker:
    def __init__(self, played=True): self.said, self.played = [], played

    def say(self, plan, event='answer'):
        self.said.append((plan.text, event))
        return SimpleNamespace(played=self.played, source='cache' if self.played else 'none', text=plan.text)


class FakeStt:
    def __init__(self, text=None, unavailable=False):
        self.text, self.unavailable, self.calls = text, unavailable, []

    def hear(self, seconds):
        self.calls.append(seconds)
        return SimpleNamespace(text=self.text, confidence=0.99) if self.text else None


def _asker(stt=None, speaker=None, state=None, tm=None):
    return Asker(stt or FakeStt('status'), speaker or FakeSpeaker(), lambda: state or S(), tm)


def test_ask_listens_understands_and_answers():
    spk, stt = FakeSpeaker(), FakeStt('how far is the nearest obstacle')
    out = _asker(stt, spk).ask(4)
    assert stt.calls == [4] and spk.said == [('Obstacle ahead at one point four meters.', 'answer')]
    assert "heard 'how far is the nearest obstacle'" in out and 'Obstacle ahead at one point' in out


def test_ask_with_nothing_heard_says_sorry_not_silence():
    spk = FakeSpeaker()
    out = _asker(FakeStt(None), spk).ask(4)
    assert spk.said[0][0] == "Sorry, I didn't catch that." and 'nothing usable' in out


def test_ask_without_a_working_microphone_says_nothing_misleading():
    spk = FakeSpeaker()
    out = _asker(FakeStt('status', unavailable=True), spk).ask(4)
    assert 'unavailable' in out and spk.said == []


def test_ask_reports_when_the_answer_could_not_be_spoken():
    out = _asker(FakeStt('status'), FakeSpeaker(played=False)).ask(4)
    assert 'could not speak' in out


def test_report_speaks_the_status_without_a_microphone():
    spk, stt = FakeSpeaker(), FakeStt('x')
    out = _asker(stt, spk).report()
    assert stt.calls == [] and spk.said[0][0].startswith('Motors off. Auto mode.') and 'said:' in out


def test_rules_decisions_are_listed_but_never_pollute_llm_latency():
    tm = VoiceTelemetry()
    a = _asker(tm=tm)
    for text in ('status', 'go forward two meters', 'disable lidar', 'go forward', 'sing me a song'):
        a.answer(text)
    snap = tm.snapshot()
    assert [r['outcome'] for r in reversed(snap['parses'])] == \
        ['accepted', 'accepted', 'refused', 'clarify', 'not_understood']
    assert all(r['lane'] == 'rules' for r in snap['parses'])
    assert snap['latency']['parse_ms']['n'] == 0 and snap['latency']['parse_ms']['p50'] is None


def test_a_driving_command_is_declined_through_the_asker_too():
    spk = FakeSpeaker()
    _asker(FakeStt('go forward two meters'), spk).ask(4)
    assert spk.said[0][0] == "Voice control isn't switched on. Use the dashboard."


def test_broken_telemetry_never_stops_an_answer():
    class Boom:
        def record_parse(self, *a, **k): raise RuntimeError('x')
    spk = FakeSpeaker()
    _asker(speaker=spk, tm=Boom()).answer('status')
    assert spk.said


if __name__ == '__main__':
    _env.run_tests(globals())
