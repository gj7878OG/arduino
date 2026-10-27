"""Speech plans: a sentence assembled from cached clips, with a live fallback.

A ``Plan`` is a list of segments, each naming a clip in the phrase cache and
the words that clip says. Speaking a plan prefers the offline path — every
clip is cached, so they are spliced into one WAV and played with no network.
If any clip is missing, the same words are synthesised live instead, and if
that is unavailable too, nothing is said (never a half-sentence).

That is why numbers are spoken from clips: "Obstacle ahead at one point four
meters" must work when the internet does not, because it is a safety-relevant
reading.

Nothing here imports from the main project, and nothing here can mean
anything to ``control_state``: it turns telemetry values into sounds.
"""

from __future__ import annotations

import contextlib
import io
import logging
import wave
from dataclasses import dataclass
from typing import Any, Optional

from .persona import render_event

log = logging.getLogger(__name__)

GAP_S = 0.07                      # silence between spliced clips
MAX_SPOKEN_NUMBER = 99.9


@dataclass(frozen=True)
class Segment:
    key: str                      # phrase-cache stem
    text: str                     # exactly what that clip says


@dataclass(frozen=True)
class Plan:
    segments: tuple

    @property
    def text(self) -> str:
        """The sentence(s), for logs, the dashboard and the live fallback.

        Persona lines are whole sentences and keep their own punctuation. A
        run of spliced pieces ("Obstacle ahead at" + "one" + "point" + "four"
        + "meters") has none, so it gets one full stop at the end of the run.
        """
        out = []
        segs = self.segments
        for i, seg in enumerate(segs):
            t = seg.text.strip()
            nxt = segs[i + 1].key if i + 1 < len(segs) else ''
            continues = nxt.startswith(('num_', 'unit_'))
            if t and t[-1] not in '.!?,' and not continues:
                t += '.'
            out.append(t)
        return ' '.join(out)

    @property
    def keys(self) -> tuple:
        return tuple(s.key for s in self.segments)

    def __bool__(self) -> bool:
        return bool(self.segments)

    def __add__(self, other: 'Plan') -> 'Plan':
        return Plan(self.segments + other.segments)


EMPTY = Plan(())


def event_plan(event: str, **slots: Any) -> Plan:
    """A one-clip plan for a persona event. Empty if the event has no text."""
    text = render_event(event, **slots)
    return Plan((Segment(event, text),)) if text else EMPTY


def number_segments(value: float) -> Optional[list]:
    """Clip segments that say ``value`` to one decimal place, or None.

    ``12.5`` -> twelve, point, five; ``25`` -> twenty, five; ``0.4`` -> zero,
    point, four. Anything negative, non-finite, or over 99.9 is not spoken:
    a number we cannot say plainly must not be said approximately.
    """
    try:
        v = round(float(value), 1)
    except (TypeError, ValueError):
        return None
    if v != v or v < 0 or v > MAX_SPOKEN_NUMBER:
        return None
    whole = int(v)
    frac = int(round((v - whole) * 10))
    if whole <= 20:
        parts = [whole]
    else:
        parts = [whole // 10 * 10] + ([whole % 10] if whole % 10 else [])
    keys = [f'num_{p}' for p in parts]
    if frac:
        keys += ['num_point', f'num_{frac}']
    from .vocab import VOCAB
    return [Segment(k, VOCAB[k]) for k in keys]


def distance_plan(metres: float) -> Optional[Plan]:
    """"Obstacle ahead at fifty centimeters" / "... one point four meters"."""
    from .vocab import VOCAB
    try:
        m = float(metres)
    except (TypeError, ValueError):
        return None
    if m != m or m < 0:
        return None
    opener = Segment('frag_obstacle_at', VOCAB['frag_obstacle_at'])
    # Under a metre, say centimetres to the nearest ten. The LiDAR cannot
    # measure closer than ~10 cm, so a smaller reading is not a real distance
    # and "ten centimeters" is the closest honest thing to say.
    if m < 0.95:
        cm = max(10, int(round(m * 100 / 10.0)) * 10)
        nums = number_segments(cm)
        unit = Segment('unit_centimeters', VOCAB['unit_centimeters'])
    else:
        nums = number_segments(m)
        one = round(m, 1) == 1.0
        unit = Segment('unit_meter' if one else 'unit_meters',
                       VOCAB['unit_meter' if one else 'unit_meters'])
    if not nums:
        return None
    return Plan(tuple([opener] + nums + [unit]))


def splice(paths: list, gap_s: float = GAP_S) -> Optional[bytes]:
    """Join WAV files into one. None if they do not share a format."""
    frames, fmt = [], None
    for path in paths:
        try:
            with wave.open(str(path), 'rb') as w:
                params = (w.getnchannels(), w.getsampwidth(), w.getframerate())
                if fmt is None:
                    fmt = params
                elif params != fmt:
                    log.warning('cannot splice %s: format %s != %s', path, params, fmt)
                    return None
                frames.append(w.readframes(w.getnframes()))
        except (OSError, wave.Error, EOFError) as exc:
            log.warning('cannot read clip %s: %s', path, exc)
            return None
    if fmt is None:
        return None
    channels, width, rate = fmt
    gap = b'\x00' * (int(rate * gap_s) * channels * width)
    out = io.BytesIO()
    with wave.open(out, 'wb') as w:
        w.setnchannels(channels); w.setsampwidth(width); w.setframerate(rate)
        w.writeframes(gap.join(frames))
    return out.getvalue()


@dataclass(frozen=True)
class Spoken:
    played: bool
    source: str                    # 'cache' | 'synth' | 'none'
    text: str


class PlanSpeaker:
    """Speaks ``Plan`` objects through a ``TtsEngine``.

    ``engine`` needs: ``.cache`` (``path_for``/``has``), ``.play_audio(bytes)``,
    ``.synth_available`` and ``.speak(key, text, event)`` — all of which
    ``TtsEngine`` has.
    """

    def __init__(self, engine: Any, telemetry: Any = None) -> None:
        self._engine = engine
        self._telemetry = telemetry

    def say(self, plan: Plan, event: str = 'answer') -> Spoken:
        if not plan:
            return Spoken(False, 'none', '')
        # Hold the speaker for the whole answer so a spliced sentence is never
        # interleaved with another announcement (PrioritySpeaker provides this).
        hold = getattr(self._engine, 'exclusive', None)
        with (hold() if callable(hold) else contextlib.nullcontext()):
            spoken = self._say(plan, event)
        if self._telemetry is not None:
            try:
                self._telemetry.record_narration(
                    event, plan.text, 'rules', spoken.played, 'cached' if spoken.source == 'cache' else None)
            except Exception as exc:                       # never break speech
                log.debug('telemetry record failed: %s', exc)
        return spoken

    def _say(self, plan: Plan, event: str) -> Spoken:
        cache = getattr(self._engine, 'cache', None)
        paths = [cache.path_for(k) for k in plan.keys] if cache is not None else []
        if paths and all(p is not None for p in paths):
            audio = splice(paths) if len(paths) > 1 else None
            if len(paths) == 1:
                out = self._engine.speak(plan.keys[0], plan.text, event=event)
                return Spoken(bool(out.played), 'cache' if out.played else 'none', plan.text)
            if audio is not None and self._engine.play_audio(audio):
                return Spoken(True, 'cache', plan.text)
        if getattr(self._engine, 'synth_available', False):
            out = self._engine.speak(f'__plan__{event}', plan.text, event=event)
            if out.played:
                return Spoken(True, 'synth', plan.text)
        log.warning('could not speak %r (no complete cache, no synthesis)', plan.text)
        return Spoken(False, 'none', plan.text)


__all__ = ['Plan', 'Segment', 'Spoken', 'PlanSpeaker', 'EMPTY', 'event_plan',
           'number_segments', 'distance_plan', 'splice']
