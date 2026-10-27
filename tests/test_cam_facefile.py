"""Tests for cam/facefile.py — cam_faces.json, the rover's only view of the camera.

The point of the file is what it will and will not say. It must tell "the camera
is offline" apart from "nobody is there", carry only what speech needs, and be
readable by the owner alone.
"""
import _env  # noqa: F401  (must be first)
import json
import os
import stat
import tempfile
import time

import numpy as np

from cam.analytics import CamAnalytics
from cam.faces import FaceStore
from cam.facefile import FacesFilePublisher
from cam.profiles import ProfileStore

FRAME = np.full((480, 640, 3), 100, np.uint8)


def _vec(i):
    v = np.zeros((1, 128), np.float32)
    v[0, i] = 1.0
    return v


class Cam:
    """Stands in for CamPublisher: only status() and latest_frame() are used."""
    def __init__(self, connected=True, age_ms=80):
        self.connected, self.age_ms = connected, age_ms

    def status(self):
        return {'connected': self.connected, 'age_ms': self.age_ms}

    def latest_frame(self):
        return 1, FRAME


class Det:
    def __init__(self): self.faces = []
    def detect(self, frame): return [dict(f) for f in self.faces]


class Rec:
    def embed(self, frame, face): return _vec(face['who'])


def _face(who, x=100):
    return {'box': [x, 100, 120, 150], 'lm': [0.0] * 10, 'score': 0.95, 'who': who}


def _rig(cam=None, **settings):
    d = tempfile.mkdtemp(dir=_env.TMP)
    store = FaceStore(os.path.join(d, 'db.pkl'), os.path.join(d, 'th'))
    store.add('Alice', [_vec(0)])
    store.add('Bob', [_vec(1)])
    profiles = ProfileStore(os.path.join(d, 'profiles.json'))
    det = Det()
    a = CamAnalytics(Cam(), store=store, profiles=profiles,
                     detector_factory=lambda: det, recognizer_factory=Rec)
    a.update_settings(dict({'greeter_enabled': True}, **settings))
    a._ensure('faces')
    pub = FacesFilePublisher(a, cam or Cam(), os.path.join(d, 'cam_faces.json'))
    return a, det, pub, profiles, d


def _look(a, det, faces, frames=3):
    det.faces = faces
    for i in range(frames):
        a._process(i + 1, FRAME)
        for t in a._tracks:
            t.embedded_at = -1e9            # let every frame re-embed, as time would


def test_running_payload_names_a_stable_opted_in_person():
    a, det, pub, profiles, _ = _rig()
    profiles.set('Alice', {'greet': True, 'greeting': 'Welcome back, Alice.'})
    _look(a, det, [_face(0)])
    p = pub.build_payload()
    assert p['state'] == 'running'
    f = p['faces'][0]
    assert (f['name'], f['stable'], f['greet'], f['greeting']) == \
        ('Alice', True, True, 'Welcome back, Alice.')
    assert p['roster'] == ['Alice', 'Bob'] and p['unknown'] == 0


def test_payload_carries_only_what_speech_needs():
    """No boxes, images, embeddings or notes: a name and a few flags per face."""
    a, det, pub, profiles, _ = _rig()
    profiles.set('Alice', {'greet': True, 'notes': 'PRIVATE NOTE'})
    _look(a, det, [_face(0)])
    p = pub.build_payload()
    assert set(p['faces'][0]) == {'track', 'name', 'sim', 'stable', 'greet', 'greeting'}
    assert 'PRIVATE NOTE' not in json.dumps(p)


def test_one_embedding_is_not_stable():
    a, det, pub, _, _ = _rig()
    _look(a, det, [_face(0)], frames=1)
    assert pub.build_payload()['faces'][0]['stable'] is False


def test_people_who_did_not_opt_in_are_marked_so():
    a, det, pub, _, _ = _rig()
    _look(a, det, [_face(0)])
    assert pub.build_payload()['faces'][0]['greet'] is False


def test_strangers_are_counted_never_named():
    a, det, pub, _, _ = _rig()
    _look(a, det, [{**_face(2), 'who': 2}])
    p = pub.build_payload()
    assert p['faces'] == [] and p['unknown'] == 1


def test_camera_offline_is_not_the_same_as_nobody_there():
    a, det, pub, _, _ = _rig(cam=Cam(connected=False, age_ms=None))
    _look(a, det, [])
    p = pub.build_payload()
    assert p['state'] == 'camera_offline' and p['faces'] == [] and p['unknown'] == 0


def test_stale_camera_frames_count_as_offline():
    a, det, pub, _, _ = _rig(cam=Cam(connected=True, age_ms=5000))
    _look(a, det, [_face(0)])
    assert pub.build_payload()['state'] == 'camera_offline'


def test_idle_when_nothing_has_been_analysed():
    a, det, pub, _, _ = _rig()
    assert pub.build_payload()['state'] == 'idle'


def test_stale_analysis_is_idle_not_running():
    a, det, pub, _, _ = _rig()
    _look(a, det, [_face(0)])
    a._greet['ts'] = time.time() - 30
    assert pub.build_payload()['state'] == 'idle'


def test_greeter_settings_are_published_for_the_voice_side():
    a, _, pub, _, _ = _rig(greet_min_sim=0.6, greet_cooldown_s=120, greet_unknown=True)
    g = pub.build_payload()['greeter']
    assert g == {'enabled': True, 'min_sim': 0.6, 'cooldown_s': 120.0, 'greet_unknown': True}


def test_published_even_with_the_overlay_switched_off():
    """The faces toggle is about drawing; the greeter must still see people."""
    a, det, pub, _, _ = _rig(faces_enabled=False)
    _look(a, det, [_face(0)])
    assert pub.build_payload()['faces'][0]['name'] == 'Alice'
    assert a.latest()['faces'] == []


def test_file_is_written_atomically_and_owner_only():
    a, det, pub, _, d = _rig()
    pub.publish_once()
    path = os.path.join(d, 'cam_faces.json')
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert json.load(open(path))['schema'] == 1
    assert not os.path.exists(path + '.tmp')


def test_writer_survives_a_failing_tick_and_stops_cleanly():
    a, det, pub, _, _ = _rig()
    pub._path = '/proc/agv-cannot-write/cam_faces.json'
    pub.start()
    time.sleep(0.2)
    assert pub._thread.is_alive()
    pub.stop()
    pub.stop()


if __name__ == '__main__':
    _env.run_tests(globals())
