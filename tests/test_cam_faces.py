"""Tests for cam/faces.py — enrolled-face storage and matching."""
import _env  # noqa: F401  (must be first)
import os
import pickle
import tempfile

import numpy as np

from cam.faces import FaceStore, make_thumb, valid_name


def _store():
    d = tempfile.mkdtemp(dir=_env.TMP)
    return FaceStore(os.path.join(d, 'db.pkl'), os.path.join(d, 'thumbs')), d


def _vec(i, n=128):
    v = np.zeros((1, n), np.float32)
    v[0, i] = 1.0
    return v


def test_valid_name_accepts_ordinary_names():
    assert all(valid_name(n) for n in ('Alice', 'Bob Smith', 'a.b-c_d', 'X1'))


def test_valid_name_rejects_paths_and_junk():
    """A name becomes part of a filename and a URL: nothing path-like gets in."""
    bad = ('', ' lead', '../etc/passwd', 'a/b', 'a\\b', '.hidden', 'x' * 33, '<b>', None, 7)
    assert not any(valid_name(n) for n in bad)


def test_match_finds_the_closest_person():
    s, _ = _store()
    s.add('Alice', [_vec(0)])
    s.add('Bob', [_vec(1)])
    name, sim = s.match(_vec(1))
    assert name == 'Bob' and abs(sim - 1.0) < 1e-5


def test_match_is_cosine_not_dot_product():
    """Scaling a feature must not change who it matches or how well."""
    s, _ = _store()
    s.add('Alice', [_vec(0) * 5.0])
    name, sim = s.match(_vec(0) * 0.1)
    assert name == 'Alice' and abs(sim - 1.0) < 1e-5


def test_match_on_empty_store_is_none():
    s, _ = _store()
    assert s.match(_vec(0)) == (None, 0.0)


def test_add_appends_samples_to_an_existing_person():
    s, _ = _store()
    s.add('Alice', [_vec(0)])
    assert s.add('Alice', [_vec(1), _vec(2)]) == 3
    assert [p['samples'] for p in s.people()] == [3]


def test_remove_forgets_the_person_and_their_thumbnail():
    s, d = _store()
    s.add('Alice', [_vec(0)], b'\xff\xd8jpeg')
    assert s.thumb('Alice') == b'\xff\xd8jpeg'
    assert s.remove('Alice') is True
    assert s.people() == [] and s.thumb('Alice') is None
    assert s.match(_vec(0)) == (None, 0.0)
    assert s.remove('Alice') is False


def test_persists_across_instances():
    s, d = _store()
    s.add('Alice', [_vec(0), _vec(1)], b'jpg')
    again = FaceStore(os.path.join(d, 'db.pkl'), os.path.join(d, 'thumbs'))
    assert again.people()[0]['name'] == 'Alice' and again.people()[0]['samples'] == 2
    assert again.match(_vec(0))[0] == 'Alice'


def test_reads_the_cli_pickle_format():
    """face_recognize.py writes {name: [(1,128) float32, ...]}; both tools share it."""
    d = tempfile.mkdtemp(dir=_env.TMP)
    path = os.path.join(d, 'db.pkl')
    with open(path, 'wb') as f:
        pickle.dump({'Cli Person': [_vec(5), _vec(6)]}, f)
    s = FaceStore(path, os.path.join(d, 'thumbs'))
    assert s.people()[0]['name'] == 'Cli Person'
    assert s.match(_vec(6))[0] == 'Cli Person'


def test_corrupt_db_starts_empty_instead_of_crashing():
    d = tempfile.mkdtemp(dir=_env.TMP)
    path = os.path.join(d, 'db.pkl')
    open(path, 'wb').write(b'not a pickle')
    assert FaceStore(path, os.path.join(d, 't')).people() == []


def test_add_rejects_bad_input():
    s, _ = _store()
    for args in (('../x', [_vec(0)]), ('Alice', [])):
        try:
            s.add(*args)
        except ValueError:
            continue
        raise AssertionError(f'accepted {args[0]!r} {len(args[1])} samples')


def test_save_leaves_no_temp_file_behind():
    s, d = _store()
    s.add('Alice', [_vec(0)])
    assert os.listdir(d) == ['db.pkl'], os.listdir(d)   # no db.pkl.tmp left over


def test_make_thumb_is_a_small_square_jpeg():
    import cv2
    frame = np.full((480, 640, 3), 90, np.uint8)
    jpg = make_thumb(frame, [200, 150, 120, 150])
    img = cv2.imdecode(np.frombuffer(jpg, np.uint8), 1)
    assert img.shape[:2] == (96, 96), img.shape


def test_make_thumb_refuses_an_off_screen_box():
    assert make_thumb(np.zeros((480, 640, 3), np.uint8), [900, 900, 50, 50]) is None


if __name__ == '__main__':
    _env.run_tests(globals())
