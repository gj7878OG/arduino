"""Diagnostic command listener for the dashboard Voice tab.

The web process drops a tiny ``voice_command.json`` into the telemetry
directory; this thread (in the *voice* process, which owns the C270)
picks it up. Same direction of IPC as ``voice_status.json`` but reversed
— a file, not a socket, so there is no listening port and no import path
from ``web.py`` into ``voice``.

Deliberately tiny surface:

  * four verbs — ``listen`` and ``speak_test`` (diagnostics), ``announce_who`` and
    ``call_person`` (say what the camera sees) — nothing else is accepted;
  * ``speak_test`` always says one fixed phrase, never browser text; the camera
    verbs speak persona templates, and the only variable text is the name of a
    person who is enrolled (checked again here, against the camera's own roster);
  * single-flight with a minimum gap, so a stuck button can't hold the
    mic or hammer a paid API;
  * stale commands (older than ``MAX_AGE_S``) are ignored so a restart
    never replays an old request.

None of the verbs reaches the parse lane or ``control_state``. "Listen" just
records, transcribes and shows the text.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from typing import Any, Optional

from ._constants import MIC_MAX_SECONDS
from .cam_lookup import CamLookup, describe, valid_name
from .persona import render_event

log = logging.getLogger(__name__)

COMMAND_FILENAME = 'voice_command.json'
VERBS = frozenset({'listen', 'speak_test', 'announce_who', 'call_person',
                   'ask', 'report'})
SPEAK_TEST_PHRASE = 'Voice check. Can you hear me?'
MIN_GAP_S = 2.0
MAX_AGE_S = 15.0
DEFAULT_LISTEN_S = 3.0


class CommandListener:
    """Polls ``voice_command.json`` and runs whitelisted diagnostics."""

    def __init__(self, out_dir: str, stt: Any = None, speaker: Any = None,
                 poll_s: float = 0.25, clock=time.time,
                 lookup: Optional[CamLookup] = None,
                 asker: Any = None) -> None:
        self._path = os.path.join(out_dir, COMMAND_FILENAME)
        self._stt = stt
        self._speaker = speaker
        self._lookup = lookup
        self._asker = asker
        self._poll_s = poll_s
        self._clock = clock
        self._lock = threading.Lock()
        self._seen: Optional[str] = None
        self._last_run = 0.0
        self._state: dict = {'id': '', 'verb': '', 'state': 'idle', 'detail': '',
                             'finished_at': 0.0}
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # ── lifecycle ────────────────────────────────────────────────────
    def start(self) -> 'CommandListener':
        cmd = self._read()
        if cmd:
            self._seen = str(cmd.get('id', ''))    # never replay on restart
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name='voice-command')
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None and thread.is_alive():
            thread.join(timeout=2.0)

    def state(self) -> dict:
        with self._lock:
            return dict(self._state)

    # ── internals ────────────────────────────────────────────────────
    def _set(self, **kw: Any) -> None:
        with self._lock:
            self._state.update(kw)

    def _read(self) -> Optional[dict]:
        try:
            with open(self._path, 'r', encoding='utf-8') as fh:
                data = json.load(fh)
            return data if isinstance(data, dict) else None
        except (OSError, ValueError):
            return None

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.poll_once()
            except Exception as exc:               # never kill the thread
                log.warning('voice command poll failed: %s', exc)
            self._stop.wait(self._poll_s)

    def poll_once(self) -> bool:
        """Handle at most one new command. True if one was processed."""
        cmd = self._read()
        if not cmd:
            return False
        cid = str(cmd.get('id', ''))
        if not cid or cid == self._seen:
            return False
        self._seen = cid
        verb = str(cmd.get('verb', ''))
        now = self._clock()

        if verb not in VERBS:
            self._set(id=cid, verb=verb[:32], state='rejected',
                      detail='unknown command', finished_at=now)
            return True
        ts = cmd.get('ts')
        if not isinstance(ts, (int, float)) or isinstance(ts, bool) \
                or now - ts > MAX_AGE_S or ts - now > MAX_AGE_S:
            self._set(id=cid, verb=verb, state='rejected',
                      detail='stale command', finished_at=now)
            return True
        if now - self._last_run < MIN_GAP_S:
            self._set(id=cid, verb=verb, state='rejected',
                      detail='too soon — wait a moment', finished_at=now)
            return True

        self._last_run = now
        self._set(id=cid, verb=verb, state='running', detail='', finished_at=0.0)
        try:
            if verb == 'listen':
                detail = self._do_listen(cmd)
            elif verb == 'speak_test':
                detail = self._do_speak()
            elif verb == 'ask':
                detail = self._do_ask(cmd)
            elif verb == 'report':
                detail = self._do_report()
            elif verb == 'announce_who':
                detail = self._do_announce()
            else:
                detail = self._do_call(cmd)
            self._set(state='done', detail=detail, finished_at=self._clock())
        except Exception as exc:
            log.warning('voice command %s failed: %s', verb, exc)
            self._set(state='error', detail=str(exc)[:120],
                      finished_at=self._clock())
        return True

    def _do_listen(self, cmd: dict) -> str:
        if self._stt is None or getattr(self._stt, 'unavailable', True):
            return 'microphone or speech-to-text unavailable'
        try:
            secs = float(cmd.get('seconds', DEFAULT_LISTEN_S))
        except (TypeError, ValueError):
            secs = DEFAULT_LISTEN_S
        secs = max(1.0, min(MIC_MAX_SECONDS, secs))
        heard = self._stt.hear(secs)
        if heard is None:
            return 'nothing usable heard (silence, noise, or below confidence floor)'
        return f'heard: {heard.text!r} ({heard.confidence:.2f})'

    def _do_ask(self, cmd: dict) -> str:
        if self._asker is None:
            return 'assistant not configured'
        try:
            secs = float(cmd.get('seconds', 4.0))
        except (TypeError, ValueError):
            secs = 4.0
        return self._asker.ask(max(1.0, min(MIC_MAX_SECONDS, secs)))

    def _do_report(self) -> str:
        if self._asker is None:
            return 'assistant not configured'
        return self._asker.report()

    def _do_speak(self) -> str:
        if self._speaker is None:
            return 'no speaker configured'
        out = self._speaker.speak_event('voice_test', SPEAK_TEST_PHRASE)
        return 'played' if getattr(out, 'played', False) else \
            'synthesised nothing audible (no cache entry, synthesis failed)'


    def _say(self, event: str, **slots: Any) -> str:
        text = render_event(event, **slots) or ''
        out = self._speaker.speak_event(event, text)
        return f'said: {text!r}' if getattr(out, 'played', False) else \
            f'could not speak (no audio): {text!r}'

    def _do_announce(self) -> str:
        if self._lookup is None or self._speaker is None:
            return 'camera link or speaker not configured'
        event, slots = describe(self._lookup.look('announce_who'))
        return self._say(event, **slots)

    def _do_call(self, cmd: dict) -> str:
        if self._lookup is None or self._speaker is None:
            return 'camera link or speaker not configured'
        name = cmd.get('name')
        if not valid_name(name):
            return 'refused: not a valid name'
        view = self._lookup.look('call_person', want_name=name)
        if not view.ok:
            return self._say('who_blind')
        if name not in view.roster:
            return 'refused: not an enrolled person'      # the dashboard checks too; do not trust it
        if any(f['name'] == name and f['stable'] for f in view.faces):
            return self._say('person_called', name=name)
        return self._say('person_not_seen', name=name)


__all__ = ['CommandListener', 'COMMAND_FILENAME', 'VERBS', 'SPEAK_TEST_PHRASE']
