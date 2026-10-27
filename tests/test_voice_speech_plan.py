"""Number composition, splicing and plan speaking (no network, no audio)."""
import _env  # noqa: F401

import contextlib
import io
import struct
import tempfile
import wave
from pathlib import Path
from types import SimpleNamespace

from voice.speech_plan import (
    GAP_S, Plan, PlanSpeaker, Segment, distance_plan, event_plan, number_segments, splice,
)
from voice.vocab import VOCAB


def _say(n):
    return ' '.join(s.text for s in number_segments(n))


# ── numbers ─────────────────────────────────────────────────────────

NUMBERS = [(0, 'zero'), (7, 'seven'), (13, 'thirteen'), (20, 'twenty'), (25, 'twenty five'),
           (30, 'thirty'), (99, 'ninety nine'), (1.4, 'one point four'), (0.4, 'zero point four'),
           (12.5, 'twelve point five'), (99.9, 'ninety nine point nine'), (2.0, 'two'),
           (2.04, 'two'), (2.06, 'two point one')]


def test_numbers_are_spoken_in_words():
    for n, words in NUMBERS:
        assert _say(n) == words, (n, _say(n))


def test_unspeakable_numbers_are_none_not_approximated():
    for n in (-1, -0.1, 100, 1000, float('nan'), float('inf'), 'abc', None, [1]):
        assert number_segments(n) is None, n


def test_every_number_up_to_99_point_9_uses_only_vocabulary_clips():
    for i in range(0, 1000):
        for seg in number_segments(i / 10):
            assert seg.key in VOCAB and VOCAB[seg.key] == seg.text, (i, seg)


# ── distances ───────────────────────────────────────────────────────

DISTANCES = [(0.04, 'Obstacle ahead at ten centimeters.'), (0.2, 'Obstacle ahead at twenty centimeters.'),
             (0.53, 'Obstacle ahead at fifty centimeters.'), (0.94, 'Obstacle ahead at ninety centimeters.'),
             (1.0, 'Obstacle ahead at one meter.'), (1.4, 'Obstacle ahead at one point four meters.'),
             (12.5, 'Obstacle ahead at twelve point five meters.')]


def test_distances():
    for m, text in DISTANCES:
        assert distance_plan(m).text == text, (m, distance_plan(m).text)


def test_distance_never_rounds_a_close_obstacle_to_something_further():
    """Under a metre it rounds to the nearest ten cm, never invents a larger gap."""
    for cm in range(10, 95):
        said = distance_plan(cm / 100).segments
        spoken_cm = int(''.join(c for c in ' '.join(s.key for s in said) if c.isdigit()) or 0)
        assert spoken_cm - cm <= 5.0 + 1e-6, (cm, spoken_cm)


def test_distance_unspeakable_is_none():
    for m in (-1, float('nan'), 150, 'x', None):
        assert distance_plan(m) is None, m


# ── plan text ───────────────────────────────────────────────────────


def test_plan_text_keeps_sentence_boundaries():
    p = event_plan('motors_disabled') + event_plan('status_mode_auto') + distance_plan(1.4)
    assert p.text == 'Motors off. Auto mode. Obstacle ahead at one point four meters.'


def test_empty_and_unknown_plans():
    assert not event_plan('no_such_event') and event_plan('no_such_event').text == ''
    assert not Plan(())


# ── splicing ────────────────────────────────────────────────────────


def _wav(path, seconds=0.2, rate=16000, channels=1, amp=1000):
    n = int(seconds * rate) * channels
    with wave.open(str(path), 'wb') as w:
        w.setnchannels(channels); w.setsampwidth(2); w.setframerate(rate)
        w.writeframes(struct.pack(f'<{n}h', *([amp] * n)))


def _secs(data):
    with wave.open(io.BytesIO(data), 'rb') as w:
        return w.getnframes() / w.getframerate()


def test_splice_joins_clips_with_a_short_gap():
    with tempfile.TemporaryDirectory() as d:
        a, b, c = (Path(d) / f'{n}.wav' for n in 'abc')
        for p in (a, b, c):
            _wav(p, 0.2)
        out = splice([a, b, c])
        assert abs(_secs(out) - (0.6 + 2 * GAP_S)) < 0.01


def test_splice_refuses_mismatched_formats_and_bad_files():
    with tempfile.TemporaryDirectory() as d:
        a, b = Path(d) / 'a.wav', Path(d) / 'b.wav'
        _wav(a, rate=16000); _wav(b, rate=24000)
        assert splice([a, b]) is None
        assert splice([a, Path(d) / 'missing.wav']) is None
        (Path(d) / 'junk.wav').write_bytes(b'not a wav')
        assert splice([a, Path(d) / 'junk.wav']) is None
        assert splice([]) is None


# ── speaking ────────────────────────────────────────────────────────


class FakeEngine:
    def __init__(self, cached=(), synth=True, play_ok=True, root=None):
        self.root, self.cached, self.synth_available, self.play_ok = root, set(cached), synth, play_ok
        self.played_audio, self.spoken, self.holds = [], [], 0
        self.cache = SimpleNamespace(path_for=self._path)

    def _path(self, key):
        if key not in self.cached:
            return None
        p = Path(self.root) / f'{key}.wav'
        if not p.exists():
            _wav(p, 0.1)
        return p

    def play_audio(self, audio):
        self.played_audio.append(audio)
        return self.play_ok

    def speak(self, key, text, event=''):
        self.spoken.append((key, text, event))
        ok = self.synth_available or key in self.cached
        return SimpleNamespace(played=ok)

    @contextlib.contextmanager
    def exclusive(self):
        self.holds += 1
        yield


def _plan(*keys):
    return Plan(tuple(Segment(k, VOCAB.get(k, k)) for k in keys))


def test_fully_cached_multi_clip_plan_is_spliced_and_played_offline():
    with tempfile.TemporaryDirectory() as d:
        e = FakeEngine(cached={'frag_obstacle_at', 'num_1', 'unit_meter'}, synth=False, root=d)
        out = PlanSpeaker(e).say(_plan('frag_obstacle_at', 'num_1', 'unit_meter'))
        assert out.played and out.source == 'cache' and len(e.played_audio) == 1 and e.spoken == []


def test_a_missing_clip_falls_back_to_live_synthesis_of_the_same_words():
    with tempfile.TemporaryDirectory() as d:
        e = FakeEngine(cached={'frag_obstacle_at', 'num_1'}, synth=True, root=d)
        out = PlanSpeaker(e).say(_plan('frag_obstacle_at', 'num_1', 'unit_meter'), event='x')
        assert out.played and out.source == 'synth' and e.played_audio == []
        assert e.spoken[0][1] == 'Obstacle ahead at one meter.'


def test_missing_clip_and_no_synthesis_says_nothing_not_half_a_sentence():
    with tempfile.TemporaryDirectory() as d:
        e = FakeEngine(cached={'frag_obstacle_at', 'num_1'}, synth=False, root=d)
        out = PlanSpeaker(e).say(_plan('frag_obstacle_at', 'num_1', 'unit_meter'))
        assert not out.played and out.source == 'none'
        assert e.played_audio == [] and e.spoken == []


def test_playback_failure_falls_back_to_synthesis():
    with tempfile.TemporaryDirectory() as d:
        e = FakeEngine(cached={'num_1', 'num_2'}, synth=True, play_ok=False, root=d)
        out = PlanSpeaker(e).say(_plan('num_1', 'num_2'))
        assert out.played and out.source == 'synth'


def test_single_cached_clip_uses_the_engine_cache_path():
    with tempfile.TemporaryDirectory() as d:
        e = FakeEngine(cached={'confirmed'}, root=d)
        out = PlanSpeaker(e).say(event_plan('confirmed'))
        assert out.played and out.source == 'cache' and e.spoken[0][0] == 'confirmed'


def test_the_speaker_is_held_for_the_whole_answer():
    with tempfile.TemporaryDirectory() as d:
        e = FakeEngine(cached={'num_1', 'num_2'}, root=d)
        PlanSpeaker(e).say(_plan('num_1', 'num_2'))
        assert e.holds == 1


def test_empty_plan_says_nothing_and_telemetry_is_recorded_otherwise():
    class Tm:
        def __init__(self): self.rows = []
        def record_narration(self, *a): self.rows.append(a)
    with tempfile.TemporaryDirectory() as d:
        tm, e = Tm(), FakeEngine(cached={'confirmed'}, root=d)
        sp = PlanSpeaker(e, tm)
        assert not sp.say(Plan(())).played and tm.rows == []
        sp.say(event_plan('confirmed'), event='answer')
        assert tm.rows and tm.rows[0][0] == 'answer' and tm.rows[0][1] == 'Confirmed.'


def test_broken_telemetry_never_breaks_speech():
    class Boom:
        def record_narration(self, *a): raise RuntimeError('x')
    with tempfile.TemporaryDirectory() as d:
        e = FakeEngine(cached={'confirmed'}, root=d)
        assert PlanSpeaker(e, Boom()).say(event_plan('confirmed')).played


if __name__ == '__main__':
    _env.run_tests(globals())
