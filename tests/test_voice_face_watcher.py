"""Tests for voice.face_watcher — unprompted greetings, so every gate is tested.

A wrong or repeated greeting is worse than none, so the cases here are mostly
"does NOT speak": greeter off, no opt-in, not stable, similarity too low, camera
blind, file stale, cooldown, speaker busy.
"""
import _env  # noqa: F401  (must be first)
import json
import os
import tempfile

from voice._constants import GREET_MIN_GAP_S, UNKNOWN_SEEN_S
from voice.face_watcher import FaceWatcher
from voice.speaker import DroppedPhrase
from voice.tts import SilentTts

WALL = 1000.0


def _face(name='Alice', sim=0.7, stable=True, greet=True, greeting=''):
    return {'track': 1, 'name': name, 'sim': sim, 'stable': stable, 'greet': greet,
            'greeting': greeting}


class Rig:
    def __init__(self, speaker=None):
        self.d = tempfile.mkdtemp(dir=_env.TMP)
        self.spk = speaker or SilentTts()
        self.t = 5000.0
        self.w = FaceWatcher(self.d, self.spk, clock=lambda: WALL, mono=lambda: self.t)

    def put(self, faces=(), unknown=0, state='running', enabled=True, greet_unknown=False,
            min_sim=0.5, cooldown=300, updated_at=WALL, **over):
        raw = {'schema': 1, 'updated_at': updated_at, 'state': state, 'data_age_ms': 100,
               'greeter': {'enabled': enabled, 'min_sim': min_sim, 'cooldown_s': cooldown,
                           'greet_unknown': greet_unknown},
               'faces': list(faces), 'unknown': unknown, 'roster': ['Alice', 'Bob']}
        raw.update(over)
        json.dump(raw, open(os.path.join(self.d, 'cam_faces.json'), 'w'))

    def tick(self, advance=0.0):
        self.t += advance
        self.w.tick()
        return [s.utterance for s in getattr(self.spk, 'spoken', [])]


def test_greets_a_stable_opted_in_person_once():
    r = Rig(); r.put([_face()])
    assert r.tick() == ['Hello, Alice.']
    assert r.tick(1.0) == ['Hello, Alice.'], 'greeted twice inside the cooldown'


def test_uses_the_persons_own_greeting():
    r = Rig(); r.put([_face(greeting='Welcome back, Alice.')])
    assert r.tick() == ['Welcome back, Alice.']


def test_greeting_event_is_person_greeted():
    r = Rig(); r.put([_face()]); r.tick()
    assert r.spk.spoken[0].event == 'person_greeted'


def test_a_hostile_greeting_is_sanitised_before_it_is_spoken():
    r = Rig(); r.put([_face(greeting='Hi\n\x07 there' + 'x' * 200)])
    spoken = r.tick()[0]
    assert '\n' not in spoken and '\x07' not in spoken and len(spoken) <= 80


def test_silent_when_the_greeter_switch_is_off():
    r = Rig(); r.put([_face()], enabled=False)
    assert r.tick() == []


def test_silent_when_the_person_did_not_opt_in():
    r = Rig(); r.put([_face(greet=False)])
    assert r.tick() == []


def test_silent_when_the_identity_is_not_stable():
    r = Rig(); r.put([_face(stable=False)])
    assert r.tick() == []


def test_silent_when_similarity_is_below_the_greeters_bar():
    r = Rig(); r.put([_face(sim=0.45)], min_sim=0.5)
    assert r.tick() == []


def test_a_hostile_file_cannot_lower_the_similarity_bar():
    """min_sim 0 in the file is clamped to 0.40: a 0.2 match still stays silent."""
    r = Rig(); r.put([_face(sim=0.2)], min_sim=0.0)
    assert r.tick() == []


def test_silent_when_the_camera_cannot_see():
    for state in ('camera_offline', 'idle'):
        r = Rig(); r.put([_face()], state=state)
        assert r.tick() == [], state


def test_silent_when_the_file_is_stale_or_missing():
    r = Rig(); r.put([_face()], updated_at=WALL - 30)
    assert r.tick() == []
    r2 = Rig()
    assert r2.tick() == []


def test_cooldown_expires():
    r = Rig(); r.put([_face()], cooldown=60)
    r.tick()
    assert r.tick(30) == ['Hello, Alice.']
    assert len(r.tick(35)) == 2


def test_only_one_greeting_per_min_gap_even_for_two_people():
    r = Rig(); r.put([_face('Alice', sim=0.9), _face('Bob', sim=0.7)])
    assert r.tick() == ['Hello, Alice.']
    assert r.tick(1.0) == ['Hello, Alice.']
    out = r.tick(GREET_MIN_GAP_S)
    assert out == ['Hello, Alice.', 'Hello, Bob.']


def test_a_dropped_greeting_is_retried_not_recorded_as_said():
    class Busy:
        def __init__(self): self.calls, self.busy = 0, True
        def speak_event(self, event, utt):
            self.calls += 1
            return DroppedPhrase(event, utt) if self.busy else SilentTts().speak_event(event, utt)
    spk = Busy()
    r = Rig(spk); r.put([_face()])
    r.tick(); r.tick(0.5)
    assert spk.calls == 2 and r.w.state()['greeted'] == 0
    spk.busy = False
    r.tick(0.5)
    assert r.w.state()['greeted'] == 1


def test_a_failed_synthesis_is_not_retried_every_tick():
    """played=False (no audio) is not 'dropped': retrying would hammer a paid API."""
    class NoAudio:
        def __init__(self): self.calls = 0
        def speak_event(self, event, utt):
            self.calls += 1
            return type('P', (), {'played': False})()
    spk = NoAudio()
    r = Rig(spk); r.put([_face()])
    for _ in range(5):
        r.tick(1.0)
    assert spk.calls == 1


def test_strangers_are_never_named_and_not_announced_by_default():
    r = Rig(); r.put([], unknown=1)
    for _ in range(5):
        assert r.tick(UNKNOWN_SEEN_S) == []


def test_unknown_person_announcement_needs_the_option_and_a_dwell():
    r = Rig(); r.put([], unknown=1, greet_unknown=True)
    assert r.tick() == []
    assert r.tick(UNKNOWN_SEEN_S - 1) == []
    assert r.tick(2) == ['Unrecognised person.']
    assert r.tick(UNKNOWN_SEEN_S) == ['Unrecognised person.'], 'repeated inside the cooldown'


def test_unknown_dwell_resets_when_the_stranger_leaves():
    r = Rig(); r.put([], unknown=1, greet_unknown=True)
    r.tick(); r.tick(UNKNOWN_SEEN_S - 1)
    r.put([], unknown=0, greet_unknown=True)
    r.tick(0.5)
    r.put([], unknown=1, greet_unknown=True)
    assert r.tick(1.0) == [] and r.tick(UNKNOWN_SEEN_S - 1) == []


def test_state_reports_for_the_status_lane():
    r = Rig(); r.put([_face()])
    r.tick()
    st = r.w.state()
    assert st['state'] == 'running' and st['enabled'] is True and st['greeted'] == 1
    assert st['last_greeted_at'] == WALL


def test_state_reports_no_data_without_a_file():
    r = Rig(); r.tick()
    assert r.w.state()['state'] == 'no_data'


def test_thread_starts_and_stops_and_survives_a_bad_file():
    r = Rig()
    open(os.path.join(r.d, 'cam_faces.json'), 'w').write('{nope')
    w = FaceWatcher(r.d, r.spk, poll_interval_s=0.01).start()
    import time
    time.sleep(0.1)
    assert w._thread.is_alive()
    w.stop()
    w.stop()


if __name__ == '__main__':
    _env.run_tests(globals())
