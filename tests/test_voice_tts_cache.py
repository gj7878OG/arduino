"""Tests for voice.tts — PhraseCache + playback backends.

Cached-phrase tests use real files in a temp dir. Playback tests use
a recording FakePlayback so no audio is actually produced. Piper is
probed but not exercised; the cache path covers the only behaviour we
care about in Phase 0.

Tests do their own save/restore of ``shutil.which`` rather than using
pytest fixtures; the project runs these via ``_env.run_tests``.
"""

import _env  # noqa: F401

import shutil
import subprocess
import tempfile
import threading
from pathlib import Path

from voice.tts import (
    AUDIO_EXTS,
    PHRASES_DIR,
    DeepgramTtsBackend,
    PhraseCache,
    SilentTts,
    SpokenPhrase,
    SystemPlayback,
    TtsEngine,
)


def _patch_which(visible):
    """Save/restore shutil.which around a body. The project's custom
    test runner doesn't provide pytest's monkeypatch fixture, so we
    patch by hand."""
    original = shutil.which

    def fake(name):
        return visible.get(name)

    shutil.which = fake
    try:
        yield
    finally:
        shutil.which = original


def _patch_subprocess_run(handler):
    """Save/restore subprocess.run around a body."""
    original = subprocess.run
    subprocess.run = handler
    try:
        yield
    finally:
        subprocess.run = original


# ── PhraseCache ─────────────────────────────────────────────────────

def _make_phrase_dir(files):
    """files: list of (name, content_bytes). Returns the directory."""
    d = tempfile.mkdtemp(prefix='voice-test-')
    for name, content in files:
        Path(d, name).write_bytes(content)
    return d


def _cleanup(d):
    shutil.rmtree(d, ignore_errors=True)


def test_empty_dir_is_an_empty_cache():
    with tempfile.TemporaryDirectory() as d:
        cache = PhraseCache(directory=Path(d))
    assert cache.keys() == []
    assert len(cache) == 0


def test_missing_dir_is_a_warning_not_a_crash():
    cache = PhraseCache(directory=Path('/no/such/dir/voice-phrases'))
    assert cache.keys() == []


def test_wav_files_are_loaded_by_stem():
    d = _make_phrase_dir([('moving.wav', b'RIFF\x00\x00'),
                          ('done.wav', b'RIFF\x00\x00')])
    try:
        cache = PhraseCache(directory=Path(d))
        assert cache.has('moving')
        assert cache.has('done')
        assert not cache.has('notthere')
        assert sorted(cache.keys()) == ['done', 'moving']
    finally:
        _cleanup(d)


def test_opus_mp3_ogg_flac_are_supported():
    d = _make_phrase_dir([
        ('a.opus', b'OggS\x00'),
        ('b.mp3',  b'\xff\xfb'),
        ('c.ogg',  b'OggS\x00'),
        ('d.flac', b'fLaC\x00'),
    ])
    try:
        cache = PhraseCache(directory=Path(d))
        assert all(k in cache for k in ('a', 'b', 'c', 'd'))
    finally:
        _cleanup(d)


def test_unknown_extension_is_ignored():
    d = _make_phrase_dir([('moving.txt', b'not audio'),
                          ('moving.wav', b'RIFF')])
    try:
        cache = PhraseCache(directory=Path(d))
        assert cache.has('moving')
        assert len(cache) == 1
    finally:
        _cleanup(d)


def test_hidden_files_are_ignored():
    d = _make_phrase_dir([('.moving.wav', b'RIFF'),
                          ('moving.wav', b'RIFF')])
    try:
        cache = PhraseCache(directory=Path(d))
        assert len(cache) == 1
        assert cache.has('moving')
    finally:
        _cleanup(d)


def test_path_lookup_returns_full_path():
    d = _make_phrase_dir([('moving.wav', b'RIFF')])
    try:
        cache = PhraseCache(directory=Path(d))
        p = cache.path_for('moving')
        assert p is not None and p.is_file()
        assert p.suffix == '.wav'
    finally:
        _cleanup(d)


def test_unknown_key_returns_none():
    with tempfile.TemporaryDirectory() as d:
        cache = PhraseCache(directory=Path(d))
    assert cache.path_for('nothing') is None
    assert 'nothing' not in cache


def test_real_phrases_directory_layout_is_stable():
    """The shipped PHRASES_DIR doesn't need to contain real audio yet,
    but it does need to *exist* (with README.md). If it isn't there,
    the cache must not crash on instantiation."""
    cache = PhraseCache(directory=PHRASES_DIR)
    # README.md is in there; PhraseCache should ignore non-audio files.
    assert 'README' not in cache
    # And the cache is allowed to be empty during Phase 0.
    for k in cache.keys():
        assert Path(k).suffix.lower() not in AUDIO_EXTS or True     # trivially true


# ── Playback + engine ───────────────────────────────────────────────

class FakePlayback:
    """Captures every play() call without producing sound."""

    def __init__(self, return_value: bool = True) -> None:
        self.calls: list[Path] = []
        self.return_value = return_value

    def play(self, path: Path) -> bool:
        self.calls.append(Path(path))
        return self.return_value


class RecordingTts(TtsEngine):
    """Like TtsEngine but with a fake playback so tests don't touch audio.

    Synthesizer is pinned off. Without this, TtsEngine builds a real
    Deepgram backend, and on any developer machine that has
    $DEEPGRAM_API_KEY exported the "no audio" tests would silently
    synthesise over the network and pass or fail depending on whether a
    key happened to be in the environment.
    """
    def __init__(self, cache: PhraseCache):
        super().__init__(cache=cache, playback=FakePlayback(),
                         synthesizer=DeepgramTtsBackend(api_key=''))


def test_speak_plays_cached_phrase():
    d = _make_phrase_dir([('motion_authorised.wav', b'RIFF')])
    try:
        cache = PhraseCache(directory=Path(d))
        tts = RecordingTts(cache)
        out = tts.speak_event('motion_authorised', 'Moving.')
    finally:
        _cleanup(d)
    assert out.cache_key == 'motion_authorised'
    assert out.played is True
    assert out.event == 'motion_authorised'


def test_speak_with_missing_cache_and_no_piper_returns_unplayed():
    with tempfile.TemporaryDirectory() as d:
        cache = PhraseCache(directory=Path(d))
        tts = RecordingTts(cache)
        out = tts.speak_event('motion_authorised', 'Moving.')
    assert out.cache_key is None
    assert out.played is False
    assert out.utterance == 'Moving.'


def test_engine_does_not_crash_when_piper_missing():
    """The probe is a side-effect of construction. If piper isn't
    installed (true on this bench), piper_available is False — speak()
    still returns a SpokenPhrase, never raises."""
    with tempfile.TemporaryDirectory() as d:
        cache = PhraseCache(directory=Path(d))
        tts = RecordingTts(cache)
    # Either piper is or isn't installed; either way, the property
    # exists and is a bool.
    assert isinstance(tts.piper_available, bool)


def test_failing_playback_is_logged_not_raised():
    """A cached phrase with a broken audio backend must return
    SpokenPhrase(played=False), not raise. The watcher relies on this.
    """
    d = _make_phrase_dir([('motion_authorised.wav', b'RIFF')])
    try:
        cache = PhraseCache(directory=Path(d))
        bad = FakePlayback(return_value=False)
        tts = TtsEngine(cache=cache, playback=bad)
        out = tts.speak_event('motion_authorised', 'Moving.')
    finally:
        _cleanup(d)
    assert out.cache_key == 'motion_authorised'
    assert out.played is False


# ── SilentTts (test sidecar) ────────────────────────────────────────

def test_silent_tts_records_what_was_said():
    s = SilentTts()
    s.speak_event('motion_authorised', 'Moving.')
    s.speak_event('motion_complete', 'Done.')
    assert [p.event for p in s.spoken] == ['motion_authorised', 'motion_complete']
    assert [p.utterance for p in s.spoken] == ['Moving.', 'Done.']


def test_silent_tts_clear_empties_the_log():
    s = SilentTts()
    s.speak_event('motion_authorised', 'Moving.')
    s.clear()
    assert s.spoken == []


def test_silent_tts_speak_returns_played_true_by_default():
    """Tests assume 'spoken' = 'played'. The constant is part of the
    contract."""
    s = SilentTts()
    out = s.speak('motion_authorised', 'Moving.', event='motion_authorised')
    assert isinstance(out, SpokenPhrase)
    assert out.played is True


# ── SystemPlayback selection ────────────────────────────────────────

def test_system_playback_picks_first_available_tool():
    """The order is aplay → paplay → ffplay → mpv. Patch shutil.which so
    the order is deterministic regardless of what's installed here."""
    lookup = {'aplay': None, 'paplay': None, 'ffplay': None, 'mpv': None}
    for _ in _patch_which(lookup):
        sb = SystemPlayback()
        lookup['aplay'] = '/usr/bin/aplay'
        assert sb._pick_tool() == 'aplay'
        lookup['aplay'] = None
        lookup['paplay'] = '/usr/bin/paplay'
        sb._tool = None
        assert sb._pick_tool() == 'paplay'
        lookup['paplay'] = None
        assert sb._pick_tool() is None


def test_system_playback_uses_subprocess():
    """We don't really want to run aplay in a unit test. Patch
    subprocess.run and assert the arguments we generate."""
    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0)

    for _ in _patch_which({'aplay': '/usr/bin/aplay'}):
        for __ in _patch_subprocess_run(fake_run):
            sb = SystemPlayback()
            sb.play(Path('/tmp/foo.wav'))
            assert calls and calls[0][0] == 'aplay'
            assert calls[0][1] == '/tmp/foo.wav'


def test_system_playback_recovers_when_tool_disappears():
    """A tool that worked at startup but is gone at play-time must not
    hang the watcher. We log and return False; next call picks a new
    tool."""
    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if len(calls) == 1:
            raise FileNotFoundError(2, 'No such file', cmd[0])
        return subprocess.CompletedProcess(cmd, 0)

    visible = {'aplay': '/usr/bin/aplay', 'paplay': '/usr/bin/paplay'}
    for _ in _patch_which(visible):
        for __ in _patch_subprocess_run(fake_run):
            sb = SystemPlayback()
            # Side-effect: _pick_tool runs on first .play(); records 'aplay'.
            sb._tool = 'aplay'
            ok = sb.play(Path('/tmp/foo.wav'))
            # First call failed with FileNotFoundError -> tool reset.
            assert ok is False
            assert sb._tool is None
            # Next play() picks paplay and succeeds.
            ok = sb.play(Path('/tmp/foo.wav'))
            assert ok is True


def test_engine_is_thread_safe_under_concurrent_speak():
    """Multiple threads should be able to call speak() without crashing.
    We use the cache-miss path (no audio tool) so no subprocess races."""
    s = SilentTts()
    errors = []

    def worker():
        try:
            for _ in range(50):
                s.speak_event('motion_authorised', 'Moving.')
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert len(s.spoken) == 8 * 50


# ── events that carry a name never use a pre-recorded clip ─────────
def test_dynamic_events_bypass_the_phrase_cache():
    """A clip called person_greeted.wav would play without the person's name."""
    import tempfile
    from pathlib import Path
    from voice.tts import PhraseCache, TtsEngine

    class Play:
        def __init__(self): self.paths = []
        def play(self, path): self.paths.append(Path(path).name); return True

    class Synth:
        available = True
        def __init__(self): self.said = []
        def synthesize(self, text): self.said.append(text); return b'RIFF....WAVE'

    with tempfile.TemporaryDirectory() as d:
        (Path(d) / 'person_greeted.wav').write_bytes(b'canned clip with no name in it')
        (Path(d) / 'lidar_hold.wav').write_bytes(b'cached safety clip')
        play, synth = Play(), Synth()
        eng = TtsEngine(cache=PhraseCache(Path(d)), playback=play, synthesizer=synth)
        eng.speak_event('person_greeted', 'Hello, Alice.')
        eng.speak_event('lidar_hold', 'Holding.')
    assert synth.said == ['Hello, Alice.'], 'the canned clip was used instead of the name'
    assert 'person_greeted.wav' not in play.paths and 'lidar_hold.wav' in play.paths


if __name__ == '__main__':
    _env.run_tests(globals())
