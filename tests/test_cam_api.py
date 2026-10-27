"""Tests for the Camera tab's HTTP API: /cam/detections and /api/cam/*.

Same shape as test_lidar_api: the Flask test client against the real app. The
camera is pinned to a node that cannot exist (see _env), so the publisher is
offline and no frames flow; that is exactly the state the enrol guard has to
handle.
"""
import _env  # noqa: F401  (must be first)
import json
import os

import numpy as np

import web
from cam import analytics


def _client():
    return web.flask_app.test_client()


def _auth():
    return {'X-AGV-Password': 'testpw'}


def _reset():
    """Factory defaults and an empty roster, so tests don't inherit each other's writes."""
    web._fail_times.clear()      # these tests send many unauthenticated calls on purpose
    web.CAM_ANALYTICS.cancel_enroll()
    web.CAM_ANALYTICS.update_settings(dict(analytics.DEFAULTS))
    for p in web.CAM_ANALYTICS.store.people():
        web.CAM_ANALYTICS.remove_person(p['name'])
    web.CAM_PROFILES_STORE.prune([])


def _vec(i):
    v = np.zeros((1, 128), np.float32)
    v[0, i] = 1.0
    return v


# ── /cam/detections ─────────────────────────────────────────────────
def test_detections_has_the_shape_the_overlay_reads():
    _reset()
    d = _client().get('/cam/detections').get_json()
    for key in ('state', 'faces', 'objects', 'enroll', 'events', 'settings', 'models', 'timing', 'people'):
        assert key in d, key
    assert d['enroll'] == {'active': False}


def test_detections_needs_no_password():
    assert _client().get('/cam/detections').status_code == 200


def test_detections_is_never_cached():
    r = _client().get('/cam/detections')
    assert 'no-store' in r.headers.get('Cache-Control', '')


def test_polling_detections_wakes_the_worker():
    web.CAM_ANALYTICS._watch_until = 0.0
    _client().get('/cam/detections')
    assert web.CAM_ANALYTICS._watch_until > 0.0


def test_existing_detections_page_is_untouched():
    """/detections is the AGV's own HTML viewer; the camera API must not shadow it."""
    r = _client().get('/detections')
    assert r.status_code == 200 and r.mimetype == 'text/html'


# ── settings ────────────────────────────────────────────────────────
def test_settings_get_includes_limits_for_the_slider():
    _reset()
    d = _client().get('/api/cam/settings').get_json()
    assert d['limits']['threshold']['min'] < d['threshold'] < d['limits']['threshold']['max']
    assert d['faces_enabled'] is True and d['objects_enabled'] is False


def test_settings_post_clamps_and_echoes_the_clamped_value():
    _reset()
    d = _client().post('/api/cam/settings', json={'threshold': 5}).get_json()
    assert d['threshold'] == analytics.LIMITS['threshold']['max']
    assert json.load(open(os.environ['AGV_CAM_SETTINGS_FILE']))['threshold'] == d['threshold']


def test_settings_post_ignores_unknown_and_malformed_fields():
    _reset()
    c = _client()
    before = c.get('/api/cam/settings').get_json()
    c.post('/api/cam/settings', json={'threshold': 'high', 'evil': 1, 'faces_enabled': 'no'})
    assert c.get('/api/cam/settings').get_json() == before
    assert c.post('/api/cam/settings', data='not json').status_code == 200


def test_settings_toggles_round_trip():
    _reset()
    c = _client()
    d = c.post('/api/cam/settings', json={'objects_enabled': True, 'faces_enabled': False}).get_json()
    assert d['objects_enabled'] is True and d['faces_enabled'] is False
    assert c.get('/api/cam/settings').get_json()['objects_enabled'] is True


# ── people ──────────────────────────────────────────────────────────
def test_people_starts_empty():
    _reset()
    assert _client().get('/api/cam/people').get_json() == {'people': []}


def test_people_lists_enrolled_and_serves_the_thumbnail():
    _reset()
    web.CAM_ANALYTICS.store.add('Alice', [_vec(0)], b'\xff\xd8thumb')
    c = _client()
    assert c.get('/api/cam/people').get_json()['people'][0]['name'] == 'Alice'
    r = c.get('/api/cam/people/Alice/thumb')
    assert r.status_code == 200 and r.mimetype == 'image/jpeg' and r.data == b'\xff\xd8thumb'


def test_thumb_for_unknown_person_is_404():
    _reset()
    assert _client().get('/api/cam/people/Nobody/thumb').status_code == 404


def test_thumb_cannot_be_used_to_read_arbitrary_files():
    _reset()
    for evil in ('..%2F..%2Fetc%2Fpasswd', '....//x', '%2e%2e'):
        assert _client().get(f'/api/cam/people/{evil}/thumb').status_code == 404, evil


def test_delete_needs_a_password():
    _reset()
    web.CAM_ANALYTICS.store.add('Alice', [_vec(0)])
    r = _client().delete('/api/cam/people/Alice')
    assert r.status_code == 403
    assert web.CAM_ANALYTICS.store.count() == 1, 'deleted without authenticating'


def test_delete_with_password_removes_the_person():
    _reset()
    web.CAM_ANALYTICS.store.add('Alice', [_vec(0)], b'jpg')
    r = _client().delete('/api/cam/people/Alice', headers=_auth())
    assert r.status_code == 200 and r.get_json()['people'] == []
    assert _client().get('/api/cam/people/Alice/thumb').status_code == 404


def test_delete_unknown_person_is_404():
    _reset()
    assert _client().delete('/api/cam/people/Nobody', headers=_auth()).status_code == 404


# ── enrolment ───────────────────────────────────────────────────────
def test_enrol_needs_a_password():
    _reset()
    r = _client().post('/api/cam/enroll', json={'name': 'Carol'})
    assert r.status_code == 403
    assert web.CAM_ANALYTICS.latest()['enroll'] == {'active': False}


def test_enrol_rejects_bad_names():
    _reset()
    for bad in ('', '../x', 'x' * 40, '<script>'):
        r = _client().post('/api/cam/enroll', json={'name': bad}, headers=_auth())
        assert r.status_code == 400, (bad, r.status_code)


def test_enrol_rejects_non_numeric_sample_count():
    _reset()
    r = _client().post('/api/cam/enroll', json={'name': 'Carol', 'samples': 'lots'}, headers=_auth())
    assert r.status_code == 400


def test_enrol_refuses_when_the_camera_is_not_streaming():
    """No frames means the progress bar would sit at 0/8 until it timed out."""
    _reset()
    r = _client().post('/api/cam/enroll', json={'name': 'Carol'}, headers=_auth())
    assert r.status_code == 409, r.status_code
    assert web.CAM_ANALYTICS.latest()['enroll'] == {'active': False}


def test_enrol_starts_once_frames_flow():
    _reset()
    real = web.CAM_PUBLISHER.status
    web.CAM_PUBLISHER.status = lambda: dict(real(), frame_seq=7)
    try:
        r = _client().post('/api/cam/enroll', json={'name': 'Carol', 'samples': 4}, headers=_auth())
        assert r.status_code == 200 and r.get_json()['want'] == 4
        assert web.CAM_ANALYTICS.latest()['enroll']['active'] is True
    finally:
        web.CAM_PUBLISHER.status = real
        web.CAM_ANALYTICS.cancel_enroll()


def test_cancel_enrol_is_open_and_always_ok():
    _reset()
    assert _client().post('/api/cam/enroll/cancel').status_code == 200

# ── profiles ────────────────────────────────────────────────────────
def test_people_include_a_blank_private_profile_by_default():
    _reset()
    web.CAM_ANALYTICS.store.add('Alice', [_vec(0)])
    p = _client().get('/api/cam/people').get_json()['people'][0]['profile']
    assert p['greet'] is False and p['greeting'] == '' and p['role'] == ''


def test_profile_put_needs_a_password():
    _reset()
    web.CAM_ANALYTICS.store.add('Alice', [_vec(0)])
    r = _client().put('/api/cam/people/Alice/profile', json={'role': 'x'})
    assert r.status_code == 403
    assert web.CAM_PROFILES_STORE.get('Alice')['role'] == ''


def test_profile_put_saves_and_returns_the_profile():
    _reset()
    web.CAM_ANALYTICS.store.add('Alice', [_vec(0)])
    r = _client().put('/api/cam/people/Alice/profile', headers=_auth(),
                      json={'role': 'Operator', 'greeting': 'Welcome back, Alice.', 'greet': True})
    assert r.status_code == 200 and r.get_json()['greet'] is True
    p = _client().get('/api/cam/people').get_json()['people'][0]['profile']
    assert p['role'] == 'Operator' and p['greeting'] == 'Welcome back, Alice.'


def test_profile_put_for_someone_not_enrolled_is_404():
    _reset()
    for name in ('Nobody', '..%2Fx', 'x' * 40):
        r = _client().put(f'/api/cam/people/{name}/profile', headers=_auth(), json={'role': 'x'})
        assert r.status_code == 404, name


def test_profile_put_rejects_a_non_boolean_opt_in():
    _reset()
    web.CAM_ANALYTICS.store.add('Alice', [_vec(0)])
    r = _client().put('/api/cam/people/Alice/profile', headers=_auth(), json={'greet': 'yes'})
    assert r.status_code == 400
    assert web.CAM_PROFILES_STORE.get('Alice')['greet'] is False


def test_notes_are_hidden_from_signed_out_readers_only():
    _reset()
    web.CAM_ANALYTICS.store.add('Alice', [_vec(0)])
    web.CAM_PROFILES_STORE.set('Alice', {'notes': 'PRIVATE', 'role': 'Op'})
    out = _client().get('/api/cam/people').get_json()['people'][0]['profile']
    assert out['notes'] == '' and out['role'] == 'Op'
    signed = _client().get('/api/cam/people', headers=_auth()).get_json()['people'][0]['profile']
    assert signed['notes'] == 'PRIVATE'


def test_deleting_a_person_deletes_their_profile():
    _reset()
    web.CAM_ANALYTICS.store.add('Alice', [_vec(0)])
    web.CAM_PROFILES_STORE.set('Alice', {'greet': True, 'greeting': 'Hi'})
    assert _client().delete('/api/cam/people/Alice', headers=_auth()).status_code == 200
    web.CAM_ANALYTICS.store.add('Alice', [_vec(0)])          # a different Alice, later
    assert web.CAM_PROFILES_STORE.get('Alice')['greet'] is False, 'a new person inherited the old consent'


# ── greeter settings ────────────────────────────────────────────────
def test_greeter_settings_need_a_password():
    """The greeter keeps the camera analysing and lets the rover say names aloud."""
    _reset()
    for key, val in (('greeter_enabled', True), ('greet_min_sim', 0.4), ('greet_cooldown_s', 30),
                     ('greet_unknown', True)):
        r = _client().post('/api/cam/settings', json={key: val})
        assert r.status_code == 403, key
    assert _client().get('/api/cam/settings').get_json()['greeter_enabled'] is False


def test_greeter_settings_can_be_changed_with_a_password_and_are_clamped():
    _reset()
    d = _client().post('/api/cam/settings', headers=_auth(),
                       json={'greeter_enabled': True, 'greet_min_sim': 0.0}).get_json()
    assert d['greeter_enabled'] is True and d['greet_min_sim'] == analytics.LIMITS['greet_min_sim']['min']
    assert 'greet_min_sim' in d['limits'] and 'greet_cooldown_s' in d['limits']
    _reset()


def test_display_settings_stay_open_without_a_password():
    _reset()
    assert _client().post('/api/cam/settings', json={'threshold': 0.5}).status_code == 200


def test_greeter_defaults_are_off():
    _reset()
    d = _client().get('/api/cam/settings').get_json()
    assert d['greeter_enabled'] is False and d['greet_unknown'] is False


if __name__ == '__main__':
    _env.run_tests(globals())
