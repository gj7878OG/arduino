# Persona

Decide the persona before writing a single line of audio code. It is cheaper
to pick a name than to migrate one later — everyone (operators, the
dashboard, the test suite, the recording log) will end up referring to the
rover by it.

This document is intentionally short. Behaviour comes from the rules, not
from a character bible.

## Name

**Working name: `Tarka`.** Two syllables, two consonants in a row,
distinctive enough to be a wake word without false triggers in a noisy
warehouse. Easy to shout across a room. Replace freely — everything in code
reads from `_constants.PERSONA_NAME`; nothing is hard-coded.

Alternates: `Mira`, `Bram`, `Otto`, `Hex`. Pick one and stop.

## Voice

- **Engine:** Piper, local, on-device.
- **Voice sample:** a neutral en-US voice, neither warm-and-fuzzy nor
  synthetic. Test *in the deployment environment* — warehouse reverb makes
  or breaks intelligibility more than the model does.
- **Cadence:** short sentences, one idea per utterance. Pauses between
  status updates so a busy operator is not talked over.

## Three rules — non-negotiable

1. **Confirm before moving.** Every wheel command gets a spoken echo of the
   parsed intent *and* a separate confirm phrase.
2. **Admit "I don't know".** Better an annoying "Sorry, which direction?"
   than a silent mistake.
3. **Speak first when something is wrong.** Person detected, line lost,
   battery low, obstacle stop — the rover announces it before the operator
   asks.

## Voice-side confirmation phrases

Varied prompt — anti-spoof against stray "yes" from radio / TV / another
operator:

| Ack | Phrase the rover says when it has parsed a motion command |
|---|---|
| long | `"Moving forward ten metres — confirm?"` |
| short | `"Going. Confirm?"` |
| rotation | `"Rotating ninety degrees left. Confirm?"` |

Varied confirm — what the operator must say to authorise:

- `"Affirmative."`
- `"Go ahead."`
- `"Do it."`
- `"Confirmed."`

The recogniser is constrained to this set for the confirm slot. Anything
else is treated as a no-op, not a rejection — safer than rejecting
"absolutely" because it isn't on the list.

## Things Tarka always says

| Event | Utterance |
|---|---|
| Wake word heard, ready | `"Yes?"` |
| Motion authorised | `"Moving."` |
| Motion complete | `"Done."` |
| Confirm timed out | `"No confirmation. Standing by."` |
| Clarification needed | `"Sorry, which direction?"` / `"Distance?"` / `"Confirm stop?"` |
| LiDAR hold | `"Holding — <reason>. Will retry when clear."` |
| Battery low | `"Battery at <pct> percent. Returning to dock."` |
| Stuck | `"I'm stuck. Need assistance."` |
| Mute engaged | `"Voice off."` |
| Mute lifted | `"Voice on."` |

## Things Tarka never says

- Jokes, sarcasm, slang, emojis.
- Apologies for things that are not its fault ("sorry the line is hard").
- A person's name, **unless** that person is enrolled on the dashboard camera, has
  ticked "Greet by name", the Greeter switch is on, and the camera is confident
  (see "People and names"). It never guesses a name, never names a stranger, and
  never says a name it has merely heard.
- Any profanity-adjacent phrase, even when the operator does.

## How it handles misunderstanding

There are only three states, and they are explicit:

| Confidence in parse | Behaviour |
|---|---|
| ≥ 0.85 (rules match) | confirm + execute |
| 0.5 – 0.85 | confirm, but the echo names the *parsed* intent: `"Did you mean forward ten metres?"` — slot fill on the missing piece |
| < 0.5 | `"Sorry, I didn't catch that. Say it again, please."` |

Misunderstanding is *not* punitive. Tarka does not say "you said it wrong."

## How it handles emergency

- "Stop" / "Halt" — top-priority interrupt at any time. Kills `enabled`,
  speaks `"Stopped."`. No confirm needed.
- LiDAR hold is *not* an emergency. Tarka says "holding" calmly, queues the
  command, retries when clear.
- Emergency stop via hardware button is silent — no TTS, ever.

## Hooks for the rest of the system

`voice/persona.py` is a pure module — it takes events from
`telemetry` / `control_state` / `lidar` and produces utterance strings. Any
other system module can call it (e.g. the dashboard can show "Tarka says:
…"). Testable without audio.

```
from voice.persona import render_event
text = render_event('lidar_hold', reason='person_ahead')
# -> 'Holding — person ahead. Will retry when clear.'
```

## People and names

Until the camera link existed the rover did not know anyone, so this document said it
never says a name. That rule now has a narrow, deliberate exception. The rover may say a
person's name only when **all** of these hold:

1. The person is enrolled (Camera tab) **and** ticked "Greet by name" for themselves.
   The default is off, per person.
2. The Greeter switch (dashboard, sign-in required) is on. The default is off.
3. The identity is *stable* (several separate face embeddings agree) and clears the
   greeter's own similarity bar, which is stricter than the bar for drawing a box.
4. The camera is actually looking. If it is offline or stale the rover says
   `"I can't see right now."`, never `"I don't see anyone."`.
5. Nothing more urgent is being said. A greeting is dropped, never queued, behind a
   safety announcement, and is never repeated inside the cooldown.

What it says, all fixed templates (`voice/persona.py`):

| Situation | Utterance |
|---|---|
| Opted-in person appears | their own greeting, else `"Hello, <name>."` |
| Stranger (optional, off by default) | `"Unrecognised person."` |
| Operator asks who is here | `"I see <names>."` / `"I see <names> and one person I don't recognise."` / `"I don't see anyone."` |
| Operator asks it to call someone | `"<name>, please come to the rover."` / `"I can't see <name>."` |

It never says a similarity score, never says "I think it's…", and never says anything
from a person's notes. Recognition is a convenience, **not security**: a photo of a person
will be recognised as them. Spoken names are synthesised by a cloud service (Deepgram)
because a name cannot be pre-recorded; Gemini is never given a name.

