"""Tests for cam/analytics.py — face tracking, identity, enrolment, settings.

A fake detector and recogniser stand in for the models, so these run in
milliseconds with no camera and no onnxruntime. The fake recogniser derives a
face's embedding from the ``who`` key on the detection dict, which lets a test
say "this box is Alice" without any pixels.
"""
import _env  # noqa: F401  (must be first)
import json
import os
import tempfile
import time

import numpy as np

from cam import analytics
from cam.analytics import CamAnalytics, clamp_settings
from cam.faces import FaceStore

FRAME = np.full((480, 640, 3), 100, np.uint8)
IDS = {'alice': 0, 'bob': 1, 'stranger': 2}


def _vec(i):
    v = np.zeros((1, 128), np.float32)
    v[0, i] = 1.0
    return v


def _face(who='alice', x=100, y=100, w=120, h=150, score=0.95, vec=None):
    return {'box': [x, y, w, h], 'lm': [0.0] * 10, 'score': score, 'who': who, 'vec': vec}


class FakeDetector:
    def __init__(self):
        self.faces, self.calls, self.fail = [], 0, False

    def detect(self, frame):
        self.calls += 1
        if self.fail:
            raise RuntimeError('boom')
        return [dict(f) for f in self.faces]


class FakeRecognizer:
    def __init__(self):
        self.calls = 0

    def embed(self, frame, face):
        self.calls += 1
        return face['vec'] if face.get('vec') is not None else _vec(IDS[face['who']])


class FakePub:
    def __init__(self):
        self.seq = 0

    def latest_frame(self):
        self.seq += 1
        return self.seq, FRAME


def _make(enrolled=('alice',), **kw):
    d = tempfile.mkdtemp(dir=_env.TMP)
    store = FaceStore(os.path.join(d, 'db.pkl'), os.path.join(d, 'th'))
    for n in enrolled:
        store.add(n.capitalize(), [_vec(IDS[n])])
    det, rec = FakeDetector(), FakeRecognizer()
    a = CamAnalytics(FakePub(), store=store, settings_path=kw.pop('settings_path', ''),
                     detector_factory=lambda: det, recognizer_factory=lambda: rec, **kw)
    a._ensure('faces')          # the worker does this lazily; tests that call _track/_enroll_step directly need it
    return a, det, rec


def _run(a, det, faces, n=1):
    det.faces = faces
    for _ in range(n):
        a._process(1, FRAME)
    return a.latest()


# ── settings ────────────────────────────────────────────────────────
def test_clamp_settings_clamps_numbers_into_limits():
    s = clamp_settings({'threshold': 5, 'object_conf': -1})
    assert s['threshold'] == analytics.LIMITS['threshold']['max']
    assert s['object_conf'] == analytics.LIMITS['object_conf']['min']


def test_clamp_settings_ignores_wrong_types():
    """A bool is an int in Python; it must not be accepted as a threshold."""
    s = clamp_settings({'threshold': True, 'faces_enabled': 'yes', 'classes': 'person',
                        'object_conf': float('nan'), 'bogus': 1})
    assert s == analytics.DEFAULTS


def test_clamp_settings_merges_onto_the_current_values():
    base = dict(analytics.DEFAULTS, objects_enabled=True)
    assert clamp_settings({'threshold': 0.5}, base)['objects_enabled'] is True


def test_settings_persist_and_reload():
    d = tempfile.mkdtemp(dir=_env.TMP)
    path = os.path.join(d, 's.json')
    a, _, _ = _make(settings_path=path)
    a.update_settings({'threshold': 0.55, 'objects_enabled': True})
    assert json.load(open(path))['threshold'] == 0.55
    b, _, _ = _make(settings_path=path)
    assert b.settings()['threshold'] == 0.55 and b.settings()['objects_enabled'] is True


def test_unreadable_settings_file_falls_back_to_defaults():
    d = tempfile.mkdtemp(dir=_env.TMP)
    path = os.path.join(d, 's.json')
    open(path, 'w').write('{nope')
    a, _, _ = _make(settings_path=path)
    assert a.settings() == analytics.DEFAULTS


# ── identity ────────────────────────────────────────────────────────
def test_enrolled_face_is_named_with_its_similarity():
    a, det, _ = _make()
    f = _run(a, det, [_face('alice')])['faces'][0]
    assert f['name'] == 'Alice' and f['known'] is True and f['sim'] == 1.0


def test_stranger_is_unknown_but_scored():
    a, det, _ = _make()
    f = _run(a, det, [_face('stranger')])['faces'][0]
    assert f['name'] is None and f['known'] is False and f['sim'] == 0.0


def test_with_nobody_enrolled_everyone_is_unknown_and_nothing_is_embedded():
    a, det, rec = _make(enrolled=())
    f = _run(a, det, [_face('alice')])['faces'][0]
    assert f['name'] is None and f['sim'] is None
    assert rec.calls == 0, 'no point paying 70 ms to compare against an empty db'


def test_threshold_slider_flips_identity_without_re_embedding():
    """The raw similarity is kept on the track, so the slider is free."""
    a, det, rec = _make()
    v = np.zeros((1, 128), np.float32)
    v[0, 0], v[0, 1] = 1.0, 1.0                         # cosine to Alice = 0.707
    faces = [_face(vec=v)]
    assert _run(a, det, faces)['faces'][0]['known'] is True
    calls = rec.calls
    a.update_settings({'threshold': 0.8})
    f = _run(a, det, faces)['faces'][0]
    assert f['known'] is False and f['name'] is None and f['sim'] == 0.71
    assert rec.calls == calls, 're-embedded just because the slider moved'


def test_known_face_is_not_re_embedded_every_frame():
    """31 frames over 3 s: two quick embeds to reach 'stable', then the slow cadence."""
    a, det, rec = _make()
    det.faces = [_face('alice')]
    st = a.settings()
    for i in range(31):
        a._track(FRAME, det.detect(FRAME), st, now=100.0 + i * 0.1)
    assert rec.calls == 3, rec.calls


def test_stable_face_is_re_embedded_after_the_slow_interval():
    a, det, rec = _make()
    det.faces = [_face('alice')]
    st = a.settings()
    a._track(FRAME, det.detect(FRAME), st, now=100.0)
    a._track(FRAME, det.detect(FRAME), st, now=100.0 + analytics.EMBED_UNKNOWN_S + 0.1)   # now stable
    a._track(FRAME, det.detect(FRAME), st, now=101.5)            # too soon for a stable face
    assert rec.calls == 2
    a._track(FRAME, det.detect(FRAME), st, now=100.9 + analytics.EMBED_KNOWN_S + 0.1)
    assert rec.calls == 3


def test_unknown_face_is_retried_sooner_than_a_known_one():
    assert analytics.EMBED_UNKNOWN_S < analytics.EMBED_KNOWN_S


def test_a_moving_face_keeps_its_track_id():
    a, det, _ = _make()
    st = a.settings()
    ids = []
    for i in range(4):
        det.faces = [_face('alice', x=100 + 8 * i)]
        ids.append(a._track(FRAME, det.detect(FRAME), st, now=100.0 + i * 0.1)[0]['track'])
    assert len(set(ids)) == 1, ids


def test_two_faces_get_two_tracks():
    a, det, _ = _make(enrolled=('alice', 'bob'))
    r = _run(a, det, [_face('alice', x=50), _face('bob', x=400)])
    assert sorted(f['name'] for f in r['faces']) == ['Alice', 'Bob']
    assert len({f['track'] for f in r['faces']}) == 2


def test_embeds_per_frame_are_capped():
    a, det, rec = _make(enrolled=('alice', 'bob'))
    _run(a, det, [_face('alice', x=10), _face('bob', x=210), _face('stranger', x=420)])
    assert rec.calls == analytics.MAX_EMBEDS_PER_FRAME


def test_unembedded_face_reports_no_similarity_yet():
    """The 3rd face waits its turn: it must not be logged or shown as 'Unknown'."""
    a, det, _ = _make(enrolled=('alice',))
    r = _run(a, det, [_face('alice', x=10), _face('alice', x=210), _face('alice', x=420)])
    assert sum(f['sim'] is None for f in r['faces']) == 1


# ── events ──────────────────────────────────────────────────────────
def test_same_person_is_logged_once_per_gap():
    a, det, _ = _make()
    r = _run(a, det, [_face('alice')], n=5)
    assert [e['name'] for e in r['events']] == ['Alice']


def test_unknown_faces_are_logged_as_unknown():
    a, det, _ = _make()
    r = _run(a, det, [_face('stranger')])
    assert [(e['kind'], e['name']) for e in r['events']] == [('unknown', None)]


def test_events_are_newest_first_and_bounded():
    a, _, _ = _make()
    for i in range(analytics.EVENTS_KEPT + 20):
        a._events.append({'t': i, 'name': 'x', 'sim': 1.0, 'kind': 'seen'})
    ev = a.latest()['events']
    assert len(ev) <= 30 and ev[0]['t'] > ev[-1]['t']
    assert len(a._events) == analytics.EVENTS_KEPT


# ── enrolment ───────────────────────────────────────────────────────
def _enroll_frames(a, det, vecs, start=100.0):
    """Feed one clear face per vec, spaced past the capture interval."""
    now = start
    for v in vecs:
        det.faces = [_face('alice', vec=v)]
        a._enroll_step(FRAME, det.detect(FRAME), now)
        now += analytics.ENROLL_SPACING_S + 0.1
    return now


def test_enrolment_saves_after_enough_distinct_samples():
    a, det, _ = _make(enrolled=())
    a.start_enroll('Carol', 3)
    _enroll_frames(a, det, [_vec(0), _vec(1), _vec(2)])
    e = a.latest()['enroll']
    assert e == {'active': False, 'result': 'saved', 'name': 'Carol', 'saved': 3}, e
    assert [(p['name'], p['samples']) for p in a.store.people()] == [('Carol', 3)]


def test_enrolled_person_is_recognised_immediately_after():
    a, det, _ = _make(enrolled=())
    a.start_enroll('Carol', 3)
    _enroll_frames(a, det, [_vec(0), _vec(1), _vec(2)])
    f = _run(a, det, [_face('alice', vec=_vec(1))])['faces'][0]
    assert f['name'] == 'Carol'


def test_enrolment_refuses_near_duplicate_samples():
    """A still subject adds nothing: ask for a head turn instead of padding the db."""
    a, det, _ = _make(enrolled=())
    a.start_enroll('Carol', 3)
    _enroll_frames(a, det, [_vec(0), _vec(0), _vec(0)])
    e = a.latest()['enroll']
    assert e['active'] and e['got'] == 1 and 'head' in e['hint'], e


def test_enrolment_needs_exactly_one_face():
    a, det, _ = _make(enrolled=())
    a.start_enroll('Carol', 3)
    det.faces = [_face(x=50), _face(x=400)]
    a._enroll_step(FRAME, det.detect(FRAME), 100.0)
    e = a.latest()['enroll']
    assert e['got'] == 0 and 'one person' in e['hint'].lower(), e
    det.faces = []
    a._enroll_step(FRAME, det.detect(FRAME), 101.0)
    assert 'No face' in a.latest()['enroll']['hint']


def test_enrolment_skips_unclear_and_small_faces():
    a, det, _ = _make(enrolled=())
    a.start_enroll('Carol', 3)
    det.faces = [_face(score=0.6)]
    a._enroll_step(FRAME, det.detect(FRAME), 100.0)
    assert a.latest()['enroll']['got'] == 0 and 'clear' in a.latest()['enroll']['hint']
    det.faces = [_face(w=50, h=60)]
    a._enroll_step(FRAME, det.detect(FRAME), 101.0)
    assert 'closer' in a.latest()['enroll']['hint']


def test_enrolment_respects_the_capture_spacing():
    a, det, _ = _make(enrolled=())
    a.start_enroll('Carol', 5)
    det.faces = [_face(vec=_vec(0))]
    a._enroll_step(FRAME, det.detect(FRAME), 100.0)
    det.faces = [_face(vec=_vec(1))]
    a._enroll_step(FRAME, det.detect(FRAME), 100.1)          # too soon
    assert a.latest()['enroll']['got'] == 1


def test_enrolment_rejects_bad_names_and_clamps_sample_count():
    a, _, _ = _make(enrolled=())
    for bad in ('', '../x', 'x' * 40):
        try:
            a.start_enroll(bad)
        except ValueError:
            continue
        raise AssertionError(f'accepted {bad!r}')
    assert a.start_enroll('Ok', 999)['want'] == 20
    assert a.start_enroll('Ok', 1)['want'] == 3


def test_cancel_enrolment_discards_samples():
    a, det, _ = _make(enrolled=())
    a.start_enroll('Carol', 3)
    _enroll_frames(a, det, [_vec(0)])
    a.cancel_enroll()
    assert a.latest()['enroll']['result'] == 'cancelled'
    assert a.store.people() == []


def test_enrolment_times_out():
    a, det, _ = _make(enrolled=())
    a.start_enroll('Carol', 3)
    det.faces = [_face()]
    a._enroll_step(FRAME, det.detect(FRAME), time.monotonic() + analytics.ENROLL_TIMEOUT_S + 1)
    assert a.latest()['enroll']['result'] == 'timed out'


def test_enrolling_works_even_with_the_faces_toggle_off():
    """Switching display off must not make 'Add face' silently do nothing."""
    a, det, _ = _make(enrolled=())
    a.update_settings({'faces_enabled': False})
    a.start_enroll('Carol', 3)
    det.faces = [_face(vec=_vec(0))]
    a._process(1, FRAME)
    assert det.calls == 1 and a.latest()['enroll']['got'] == 1


# ── worker ──────────────────────────────────────────────────────────
def test_faces_toggle_off_publishes_no_faces_and_skips_the_detector():
    a, det, _ = _make()
    a.update_settings({'faces_enabled': False})
    r = _run(a, det, [_face()])
    assert r['faces'] == [] and det.calls == 0


def test_worker_idles_until_someone_watches():
    a, det, _ = _make()
    det.faces = [_face()]
    a.start()
    try:
        time.sleep(0.6)
        assert det.calls == 0, 'analysing an unwatched camera'
        assert a.latest()['state'] == 'idle'
        a.touch()
        assert _env.poll(lambda: det.calls > 0, 3), 'did not wake on touch'
        assert _env.poll(lambda: a.latest()['state'] == 'running', 3)
    finally:
        a.stop()


def test_model_failure_is_reported_and_does_not_kill_the_worker():
    a, det, _ = _make()
    det.fail = True
    a.start()
    try:
        a.touch()
        assert _env.poll(lambda: det.calls > 2, 3)
        assert a._thread.is_alive()
    finally:
        a.stop()


def test_factory_failure_shows_in_models_and_is_retried_later():
    calls = []

    def boom():
        calls.append(1)
        raise OSError('model missing')

    d = tempfile.mkdtemp(dir=_env.TMP)
    a = CamAnalytics(FakePub(), store=FaceStore(os.path.join(d, 'db.pkl'), os.path.join(d, 't')),
                     detector_factory=boom, recognizer_factory=lambda: FakeRecognizer())
    a._process(1, FRAME)
    a._process(2, FRAME)
    assert len(calls) == 1, 'hammering a missing model every frame'
    assert 'model missing' in a.latest()['models']['faces']


def test_stop_is_idempotent_and_safe_before_start():
    a, _, _ = _make()
    a.stop()
    a.stop()
    a.start()
    a.stop()
    a.stop()


# ── stable identity (a name is only stable after separate embeddings agree) ──
def _stable_run(a, det, faces, n, step=0.9, start=100.0):
    st = a.settings()
    out = None
    for i in range(n):
        det.faces = faces
        out = a._track(FRAME, det.detect(FRAME), st, now=start + i * step)
    return out[0]


def test_one_embedding_is_never_stable():
    a, det, _ = _make()
    assert _stable_run(a, det, [_face('alice')], 1)['stable'] is False


def test_two_agreeing_embeddings_make_a_name_stable():
    a, det, _ = _make()
    f = _stable_run(a, det, [_face('alice')], 2)
    assert f['stable'] is True and f['name'] == 'Alice'


def test_a_different_person_resets_stability():
    """Alice, then someone who matches Bob, in the same box: not stable as either."""
    a, det, _ = _make(enrolled=('alice', 'bob'))
    st = a.settings()
    det.faces = [_face('alice')]
    a._track(FRAME, det.detect(FRAME), st, now=100.0)
    det.faces = [_face('bob')]
    f = a._track(FRAME, det.detect(FRAME), st, now=100.9)[0]
    assert f['stable'] is False


def test_similarity_below_the_greeting_bar_is_never_stable():
    """Known enough to draw a box (>= 0.363), not enough to say a name aloud."""
    a, det, _ = _make()
    v = np.zeros((1, 128), np.float32)
    v[0, 0], v[0, 1] = 1.0, 1.4                          # cosine to Alice ~0.58
    a.update_settings({'greet_min_sim': 0.7})
    f = _stable_run(a, det, [_face(vec=v)], 3)
    assert f['known'] is True and f['stable'] is False


def test_raising_the_greeting_bar_unstabilises_live_without_re_embedding():
    a, det, rec = _make()
    v = np.zeros((1, 128), np.float32)
    v[0, 0], v[0, 1] = 1.0, 1.0                          # cosine to Alice = 0.707
    assert _stable_run(a, det, [_face(vec=v)], 2)['stable'] is True
    calls = rec.calls
    a.update_settings({'greet_min_sim': 0.8})
    f = a._track(FRAME, det.detect(FRAME), a.settings(), now=100.95)[0]
    assert f['stable'] is False and f['known'] is True and rec.calls == calls


def test_greeter_settings_default_off_and_clamp():
    s = clamp_settings({})
    assert s['greeter_enabled'] is False and s['greet_unknown'] is False
    s = clamp_settings({'greet_min_sim': 0.01, 'greet_cooldown_s': 99999})
    assert s['greet_min_sim'] == analytics.LIMITS['greet_min_sim']['min']
    assert s['greet_cooldown_s'] == analytics.LIMITS['greet_cooldown_s']['max']
    assert clamp_settings({'greeter_enabled': 'yes'})['greeter_enabled'] is False


# ── the rover's view ────────────────────────────────────────────────
def test_greet_snapshot_has_faces_even_with_the_overlay_off():
    a, det, _ = _make()
    a.update_settings({'faces_enabled': False, 'greeter_enabled': True})
    r = _run(a, det, [_face('alice')])
    assert r['faces'] == []
    assert [f['name'] for f in a.greet_snapshot()['faces']] == ['Alice']


def test_greeter_runs_with_nobody_watching_and_stays_off_otherwise():
    a, det, _ = _make()
    det.faces = [_face('alice')]
    a.start()
    try:
        time.sleep(0.6)
        assert det.calls == 0, 'analysing with the greeter off and nobody watching'
        a.update_settings({'greeter_enabled': True})
        assert _env.poll(lambda: det.calls > 0, 3), 'greeter never woke'
        assert a.greet_snapshot()['faces'][0]['name'] == 'Alice'
    finally:
        a.stop()


def test_unwatched_greeter_is_rate_capped():
    """~3 Hz: the Jetson also runs the AGV and nobody is looking at the boxes."""
    a, det, _ = _make()
    a.update_settings({'greeter_enabled': True})
    det.faces = [_face('alice')]
    a.start()
    try:
        time.sleep(1.5)
        assert det.calls <= 7, det.calls
    finally:
        a.stop()


def test_unwatched_greeter_never_runs_the_object_model():
    built = []
    d = tempfile.mkdtemp(dir=_env.TMP)
    det = FakeDetector()
    a = CamAnalytics(FakePub(), store=FaceStore(os.path.join(d, 'db.pkl'), os.path.join(d, 't')),
                     detector_factory=lambda: det, recognizer_factory=FakeRecognizer,
                     objects_factory=lambda: built.append(1))
    a.update_settings({'greeter_enabled': True, 'objects_enabled': True})
    a._quiet = True
    a._process(1, FRAME)
    assert built == [], 'loaded YOLO for an unwatched greeter'


def test_greeter_off_clears_the_snapshot():
    a, det, _ = _make()
    a.update_settings({'greeter_enabled': True})
    _run(a, det, [_face('alice')])
    a.update_settings({'greeter_enabled': False})
    a.start()
    try:
        assert _env.poll(lambda: a.greet_snapshot()['ts'] is None, 3), 'kept naming people while off'
    finally:
        a.stop()


# ── wake requests from the voice process ────────────────────────────
def _watch(**req):
    d = tempfile.mkdtemp(dir=_env.TMP)
    path = os.path.join(d, 'cam_watch.json')
    now = [1000.0]
    a, det, _ = _make(watch_path=path, clock=lambda: now[0])
    base = {'id': 'w1', 'ts': 1000.0, 'ttl_s': 8}
    base.update(req)
    json.dump(base, open(path, 'w'))
    return a, path, now


def test_fresh_wake_request_wakes_the_camera():
    a, _, _ = _watch()
    assert a.poll_watch() is True and a._ext_until > time.monotonic()


def test_a_wake_request_is_honoured_once():
    a, path, _ = _watch()
    assert a.poll_watch() is True
    a._ext_until = 0.0
    assert a.poll_watch() is False and a._ext_until == 0.0


def test_stale_wake_file_is_ignored():
    """A leftover file from before a restart is not a request."""
    a, _, now = _watch(ts=900.0)
    assert a.poll_watch() is False and a._ext_until == 0.0


def test_future_dated_wake_file_is_ignored():
    a, _, _ = _watch(ts=5000.0)
    assert a.poll_watch() is False


def test_wake_ttl_is_clamped():
    a, _, _ = _watch(ttl_s=10 ** 6)
    a.poll_watch()
    assert a._ext_until - time.monotonic() <= analytics.WATCH_MAX_TTL_S + 0.5


def test_malformed_wake_requests_are_ignored():
    for bad in ({'id': ''}, {'id': 5}, {'ts': 'now'}, {'ts': True}):
        a, _, _ = _watch(**bad)
        assert a.poll_watch() is False and a._ext_until == 0.0, bad


def test_missing_or_corrupt_wake_file_is_fine():
    a, path, _ = _watch()
    open(path, 'w').write('{nope')
    assert a.poll_watch() is False
    os.unlink(path)
    assert a.poll_watch() is False


def test_asked_to_look_runs_detection_without_the_greeter():
    a, det, _ = _make()
    det.faces = [_face('alice')]
    a._ext_until = time.monotonic() + 5
    a.update_settings({'faces_enabled': False, 'greeter_enabled': False})
    a._process(1, FRAME)
    assert det.calls == 1 and a.greet_snapshot()['faces'][0]['name'] == 'Alice'


# ── removing a person ───────────────────────────────────────────────
def test_remove_person_forgets_profile_and_live_tracks():
    from cam.profiles import ProfileStore
    d = tempfile.mkdtemp(dir=_env.TMP)
    profiles = ProfileStore(os.path.join(d, 'p.json'))
    profiles.set('Alice', {'greet': True})
    a, det, _ = _make(profiles=profiles)
    _stable_run(a, det, [_face('alice')], 2)
    assert a._tracks
    assert a.remove_person('Alice') is True
    assert profiles.get('Alice')['greet'] is False and a._tracks == []
    assert a.remove_person('Alice') is False


def test_orphan_profiles_are_pruned_at_start():
    from cam.profiles import ProfileStore
    d = tempfile.mkdtemp(dir=_env.TMP)
    profiles = ProfileStore(os.path.join(d, 'p.json'))
    profiles.set('Ghost', {'greet': True})
    _make(profiles=profiles)
    assert profiles.get('Ghost')['greet'] is False


if __name__ == '__main__':
    _env.run_tests(globals())
