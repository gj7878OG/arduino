"""Tests for voice.cam_lookup — reading cam_faces.json and asking the camera to look.

The file crosses a process boundary, so it is treated as untrusted: a corrupt or
hostile file must not make the rover say something it was never meant to, and a
camera that cannot see must never be reported as "nobody is there".
"""
import _env  # noqa: F401  (must be first)
import json
import os
import tempfile

from voice import cam_lookup as cl
from voice.cam_lookup import CamLookup, clean_text, describe, parse_faces, valid_name

NOW = 1000.0


def _face(name='Alice', sim=0.7, stable=True, greet=True, greeting=''):
    return {'track': 1, 'name': name, 'sim': sim, 'stable': stable, 'greet': greet,
            'greeting': greeting}


def _raw(**over):
    raw = {'schema': 1, 'updated_at': NOW, 'state': 'running', 'data_age_ms': 100,
           'greeter': {'enabled': True, 'min_sim': 0.5, 'cooldown_s': 300, 'greet_unknown': False},
           'faces': [_face()], 'unknown': 0, 'roster': ['Alice', 'Bob']}
    raw.update(over)
    return raw


# ── parsing hostile input ───────────────────────────────────────────
def test_good_file_parses():
    v = parse_faces(_raw(), NOW)
    assert v.ok and v.faces[0]['name'] == 'Alice' and v.roster == ('Alice', 'Bob')
    assert v.greeter['enabled'] is True


def test_garbage_is_no_data():
    for raw in (None, [], 'x', 5, {}, {'schema': 2}, _raw(schema='1')):
        assert parse_faces(raw, NOW).state == 'no_data', raw


def test_stale_or_future_file_is_no_data():
    """The dashboard stopped publishing: the last picture is not the current one."""
    assert parse_faces(_raw(updated_at=NOW - 10), NOW).state == 'no_data'
    assert parse_faces(_raw(updated_at=NOW + 10), NOW).state == 'no_data'
    assert parse_faces(_raw(updated_at=True), NOW).state == 'no_data'


def test_unknown_state_is_no_data():
    assert parse_faces(_raw(state='sleeping'), NOW).state == 'no_data'


def test_only_a_running_camera_counts_as_looking():
    for state in ('idle', 'camera_offline'):
        v = parse_faces(_raw(state=state), NOW)
        assert v.state == state and not v.ok


def test_old_analysis_downgrades_running_to_idle():
    assert parse_faces(_raw(data_age_ms=9000), NOW).state == 'idle'
    assert parse_faces(_raw(data_age_ms=None), NOW).state == 'idle'


def test_invalid_names_are_dropped():
    """A name ends up in speech: nothing path-like, markup-like or oversized."""
    bad = ['../x', 'a/b', '<b>', '', 'x' * 40, None, 7, '.hidden']
    v = parse_faces(_raw(faces=[_face(n) for n in bad] + [_face('Ok Name')]), NOW)
    assert [f['name'] for f in v.faces] == ['Ok Name']


def test_greeting_text_is_sanitised():
    v = parse_faces(_raw(faces=[_face(greeting='Hi\n\x07there\x1b' + 'x' * 200)]), NOW)
    g = v.faces[0]['greeting']
    assert '\n' not in g and '\x07' not in g and '\x1b' not in g and len(g) <= 80


def test_flags_must_be_real_booleans():
    f = _face()
    f.update(stable='yes', greet=1)
    v = parse_faces(_raw(faces=[f]), NOW)
    assert v.faces[0]['stable'] is False and v.faces[0]['greet'] is False
    g = parse_faces(_raw(greeter={'enabled': 'true'}), NOW).greeter
    assert g['enabled'] is False


def test_thresholds_are_clamped_so_a_bad_file_cannot_lower_the_bar():
    g = parse_faces(_raw(greeter={'enabled': True, 'min_sim': 0.0, 'cooldown_s': 0}), NOW).greeter
    assert g['min_sim'] == 0.40 and g['cooldown_s'] == 30.0
    g = parse_faces(_raw(greeter={'enabled': True, 'min_sim': 'low', 'cooldown_s': float('nan')}), NOW).greeter
    assert g['min_sim'] == 0.5 and g['cooldown_s'] == 300.0


def test_negative_or_odd_unknown_counts_are_zero():
    for bad in (-3, 'two', None, True, 2.5):
        assert parse_faces(_raw(unknown=bad), NOW).unknown == 0, bad


def test_valid_name_and_clean_text():
    assert valid_name('Rushabh') and not valid_name('../etc') and not valid_name(None)
    assert clean_text('a\x00b') == 'a b' and clean_text(3) == ''


# ── describing what is in view ──────────────────────────────────────
def _v(faces=(), unknown=0, state='running'):
    return parse_faces(_raw(faces=list(faces), unknown=unknown, state=state), NOW)


def test_blind_states_never_say_nobody():
    for state in ('camera_offline', 'idle'):
        assert describe(_v(state=state))[0] == 'who_blind'
    assert describe(cl.FacesView())[0] == 'who_blind'


def test_nobody():
    assert describe(_v()) == ('who_none', {})


def test_known_people_are_named_sorted_and_joined():
    ev, slots = describe(_v([_face('Bob'), _face('Alice')]))
    assert ev == 'who_known' and slots == {'names': 'Alice and Bob'}
    assert describe(_v([_face('C'), _face('A'), _face('B')]))[1]['names'] == 'A, B and C'


def test_strangers_are_counted_in_words():
    assert describe(_v(unknown=1)) == ('who_strangers', {'others': 'one person'})
    assert describe(_v(unknown=3))[1]['others'] == 'three people'
    assert describe(_v(unknown=12))[1]['others'] == '12 people'


def test_mixed():
    ev, slots = describe(_v([_face('Alice')], unknown=2))
    assert ev == 'who_mixed' and slots == {'names': 'Alice', 'others': 'two people'}


def test_unstable_name_is_not_said_it_counts_as_someone_unsure():
    """Seen once, not yet confirmed: 'a person I don't recognise', never the name."""
    ev, slots = describe(_v([_face('Alice', stable=False)]))
    assert ev == 'who_strangers' and 'Alice' not in json.dumps(slots)


# ── CamLookup ───────────────────────────────────────────────────────
class World:
    """A fake clock whose sleep() also plays the dashboard's part."""

    def __init__(self, script):
        self.d = tempfile.mkdtemp(dir=_env.TMP)
        self.t = NOW
        self.script = script
        self.lookup = CamLookup(self.d, clock=lambda: self.t, sleep=self._sleep,
                                timeout_s=6.0, settle_s=1.5, poll_s=0.25)

    def _sleep(self, s):
        self.t += s
        raw = self.script(self.t)
        if raw is not None:
            json.dump(raw, open(os.path.join(self.d, 'cam_faces.json'), 'w'))

    def watch(self):
        return json.load(open(os.path.join(self.d, 'cam_watch.json')))


def test_look_asks_the_camera_to_wake():
    w = World(lambda t: _raw(updated_at=t))
    w.lookup.look('announce_who')
    req = w.watch()
    assert req['id'] and req['ts'] == NOW and 1 <= req['ttl_s'] <= 30
    assert req['reason'] == 'announce_who'


def test_look_waits_for_a_settled_view_before_saying_nobody():
    seen = []
    w = World(lambda t: seen.append(t) or _raw(updated_at=t, faces=[]))
    v = w.lookup.look('announce_who')
    assert v.ok and v.faces == () and w.t - NOW >= 1.5, 'claimed nobody from one early frame'


def test_look_ignores_a_view_older_than_the_request():
    w = World(lambda t: _raw(updated_at=NOW - 0.1, faces=[_face('Alice')]))
    v = w.lookup.look('announce_who')
    assert not v.ok and v.faces == (), 'trusted a picture taken before it asked'
    assert describe(v)[0] == 'who_blind'


def test_look_returns_blind_early_when_the_camera_is_offline():
    w = World(lambda t: _raw(updated_at=t, state='camera_offline'))
    v = w.lookup.look('announce_who')
    assert v.state == 'camera_offline' and w.t - NOW < 3.0


def test_look_is_blind_when_the_dashboard_is_not_publishing():
    w = World(lambda t: None)
    v = w.lookup.look('announce_who')
    assert v.state == 'no_data' and not v.ok


def test_look_waits_for_unstable_faces_to_settle():
    def script(t):
        stable = t - NOW > 3.0
        return _raw(updated_at=t, faces=[_face('Alice', stable=stable)])
    w = World(script)
    v = w.lookup.look('announce_who')
    assert v.faces[0]['stable'] is True and w.t - NOW > 3.0


def test_look_for_a_name_returns_as_soon_as_that_person_is_stable():
    w = World(lambda t: _raw(updated_at=t, faces=[_face('Alice'), _face('Bob', stable=False)]))
    w.lookup.look('call_person', want_name='Alice')
    assert w.t - NOW < 1.0


def test_look_for_a_name_that_never_appears_times_out():
    w = World(lambda t: _raw(updated_at=t, faces=[_face('Bob')]))
    v = w.lookup.look('call_person', want_name='Alice')
    assert w.t - NOW >= 6.0 and not any(f['name'] == 'Alice' for f in v.faces)


def test_look_survives_an_unwritable_directory():
    lookup = CamLookup('/proc/agv-nope', clock=lambda: NOW, sleep=lambda s: None, timeout_s=0.0)
    assert lookup.look('announce_who').state == 'no_data'


if __name__ == '__main__':
    _env.run_tests(globals())
