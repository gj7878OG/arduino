"""Greet people the camera recognises. Unprompted speech, so it is strict.

Reads ``cam_faces.json`` (see ``voice/cam_lookup.py``) and says something only
when *all* of these hold:

  * the file is fresh and the camera is actually looking (``state == running``);
  * the dashboard's greeter switch is on;
  * the face is **stable** (several separate embeddings agreed) and its
    similarity clears the greeter's own bar, not the drawing threshold;
  * that person opted in to being greeted;
  * their cooldown has passed, and at most one greeting per ``GREET_MIN_GAP_S``;
  * the speaker is free and no safety announcement was made recently
    (``PrioritySpeaker`` enforces the last two; a dropped greeting is retried
    on the next tick instead of being recorded as said).

Strangers are never named. ``person_unknown`` ("Unrecognised person.") is
optional, off by default, and needs the stranger to stay in view for a while.

Like every voice module this returns no value that could mean anything to
``control_state``: it speaks, nothing else. ``tests/test_cam_voice_link.py``
checks that by AST, the way ``explain.py`` is checked.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable, Optional

from ._constants import GREET_MIN_GAP_S, UNKNOWN_SEEN_S
from .cam_lookup import clean_text, read_faces
from .persona import render_event

log = logging.getLogger(__name__)

_UNKNOWN_KEY = '\x00unknown'


class FaceWatcher:
    def __init__(self, telemetry_dir: str, speaker: Any, poll_interval_s: float = 0.5,
                 clock: Callable[[], float] = time.time,
                 mono: Callable[[], float] = time.monotonic) -> None:
        self._dir = telemetry_dir
        self._speaker = speaker
        self._poll_s = poll_interval_s
        self._clock, self._mono = clock, mono
        self._lock = threading.Lock()
        self._last_by_name: dict = {}
        self._last_any = -1e9
        self._unknown_since: Optional[float] = None
        self._status = {'state': 'no_data', 'enabled': False, 'greeted': 0,
                        'last_greeted_at': None}
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._last_warn = 0.0

    # ── lifecycle ────────────────────────────────────────────────────
    def start(self) -> 'FaceWatcher':
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True, name='voice-faces')
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None and thread.is_alive():
            thread.join(timeout=2.0)

    def state(self) -> dict:
        with self._lock:
            return dict(self._status)

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception as exc:                  # noqa: BLE001 - never kill the thread
                now = self._mono()
                if now - self._last_warn >= 10.0:
                    self._last_warn = now
                    log.warning('face watcher tick failed: %s', exc)
            self._stop.wait(self._poll_s)

    # ── one poll ─────────────────────────────────────────────────────
    def tick(self) -> None:
        view = read_faces(self._dir, self._clock())
        enabled = bool(view.greeter.get('enabled'))
        with self._lock:
            self._status.update(state=view.state, enabled=enabled)
        if not view.ok or not enabled:
            self._unknown_since = None
            return

        now = self._mono()
        cooldown = view.greeter['cooldown_s']
        if now - self._last_any < GREET_MIN_GAP_S:
            return

        for f in sorted(view.faces, key=lambda x: -x['sim']):
            if not (f['stable'] and f['greet'] and f['sim'] >= view.greeter['min_sim']):
                continue
            if now - self._last_by_name.get(f['name'], -1e9) < cooldown:
                continue
            text = clean_text(f['greeting']) or f"Hello, {f['name']}."
            self._say('person_greeted', f['name'], now, greeting=text)
            return                                    # one per tick; if busy, try again next tick

        if view.greeter['greet_unknown'] and view.unknown > 0:
            if self._unknown_since is None:
                self._unknown_since = now
            elif now - self._unknown_since >= UNKNOWN_SEEN_S \
                    and now - self._last_by_name.get(_UNKNOWN_KEY, -1e9) >= cooldown:
                self._say('person_unknown', _UNKNOWN_KEY, now)
        else:
            self._unknown_since = None

    def _say(self, event: str, key: str, now: float, **slots) -> bool:
        utterance = render_event(event, **slots)
        if not utterance:
            return False
        out = self._speaker.speak_event(event, utterance)
        if getattr(out, 'dropped', False):
            return False             # speaker busy or a safety call just spoke: not said, not recorded
        self._last_by_name[key] = now
        self._last_any = now
        with self._lock:
            self._status['greeted'] += 1
            self._status['last_greeted_at'] = self._clock()
        log.info('event=%s utterance=%r', event, utterance)
        return True


__all__ = ['FaceWatcher']
