"""Ask Tarka: listen, understand, answer aloud.

The first end-to-end voice flow: microphone -> speech-to-text -> rules engine
-> spoken answer. It only *answers questions* (status, obstacle distance,
mode, help, and honest "I can't" lines). A motion or stop command is
recognised and then declined with "Voice control isn't switched on" — it is
never acted on and never answered with "Moving." / "Stopped.".

No LLM is involved: the rules decide in microseconds and the answer is
assembled from cached clips, so this works without the network except for
the speech-to-text upload.

Nothing here can reach ``control_state``.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional

from .intents import Interpretation, interpret
from .respond import respond, status_report
from .rover_state import RoverState
from .speech_plan import Plan, PlanSpeaker, Spoken, event_plan

log = logging.getLogger(__name__)

DEFAULT_ASK_S = 4.0

_OUTCOME = {'intent': 'accepted', 'refuse': 'refused', 'clarify': 'clarify',
            'none': 'not_understood'}


@dataclass(frozen=True)
class Answer:
    heard: str
    result: Interpretation
    plan: Plan
    spoken: Spoken


class Asker:
    def __init__(self, stt: Any, speaker: PlanSpeaker,
                 state: Callable[[], RoverState],
                 telemetry: Any = None,
                 interpreter: Callable[..., Interpretation] = interpret) -> None:
        self._stt = stt
        self._speaker = speaker
        self._state = state
        self._telemetry = telemetry
        self._interpret = interpreter

    # ── text in, answer out (also the unit-test seam) ───────────────
    def answer(self, text: str) -> Answer:
        start = time.perf_counter()
        result = self._interpret(text)
        ms = (time.perf_counter() - start) * 1000
        plan = respond(result, self._state())
        if self._telemetry is not None:
            try:
                intent = result.intent
                self._telemetry.record_parse(
                    text, _OUTCOME.get(result.kind, 'not_understood'),
                    intent.action if intent else (result.event or ''),
                    intent.confidence if intent else 0.0, ms, lane='rules')
            except Exception as exc:
                log.debug('telemetry record failed: %s', exc)
        spoken = self._speaker.say(plan, event='answer')
        log.info('asked %r -> %s/%s -> %r (%s)', text, result.kind,
                 result.rule or result.event, plan.text, spoken.source)
        return Answer(text, result, plan, spoken)

    # ── dashboard verbs ─────────────────────────────────────────────
    def ask(self, seconds: float = DEFAULT_ASK_S) -> str:
        """Record, transcribe, answer. Returns a one-line summary for the tab."""
        if self._stt is None or getattr(self._stt, 'unavailable', True):
            return 'microphone or speech-to-text unavailable'
        heard = self._stt.hear(seconds)
        if heard is None:
            spoken = self._speaker.say(event_plan('clarify_default'), event='answer')
            return 'heard nothing usable' + ('' if spoken.played else ' (and could not speak)')
        ans = self.answer(heard.text)
        return _summary(heard.text, ans)

    def report(self) -> str:
        """Say the status report now (no microphone)."""
        plan = status_report(self._state())
        spoken = self._speaker.say(plan, event='status_report')
        return f'said: {plan.text!r} ({spoken.source})' if spoken.played \
            else f'could not speak: {plan.text!r}'


def _summary(heard: str, ans: Answer) -> str:
    how = f'said: {ans.plan.text!r} ({ans.spoken.source})' if ans.spoken.played \
        else f'could not speak: {ans.plan.text!r}'
    return f'heard {heard!r} → {how}'


__all__ = ['Asker', 'Answer']
