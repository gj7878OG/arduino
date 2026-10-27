"""One speaker for every thread, with priorities.

The rover now has three things that speak: the event watcher (safety and mode
announcements), the command listener (answers to the dashboard) and the face
watcher (unprompted greetings). Playback blocks, and a greeting has to be
synthesised in the cloud first (~2.5 s), so without arbitration a greeting could
hold the speaker while a LiDAR hold waited behind it, or two clips could play
over each other.

Rules:
  * safety speech always gets the speaker, waiting only for the clip in flight;
  * answers to an operator wait their turn;
  * unprompted speech (``DROPPABLE_EVENTS``) is **dropped, never queued** if the
    speaker is busy or a safety announcement was made recently. A greeting that
    arrives ten seconds late is worse than no greeting.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional

from ._constants import DROPPABLE_EVENTS, SAFETY_EVENTS, SAFETY_QUIET_S


@dataclass
class DroppedPhrase:
    """Returned instead of a ``SpokenPhrase`` when unprompted speech was dropped."""
    event: str
    utterance: str
    cache_key: Optional[str] = None
    played: bool = False
    dropped: bool = True


class PrioritySpeaker:
    def __init__(self, speaker: Any, clock: Callable[[], float] = time.monotonic) -> None:
        self._speaker = speaker              # `_speaker` so status.py unwraps it like the others
        self._clock = clock
        self._lock = threading.Lock()
        self._last_safety = -1e9

    def speak_event(self, event: str, utterance: str):
        if event in DROPPABLE_EVENTS:
            if self._clock() - self._last_safety < SAFETY_QUIET_S:
                return DroppedPhrase(event, utterance)
            if not self._lock.acquire(blocking=False):
                return DroppedPhrase(event, utterance)
            try:
                return self._speaker.speak_event(event, utterance)
            finally:
                self._lock.release()
        if event in SAFETY_EVENTS:
            # Stamp before waiting: a greeting that arrives while we queue must drop.
            self._last_safety = self._clock()
        with self._lock:
            return self._speaker.speak_event(event, utterance)

    def exclusive(self):
        """Hold the speaker for a multi-part utterance (a spliced answer).

        Anything built from several clips must not be interleaved with a
        safety announcement. Use ``with speaker.exclusive():``; inside it,
        call the engine's ``speak`` / ``play_audio`` — not ``speak_event``,
        which takes this same lock.
        """
        return self._lock

    def __getattr__(self, name: str) -> Any:
        return getattr(self._speaker, name)


__all__ = ['PrioritySpeaker', 'DroppedPhrase']
