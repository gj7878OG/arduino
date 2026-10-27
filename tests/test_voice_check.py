"""voice.voice_check scoring and runner logic (no microphone, no network)."""
import _env  # noqa: F401

import struct
import wave
import io
from types import SimpleNamespace

from voice.stt import SttError
from voice.voice_check import SCRIPT, check_phrase, normalize, similarity, summarize


def _wav():
    b = io.BytesIO()
    with wave.open(b, 'wb') as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(16000)
        w.writeframes(struct.pack('<h', 300) * 1600)
    return b.getvalue()


def test_normalize_unifies_spelling_and_numbers():
    assert normalize('Go forward two metres.') == normalize('go forward 2 meters')
    assert normalize('Turn left ninety degrees') == normalize('turn left 90 degrees')


def test_decimals_survive():
    assert normalize('Go forward zero point five meters') == normalize('go forward 0.5 meters')


def test_wrong_words_score_low():
    assert similarity('Go forward two meters', 'Go backward ten meters') < 0.8
    assert similarity('Stop', 'Stop.') == 1.0


def test_script_has_no_empty_group_or_phrase():
    assert SCRIPT and all(SCRIPT[g] and all(p.strip() for p in SCRIPT[g]) for g in SCRIPT)


def _t(text, conf=0.95): return lambda pcm: SimpleNamespace(text=text, confidence=conf)


def test_pass_needs_match_and_confidence():
    ok = check_phrase('c', 'Go forward two meters', _wav, _t('go forward 2 metres'))
    assert ok.passed
    low = check_phrase('c', 'Go forward two meters', _wav, _t('go forward 2 metres', 0.3))
    assert not low.passed and low.heard
    wrong = check_phrase('c', 'Stop', _wav, _t('Start the engine'))
    assert not wrong.passed


def test_no_audio_and_errors_never_raise():
    assert check_phrase('c', 'Stop', lambda: None, _t('x')).error == 'no audio captured'
    def boom(pcm): raise SttError('no speech recognised in audio')
    r = check_phrase('c', 'Stop', _wav, boom)
    assert not r.passed and 'no speech' in r.error
    def net(pcm): raise ConnectionError('down')
    assert 'ConnectionError' in check_phrase('c', 'Stop', _wav, net).error


def test_summary_counts():
    rs = [check_phrase('a', 'Stop', _wav, _t('stop')), check_phrase('a', 'Stop', _wav, _t('nope'))]
    assert 'TOTAL     1/2' in summarize(rs)


if __name__ == '__main__':
    _env.run_tests(globals())
