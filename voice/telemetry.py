"""Voice telemetry: bounded in-memory records + thin wrappers that fill them.

The dashboard's Voice tab needs to know what the voice stack just heard,
said and parsed. The voice modules already compute all of it and then
throw it away; this module keeps it, without editing them.

Design:

  ``VoiceTelemetry``      thread-safe bounded ring buffers. Fixed memory
                          (``deque(maxlen=N)``) — it runs for weeks.
  ``TelemetrySpeaker``    wraps a ``Speaker``; records what was narrated
                          and whether audio *actually played*.
  ``TelemetryBackend``    wraps an ``SttBackend``; records every
                          transcript including ones the confidence floor
                          later discards, and every failure.
  ``TelemetryParser``     wraps an ``AgentParser``; separates "the model
                          could not be reached / was throttled" from "the
                          validator rejected it" — two very different
                          things that both used to surface as ``None``.

Wrappers delegate everything and never raise on the recording path: a
telemetry fault must not be able to silence the rover.

No secrets are recorded: transcripts and event names only, never keys
and never audio.

Nothing here imports from the main project.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from typing import Any, Callable, Deque, Optional

log = logging.getLogger(__name__)

RING_SIZE = 25
LATENCY_WINDOW = 50
_THROTTLE_MARKERS = ('429', 'RESOURCE_EXHAUSTED', 'rate limit', 'quota')


def is_throttled(message: str) -> bool:
    """True if an error message looks like provider rate limiting."""
    low = (message or '').lower()
    return any(m.lower() in low for m in _THROTTLE_MARKERS)


def _percentile(values: list[float], pct: float) -> Optional[int]:
    """Nearest-rank percentile. None when there is too little data to mean it."""
    if not values:
        return None
    ordered = sorted(values)
    idx = max(0, min(len(ordered) - 1, int(round(pct / 100.0 * len(ordered) + 0.5)) - 1))
    return int(round(ordered[idx]))


class VoiceTelemetry:
    """Bounded, thread-safe record of recent voice activity."""

    def __init__(self, ring_size: int = RING_SIZE,
                 clock: Callable[[], float] = time.time) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._transcripts: Deque[dict] = deque(maxlen=ring_size)
        self._narration: Deque[dict] = deque(maxlen=ring_size)
        self._parses: Deque[dict] = deque(maxlen=ring_size)
        self._stt_ms: Deque[float] = deque(maxlen=LATENCY_WINDOW)
        self._parse_ms: Deque[float] = deque(maxlen=LATENCY_WINDOW)
        self._stt_throttled = 0
        self._parse_throttled = 0
        self._last_update = 0.0

    # ── recorders ───────────────────────────────────────────────────

    def record_transcript(self, text: str, confidence: float, source: str,
                          language: str, latency_ms: float, floor: float) -> None:
        with self._lock:
            self._transcripts.append({
                't': self._clock(), 'text': text,
                'confidence': round(float(confidence), 3),
                'source': source, 'language': language,
                'latency_ms': int(round(latency_ms)),
                'below_floor': float(confidence) < floor,
                'error': '',
            })
            self._stt_ms.append(latency_ms)
            self._touch()

    def record_stt_error(self, message: str, latency_ms: float) -> None:
        throttled = is_throttled(message)
        with self._lock:
            self._transcripts.append({
                't': self._clock(), 'text': '', 'confidence': 0.0,
                'source': '', 'language': '',
                'latency_ms': int(round(latency_ms)),
                'below_floor': False,
                'error': str(message)[:160],
            })
            if throttled:
                self._stt_throttled += 1
            else:
                # A throttled call says nothing about speed; a plain
                # failure that took 40 s is real data.
                self._stt_ms.append(latency_ms)
            self._touch()

    def record_narration(self, event: str, text: str, source: str,
                         played: bool, cache_key: Optional[str]) -> None:
        with self._lock:
            self._narration.append({
                't': self._clock(), 'event': event, 'text': text,
                'source': source, 'played': bool(played),
                'cached': cache_key is not None,
            })
            self._touch()

    def record_parse(self, text: str, outcome: str, action: str,
                     confidence: float, latency_ms: float,
                     error: str = '', lane: str = 'llm') -> None:
        """``lane`` is ``llm`` or ``rules``. Rules decisions take microseconds
        and would drag the LLM latency median toward zero, so they are listed
        but never timed."""
        throttled = outcome == 'throttled'
        with self._lock:
            self._parses.append({
                't': self._clock(), 'text': text, 'outcome': outcome,
                'action': action, 'confidence': round(float(confidence), 3),
                'latency_ms': int(round(latency_ms)),
                'error': str(error)[:160],
                'lane': lane,
            })
            if lane != 'llm':
                pass
            elif throttled:
                self._parse_throttled += 1
            elif outcome != 'backend_error':
                self._parse_ms.append(latency_ms)
            self._touch()

    def _touch(self) -> None:
        self._last_update = self._clock()

    # ── read side ───────────────────────────────────────────────────

    def snapshot(self) -> dict:
        """Bounded, JSON-safe copy. Newest first."""
        with self._lock:
            stt = list(self._stt_ms)
            parse = list(self._parse_ms)
            return {
                'transcripts': list(reversed(self._transcripts)),
                'narration': list(reversed(self._narration)),
                'parses': list(reversed(self._parses)),
                'latency': {
                    'stt_ms': {'p50': _percentile(stt, 50),
                               'p95': _percentile(stt, 95),
                               'n': len(stt), 'throttled': self._stt_throttled},
                    'parse_ms': {'p50': _percentile(parse, 50),
                                 'p95': _percentile(parse, 95),
                                 'n': len(parse), 'throttled': self._parse_throttled},
                },
                'last_activity': self._last_update,
            }


# ── wrappers ────────────────────────────────────────────────────────


def _safely(fn: Callable[..., None], *args: Any, **kwargs: Any) -> None:
    try:
        fn(*args, **kwargs)
    except Exception as exc:                       # never break the rover
        log.debug('telemetry record failed: %s', exc)


class TelemetrySpeaker:
    """Records each narration, then delegates to the real speaker."""

    def __init__(self, speaker: Any, telemetry: VoiceTelemetry) -> None:
        self._speaker = speaker
        self._telemetry = telemetry

    def speak_event(self, event: str, utterance: str):
        result = self._speaker.speak_event(event, utterance)
        _safely(self._telemetry.record_narration, event, utterance, 'persona',
                bool(getattr(result, 'played', False)),
                getattr(result, 'cache_key', None))
        return result

    def __getattr__(self, name: str) -> Any:
        return getattr(self._speaker, name)


class TelemetryBackend:
    """Wraps an ``SttBackend``. Sees transcripts the engine's floor drops."""

    def __init__(self, backend: Any, telemetry: VoiceTelemetry,
                 floor: float) -> None:
        self._backend = backend
        self._telemetry = telemetry
        self._floor = floor

    def transcribe(self, pcm: bytes, *args: Any, **kwargs: Any):
        start = time.monotonic()
        try:
            transcript = self._backend.transcribe(pcm, *args, **kwargs)
        except Exception as exc:
            _safely(self._telemetry.record_stt_error, str(exc),
                    (time.monotonic() - start) * 1000)
            raise
        _safely(self._telemetry.record_transcript,
                transcript.text, transcript.confidence, transcript.source,
                transcript.language, (time.monotonic() - start) * 1000,
                self._floor)
        return transcript

    def __getattr__(self, name: str) -> Any:
        return getattr(self._backend, name)


class _BackendProbe:
    """Remembers the last backend failure so the parser wrapper can tell
    'model unreachable' from 'validator said no'."""

    def __init__(self, backend: Any) -> None:
        self._backend = backend
        self.error = ''

    def parse(self, text: str):
        self.error = ''
        try:
            return self._backend.parse(text)
        except Exception as exc:
            self.error = str(exc)
            raise

    def __getattr__(self, name: str) -> Any:
        return getattr(self._backend, name)


class TelemetryParser:
    """Wraps an ``AgentParser``; classifies every parse outcome."""

    def __init__(self, parser: Any, telemetry: VoiceTelemetry) -> None:
        self._parser = parser
        self._telemetry = telemetry
        self._probe = _BackendProbe(parser.backend)
        parser.backend = self._probe

    def parse(self, text: str):
        start = time.monotonic()
        try:
            intent = self._parser.parse(text)
        except Exception:
            self._record(text, None, start)     # still counts; then re-raise
            raise
        self._record(text, intent, start)
        return intent

    def _record(self, text: str, intent: Any, start: float) -> None:
        ms = (time.monotonic() - start) * 1000
        error = self._probe.error
        if intent is not None:
            outcome, action, conf = 'accepted', intent.action, intent.confidence
        elif error:
            outcome = 'throttled' if is_throttled(error) else 'backend_error'
            action, conf = '', 0.0
        else:
            outcome, action, conf = 'rejected', '', 0.0
        _safely(self._telemetry.record_parse, text, outcome, action, conf, ms, error)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._parser, name)


__all__ = ['VoiceTelemetry', 'TelemetrySpeaker', 'TelemetryBackend',
           'TelemetryParser', 'is_throttled', 'RING_SIZE']
