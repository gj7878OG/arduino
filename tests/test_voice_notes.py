"""Tests for voice.notes — bounded store of recent captures."""

import _env  # noqa: F401

import io
import os
import struct
import tempfile
import wave

from voice.notes import MAX_NOTES, NOTE_ID_RE, NoteStore, record_notes_from
from voice.telemetry import VoiceTelemetry


def _wav(loud=True, n=16000):
    amp = 3000 if loud else 3
    pcm = b''.join(struct.pack('<h', amp if i % 2 else -amp) for i in range(n))
    buf = io.BytesIO()
    with wave.open(buf, 'wb') as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(16000); w.writeframes(pcm)
    return buf.getvalue()


def test_save_writes_a_playable_file_and_metadata():
    with tempfile.TemporaryDirectory() as d:
        st = NoteStore(d)
        m = st.save(_wav(), 3.0, 900.0, True)
        assert NOTE_ID_RE.match(m['id'])
        assert os.path.getsize(os.path.join(st.directory, m['id'] + '.wav')) == m['bytes']
        assert st.list()[0]['id'] == m['id'] and st.list()[0]['resolved'] is False


def test_store_is_bounded_and_deletes_old_files():
    with tempfile.TemporaryDirectory() as d:
        st = NoteStore(d, max_notes=3)
        ids = [st.save(_wav(), 1, 1, True)['id'] for _ in range(6)]
        assert [n['id'] for n in st.list()] == list(reversed(ids[-3:]))
        assert sorted(os.listdir(st.directory)) == sorted(i + '.wav' for i in ids[-3:])


def test_default_bound_matches_constant():
    with tempfile.TemporaryDirectory() as d:
        st = NoteStore(d)
        for _ in range(MAX_NOTES + 4):
            st.save(_wav(), 1, 1, True)
        assert len(st.list()) == MAX_NOTES


def test_directory_is_wiped_on_start():
    with tempfile.TemporaryDirectory() as d:
        first = NoteStore(d); first.save(_wav(), 1, 1, True)
        second = NoteStore(d)
        assert os.listdir(second.directory) == [] and second.list() == []


def test_empty_audio_is_not_saved():
    with tempfile.TemporaryDirectory() as d:
        assert NoteStore(d).save(b'', 1, 0, False) is None


def test_unwritable_dir_returns_none_not_crash():
    with tempfile.TemporaryDirectory() as d:
        st = NoteStore(d)
        st._dir = '/proc/nope'
        assert st.save(_wav(), 1, 1, True) is None


def test_attach_result_goes_to_newest_unresolved_note():
    with tempfile.TemporaryDirectory() as d:
        st = NoteStore(d)
        a = st.save(_wav(), 1, 1, True)['id']
        b = st.save(_wav(), 1, 1, True)['id']
        st.attach_result(text='stop', confidence=0.9)
        notes = {n['id']: n for n in st.list()}
        assert notes[b]['text'] == 'stop' and notes[b]['resolved']
        assert notes[a]['resolved'] is False
        st.attach_result(error='no speech recognised')
        assert {n['id']: n for n in st.list()}[a]['error'] == 'no speech recognised'


def test_returned_lists_are_copies():
    with tempfile.TemporaryDirectory() as d:
        st = NoteStore(d); st.save(_wav(), 1, 1, True)
        st.list()[0]['text'] = 'tampered'
        assert st.list()[0]['text'] == ''


class FakeMic:
    def __init__(self, wav): self.wav = wav
    def record(self, seconds=3.0): return self.wav
    def listen(self, seconds=3.0, meter=None):
        return self.record(seconds)


def test_gate_rejected_capture_is_still_saved():
    """The quiet captures are the ones worth listening to."""
    with tempfile.TemporaryDirectory() as d:
        st = NoteStore(d); mic = FakeMic(_wav(loud=False))
        record_notes_from(mic, st)
        assert mic.listen(3.0) == mic.wav          # behaviour unchanged
        n = st.list()[0]
        assert n['gate'] is False and n['peak_rms'] < 250


def test_loud_capture_marked_as_passing_gate():
    with tempfile.TemporaryDirectory() as d:
        st = NoteStore(d); mic = FakeMic(_wav(loud=True))
        record_notes_from(mic, st); mic.listen(3.0)
        assert st.list()[0]['gate'] is True and st.list()[0]['seconds'] == 3.0


def test_failed_capture_saves_nothing_and_returns_none():
    with tempfile.TemporaryDirectory() as d:
        st = NoteStore(d); mic = FakeMic(None)
        record_notes_from(mic, st)
        assert mic.record(3) is None and st.list() == []


def test_a_broken_store_never_breaks_capture():
    class Boom:
        def save(self, *a, **k): raise RuntimeError('disk full')
    mic = FakeMic(_wav()); record_notes_from(mic, Boom())
    assert mic.record(1) == mic.wav


def test_telemetry_copies_stt_result_onto_the_note():
    with tempfile.TemporaryDirectory() as d:
        st = NoteStore(d); tm = VoiceTelemetry(); tm.attach_notes(st)
        st.save(_wav(), 1, 900, True)
        tm.record_transcript('go forward', 0.93, 'deepgram', 'en', 800, 0.55)
        n = st.list()[0]
        assert n['text'] == 'go forward' and abs(n['confidence'] - 0.93) < 1e-6
        st.save(_wav(), 1, 900, True)
        tm.record_stt_error('no speech recognised in audio', 700)
        assert st.list()[0]['error'].startswith('no speech')


if __name__ == '__main__':
    _env.run_tests(globals())
