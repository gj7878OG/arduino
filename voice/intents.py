"""Rules engine — the primary command lane.

Turns a transcript into an ``Intent`` with plain string rules: no network,
no model, microseconds, the same answer every time. The LLM lane
(``voice.agent``) is the *fallback* for what these rules cannot place.

The safety stance, in order of priority:

  1. **Stop wins.** Any stop word anywhere in the utterance is a stop. A
     wrongly-heard stop costs a second; a missed one costs a collision.
  2. **Refuse, don't interpret.** Anything that sounds like disabling a
     safety layer, overriding, or switching systems on/off is answered with
     the persona's "use the dashboard" line. It is never handed to the LLM
     to reason about.
  3. **Understand all of it or none of it.** If a single word is left over
     after the grammar has taken what it knows, the result is ``none`` and
     the caller falls back. The engine never guesses a distance, a direction
     or an angle: a missing slot becomes a spoken clarification question.
  4. **Same gate as the LLM.** Every intent produced here must pass
     ``validate_intent``; if it somehow does not, the result is ``none``.

This module only *interprets*. It cannot reach ``control_state`` and does
not import it: turning an interpretation into motion is ``dialogue`` /
``safety``'s job, behind the confirm gate.

Nothing here imports from the main project.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from ._constants import (
    MOTION_MAX_DISTANCE_M,
    MOTION_MIN_DISTANCE_M,
)
from .intent import Intent, validate_intent

log = logging.getLogger(__name__)

# ── result type ─────────────────────────────────────────────────────


@dataclass(frozen=True)
class Interpretation:
    """What the engine made of an utterance.

    ``kind``:
      ``intent``   a validated ``Intent`` is in ``intent``
      ``clarify``  understood but incomplete: speak persona ``event``
      ``refuse``   understood and declined: speak persona ``event``
      ``none``     not understood — fall back to the LLM lane (or ask again)
    ``source`` is ``rules`` or ``llm`` so logs and the dashboard can say who
    decided. ``notes`` records any assumption made (e.g. "assumed metres").
    """
    kind: str
    intent: Optional[Intent] = None
    event: str = ''
    rule: str = ''
    source: str = 'rules'
    notes: tuple = field(default_factory=tuple)


_NONE = Interpretation(kind='none')


def _clarify(event: str, rule: str) -> Interpretation:
    return Interpretation(kind='clarify', event=event, rule=rule)


# ── vocabulary ──────────────────────────────────────────────────────

STOP_WORDS = frozenset({'stop', 'halt', 'freeze', 'whoa', 'brake', 'abort',
                        'cancel', 'pause', 'emergency'})

# Words that mean "change what the rover is allowed to do". Never interpreted.
REFUSE_WORDS = frozenset({
    'disable', 'disengage', 'deactivate', 'override', 'overrule', 'bypass',
    'ignore', 'enable', 'password', 'unlock', 'sudo', 'shutdown', 'reboot',
    'firmware', 'configure', 'settings', 'calibrate',
})
REFUSE_BIGRAMS = frozenset({('turn', 'off'), ('switch', 'off'), ('power', 'off'),
                            ('shut', 'down'), ('turn', 'on'), ('switch', 'on'),
                            ('power', 'on'), ('switch', 'to')})

STATUS_WORDS = frozenset({'status', 'battery', 'report', 'health', 'charge'})
BATTERY_WORDS = frozenset({'battery', 'charge'})
OBSTACLE_WORDS = frozenset({'obstacle', 'obstacles', 'obstruction', 'nearest', 'closest',
                            'distance', 'anything', 'blocked', 'far', 'something'})
HELP_WORDS = frozenset({'help', 'introduce', 'yourself'})
POSITION_WORDS = frozenset({'position', 'location', 'pose', 'coordinates'})

GO_VERBS = frozenset({'go', 'move', 'drive', 'head', 'proceed', 'advance', 'walk',
                      'travel'})
TURN_VERBS = frozenset({'turn', 'rotate', 'spin', 'pivot'})

DIRECTIONS = {
    'forward': 'forward', 'forwards': 'forward', 'ahead': 'forward',
    'straight': 'forward',
    'back': 'back', 'backward': 'back', 'backwards': 'back', 'reverse': 'back',
    'backup': 'back',
    'left': 'left', 'right': 'right',
}

# Words that carry no command. Deliberately small: every word added here is a
# word the engine will silently ignore, so it must be something that cannot
# change the meaning of a motion command.
FILLER = frozenset({
    'please', 'tarka', 'hey', 'hi', 'ok', 'okay', 'can', 'could', 'you',
    'would', 'will', 'now', 'just', 'rover', 'the', 'a', 'an', 'about',
    'approximately', 'around', 'little', 'bit', 'to', 'for', 'by', 'of',
    'me', 'us', 'it', 'is', 'are', 'what', 'whats', 'how', 'where', 'tell',
    'give', 'show', 'my', 'your', 'status', 'up',
})

LENGTH_UNITS = {
    'meter': 1.0, 'meters': 1.0, 'metre': 1.0, 'metres': 1.0, 'm': 1.0,
    'centimeter': 0.01, 'centimeters': 0.01, 'centimetre': 0.01,
    'centimetres': 0.01, 'cm': 0.01,
    'foot': 0.3048, 'feet': 0.3048, 'ft': 0.3048,
}
ANGLE_UNITS = frozenset({'degree', 'degrees', 'deg'})

_ONES = {w: i for i, w in enumerate(
    'zero one two three four five six seven eight nine ten eleven twelve '
    'thirteen fourteen fifteen sixteen seventeen eighteen nineteen'.split())}
_TENS = {'twenty': 20, 'thirty': 30, 'forty': 40, 'fifty': 50, 'sixty': 60,
         'seventy': 70, 'eighty': 80, 'ninety': 90}
_DIGITS = {str(i): i for i in range(10)} | {w: i for w, i in _ONES.items() if i < 10}


# ── tokenising ──────────────────────────────────────────────────────


def tokenize(text: str) -> list[str]:
    """Lower-case words. Keeps decimals; splits ``2m`` / ``90°`` / ``45deg``."""
    t = (text or '').lower().replace('°', ' degrees ').replace("'", '')
    t = re.sub(r'(\d),(\d{3})', r'\1\2', t)                 # 1,000 -> 1000
    t = re.sub(r'(?<!\d)\.|\.(?!\d)', ' ', t)               # sentence dots only
    t = re.sub(r'(\d)([a-z])', r'\1 \2', t)                 # 2m -> 2 m
    t = re.sub(r'([a-z])(\d)', r'\1 \2', t)
    t = re.sub(r'[^a-z0-9.\s]+', ' ', t)
    return t.split()


def _parse_number(tokens: list[str], i: int) -> Optional[tuple[float, int]]:
    """Parse a number starting at ``tokens[i]``; return (value, next_index)."""
    if i >= len(tokens):
        return None
    tok = tokens[i]
    if re.fullmatch(r'\d+(\.\d+)?', tok):
        return float(tok), i + 1

    value: Optional[float] = None
    j = i
    if tok == 'point':                                       # "point five"
        value = 0.0
    elif tok in _ONES or tok in _TENS:
        if tok in _TENS:
            value = float(_TENS[tok]); j += 1
            if j < len(tokens) and tokens[j] in _ONES and _ONES[tokens[j]] < 10 \
                    and _ONES[tokens[j]] > 0:
                value += _ONES[tokens[j]]; j += 1
        else:
            value = float(_ONES[tok]); j += 1
        if j < len(tokens) and tokens[j] == 'hundred' and 1 <= value <= 9:
            value *= 100; j += 1
            if j < len(tokens) and tokens[j] in _TENS:
                value += _TENS[tokens[j]]; j += 1
                if j < len(tokens) and tokens[j] in _ONES and 0 < _ONES[tokens[j]] < 10:
                    value += _ONES[tokens[j]]; j += 1
            elif j < len(tokens) and tokens[j] in _ONES and 0 < _ONES[tokens[j]] < 20:
                value += _ONES[tokens[j]]; j += 1
        elif j < len(tokens) and tokens[j] == 'thousand':
            value *= 1000; j += 1
    elif tok == 'hundred':
        value, j = 100.0, i + 1
    elif tok == 'thousand':
        value, j = 1000.0, i + 1
    else:
        return None

    if j < len(tokens) and tokens[j] == 'point':             # decimals
        k, frac, scale = j + 1, 0.0, 0.1
        got = False
        while k < len(tokens) and tokens[k] in _DIGITS:
            frac += _DIGITS[tokens[k]] * scale; scale /= 10; k += 1; got = True
        if got:
            return value + frac, k
        return None                                          # "point" with no digits
    if tok == 'point':
        return None
    return value, j


# ── the grammar ─────────────────────────────────────────────────────


def _validated(intent: Intent) -> Optional[Intent]:
    return intent if validate_intent(intent) else None


def _motion(tokens: list[str], text: str) -> Interpretation:
    """Parse "go forward 2 meters" / "turn left 90 degrees" and friends."""
    dirs: list[str] = []
    turn = go = False
    amount: Optional[float] = None
    unit: Optional[str] = None                 # 'length' | 'angle' | None
    scale = 1.0
    preset_deg: Optional[float] = None
    notes: list[str] = []
    i = 0
    n = len(tokens)
    while i < n:
        tok = tokens[i]
        if tok in ('quarter', 'half') and i + 1 < n and tokens[i + 1] == 'turn':
            if preset_deg is not None:
                return _NONE
            preset_deg = 90.0 if tok == 'quarter' else 180.0
            turn = True; i += 2; continue
        if tok in GO_VERBS:
            go = True; i += 1; continue
        if tok in TURN_VERBS:
            turn = True; i += 1; continue
        if tok in DIRECTIONS:
            dirs.append(DIRECTIONS[tok]); i += 1; continue
        if tok == 'half' and amount is None:                 # "half a metre"
            amount = 0.5; i += 1
            while i < n and tokens[i] in ('a', 'an', 'of'):
                i += 1
            continue
        num = _parse_number(tokens, i)
        if num is not None:
            if amount is not None:
                return _NONE                                 # two numbers
            amount, i = num
            continue
        if tok in LENGTH_UNITS or tok in ANGLE_UNITS:
            if unit is not None:
                return _NONE
            if tok in ANGLE_UNITS:
                unit = 'angle'
            else:
                unit, scale = 'length', LENGTH_UNITS[tok]
            if amount is None:                               # "a metre"
                if i > 0 and tokens[i - 1] in ('a', 'an') and unit == 'length':
                    amount = 1.0
                else:
                    return _NONE
            i += 1; continue
        if tok in FILLER:
            i += 1; continue
        return _NONE                                         # a word we don't know

    if not (go or turn or dirs or amount is not None or preset_deg is not None):
        return _NONE                                         # nothing motion-like

    if len(set(dirs)) > 1:
        return _NONE                                         # "forward left"
    if not dirs:
        if turn or amount is not None or go:
            return _clarify('clarify_direction', 'motion.no_direction')
        return _NONE
    d = dirs[0]
    if turn and d in ('forward', 'back'):
        return _NONE                                         # "turn back": ambiguous

    if preset_deg is not None:
        if amount is not None or d not in ('left', 'right'):
            return _NONE
        deg = preset_deg
        return _finish(Intent('motion', {'dir': d, 'deg': deg}, 1.0, text), text, notes)

    if amount is None:
        return _clarify('clarify_distance', 'motion.no_amount')

    if unit == 'angle':
        if d in ('forward', 'back'):
            return _NONE
        value, key = amount, 'deg'
    elif unit == 'length':
        value, key = round(amount * scale, 3), 'dist_m'
    else:                                                    # bare number
        if d in ('left', 'right'):
            if not turn:
                return _NONE                                 # "left 2": metres or degrees?
            value, key = amount, 'deg'
            notes.append('assumed degrees')
        else:
            value, key = amount, 'dist_m'
            notes.append('assumed metres')

    lo, hi = (0.0, 360.0) if key == 'deg' else (MOTION_MIN_DISTANCE_M, MOTION_MAX_DISTANCE_M)
    if not (lo < value <= hi if key == 'deg' else lo <= value <= hi):
        return _clarify('clarify_distance', 'motion.out_of_range')

    return _finish(Intent('motion', {'dir': d, key: value}, 1.0, text), text, notes)


def _finish(intent: Intent, text: str, notes: list[str]) -> Interpretation:
    ok = _validated(intent)
    if ok is None:                                           # fail closed
        log.warning('rules produced an intent the validator rejects: %r', intent)
        return _NONE
    return Interpretation(kind='intent', intent=ok, rule='motion', notes=tuple(notes))


def parse_rules(text: str) -> Interpretation:
    """Interpret ``text`` by rules alone. Never raises."""
    if not isinstance(text, str):
        return _NONE
    try:
        tokens = tokenize(text)
        if not tokens:
            return _NONE
        words = set(tokens)

        # 1. stop wins over everything, including a longer command.
        if words & STOP_WORDS:
            return Interpretation('intent', Intent('stop', {}, 1.0, text), rule='stop')

        # 2. refuse safety/system changes — never interpreted, never sent on.
        pairs = set(zip(tokens, tokens[1:]))
        if (words & REFUSE_WORDS) or (pairs & REFUSE_BIGRAMS):
            return Interpretation('refuse', event='out_of_scope', rule='refuse')

        motion_words = (GO_VERBS | TURN_VERBS | set(DIRECTIONS)) & words

        # 3. questions about the rover — only when nothing is asking it to
        #    move. A direction word alone ("is anything ahead") is fine; a
        #    go/turn verb ("go check the battery") is a motion request.
        if not (words & (GO_VERBS | TURN_VERBS)):
            answered = _query(tokens, words, text)
            if answered is not None:
                return answered
        if not motion_words:
            return _NONE

        # 4. motion.
        return _motion(tokens, text)
    except Exception as exc:                                 # the contract: never raise
        log.warning('rules engine failed on %r: %s', text, exc)
        return _NONE


def _status(text: str, topic: str) -> Interpretation:
    return Interpretation('intent', Intent('query_status', {'topic': topic}, 1.0, text),
                          rule=f'query_status.{topic}')


def _query(tokens: list[str], words: set, text: str) -> Optional[Interpretation]:
    """Questions about the rover. ``query_status`` carries a ``topic`` arg
    (overall | battery | obstacle | mode | help) that only ``respond`` reads;
    the validator's action whitelist is unchanged."""
    if words & POSITION_WORDS or ('where' in words and 'you' in words):
        if _only(tokens, POSITION_WORDS | FILLER | {'where', 'you'}):
            return Interpretation('intent', Intent('query_position', {}, 1.0, text),
                                  rule='query_position')

    if words & BATTERY_WORDS:
        if _only(tokens, STATUS_WORDS | FILLER | {'level', 'how', 'you', 'much', 'left',
                                                  'remaining', 'percent', 'are', 'check'}):
            return _status(text, 'battery')

    if (words & OBSTACLE_WORDS) or ('front' in words and 'in' in words):
        if _only(tokens, OBSTACLE_WORDS | FILLER | {'is', 'there', 'in', 'front', 'ahead',
                                                    'forward', 'how', 'any', 'near', 'close'}):
            return _status(text, 'obstacle')

    if (words & HELP_WORDS) or {'who', 'you'} <= words or {'what', 'can', 'you', 'do'} <= words \
            or {'your', 'name'} <= words:
        if _only(tokens, HELP_WORDS | FILLER | {'who', 'are', 'you', 'what', 'can', 'do',
                                               'your', 'name', 'tell', 'me', 'about'}):
            return _status(text, 'help')

    if 'mode' in words:
        if _only(tokens, FILLER | {'mode', 'what', 'are', 'you', 'in', 'is', 'your', 'current'}):
            return _status(text, 'mode')

    if words & STATUS_WORDS or {'how', 'you'} <= words:
        if _only(tokens, STATUS_WORDS | FILLER | {'level', 'how', 'you', 'doing', 'are', 'check'}):
            return _status(text, 'overall')
    return None


def _only(tokens: list[str], allowed: set) -> bool:
    return all(t in allowed for t in tokens)


# ── the two-lane dispatcher ─────────────────────────────────────────


def interpret(text: str, llm: Optional[Any] = None) -> Interpretation:
    """Rules first; the LLM only for what the rules returned ``none`` on.

    ``llm`` is anything with ``parse(text) -> Optional[Intent]`` — normally
    ``AgentParser``, which has already run the same validator. A refusal or
    a clarification from the rules is final: the LLM is never asked to
    second-guess it. An LLM failure, timeout or rejection is just ``none``.
    """
    result = parse_rules(text)
    if result.kind != 'none' or llm is None:
        return result
    try:
        intent = llm.parse(text)
    except Exception as exc:
        log.info('llm lane failed (%s); treating as not understood', type(exc).__name__)
        return _NONE
    if intent is None or not validate_intent(intent):
        return _NONE
    if intent.action == 'unknown':
        return _NONE
    return Interpretation(kind='intent', intent=intent, rule='llm', source='llm')


__all__ = ['Interpretation', 'parse_rules', 'interpret', 'tokenize',
           'STOP_WORDS', 'REFUSE_WORDS']
