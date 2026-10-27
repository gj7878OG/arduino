"""control_state record_mode + /api/record mode (isolated temp control.json)."""
import _env  # noqa: F401  (must be first)
import json
import os

import config
import control_state
import web

assert control_state.CONTROL_PATH.startswith(_env.TMP), control_state.CONTROL_PATH


def _tmp(name):
    d = os.path.join(_env.TMP, name)
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, 'control.json')


def test_old_file_without_key():
    p = _tmp('old')
    json.dump({'enabled': False, 'record': True, 'record_rate': 5}, open(p, 'w'))
    st = control_state.read_state(p)
    assert st['record_mode'] == config.REC_MODE_DEFAULT == 'continuous'
    assert control_state.read_state(_tmp('missing'))['record_mode'] == 'continuous'


def test_round_trip():
    p = _tmp('rt')
    assert control_state.write_state(record_mode='triggered', path=p)['record_mode'] == 'triggered'
    assert control_state.read_state(p)['record_mode'] == 'triggered'
    control_state.write_state(record=True, path=p)          # other writes keep it
    assert control_state.read_state(p)['record_mode'] == 'triggered'
    control_state.write_state(record_mode='continuous', path=p)
    assert control_state.read_state(p)['record_mode'] == 'continuous'


def test_invalid_gives_default():
    p = _tmp('bad')
    json.dump({'record_mode': 'bogus'}, open(p, 'w'))
    assert control_state.read_state(p)['record_mode'] == 'continuous'
    control_state.write_state(record_mode='triggered', path=p)
    assert control_state.write_state(record_mode='nope', path=p)['record_mode'] == 'continuous'
    assert control_state.read_state(p)['record_mode'] == 'continuous'


def test_api_record_mode():
    c = web.flask_app.test_client()
    old = ('record', 'rate', 'views', 'rec_on', 'rec_session', 'rec_frames', 'rec_problems', 'rec_marks',
           'rec_dropped', 'rec_mb', 'rec_elapsed_s', 'rec_free_gb', 'rec_stop_reason')
    d = c.get('/api/record').get_json()
    assert all(k in d for k in old), d
    assert all(k in d for k in ('mode', 'rec_format', 'rec_mode', 'rec_shard', 'rec_ring_s'))
    assert d['mode'] == 'continuous'
    r = c.post('/api/record', json={'action': 'start', 'rate': 5, 'mode': 'triggered'})
    assert r.status_code == 200 and r.get_json()['mode'] == 'triggered'
    assert c.get('/api/record').get_json()['mode'] == 'triggered'
    assert json.load(open(control_state.CONTROL_PATH))['record_mode'] == 'triggered'
    assert c.post('/api/record', json={'action': 'stop'}).status_code == 200
    # start without mode keeps the previous one
    r = c.post('/api/record', json={'action': 'start', 'rate': 5}).get_json()
    assert r['mode'] == 'triggered' and r['record'] is True
    r = c.post('/api/record', json={'action': 'start', 'rate': 5, 'mode': 'junk'}).get_json()
    assert r['mode'] == 'continuous'
    r = c.post('/api/record', json={'action': 'stop'})
    assert r.status_code == 200 and r.get_json()['record'] is False


def test_closed_loop_is_a_preference_not_a_command():
    """Untick the encoders, press STOP / AUTO, and it must stay unticked.

    This used to snap back to the config default on every stop and every
    mode change, so the operator's choice was silently re-armed under them
    on the next run. The manual drive command still has to be cleared —
    only the standing preference survives.
    """
    p = _tmp('loop')
    control_state.write_state(closed_loop=False, path=p)
    assert control_state.read_state(p)['closed_loop'] is False

    for kw in ({'enabled': False}, {'enabled': True}, {'mode': 'manual'},
               {'mode': 'auto'}, {'enabled': False, 'mode': 'auto'}):
        st = control_state.write_state(path=p, **kw)
        assert st['closed_loop'] is False, f'{kw} re-armed the encoder PID'

    # a fresh process reads the same choice back off disk
    assert control_state.read_state(p)['closed_loop'] is False
    # ... and it still toggles when the operator asks
    assert control_state.write_state(closed_loop=True, path=p)['closed_loop'] is True


def test_stop_still_zeroes_the_manual_command():
    """The reset that closed_loop used to ride along with is still there."""
    p = _tmp('loop_stop')
    control_state.write_state(closed_loop=False, manual_lr=(0.5, -0.5), raw=True, path=p)
    st = control_state.write_state(enabled=False, path=p)
    assert st['manual_l'] is None and st['manual_r'] is None
    assert st['manual_raw'] is False
    assert st['closed_loop'] is False


def test_status_reports_saved_choice_without_the_agv():
    """With only the dashboard running there is no AGV to publish the wheel
    control choice, so the status the WebSocket pushes must read it out of
    control.json. Without that the tick box falls back to true and re-ticks
    itself within 2 s of the operator unticking it."""
    control_state.write_state(closed_loop=False)
    assert web._read_status()['closed_loop_wanted'] is False
    control_state.write_state(closed_loop=True)
    assert web._read_status()['closed_loop_wanted'] is True


def test_status_prefers_the_running_agv_value():
    """A running AGV publishes the choice itself; that wins over the file,
    so the two can never disagree once telemetry is live."""
    control_state.write_state(closed_loop=False)
    json.dump({'closed_loop_wanted': True}, open(web.STATUS_PATH, 'w'))
    assert web._read_status()['closed_loop_wanted'] is True
    os.remove(web.STATUS_PATH)


if __name__ == '__main__':
    _env.run_tests(globals())
