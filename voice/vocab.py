"""Spoken building blocks that are not persona events.

Numbers, units and sentence openers are *pieces*: they are spliced together
at speaking time ("Obstacle ahead at" + "one" + "point" + "four" + "meters")
so a distance can be said with no network. They live here, not in
``persona.py``, because none of them is a thing Tarka says on its own.

The clip filename stem is the key, exactly like the phrase cache. The text is
what the recorder synthesises *and* what live synthesis falls back to, so a
spliced answer and a synthesised one say the same words.

No punctuation in the texts: a full stop makes the voice fall as if the
sentence ended, which sounds wrong in the middle of a number.
"""

from __future__ import annotations

_ONES = ('zero one two three four five six seven eight nine ten eleven twelve '
         'thirteen fourteen fifteen sixteen seventeen eighteen nineteen twenty').split()
_TENS = {30: 'thirty', 40: 'forty', 50: 'fifty', 60: 'sixty', 70: 'seventy',
         80: 'eighty', 90: 'ninety'}

VOCAB: dict[str, str] = {f'num_{i}': w for i, w in enumerate(_ONES)}
VOCAB.update({f'num_{n}': w for n, w in _TENS.items()})
VOCAB.update({
    'num_100': 'one hundred',
    'num_point': 'point',
    'unit_meter': 'meter',
    'unit_meters': 'meters',
    'unit_centimeters': 'centimeters',
    'unit_degrees': 'degrees',
    'frag_obstacle_at': 'Obstacle ahead at',
})

# What to *synthesise* for a few clips, when the plain words are rendered badly
# by the voice. The manifest still records the intended words (VOCAB). Found
# by read-back: bare "eight" came out as "Eat", "four" as "Feel"; the
# capitalised, full-stopped form is read back correctly every time.
SYNTH_TEXT: dict[str, str] = {
    'num_4': 'Four.', 'num_8': 'Eight.', 'num_18': 'Eighteen.', 'num_80': 'Eighty.',
}

__all__ = ['VOCAB', 'SYNTH_TEXT']
