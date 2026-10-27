"""Gemini as narrator — turns validated events into a sentence a person
understands.

Why this exists
---------------
``voice/persona.py`` renders fixed templates: *"Holding — obstacle."*
Accurate, and impersonal. But the events it renders from carry detail
the template has no slot for — three obstacles, not one; a battery at
twelve percent; a stale LiDAR with no fresh returns. Gemini can turn
that raw event into *"Holding — three obstacles inside the stop zone."*
That is the whole product: the robot explains itself in its own words.

The safety boundary, precisely
------------------------------
This module returns a ``str``. Not an ``Intent``, not a dict, not a
number. There is deliberately no import of ``voice.intent`` here and no
code path from this file to ``control_state.write_state``. Gemini is
allowed to reason about **what to say** and nothing else. If someone
later rewires this to return an Intent, they have changed the
architecture, not added a feature — ``voice/ARCHITECTURE.md`` rule #4.

Latency lands in the *explanation*, never in the motion decision. The
operator has already heard the wheels stop; a 400 ms delay before the
explanation is unnoticeable. A delay before *stopping* would not be.

Failure is invisible by design. If Gemini times out, is rate-limited, or
has no key, ``explain()`` returns the persona template — the operator
always hears *something* explaining what happened.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional

from .persona import render_event

log = logging.getLogger(__name__)

# Deliberately the free tier: this fires a handful of times per shift.
# Any key from AI Studio works — Speech-to-Text needs a Cloud project,
# this does not.
API_KEY_ENVS = ('GEMINI_API_KEY', 'GOOGLE_API_KEY')

MODEL = 'gemini-3.8-flash'

# One sentence. Tarka is an AGV in a warehouse, not a chatbot.
MAX_SENTENCES = 2
MAX_CHARS = 180

# Hard ceiling on output length. A runaway generation must never become
# an unbounded TTS utterance in a safety context.
SYSTEM_PROMPT = (
    'You are the voice of an automated guided vehicle (AGV) in a '
    'warehouse. You will be told, as JSON, an event that has ALREADY '
    'HAPPENED and the raw state behind it.\n'
    '\n'
    'Write ONE short sentence (max 2 short sentences, under 25 words) '
    'that tells the operator what just happened, in plain language.\n'
    '\n'
    'Rules:\n'
    '- Report only what the JSON states. Never invent a cause, a '
    'distance, a location, or a next step.\n'
    '- You have no authority and no ability to change anything. You '
    'are describing, not instructing. Never issue an instruction to '
    'the operator or about the robot\'s next move.\n'
    '- Never mention JSON, events, systems, confidence, or that you are '
    'an AI.\n'
    '- Be calm and factual. This may be an emergency notice.\n'
    '- Output the sentence only — no preamble, no quotes, no markdown.'
)


# ── Result ──────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Explanation:
    """What Tarka will say, and where it came from."""
    text: str
    event: str
    source: str                # 'gemini' | 'persona'


# ── Backend ─────────────────────────────────────────────────────────


class ExplainError(Exception):
    """The narration backend produced nothing usable."""


def _finish_reason(response: Any) -> str:
    """The first candidate's finish reason as text, '' if unavailable."""
    try:
        return str(response.candidates[0].finish_reason or '')
    except (AttributeError, IndexError, TypeError):
        return ''


class ExplainerBackend:
    """Thin Gemini wrapper. Returns prose or raises ``ExplainError``."""

    def __init__(self,
                 api_key: str,
                 model: str = MODEL,
                 timeout_s: float = 5.0) -> None:
        self.api_key = api_key
        self.model = model
        self.timeout_s = timeout_s
        self._client: Any = None
        self._sdk = ''

    @property
    def available(self) -> bool:
        """Ready to narrate — true before any call has been made.

        Checks that a key is present and the SDK imports. Deliberately
        does *not* build a client: this is a health check, and making it
        report False until the first real request would mean a correctly
        configured lane always looked broken at startup.
        """
        if not self.api_key:
            return False
        try:
            import google.genai  # noqa: F401
        except ImportError:
            return False
        return True

    def _ensure_client(self) -> None:
        if self._client is not None:
            return
        from google import genai
        self._client = genai.Client(api_key=self.api_key)
        self._sdk = 'new'

    def complete(self, prompt: str) -> str:
        from google.genai import errors as genai_errors  # noqa: F401
        self._ensure_client()
        response = self._client.models.generate_content(
            model=self.model,
            contents=prompt,
            config={
                'system_instruction': SYSTEM_PROMPT,
                'temperature': 0.2,
                'max_output_tokens': 90,
                # Reasoning is wasted on a two-sentence narration and, worse,
                # it spends the output budget: with thinking on, Gemini was
                # cut off after one word ("I") and that fragment was spoken.
                'thinking_config': {'thinking_budget': 0},
            },
        )
        # A truncated narration must degrade to the persona template, never
        # be spoken as a half-sentence.
        finish = _finish_reason(response)
        if 'MAX_TOKENS' in finish:
            raise ExplainError(f'narration truncated ({finish})')
        return (response.text or '').strip()


# ── Post-processing ─────────────────────────────────────────────────


def sanitize(text: Any, max_chars: int = MAX_CHARS) -> Optional[str]:
    """Strip anything that would sound wrong coming out of a robot.

    Removes markdown fences and quotes (LLM habits that leak through
    despite the prompt), collapses to plain prose, and hard-truncates.
    Returns None when nothing usable survives — including when the
    backend hands back something that isn't a string at all.
    """
    if not isinstance(text, str) or not text:
        return None
    s = text.strip()
    for fence in ('```', '***'):
        s = s.strip(fence).strip()
    # Repeated wrapping quotes ('"""…"') — peel until they stop, then
    # trim any stragglers off the ends. Leading/trailing only, so an
    # internal apostrophe ("battery's low") survives.
    while len(s) >= 2 and s[0] in '"\'' and s[-1] == s[0]:
        s = s[1:-1].strip()
    s = s.strip('"\'').strip()
    s = ' '.join(s.split())          # collapse newlines/tabs
    if not s:
        return None
    if len(s) > max_chars:
        cut = s[:max_chars].rsplit(' ', 1)[0]
        s = (cut or s[:max_chars]).rstrip(' ,.;:') + '.'
    return s or None


# ── Engine ──────────────────────────────────────────────────────────


@dataclass
class EventExplainer:
    """Validates nothing, controls nothing, explains everything.

    ``explain(event, **state)`` returns an ``Explanation``. If Gemini is
    unavailable or slow, the persona template is returned instead and
    ``source='persona'``. The caller cannot tell the difference unless
    it looks — which is the point: narration must never be the thing
    that breaks.
    """

    backend: Optional[ExplainerBackend] = None
    cooldown_s: float = 2.0        # don't re-ask Gemini for the same burst
    _last_call: float = field(default=0.0, repr=False)
    _last_event: str = field(default='', repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def __post_init__(self) -> None:
        if self.backend is None:
            self.backend = self._auto_backend()

    @staticmethod
    def _auto_backend() -> Optional[ExplainerBackend]:
        for env in API_KEY_ENVS:
            key = os.environ.get(env, '').strip()
            if key:
                log.info('explanation lane using $%s', env)
                return ExplainerBackend(api_key=key)
        log.info('no Gemini key in %s; explanations use persona '
                 'templates only', ' or '.join(API_KEY_ENVS))
        return None

    @property
    def available(self) -> bool:
        return self.backend is not None and self.backend.available

    def explain(self, event: str, **state: Any) -> Explanation:
        """Describe a past event. Never fails."""
        fallback_text = render_event(event, **state) or ''
        fallback = Explanation(text=fallback_text, event=event, source='persona')

        if self.backend is None or not self.backend.available:
            return fallback

        with self._lock:
            # Bursty events (LiDAR flickers) would otherwise spend the
            # free-tier quota narrating the same thing repeatedly.
            now = time.monotonic()
            if (self._last_event == event
                    and (now - self._last_call) < self.cooldown_s):
                return fallback
            self._last_call = now
            self._last_event = event

        prompt = self._build_prompt(event, state)
        try:
            raw = self.backend.complete(prompt)
        except Exception as exc:
            log.info('explanation backend failed (%s); using persona '
                     'template', type(exc).__name__)
            return fallback

        text = sanitize(raw)
        if not text:
            return fallback
        log.debug('explained %s -> %r', event, text)
        return Explanation(text=text, event=event, source='gemini')

    @staticmethod
    def _build_prompt(event: str, state: Mapping[str, Any]) -> str:
        """Give Gemini the event name *and* the raw state.

        The event name alone produces confident fiction ("a collision
        was avoided"). The state is what keeps it honest — and it is
        all validator-approved telemetry, never an intent.
        """
        lines = [f'event: {event}']
        for key, value in state.items():
            lines.append(f'{key}: {value}')
        return 'What just happened, as JSON:\n' + '\n'.join(lines)


__all__ = [
    'EventExplainer',
    'Explanation',
    'ExplainerBackend',
    'sanitize',
    'MODEL',
    'SYSTEM_PROMPT',
]