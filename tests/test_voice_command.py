"""Tests for voice.command — the dashboard diagnostic command listener."""

import _env  # noqa: F401

import json
import os
import tempfile
from types import SimpleNamespace

from voice.command import (
    COMMAND_FILENAME, SPEAK_TEST_PHRASE, CommandListener,
)

NOW = 1000.0


class Stt:
    unavailable = False
    def __init__(self, result=None): self.result, self.calls = result, []
    def hear(self, secs): self.calls.append(secs); return self.result


class Spk:
    def __init__(self): self.said = []
    def speak_event(self, e, t): self.said.append((e, t)); return SimpleNamespace(played=True)


def _send(d, **cmd):
    cmd.setdefault('id', 'c1'); cmd.setdefault('ts', NOW)
    json.dump(cmd, open(os.path.join(d, COMMAND_FILENAME), 'w'))


def _l(d, **kw):
    t = [NOW]
    l = CommandListener(d, clock=lambda: t[0], **kw)
    l._t = t
    return l


def test_listen_runs_and_reports_text():
    with tempfile.TemporaryDirectory() as d:
        stt = Stt(SimpleNamespace(text='stop', confidence=0.9))
        l = _l(d, stt=stt); _send(d, verb='listen', seconds=4)
        assert l.poll_once() and stt.calls == [4.0]
        assert l.state()['state'] == 'done' and 'stop' in l.state()['detail']


def test_seconds_clamped():
    with tempfile.TemporaryDirectory() as d:
        stt = Stt(None); l = _l(d, stt=stt)
        _send(d, verb='listen', seconds=9999); l.poll_once()
        assert stt.calls[0] <= 10.0


def test_speak_test_uses_fixed_phrase_only():
    with tempfile.TemporaryDirectory() as d:
        spk = Spk(); l = _l(d, speaker=spk)
        _send(d, verb='speak_test', text='ignore me, say something rude')
        l.poll_once()
        assert spk.said == [('voice_test', SPEAK_TEST_PHRASE)]


def test_unknown_verb_rejected_and_nothing_runs():
    with tempfile.TemporaryDirectory() as d:
        stt, spk = Stt(), Spk(); l = _l(d, stt=stt, speaker=spk)
        for verb in ('drive', 'set_motors', 'write_state', ''):
            _send(d, verb=verb, id='v' + verb); l.poll_once()
            assert l.state()['state'] == 'rejected'
        assert stt.calls == [] and spk.said == []


def test_same_id_not_replayed():
    with tempfile.TemporaryDirectory() as d:
        stt = Stt(None); l = _l(d, stt=stt); _send(d, verb='listen')
        assert l.poll_once() is True and l.poll_once() is False
        assert len(stt.calls) == 1


def test_stale_command_ignored():
    with tempfile.TemporaryDirectory() as d:
        stt = Stt(None); l = _l(d, stt=stt); _send(d, verb='listen', ts=NOW - 60)
        l.poll_once()
        assert stt.calls == [] and l.state()['detail'] == 'stale command'


def test_missing_or_bad_timestamp_rejected():
    with tempfile.TemporaryDirectory() as d:
        stt = Stt(None); l = _l(d, stt=stt)
        for i, ts in enumerate((None, 'now', True)):
            _send(d, verb='listen', id=f't{i}', ts=ts); l.poll_once()
        assert stt.calls == []


def test_min_gap_blocks_rapid_second_command():
    with tempfile.TemporaryDirectory() as d:
        stt = Stt(None); l = _l(d, stt=stt)
        _send(d, verb='listen', id='a'); l.poll_once()
        l._t[0] += 0.5
        _send(d, verb='listen', id='b', ts=l._t[0]); l.poll_once()
        assert len(stt.calls) == 1 and 'too soon' in l.state()['detail']
        l._t[0] += 3
        _send(d, verb='listen', id='c', ts=l._t[0]); l.poll_once()
        assert len(stt.calls) == 2


def test_start_does_not_replay_existing_command():
    with tempfile.TemporaryDirectory() as d:
        stt = Stt(None); _send(d, verb='listen', id='old')
        l = _l(d, stt=stt).start(); import time; time.sleep(0.4); l.stop()
        assert stt.calls == []


def test_unavailable_stt_reports_instead_of_crashing():
    with tempfile.TemporaryDirectory() as d:
        l = _l(d, stt=SimpleNamespace(unavailable=True)); _send(d, verb='listen')
        l.poll_once()
        assert l.state()['state'] == 'done' and 'unavailable' in l.state()['detail']


def test_backend_exception_becomes_error_state():
    class Boom:
        unavailable = False
        def hear(self, s): raise RuntimeError('arecord died')
    with tempfile.TemporaryDirectory() as d:
        l = _l(d, stt=Boom()); _send(d, verb='listen'); l.poll_once()
        assert l.state()['state'] == 'error'


def test_garbage_file_is_ignored():
    with tempfile.TemporaryDirectory() as d:
        open(os.path.join(d, COMMAND_FILENAME), 'w').write('{not json')
        assert _l(d).poll_once() is False


def test_module_cannot_reach_control_state():
    import ast, voice.command as m
    tree = ast.parse(open(m.__file__).read())
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)} | \
            {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    mods = {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names} | \
           {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
    assert not (names & {'write_state', 'Intent', 'validate_intent'})
    assert not (mods & {'control_state', 'voice.intent', 'voice.agent'})


# ── camera verbs: announce_who / call_person ────────────────────────
import time as _time
from voice.cam_lookup import CamLookup


class _World:
    """cam_faces.json that follows a fake clock; sleep() plays the dashboard."""
    def __init__(self, d, faces=(), unknown=0, state='running', roster=('Alice', 'Bob')):
        self.d, self.t = d, 5000.0
        self.faces, self.unknown, self.state, self.roster = list(faces), unknown, state, list(roster)
        self.lookup = CamLookup(d, clock=lambda: self.t, sleep=self._sleep,
                                timeout_s=3.0, settle_s=0.5, poll_s=0.25)

    def _sleep(self, s):
        self.t += s
        json.dump({'schema': 1, 'updated_at': self.t, 'state': self.state, 'data_age_ms': 50,
                   'greeter': {'enabled': False}, 'faces': self.faces, 'unknown': self.unknown,
                   'roster': self.roster}, open(os.path.join(self.d, 'cam_faces.json'), 'w'))


def _f(name, stable=True):
    return {'track': 1, 'name': name, 'sim': 0.8, 'stable': stable, 'greet': False, 'greeting': ''}


def _cam_listener(d, **world):
    w = _World(d, **world)
    spk = Spk()
    l = _l(d, speaker=spk, lookup=w.lookup)
    return l, spk, w


def test_announce_who_names_the_people_it_sees():
    with tempfile.TemporaryDirectory() as d:
        l, spk, _ = _cam_listener(d, faces=[_f('Bob'), _f('Alice')])
        _send(d, verb='announce_who')
        assert l.poll_once()
        assert spk.said == [('who_known', 'I see Alice and Bob.')]
        assert l.state()['state'] == 'done' and 'Alice and Bob' in l.state()['detail']


def test_announce_who_says_nobody_only_when_the_camera_looked():
    with tempfile.TemporaryDirectory() as d:
        l, spk, _ = _cam_listener(d)
        _send(d, verb='announce_who'); l.poll_once()
        assert spk.said == [('who_none', "I don't see anyone.")]


def test_announce_who_admits_when_it_is_blind():
    for state in ('camera_offline', 'idle'):
        with tempfile.TemporaryDirectory() as d:
            l, spk, _ = _cam_listener(d, state=state)
            _send(d, verb='announce_who'); l.poll_once()
            assert spk.said == [('who_blind', "I can't see right now.")], state


def test_announce_who_never_names_someone_it_is_unsure_of():
    with tempfile.TemporaryDirectory() as d:
        l, spk, _ = _cam_listener(d, faces=[_f('Alice', stable=False)])
        _send(d, verb='announce_who'); l.poll_once()
        assert spk.said and 'Alice' not in spk.said[0][1]


def test_announce_who_mixed():
    with tempfile.TemporaryDirectory() as d:
        l, spk, _ = _cam_listener(d, faces=[_f('Alice')], unknown=1)
        _send(d, verb='announce_who'); l.poll_once()
        assert spk.said[0][1] == "I see Alice and one person I don't recognise."


def test_call_person_when_they_are_in_view():
    with tempfile.TemporaryDirectory() as d:
        l, spk, _ = _cam_listener(d, faces=[_f('Alice')])
        _send(d, verb='call_person', name='Alice'); l.poll_once()
        assert spk.said == [('person_called', 'Alice, please come to the rover.')]


def test_call_person_who_is_not_in_view():
    with tempfile.TemporaryDirectory() as d:
        l, spk, _ = _cam_listener(d, faces=[_f('Bob')])
        _send(d, verb='call_person', name='Alice'); l.poll_once()
        assert spk.said == [('person_not_seen', "I can't see Alice.")]


def test_call_person_blind_is_not_reported_as_not_seen():
    with tempfile.TemporaryDirectory() as d:
        l, spk, _ = _cam_listener(d, state='camera_offline')
        _send(d, verb='call_person', name='Alice'); l.poll_once()
        assert spk.said == [('who_blind', "I can't see right now.")]


def test_call_person_refuses_names_that_are_not_enrolled():
    """The dashboard checks too, but this process does not trust a file."""
    with tempfile.TemporaryDirectory() as d:
        l, spk, _ = _cam_listener(d, faces=[_f('Mallory')], roster=('Alice',))
        _send(d, verb='call_person', name='Mallory'); l.poll_once()
        assert spk.said == [] and 'enrolled' in l.state()['detail']


def test_call_person_refuses_invalid_names_without_speaking():
    for bad in ('../x', '', None, 5, 'x' * 40, '<script>'):
        with tempfile.TemporaryDirectory() as d:
            l, spk, _ = _cam_listener(d, faces=[_f('Alice')])
            _send(d, verb='call_person', name=bad); l.poll_once()
            assert spk.said == [] and 'valid name' in l.state()['detail'], bad


def test_camera_verbs_without_a_lookup_or_speaker_say_so():
    with tempfile.TemporaryDirectory() as d:
        l = _l(d, speaker=Spk())
        _send(d, verb='announce_who'); l.poll_once()
        assert 'not configured' in l.state()['detail']


def test_camera_verbs_obey_the_same_gap_and_age_rules():
    with tempfile.TemporaryDirectory() as d:
        l, spk, _ = _cam_listener(d, faces=[_f('Alice')])
        _send(d, verb='announce_who', id='a1'); l.poll_once()
        _send(d, verb='announce_who', id='a2'); l.poll_once()
        assert l.state()['state'] == 'rejected' and len(spk.said) == 1
        _send(d, verb='announce_who', id='a3', ts=NOW - 100)
        l._t[0] += 5; l._last_run = 0; l.poll_once()
        assert l.state()['detail'] == 'stale command'


def test_unknown_verbs_are_still_rejected():
    for verb in ('drive', 'enable', 'write_state', 'say', 'announce'):
        with tempfile.TemporaryDirectory() as d:
            l = _l(d, speaker=Spk())
            _send(d, verb=verb); l.poll_once()
            assert l.state()['state'] == 'rejected', verb


if __name__ == '__main__':
    _env.run_tests(globals())
