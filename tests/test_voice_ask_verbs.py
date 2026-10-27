"""The ``ask`` and ``report`` dashboard verbs, voice side and web side."""
import _env  # noqa: F401

import json
import os
import tempfile
import time

from voice.command import COMMAND_FILENAME, VERBS, CommandListener

NOW = 1000.0


class FakeAsker:
    def __init__(self): self.calls = []
    def ask(self, secs): self.calls.append(('ask', secs)); return 'heard x'
    def report(self): self.calls.append(('report',)); return 'said: status'


def _send(d, **cmd):
    cmd.setdefault('id', 'c1'); cmd.setdefault('ts', NOW)
    json.dump(cmd, open(os.path.join(d, COMMAND_FILENAME), 'w'))


def _l(d, asker):
    return CommandListener(d, clock=lambda: NOW, asker=asker)


def test_the_new_verbs_are_whitelisted():
    assert {'ask', 'report'} <= VERBS


def test_ask_runs_with_a_clamped_duration():
    with tempfile.TemporaryDirectory() as d:
        a = FakeAsker(); l = _l(d, a)
        _send(d, verb='ask', seconds=9999); l.poll_once()
        assert a.calls == [('ask', 10.0)] and l.state()['detail'] == 'heard x'
        _send(d, verb='ask', id='c2', seconds=-3, ts=NOW + 3); l._clock = lambda: NOW + 3; l.poll_once()
        assert a.calls[-1] == ('ask', 1.0)


def test_ask_has_a_sane_default_and_survives_junk_seconds():
    with tempfile.TemporaryDirectory() as d:
        a = FakeAsker(); l = _l(d, a)
        _send(d, verb='ask', seconds='lots'); l.poll_once()
        assert a.calls == [('ask', 4.0)]


def test_report_runs_without_a_microphone():
    with tempfile.TemporaryDirectory() as d:
        a = FakeAsker(); l = _l(d, a)
        _send(d, verb='report'); l.poll_once()
        assert a.calls == [('report',)] and l.state()['state'] == 'done'


def test_without_an_asker_the_verbs_say_so_instead_of_crashing():
    with tempfile.TemporaryDirectory() as d:
        l = _l(d, None)
        _send(d, verb='ask'); l.poll_once()
        assert l.state()['detail'] == 'assistant not configured'
        _send(d, verb='report', id='c2', ts=NOW + 3); l._clock = lambda: NOW + 3; l.poll_once()
        assert l.state()['detail'] == 'assistant not configured'


def test_an_asker_exception_becomes_an_error_state():
    class Boom:
        def ask(self, s): raise RuntimeError('arecord died')
    with tempfile.TemporaryDirectory() as d:
        l = _l(d, Boom()); _send(d, verb='ask'); l.poll_once()
        assert l.state()['state'] == 'error'


def test_non_whitelisted_verbs_are_still_rejected():
    with tempfile.TemporaryDirectory() as d:
        a = FakeAsker(); l = _l(d, a)
        for v in ('drive', 'move', 'stop', 'say', 'write_state'):
            _send(d, verb=v, id='v' + v); l.poll_once()
            assert l.state()['state'] == 'rejected', v
        assert a.calls == []


# ── web side ────────────────────────────────────────────────────────

import web  # noqa: E402

AUTH = {'X-AGV-Password': 'testpw'}
STATUS = str(web.VOICE_STATUS_PATH)
COMMAND = str(web.VOICE_COMMAND_PATH)


def _running():
    json.dump({'schema': 1, 'updated_at': time.time(), 'pid': 1, 'stale_after_s': 10, 'lanes': {}},
              open(STATUS, 'w'))
    if os.path.exists(COMMAND):
        os.unlink(COMMAND)


def _post(body, headers=AUTH):
    return web.flask_app.test_client().post('/api/voice/command', json=body, headers=headers)


def test_web_accepts_ask_and_report():
    assert {'ask', 'report'} <= set(web.VOICE_VERBS)
    _running()
    assert _post({'verb': 'report'}).status_code == 202
    assert json.load(open(COMMAND)) .keys() == {'id', 'verb', 'ts'}
    assert _post({'verb': 'ask', 'seconds': 99}).status_code == 202
    cmd = json.load(open(COMMAND))
    assert cmd['verb'] == 'ask' and cmd['seconds'] == 10.0


def test_web_ask_needs_the_password_and_a_running_voice_process():
    _running()
    assert _post({'verb': 'ask'}, headers={}).status_code == 403
    assert _post({'verb': 'ask', 'seconds': 'x'}).status_code == 400
    os.unlink(STATUS)
    assert _post({'verb': 'ask'}).status_code == 503


def test_web_ask_carries_no_free_text():
    _running()
    _post({'verb': 'report', 'text': 'say something rude', 'name': 'x'})
    assert set(json.load(open(COMMAND))) == {'id', 'verb', 'ts'}


if __name__ == '__main__':
    _env.run_tests(globals())
