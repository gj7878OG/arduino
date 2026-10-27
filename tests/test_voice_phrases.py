"""Phrase recorder logic (fake synth, no network) + checks on the shipped clips.

The shipped-clip tests are the important ones: a clip is audio, so nothing
else notices when the persona's wording changes and the recording still says
the old words.
"""
import _env  # noqa: F401

import io
import json
import struct
import tempfile
import wave
from pathlib import Path

from voice._constants import DYNAMIC_EVENTS, PERSONA_EVENTS
from voice.persona import render_event
from voice.record_phrases import (
    MANIFEST_NAME, load_manifest, phrase_text, plan, record_all, trim_silence, wav_seconds,
)
from voice.tts import PHRASES_DIR, PhraseCache
from voice.vocab import VOCAB
from voice.voice_check import similarity


def _wav(lead=0.5, tone=0.4, tail=0.5, rate=16000, amp=8000):
    n = lambda s: int(s * rate)
    samples = [0] * n(lead) + [amp if i % 20 < 10 else -amp for i in range(n(tone))] + [0] * n(tail)
    b = io.BytesIO()
    with wave.open(b, 'wb') as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(rate)
        w.writeframes(struct.pack(f'<{len(samples)}h', *samples))
    return b.getvalue()


# ── what gets recorded ──────────────────────────────────────────────


def test_slot_events_are_never_recordable():
    for e in ('battery_low', 'motors_enabled', 'mode_changed', 'who_strangers'):
        assert phrase_text(e) is None, e
    for e in DYNAMIC_EVENTS:
        assert phrase_text(e) is None, e


def test_unknown_event_is_not_recordable():
    assert phrase_text('self_destruct') is None


def test_lidar_hold_is_recorded_with_its_standing_reason():
    assert phrase_text('lidar_hold') == 'Holding — obstacle ahead. Will retry when clear.'


def test_plan_has_no_placeholders_and_covers_the_safety_phrases():
    p = plan()
    assert all('{' not in t for t in p.values())
    for must in ('emergency_stop', 'lidar_hold', 'stuck', 'motion_authorised', 'wake_ready'):
        assert must in p, must


def test_plan_only_restricts():
    assert list(plan(['emergency_stop'])) == ['emergency_stop']
    assert plan(['nope']) == {}


# ── trimming ────────────────────────────────────────────────────────


def test_trim_removes_silence_but_keeps_the_speech():
    raw = _wav()
    clip = trim_silence(raw)
    assert wav_seconds(raw) > 1.3 and 0.4 < wav_seconds(clip) < 0.8


def test_trim_leaves_silent_or_odd_audio_alone():
    silent = _wav(tone=0.0, lead=0.5, tail=0.0)
    assert trim_silence(silent) == silent
    assert trim_silence(b'not a wav') == b'not a wav'


def test_trim_keeps_a_quiet_consonant_before_a_loud_vowel():
    """Regression: a tight silence gate ate the 't' of 'ten' ('Term') and the
    'e' of 'eight' ('Eat'). The quiet onset must survive trimming."""
    rate = 16000
    quiet = [300 if i % 8 < 4 else -300 for i in range(int(0.05 * rate))]
    vowel = [9000 if i % 20 < 10 else -9000 for i in range(int(0.2 * rate))]
    samples = [0] * int(0.5 * rate) + quiet + vowel + [0] * int(0.5 * rate)
    b = io.BytesIO()
    with wave.open(b, 'wb') as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(rate)
        w.writeframes(struct.pack(f'<{len(samples)}h', *samples))
    out = trim_silence(b.getvalue())
    with wave.open(io.BytesIO(out), 'rb') as w:
        got = struct.unpack(f'<{w.getnframes()}h', w.readframes(w.getnframes()))
    assert any(250 <= abs(x) <= 350 for x in got), 'the quiet onset was trimmed away'


def test_readback_is_number_aware_but_still_strict():
    from voice.record_phrases import readback_match
    for e, h in (('thirteen', '13.'), ('seventy', '70.'), ('eighty', '80.'), ('twenty', '20'),
                 ('one hundred', '100.'), ('two', 'To.'), ('four', 'For.'), ('meter', 'Meter.')):
        assert readback_match(e, h) >= 0.8, (e, h)
    for e, h in (('thirteen', '14.'), ('ten', 'Term.'), ('eight', 'Eat.'), ('meters', 'Mira.')):
        assert readback_match(e, h) < 0.8, (e, h)


# ── recording ───────────────────────────────────────────────────────


def _record(d, events, synth=None, heard=None, **kw):
    synth = synth or (lambda text: _wav())
    transcribe = (lambda wav: heard) if heard is not None else (lambda wav: events_text[0])
    return record_all(Path(d), synth, transcribe, events, voice='test', match=similarity,
                      sleep=lambda s: None, **kw)


events_text = ['']


def test_good_clip_is_written_with_a_manifest_entry():
    with tempfile.TemporaryDirectory() as d:
        r = _record(d, {'wake_ready': 'Yes?'}, heard='Yes?')
        assert r['wake_ready']['status'] == 'recorded'
        assert (Path(d) / 'wake_ready.wav').is_file()
        m = load_manifest(Path(d))
        assert m['wake_ready']['text'] == 'Yes?' and m['wake_ready']['voice'] == 'test'


def test_a_clip_the_recogniser_cannot_read_back_is_not_written():
    with tempfile.TemporaryDirectory() as d:
        r = _record(d, {'emergency_stop': 'Stopped.'}, heard='Start the engine now')
        assert r['emergency_stop']['status'] == 'failed' and 'mismatch' in r['emergency_stop']['error']
        assert not (Path(d) / 'emergency_stop.wav').exists()
        assert 'emergency_stop' not in load_manifest(Path(d))


def test_failures_are_isolated_and_reported():
    def synth(text):
        if text == 'Bad.':
            return None
        return _wav()
    with tempfile.TemporaryDirectory() as d:
        r = _record(d, {'a': 'Bad.', 'b': 'Good.'}, synth=synth, heard='Good.')
        assert r['a']['status'] == 'failed' and r['b']['status'] == 'failed' or r['b']['status'] == 'recorded'
        assert r['a']['status'] == 'failed'


def test_synth_exception_is_a_failed_row_not_a_crash():
    def boom(text): raise ConnectionError('offline')
    with tempfile.TemporaryDirectory() as d:
        r = _record(d, {'wake_ready': 'Yes?'}, synth=boom, heard='Yes?')
        assert r['wake_ready']['status'] == 'failed' and 'offline' in r['wake_ready']['error']


def test_too_long_or_empty_clip_is_rejected():
    with tempfile.TemporaryDirectory() as d:
        long = _wav(lead=0.0, tone=8.0, tail=0.0)
        r = _record(d, {'a': 'Yes?'}, synth=lambda t: long, heard='Yes?')
        assert r['a']['status'] == 'failed'
        assert not (Path(d) / 'a.wav').exists()


def test_existing_current_clip_is_skipped_unless_forced():
    with tempfile.TemporaryDirectory() as d:
        _record(d, {'wake_ready': 'Yes?'}, heard='Yes?')
        calls = []
        again = record_all(Path(d), lambda t: calls.append(t) or _wav(), None, {'wake_ready': 'Yes?'},
                           sleep=lambda s: None)
        assert again['wake_ready']['status'] == 'skipped' and calls == []
        forced = record_all(Path(d), lambda t: calls.append(t) or _wav(), None, {'wake_ready': 'Yes?'},
                            force=True, sleep=lambda s: None)
        assert forced['wake_ready']['status'] == 'recorded' and calls == ['Yes?']


def test_synth_text_override_is_used_but_the_manifest_keeps_the_intended_words():
    seen = []
    with tempfile.TemporaryDirectory() as d:
        r = record_all(Path(d), lambda t: seen.append(t) or _wav(), None, {'num_8': 'eight'},
                       synth_text={'num_8': 'Eight.'}, sleep=lambda s: None)
        assert r['num_8']['status'] == 'recorded' and seen == ['Eight.']
        assert load_manifest(Path(d))['num_8']['text'] == 'eight'


def test_every_synth_override_targets_a_real_clip():
    from voice.vocab import SYNTH_TEXT
    assert SYNTH_TEXT and all(k in VOCAB for k in SYNTH_TEXT)


def test_changed_persona_text_triggers_a_rerecord():
    with tempfile.TemporaryDirectory() as d:
        _record(d, {'wake_ready': 'Yes?'}, heard='Yes?')
        calls = []
        r = record_all(Path(d), lambda t: calls.append(t) or _wav(), None,
                       {'wake_ready': 'Yes, operator?'}, sleep=lambda s: None)
        assert r['wake_ready']['status'] == 'recorded' and calls == ['Yes, operator?']
        assert load_manifest(Path(d))['wake_ready']['text'] == 'Yes, operator?'


def test_no_partial_file_left_behind():
    with tempfile.TemporaryDirectory() as d:
        _record(d, {'wake_ready': 'Yes?'}, heard='Yes?')
        assert not list(Path(d).glob('*.tmp'))


# ── the shipped clips ───────────────────────────────────────────────

_SHIPPED = sorted(p for p in PHRASES_DIR.glob('*.wav'))


def test_every_shipped_clip_is_a_valid_short_wav():
    for p in _SHIPPED:
        with wave.open(str(p), 'rb') as w:
            secs = w.getnframes() / w.getframerate()
            assert w.getnframes() > 0 and 0.12 <= secs <= 6.0, (p.name, secs)
            assert w.getnchannels() == 1 and w.getsampwidth() == 2, p.name


def test_every_shipped_clip_is_a_real_persona_event_without_a_slot():
    for p in _SHIPPED:
        assert p.stem in PERSONA_EVENTS or p.stem in VOCAB, f'{p.name} is not an event or vocabulary name'
        assert p.stem not in DYNAMIC_EVENTS, f'{p.name} has a live name slot'
        assert phrase_text(p.stem), f'{p.name} has a live slot — the clip would play without it'


def test_shipped_clips_say_the_current_persona_wording():
    """The reason this file exists. Change persona.py and forget to re-record,
    and Tarka keeps saying the old sentence out loud."""
    m = load_manifest(PHRASES_DIR)
    for p in _SHIPPED:
        assert p.stem in m, f'{p.name} has no manifest entry — re-run voice.record_phrases'
        assert m[p.stem]['text'] == phrase_text(p.stem), \
            f'{p.stem}: clip says {m[p.stem]["text"]!r} but persona now says {phrase_text(p.stem)!r}. ' \
            'Re-run: python -m voice.record_phrases'


def test_every_planned_clip_is_shipped():
    """Pack 1 + Pack 2 are complete: nothing the recorder plans is missing."""
    missing = [e for e in plan() if not (PHRASES_DIR / f'{e}.wav').is_file()]
    assert not missing, f'missing clips: {missing} — run python -m voice.record_phrases'


def test_every_answer_the_rover_can_give_plays_fully_offline():
    """The point of Pack 1 and 2: no status/obstacle answer needs the network."""
    import itertools
    from voice.intents import parse_rules
    from voice.respond import respond
    from voice.rover_state import RoverState
    cache = PhraseCache()
    states = [RoverState(fresh=f, enabled=en, mode=m, camera_connected=c, lidar_enabled=le,
                         lidar_connected=lc, lidar_age_ms=age, lidar_min_front_m=d, lidar_action=a)
              for f, en, m, c, le, lc, age, d, a in itertools.product(
                  (True, False), (True, False, None), ('auto', 'manual', None), (True, False),
                  (True, False), (True,), (50, 9000), (None, 0.05, 0.4, 1.0, 1.4, 12.5, 99.0),
                  ('CLEAR', 'SLOW', 'ESTOP'))]
    for text in ('status', 'how far is the nearest obstacle', 'what mode are you in', 'who are you',
                 'what is the battery level', 'go forward two meters', 'disable lidar',
                 'go forward', 'where are you', 'sing me a song'):
        for s in states[::37] + states[:3]:
            for key in respond(parse_rules(text), s).keys:
                assert key in cache, f'{key!r} (for {text!r}) is not in the phrase cache'


def test_manifest_has_no_entry_without_a_clip():
    for event in load_manifest(PHRASES_DIR):
        assert (PHRASES_DIR / f'{event}.wav').is_file(), f'manifest lists missing clip {event}'


def test_phrase_cache_loads_the_shipped_clips():
    cache = PhraseCache()
    for p in _SHIPPED:
        assert p.stem in cache


if __name__ == '__main__':
    _env.run_tests(globals())
