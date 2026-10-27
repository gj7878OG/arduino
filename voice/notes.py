"""Voice notes: the last few microphone captures, kept so they can be played back.

Why: the loudness gate passes anything loud, and Deepgram sometimes then
hears no speech. The only way to know whether the C270 is capturing speech,
fan noise or silence is to *listen to it*. The dashboard's Voice tab lists
these notes with a Play button.

Privacy and bounds — this is room audio:

  * only the newest ``MAX_NOTES`` are kept; older files are deleted;
  * the directory is wiped when the voice process starts;
  * the dashboard serves them only to a signed-in session;
  * ids are generated here and validated on the web side, never taken
    from a filename the browser chose.

Nothing here imports from the main project.
"""

from __future__ import annotations

import logging
import os
import re
import secrets
import shutil
import threading
import time
from typing import Any, Callable, Optional

from .mic import LevelMeter

log = logging.getLogger(__name__)

NOTES_DIRNAME = 'voice_notes'
MAX_NOTES = 10
NOTE_ID_RE = re.compile(r'^[0-9]{10,14}-[0-9a-f]{4}$')


class NoteStore:
    """Bounded on-disk store of recent captures plus their metadata."""

    def __init__(self, out_dir: str, max_notes: int = MAX_NOTES,
                 clock: Callable[[], float] = time.time) -> None:
        self._dir = os.path.join(out_dir, NOTES_DIRNAME)
        self._max = max_notes
        self._clock = clock
        self._lock = threading.Lock()
        self._notes: list[dict] = []          # oldest first
        shutil.rmtree(self._dir, ignore_errors=True)
        os.makedirs(self._dir, exist_ok=True)

    @property
    def directory(self) -> str:
        return self._dir

    def save(self, wav: bytes, seconds: float, peak_rms: float,
             passed_gate: bool) -> Optional[dict]:
        """Store one capture. Returns its metadata, or None on failure."""
        if not wav:
            return None
        note_id = f'{int(self._clock() * 1000)}-{secrets.token_hex(2)}'
        path = os.path.join(self._dir, note_id + '.wav')
        try:
            with open(path, 'wb') as fh:
                fh.write(wav)
        except OSError as exc:
            log.warning('voice note not saved: %s', exc)
            return None
        meta = {'id': note_id, 't': self._clock(), 'seconds': round(seconds, 1),
                'bytes': len(wav), 'peak_rms': round(float(peak_rms), 1),
                'gate': bool(passed_gate), 'text': '', 'confidence': None,
                'error': '', 'resolved': False}
        with self._lock:
            self._notes.append(meta)
            while len(self._notes) > self._max:
                old = self._notes.pop(0)
                try:
                    os.unlink(os.path.join(self._dir, old['id'] + '.wav'))
                except OSError:
                    pass
        return dict(meta)

    def attach_result(self, text: str = '', confidence: Optional[float] = None,
                      error: str = '') -> None:
        """Record what STT made of the newest note that has no result yet."""
        with self._lock:
            for note in reversed(self._notes):
                if not note['resolved']:
                    note.update(text=text, confidence=confidence,
                                error=error[:160], resolved=True)
                    return

    def list(self) -> list[dict]:
        """Newest first. Copies, so a caller cannot mutate the store."""
        with self._lock:
            return [dict(n) for n in reversed(self._notes)]


def record_notes_from(mic: Any, store: NoteStore, seconds_hint: float = 0.0) -> None:
    """Make ``mic.record()`` save every capture as a voice note.

    ``MicCapture.listen()`` calls ``self.record()``, so overriding the
    instance attribute catches captures the loudness gate then rejects —
    exactly the ones worth listening to. The wrapped call returns the same
    bytes; a failure to save never changes capture behaviour.
    """
    original = mic.record

    def record(seconds: float = 3.0):
        wav = original(seconds)
        if wav:
            try:
                meter = LevelMeter.for_defaults()
                passed = meter.feed(wav)
                store.save(wav, seconds, meter.peak_rms, passed)
            except Exception as exc:
                log.debug('voice note record failed: %s', exc)
        return wav

    mic.record = record


__all__ = ['NoteStore', 'record_notes_from', 'NOTES_DIRNAME', 'NOTE_ID_RE', 'MAX_NOTES']
