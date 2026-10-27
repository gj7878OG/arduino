"""The seam between the camera (web process) and the rover's voice (voice process).

Two processes that share only files can drift apart silently: a renamed file, a
changed schema, and the rover goes quiet with no error anywhere. These tests pin
the contract from both sides using the *real* writer and the *real* reader, and
then check the two promises that matter most: nothing here can reach the motors,
and no name is ever handed to Gemini.
"""
import _env  # noqa: F401  (must be first)
import ast
import glob
import json
import os
import tempfile

import test_cam_facefile as tf
import web
from cam.analytics import CamAnalytics
from cam.facefile import SCHEMA
from voice import _constants as vc
from voice.cam_lookup import CamLookup, describe, parse_faces

REPO = _env.REPO


# ── names ───────────────────────────────────────────────────────────
def test_both_sides_agree_on_the_file_names():
    assert web.CAM_FACES_PATH.name == vc.CAM_FACES_FILENAME
    assert web.CAM_WATCH_PATH.name == vc.CAM_WATCH_FILENAME


def test_both_sides_agree_on_the_directory():
    assert str(web.CAM_FACES_PATH.parent) == str(web.VOICE_STATUS_PATH.parent)


def test_schema_version_matches():
    assert SCHEMA == 1


# ── the real writer feeds the real reader ───────────────────────────
def test_published_file_is_understood_by_the_voice_reader():
    a, det, pub, profiles, _ = tf._rig()
    profiles.set('Alice', {'greet': True, 'greeting': 'Welcome back, Alice.'})
    tf._look(a, det, [tf._face(0)])
    p = pub.build_payload()
    v = parse_faces(json.loads(json.dumps(p)), p['updated_at'])
    assert v.ok and v.roster == ('Alice', 'Bob')
    f = v.faces[0]
    assert (f['name'], f['stable'], f['greet'], f['greeting']) == \
        ('Alice', True, True, 'Welcome back, Alice.')
    assert v.greeter == {'enabled': True, 'min_sim': 0.5, 'cooldown_s': 300.0,
                         'greet_unknown': False}
    assert describe(v) == ('who_known', {'names': 'Alice'})


def test_offline_camera_round_trips_as_blind_not_empty():
    a, det, pub, _, _ = tf._rig(cam=tf.Cam(connected=False, age_ms=None))
    p = pub.build_payload()
    v = parse_faces(json.loads(json.dumps(p)), p['updated_at'])
    assert not v.ok and describe(v)[0] == 'who_blind'


def test_voice_wake_request_is_accepted_by_the_camera():
    """CamLookup._ask writes cam_watch.json; CamAnalytics.poll_watch must honour it."""
    d = tempfile.mkdtemp(dir=_env.TMP)
    CamLookup(d)._ask('announce_who')
    a = CamAnalytics(tf.Cam(), store=tf.FaceStore(os.path.join(d, 'db.pkl'), os.path.join(d, 't')),
                     watch_path=os.path.join(d, vc.CAM_WATCH_FILENAME))
    assert a.poll_watch() is True


# ── what these modules may not touch ────────────────────────────────
def _imports(path):
    tree = ast.parse(open(os.path.join(REPO, path)).read())
    names = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            names.update(a.name.split('.')[0] for a in n.names)
        elif isinstance(n, ast.ImportFrom):
            if n.level:                       # `from .x import` inside voice/
                names.add((n.module or '').split('.')[0])
            elif n.module:
                names.add(n.module.split('.')[0])
    return names


def _identifiers(path):
    tree = ast.parse(open(os.path.join(REPO, path)).read())
    return ({n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
            | {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)})


NO_MOTORS_NO_LLM = {'control_state', 'explain', 'agent', 'intent', 'web', 'cam', 'config'}
SPEECH_ONLY = ('voice/face_watcher.py', 'voice/cam_lookup.py', 'voice/speaker.py', 'voice/command.py')


def test_camera_speech_modules_cannot_reach_the_motors_or_gemini():
    """Same guard explain.py has: the module imports nothing that could drive a wheel
    (control_state, the intent validator) or send a name to a model (explain, agent)."""
    for path in SPEECH_ONLY:
        bad = _imports(path) & NO_MOTORS_NO_LLM
        assert not bad, f'{path} imports {sorted(bad)}'
        ids = _identifiers(path)
        assert not ({'write_state', 'validate_intent', 'Intent', 'EventExplainer'} & ids), path


def test_the_camera_package_never_imports_voice():
    for path in glob.glob(os.path.join(REPO, 'cam', '*.py')):
        assert 'voice' not in _imports(os.path.relpath(path, REPO)), path


def test_the_runner_keeps_names_away_from_the_narrator():
    """EventExplainer is built for the status lane only; it is never handed to a
    component that speaks names."""
    src = open(os.path.join(REPO, 'voice/run_phase0.py')).read()
    for call in ('FaceWatcher(', 'CommandListener('):
        seg = src[src.index(call):src.index(')', src.index(call))]
        assert 'explain' not in seg.lower(), call


def test_camera_routes_do_not_touch_control_state():
    tree = ast.parse(open(os.path.join(REPO, 'web.py')).read())
    for fn in ast.walk(tree):
        if isinstance(fn, ast.FunctionDef) and fn.name.startswith(('api_cam', 'cam_')):
            ids = {n.id for n in ast.walk(fn) if isinstance(n, ast.Name)} | \
                  {n.attr for n in ast.walk(fn) if isinstance(n, ast.Attribute)}
            assert not ({'write_state', 'control_state', '_CONTROL'} & ids), fn.name


if __name__ == '__main__':
    _env.run_tests(globals())
