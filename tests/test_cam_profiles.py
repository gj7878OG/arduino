"""Tests for cam/profiles.py — per-person info (role, greeting, notes, opt-in)."""
import _env  # noqa: F401  (must be first)
import json
import os
import stat
import tempfile

from cam.profiles import ProfileStore, clean_text, public_view


def _store():
    d = tempfile.mkdtemp(dir=_env.TMP)
    path = os.path.join(d, 'profiles.json')
    return ProfileStore(path), path


def test_unknown_person_gets_a_private_blank_profile():
    s, _ = _store()
    p = s.get('Nobody')
    assert p['greet'] is False and p['greeting'] == '' and p['role'] == '' and p['notes'] == ''


def test_set_merges_and_stamps_time():
    s, _ = _store()
    s.set('Alice', {'role': 'Operator'})
    p = s.set('Alice', {'greeting': 'Hi Alice'})
    assert p['role'] == 'Operator' and p['greeting'] == 'Hi Alice' and p['updated_at']


def test_greet_defaults_off_and_must_be_a_real_boolean():
    """Opt-in to being named aloud: 'yes', 1 and 'true' are not consent."""
    s, _ = _store()
    assert s.set('Alice', {'role': 'x'})['greet'] is False
    for bad in ('true', 1, 'yes', None):
        try:
            s.set('Alice', {'greet': bad})
        except ValueError:
            continue
        raise AssertionError(f'accepted greet={bad!r}')
    assert s.set('Alice', {'greet': True})['greet'] is True


def test_text_is_capped_and_single_line():
    s, _ = _store()
    p = s.set('Alice', {'role': 'r' * 100, 'greeting': 'g' * 200, 'notes': 'n' * 900})
    assert (len(p['role']), len(p['greeting']), len(p['notes'])) == (40, 80, 300)


def test_control_characters_and_newlines_are_stripped():
    """A greeting is spoken aloud: no newlines, bells or escape codes get through."""
    assert clean_text('Hi\n\x07there\x1b[31m you', 80) == 'Hi there [31m you'
    assert clean_text(None, 10) == '' and clean_text(5, 10) == ''


def test_unknown_fields_are_ignored():
    s, _ = _store()
    assert 'admin' not in s.set('Alice', {'admin': True, 'role': 'x'})


def test_persists_and_reloads():
    s, path = _store()
    s.set('Alice', {'role': 'Op', 'greet': True})
    again = ProfileStore(path)
    assert again.get('Alice')['role'] == 'Op' and again.get('Alice')['greet'] is True


def test_file_is_owner_only():
    """Notes about people: not for other local users."""
    s, path = _store()
    s.set('Alice', {'notes': 'private'})
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600


def test_corrupt_file_starts_empty():
    d = tempfile.mkdtemp(dir=_env.TMP)
    path = os.path.join(d, 'p.json')
    open(path, 'w').write('{nope')
    assert ProfileStore(path).get('Alice')['greet'] is False


def test_hostile_file_values_are_normalised_on_load():
    d = tempfile.mkdtemp(dir=_env.TMP)
    path = os.path.join(d, 'p.json')
    json.dump({'Alice': {'greeting': 'x\x07' * 100, 'greet': 'true', 'role': 5}, 'Bad': 7},
              open(path, 'w'))
    s = ProfileStore(path)
    a = s.get('Alice')
    assert a['greet'] is False and a['role'] == '' and '\x07' not in a['greeting']
    assert len(a['greeting']) <= 80


def test_remove_and_prune():
    s, _ = _store()
    s.set('Alice', {'greet': True})
    s.set('Bob', {'greet': True})
    s.remove('Alice')
    assert s.get('Alice')['greet'] is False and s.get('Bob')['greet'] is True
    s.prune(['Carol'])
    assert s.get('Bob')['greet'] is False


def test_notes_are_redacted_for_signed_out_readers():
    p = {'role': 'Op', 'greeting': 'Hi', 'notes': 'private', 'greet': True}
    assert public_view(p, False)['notes'] == '' and public_view(p, True)['notes'] == 'private'
    assert public_view(p, False)['role'] == 'Op'


if __name__ == '__main__':
    _env.run_tests(globals())
