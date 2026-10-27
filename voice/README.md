# Voice — talking to the AGV

Voice is an **input/output channel, not a control surface**. It reads
from the mic, parses intent, and — for motion — would go through the
same `control_state.write_state` path the dashboard uses. Outgoing
speech goes through a persona module and TTS.

Nothing in the main project was modified. `voice/` reads `control.json`
and `points.json` exactly the way `web.py` does.

---

## Status

Live-verified against real hardware and real APIs on a Jetson Orin Nano:

| Lane | State |
|---|---|
| C270 microphone capture | ✅ works — 16 kHz mono, speech gate |
| Deepgram STT (`nova-3`) | ✅ works — ~2.3 s round trip |
| Deepgram TTS (`aura-2-thalia-en`) | ✅ works — ~2.5 s, valid WAV |
| Gemini narrator | ✅ works |
| Gemini parser | ⚠️ works, **latency unmeasured** (see below) |
| Rules engine | ❌ not written — **this is the blocker** |
| Confirm gate / dialogue | ❌ not written |

**229 tests pass.** `python tests/test_voice_<name>.py`

---

## Setup

Two environment variables. Get the Deepgram key from
[console.deepgram.com](https://console.deepgram.com) ($200 credit, no
card required) and the Gemini key from
[Google AI Studio](https://aistudio.google.com) (free tier).

```bash
export DEEPGRAM_API_KEY='...'
export GEMINI_API_KEY='...'
```

Make it survive reboots:

```bash
cat >> ~/.bashrc <<'EOF'
export DEEPGRAM_API_KEY='...'
export GEMINI_API_KEY='...'
export PYTHONPATH=$HOME/arduino:$PYTHONPATH
EOF
source ~/.bashrc
```

Check it took:

```bash
cd ~/arduino && python -c "
from voice import SttEngine, TtsEngine, EventExplainer
s = SttEngine()
print('STT    :', type(s.backend).__name__, s.backend.available)
print('TTS    :', TtsEngine().synth_available)
print('Explain:', EventExplainer().available)"
```

Expected: all three `True`.

> Run from `~/arduino`, not from a subdirectory. `voice/` is a package
> at the repo root; `PYTHONPATH` above makes it work from anywhere.

---

## Hardware

The C270 is found by **USB VID:PID `046d:0825`**, not by ALSA card
number — card indices renumber the moment someone plugs in a USB
headset. `voice/mic.py` walks sysfs from each `/sys/class/sound/cardN`
up to its USB ancestor at startup.

On this machine it resolves to `plughw:2,0`.

**The C270 has a physical mute slider on top.** If STT starts returning
empty transcripts, check that first.

---

## Architecture

```
mic ──▶ level gate ──▶ Deepgram nova-3 ──▶ confidence gate ──▶ text
                                                                │
                                     ┌──────────────────────────┤
                                     ▼                          │
                            rules engine (fast)                  │
                                     │ miss                     │
                                     ▼                          │
                          Gemini parser (slow) ──▶ validator     │
                                                                ▼
                                                     confirm gate ──▶ control_state
```

Two lanes, and the ordering is the whole design:

1. **Rules engine** — deterministic, milliseconds, handles the ~90 % of
   commands an operator actually says. **Not written yet.**
2. **Gemini** — fallback parser for genuinely ambiguous phrasing. Turns
   JSON into an `Intent`. Never touches motors.

`voice/intent.py` is the validator and the safety boundary. It enforces
the action whitelist, the confidence floor, and per-action argument
schema **in Python**, not in the prompt. Gemini asks to be passive; the
validator makes it passive. Tests in `test_voice_intent.py` pin 29
rejection cases including `disable_lidar` and unbounded distances.

---

## The five safety rules

1. **One path to motors.** Voice writes through `control_state` exactly
   as `web.py` does. No second control path, ever.
2. **Confirm before motion.** Every motion command gets a spoken echo
   before wheels turn. *(not built yet)*
3. **Safety override is absolute.** LiDAR / camera holds always beat a
   voice command. Voice never clears a hold.
4. **The LLM is a parser, not a controller.** It returns JSON. It
   cannot act.
5. **Hardware mute wins.** The mute switch beats everything, always.

---

## ⚠ Known-open: parse-lane latency is unmeasured

**This is the most important thing in this document.**

`ARCHITECTURE.md` originally estimated 0.3–0.8 s for the Gemini lane.
That figure came from memory and is **unverified — probably
optimistic.** A live benchmark was contaminated by free-tier `429`
errors that surfaced as `None`, so no trustworthy number exists yet.

What is known:

- A run showed a median of 4.60 s with individual calls spanning
  0.30 s–47.13 s — but that spread is rate limiting, not model speed.
- A spaced run showed 0.39 s median with **every** call returning
  `None` — a fast-fail, not a fast answer.
- Both measurements are worthless. The real number is unknown.

**Consequence:** at multi-second latency the Gemini lane cannot sit
inside an interactive confirm loop. An operator says *"go forward two
metres"*, waits ten seconds, and stops using the feature.

This *reinforces* the original two-lane design rather than undermining
it — and it means **the rules engine is not an optimization, it is what
makes voice usable.** It must be the primary lane, in milliseconds,
with Gemini reserved for phrasing that genuinely needs it.

To measure honestly: wait for the free-tier rate limit to reset, then
space calls at least 8 s apart and log raw outcomes rather than just
success/failure.

---

## Modules

| File | Role |
|---|---|
| `mic.py` | C270 capture, sysfs discovery, RMS speech gate |
| `stt.py` | Deepgram (default) / Google Speech v2, confidence floor |
| `tts.py` | Phrase cache first, Deepgram Aura as fallback |
| `persona.py` | Canonical utterance templates (pure, no audio I/O) |
| `event_watcher.py` | Read-only narration of `control.json` changes |
| `agent.py` | Gemini parser lane → `Intent` |
| `intent.py` | `Intent` + the validator (prompt-injection defence) |
| `explain.py` | Gemini narrator; returns `str`, never an `Intent` |
| `_constants.py` | All thresholds and vocabulary |
| `run_phase0.py` | CLI for the narration stack (`--no-faces` turns the camera link off) |
| `cam_lookup.py` | reads `cam_faces.json`, asks the camera to look (`cam_watch.json`) |
| `face_watcher.py` | unprompted greetings for opted-in, recognised people |
| `speaker.py` | one speaker for all threads; safety speech outranks greetings |

### Why `explain.py` can never control the robot

`EventExplainer.explain()` returns a **`str`**. It does not import
`voice.intent`, and two tests parse its AST to prove no identifier like
`write_state`, `Intent`, or `validate_intent` appears in it. There is no
type it could return that means anything to `control_state`.

Gemini may reason freely about **what to say**. It may not reason about
**what to do**.

---

## Failure behaviour

Everything degrades rather than raises, because a voice feature that
crashes the rover is worse than one that stays quiet:

| Failure | Behaviour |
|---|---|
| No API key | `available = False`, logged, voice input off |
| Network / 429 / timeout | Persona template; command silently dropped |
| Mic missing or muted | No transcript; nothing sent |
| Deepgram returns nothing | `None` → "Sorry, I didn't catch that." |
| Low-confidence transcript | Dropped by the 0.55 floor |

Note that `None` from the parser is **ambiguous** — it means either
*"rejected for safety"* or *"the network failed"*. Those deserve
different spoken replies. Worth fixing.

The free-tier rate limit was hit during development; a `429` backoff on
the narrator is a known gap.

---

## Camera link (who the rover can see)

The dashboard process writes `cam_faces.json` (names, a stability flag, greetings of
people who opted in; mode 0600) and the voice process reads it. The other direction is
`cam_watch.json`: the rover asks the camera to wake for a few seconds. Files, not sockets
or imports, like everything else here. Full plan and measurements:
`docs/cam-voice-integration-plan.md`; the naming rule: `PERSONA.md` "People and names".

* **Greeting** (`face_watcher.py`): off unless the dashboard's Greeter switch is on and
  the person opted in; needs a stable identity above the greeter's similarity bar; per-person
  cooldown; one greeting per 10 s; dropped if a safety announcement just spoke.
* **Dashboard buttons** (`command.py` verbs `announce_who`, `call_person`): say who is in
  view, or call a named, enrolled person. Fixed templates only.
* **Honest when blind**: an offline or stale camera is `"I can't see right now."`.
* The Voice tab gets a `faces` lane when this is enabled.

## Not yet built

- `intents.py` — the rules engine (the blocker)
- `dialogue.py` — confirm gate, slot filling, turn-taking
- `safety.py` — voice-side clamps and mute
- `wake.py` — wake word
- `phrases/*.wav` — the 10 canonical pre-baked phrases. The directory is
  empty, so **Tarka can only speak text via Deepgram right now.**
- A spoken `"who is there?"` (needs the wake word, listen loop and rules engine; the dashboard buttons cover it for now)
