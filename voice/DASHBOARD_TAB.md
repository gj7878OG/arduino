# Plan — a Voice tab on the AGV dashboard

Status: **Implemented** (phases A, A2, B, C). Revised 2026-10-22. See §11.

Goal: a read-only "Voice" tab that answers, at a glance and in detail,
*is the voice stack actually working right now, and what did it just hear?*

---

## 1. The constraint, stated plainly

The standing rule for this workstream has been **"don't make any changes to
the main project."** A dashboard tab cannot be built without touching
`web.py` and `templates/dashboard.html`.

This plan resolves that by making the main-project diff as small and as
inert as it is possible to make:

| File | Change | Size | Nature |
|---|---|---|---|
| `web.py` | **one** new route | ~18 lines | read-only `GET`, no auth needed |
| `templates/dashboard.html` | 5 one-line inserts | ~5 lines | nav button, 3 includes, `TAB_IDS` entry |
| `templates/_voice_tab.{html,js,css}` | **new files** | ~380 lines | new |
| `voice/status.py` | **new file** | ~230 lines | new, voice-side |
| `voice/telemetry.py` | **new file** | ~150 lines | new, voice-side |
| `voice/mic.py`, `stt.py`, `tts.py`, `explain.py`, `agent.py`, `event_watcher.py`, `run_phase0.py` | record-and-forward hooks | ~6 lines each | additive, no behaviour change |

Nothing in `control_state.py`, `config.py`, `agv_drive_arduino.py`,
`telemetry.py`, or the Drive/Chassis/Test/Record tabs changes. There is no
new POST endpoint that can move a wheel or write control state.

**This is the same shape `lidar/` already uses** — see §2.

---

## 2. Architecture: publish a file, read it over HTTP

The voice stack is a **separate process** (`python -m voice.run_phase0`),
not a library inside `web.py`. That is deliberate and it is load-bearing:

> The C270's ALSA device is **exclusive**. If `web.py` also opened
> `MicCapture`, exactly one of the two processes would win the device and
> the loser would fail *nondeterministically at startup*. And if `web.py`
> constructed its own `EventWatcher`, Tarka would say every phrase twice.

So the tab **must not** import `voice`. Instead, mirror
`lidar/publisher.py` exactly:

```
 voice process                        file                       web process
┌──────────────────┐                                        ┌──────────────────┐
│ run_phase0.py    │                                        │ web.py           │
│                  │                                        │                  │
│ VoiceTelemetry ──┼──record──► RingBuffers                 │                  │
│                  │        (in-memory, bounded)            │                  │
│ StatusPublisher ─┼──atomic write──► $TELEMETRY_DIR/      │                  │
│                  │        voice_status.json               │                  │
│                  │                    │                   │                  │
│ EventWatcher     │                    │                   │                  │
│ SttEngine        │                    │                   │                  │
│ MicCapture       │                    │                   │                  │
└──────────────────┘                    ▼                   ▼                  │
                              /tmp/agv-dashboard/    ┌────────────────────┐     │
                              │  voice_status.json ◄─┤ GET /api/voice/... │     │
                              │  points.json         │ (read + jsonify)   │     │
                              │  control.json        └────────────────────┘     │
                              └────────────────────────────────────────────────┘
```

Write-then-`os.replace()`, same as `PointsPublisher.publish_once()`. A
reader never sees a half-written file, and a crashed writer leaves the last
good payload rather than a truncated one.

**Staleness is a first-class state, not an error.** If the file is older
than `VOICE_STALE_S` (10 s), the tab says *voice process not running* in
red. This matters: the whole reason this project has a LiDAR stale detector
is that a sensor which silently stops must not look healthy. A Voice tab
that renders an empty-but-green panel when the mic is unplugged is the
same bug wearing a nicer shirt.

---

## 3. What the tab shows

Mirroring `_lidar_tab.html`'s structure and visual language. **As built** the
tab is two columns (`grid` 2fr | 1fr, collapsing to one below 760px), split by
what a card is *for* rather than by size:

    left  (wide)   hardware     Microphone · Speaker
                   stack        Voice stack
                   action       Try it
    right (narrow) measurement   Latency
                   conversation Live transcript · Voice notes · Narration
                   command      Command parsing

Latency used to sit dead last, far from the stack it measures; it now heads the
right column, directly above the transcript whose timings it reports. It was
originally grouped with the stack for that reason, but doing so left the columns
badly lopsided (1340px against 660px) — measurement reads better beside the
results than beside the machinery. Every card body is wrapped in `.tab-body` for
the same inset the Drive tab's cards use.

The "Card N" headings that follow describe what each card *is*; the numbers no
longer match the reading order.

### Card 1 — Voice health (the status bar)

Five lane chips, each `ok / degraded / down` with a reason string:

| Lane | Shows |
|---|---|
| **Mic** | `046d:0825` → card 2 → `plughw:2,0`, `arecord` present, product name |
| **STT** | backend (`deepgram` \| `google` \| `offline`), model `nova-3`, confidence floor `0.55` |
| **TTS** | cached phrases `0 / 20`, synthesis on/off, playback tool (`aplay`) |
| **Narrator** | backend `gemini`, model `gemini-3.8-flash`, rate-limited state |
| **Parse lane** | `agent.py` status — see the honest note below |

### Card 2 — Live transcript

The thing you actually asked for. Last utterance, large:

```
  "go forward two meters"
  0.99 confidence   ·   deepgram · en   ·   1.68 s   ·   14:32:07
```

plus a scrolling list of the last 25, each row showing text, confidence,
source, and latency. Confidence is rendered as a bar with the **0.55 floor
marked as a line** — so a 0.40 that's visibly *below* the gate is legible
at a glance, rather than a number you have to remember the threshold for.

### Card 3 — Microphone level meter

A live bar of peak RMS against the threshold, because **the RMS gate
detects loudness, not speech.** This is a known defect (a closing door
passes it). Showing the meter is not decoration — it's the fastest way for
an operator to see the gate firing on nothing. The card is captioned
accordingly rather than pretending to be a VAD.

### Card 4 — Narration log

Every event Tarka narrated: event name, the prose, source
(`gemini` \| `persona`), whether cooldown suppressed it, and whether audio
**actually played** (`SpokenPhrase.played`). That last field is the one
that matters — `played=False` is how you discover the phrase cache is empty
and synthesis is failing, which is currently the *normal* state.

### Card 5 — Parse lane

Raw utterance → outcome → which gate rejected it. One row per attempt,
last 25. This makes the 4-gate validator visible: an injection attempt
should appear as `rejected · gate=action_whitelist`, which is evidence the
defense works rather than a claim that it does.

**Honest label:** this lane is *currently the only lane*. The rules engine
(`voice/intents.py`) does not exist, so every spoken command routes here —
slow and rate-limited. The card header should say so until that changes.

### Card 6 — Latency

Rolling p50 / p95 for STT and parse, **with rate-limited calls counted
separately and never folded into the median.** The last benchmark was
contaminated by `429`s that surfaced as silent `None`s, which made a 4.60 s
median meaningless. Showing the count of throttled calls next to the
number is what makes this panel trustworthy rather than decorative.

Also on this card: which of the three env vars are present —
`DEEPGRAM_API_KEY ✓`, `GEMINI_API_KEY ✗` — **boolean only, never a value.**

---

## 4. Files

### New — `voice/telemetry.py`

Bounded ring buffers and a `VoiceTelemetry` object that the voice modules
call into. Buffers are `collections.deque(maxlen=N)` — fixed memory, no
unbounded growth on a device that runs for weeks.

```python
class VoiceTelemetry:
    def record_transcript(t: Transcript, latency_ms: int, below_floor: bool)
    def record_meter(peak_rms: float, speech: bool)
    def record_narration(event, text, source, played, suppressed)
    def record_parse(text, outcome, action, confidence, gate, latency_ms, throttled)
    def snapshot() -> dict          # the payload, bounded
    @property stale_s / last_update
```

Zero imports from the main project. Pure data.

### New — `voice/status.py`

`VoiceStatusPublisher` — a direct sibling of `PointsPublisher`. Same
`start()` / `stop()` / `publish_once()` / `_loop()` shape, same
`os.replace()` atomicity, same warn-throttle-at-10 Hz discipline, same
`__repr__`. Publishes `voice_status.json` on a timer.

Payload schema (versioned, with `schema: 1`):

```jsonc
{
  "schema": 1,
  "updated_at": 1750000000.0,
  "pid": 4821,
  "stale_after_s": 10.0,
  "lanes": {
    "mic":      {"ok": true, "usb_id": "046d:0825", "card": 0,
                 "device": "plughw:0,0", "product": "C270", "reason": ""},
    "stt":      {"ok": true, "backend": "deepgram", "model": "nova-3",
                 "confidence_floor": 0.55, "reason": ""},
    "tts":      {"ok": false, "cached": 0, "expected": 20,
                 "synth": true, "tool": "aplay", "reason": "0/20 phrases cached"},
    "narrator": {"ok": true, "backend": "gemini",
                 "model": "gemini-3.8-flash", "throttled": false, "reason": ""},
    "parser":   {"ok": true, "model": "gemini-3.8-flash", "reason": ""}
  },
  "keys": {"DEEPGRAM_API_KEY": true, "GEMINI_API_KEY": true},   // bools only
  "meter": {"peak_rms": 894.0, "threshold": 250.0, "speech": true},
  "transcripts": [ /* ≤25 */ ],
  "narration":   [ /* ≤25 */ ],
  "parses":      [ /* ≤25 */ ],
  "latency": {"stt_ms": {"p50": 1680, "p95": null, "throttled": 0, "n": 1},
              "parse_ms": {"p50": null, "p95": null, "throttled": 3, "n": 3}}
}
```

**Secrets never enter this file.** A test asserts that no value in the
payload has length > 64 or matches a key-like shape, so this can't rot
into a leak later.

### Modified — `voice/*.py` (additive, ~6 lines each)

Each module already *has* the information; it just doesn't keep it. The
change is a call, not a redesign.

| File | Addition |
|---|---|
| `mic.py` | `listen()` feeds the meter |
| `stt.py` | `hear()` records transcript + latency + below-floor |
| `tts.py` | `speak_event()` records played / cache_key |
| `explain.py` | `explain()` records text + source |
| `agent.py` | `_parse_new()` records outcome + gate + latency |
| `event_watcher.py` | forwards events to telemetry |
| `run_phase0.py` | construct `VoiceTelemetry` + `VoiceStatusPublisher`, `.start()` |

All guarded so `telemetry=None` is the default and the old behaviour is
unchanged when it's absent. The 232 existing tests keep passing
unmodified — that's the acceptance criterion for this phase.

### New — `web.py` route

One read-only `GET`, placed after `points_json()` and copying its
error-fallback idiom verbatim (referenced by name, not line number, so
this does not rot every time a route is added):

```python
@flask_app.route('/api/voice/status', methods=['GET'])
def api_voice_status():
    """Voice pipeline telemetry. Read-only.

    Mirrors points_json(): the voice process publishes atomically and we
    just re-serve. Never returns an API key, never returns audio, and
    there is deliberately no POST — a diagnostics tab must not become a
    control surface (ARCHITECTURE.md rule #4).
    """
```

Returns 200 with a `stale: true` marker and the file's age when the
payload is missing or old. Never 500s — a missing voice process must not
break the dashboard.

### New — `templates/_voice_tab.{html,js,css}`

Structurally identical to `_lidar_tab.*`. The JS follows `_lidar_tab.js`:
`updateVoiceLive()` on the fetch, `authFetch` for anything privileged
(nothing here is), `setInterval` polling at 500 ms, and a
`_ensureVoiceInited()` lazy hook so the tab costs nothing until opened.

### Modified — `templates/dashboard.html` (5 one-line inserts)

| Where (find it by) | Insert |
|---|---|
| after `_cam_tab.css` | `{% include '_voice_tab.css' %}` |
| next to the other `tabbtn-*` buttons | `<button … data-tab="voice" id="tabbtn-voice">Voice<i class="tabdot" id="dot-voice"></i></button>` |
| with the other `{% include '_*_tab.html' %}` | `{% include '_voice_tab.html' %}` |
| next to the other `{% include '_*_tab.js' %}` | `{% include '_voice_tab.js' %}` |
| in `const TAB_IDS = [...]` | add `'voice'` |

Line numbers are deliberately not given here — they were in the first draft of
this document and every one of them was wrong within a day. Anchor on the
surrounding markup instead.

Keyboard nav (`←`/`→`), `localStorage` tab persistence, and the a11y
`role="tablist"` wiring all come free from `showTab()`.

---

## 5. Safety rules for this tab

1. **Read-only. No POST.** The tab cannot command the rover. If voice
   input later needs a control, it goes through the same confirm gate as
   everything else — not through a dashboard button.
2. **No secrets.** Booleans only, enforced by test.
3. **No audio.** Transcripts yes, PCM bytes never.
4. **Transcripts are retained in the file** (≤25) and in the browser's
   memory. Local-only, never leaves the LAN. Worth knowing that operator
   speech is in there.
5. **Staleness shown, never hidden.** A green panel with no data behind it
   is the failure mode this project already has a name for.

---

## 6. Tests

Mirroring the existing three-file precedent:

| File | Covers |
|---|---|
| `tests/test_voice_telemetry.py` | ring buffers bounded, wrap correctly, never raise |
| `tests/test_voice_status.py` | atomic write, no secrets, schema complete, stale detection |
| `tests/test_web_voice_status.py` | route is GET-only; missing file → 200 with stale, not 500 |
| `tests/test_dashboard_ui_voice.py` | DOM ids present, tab wired, `TAB_IDS` contains `voice` |

Plus the architectural test already in this project's style: an AST scan
asserting `web.py`'s new route does not import `control_state` or reference
`write_state`. A diagnostics tab that can write control state would be a
third path to the motors.

---

## 7. Sequencing

Four commits, each independently shippable and each leaving the rover
working:

**A — `voice/` telemetry only.** New files + additive hooks. No main-project
change at all. *Deliverable:* a JSON file appears in `/tmp/agv-dashboard`
that you can `cat`. **This alone answers most of what you want to know.**

**B — web route.** ~18 lines in `web.py` + route tests. No UI.

**C — the tab.** Templates + `dashboard.html` + UI tests.

**D — optional: two debug buttons.** *See §8.*

---

## 8. Open questions for you

**D — debug buttons (optional, my recommendation: yes, but as a separate
commit).** Right now testing the mic means a CLI script. Two buttons would
fix that:

- **Listen now** — record 3 s, transcribe, show the result in Card 2
- **Speak test** — synthesise a fixed phrase and play it

Implemented as a `voice_command.json` file the voice process picks up on
its next tick — same direction of IPC as the status file, no sockets, no
network listener, **and it cannot touch `control_state`** because the voice
process has no code path there for a test phrase.

Cost: this is a control channel, and it is a deliberate exception to
"read-only". It only touches the mic and the speaker, never the motors. My
view is that a voice tab you can't *test* with is half a diagnostic tool —
but it's your call, and it ships separately so you can drop it.

**Two things I want on the record before building:**

1. **Card 5 will look bad, and it should.** With no rules engine, that lane
   is everything, it's slow, and it's rate-limited. The tab will make that
   obvious rather than hiding it.
2. **Card 3 will expose a real bug.** The RMS gate passes loud non-speech;
   you saw this live. Surfacing the meter is how it gets fixed rather than
   rediscovered every few weeks.

---

## 9. What this does not do

- It does not make voice usable. That still needs `voice/intents.py`.
- It does not record the 10 (now 20) canonical phrases. The cache is
  `0 / 20` and everything depends on Deepgram being up.
- It does not measure real latency — it *starts* measuring it, honestly,
  by keeping throttled calls out of the median.

---

## 10. Revision 2 — what review found wrong with this plan

Written after building Phase A and printing a real payload.

### 10.1 Structural flaw: three of six cards have no data source

`run_phase0.py` is TTS-only by design — **nothing in the voice process
ever listens.** So Transcript, Mic meter, Parse lane and STT latency would
render permanently empty. The plan promoted the *display* and left out the
*producer*.

**Fix: §8 "debug buttons" is no longer optional.** "Listen now" is the
only thing that makes the transcript card live until voice v1 (wake word +
dialogue loop) exists. The user's headline request — *"I want transcript on
the tab"* — depends on it. Re-sequenced: Phase D moves ahead of C, and the
command is **listen-only**: record → STT → show. It does not feed the parse
lane or `control_state`.

Constraints on the command channel (it is a deliberate exception to
read-only, and it opens a microphone):

- **Password-gated POST**, like every other privileged route — recording
  room audio is a privacy action, not a diagnostic freebie.
- Fixed verb whitelist: `listen`, `speak_test`. No free text from the
  browser reaches TTS except a fixed phrase.
- Single-flight + 2 s minimum gap, so a stuck button cannot hammer a paid
  API or hold the mic.
- Delivered via a `voice_command.json` the voice process polls; **no
  listening socket**, no import path from `web.py` into `voice`.

### 10.2 Show `ok / degraded / down`, not a boolean

The plan promised three states; the payload carries a bool. Real data
shows why it matters: TTS reports `ok: true` with **0/20 phrases cached**.
That's "working but fragile (all speech depends on Deepgram)", not green.
Add `state` to each lane; cache-empty → `degraded`.

### 10.3 Don't hardcode the ALSA card

The plan's example showed `card 2`. This machine now reports **card 0**
for the same C270. Discovery by VID:PID is the reason that works; the tab
must display what was found, never an expected value.

### 10.4 Transcript privacy

`/api/voice/status` mirrors `points.json` (open GET). Lane health is
harmless; **transcripts are operator speech.** Serve lanes/health openly
but include `transcripts`, `narration` text and `parses` only for an
authenticated session (reuse `_rec_read_ok`). Costs ~3 lines in the route.

### 10.5 Smaller corrections

- `dashboard.html` needs **five** inserts, not four (css include, nav
  button, html include, js include, `TAB_IDS`).
- Parse lane is not constructed in `run_phase0` — wire `AgentParser` when a
  Gemini key is present; otherwise the lane correctly reads "not configured".
- Hooks → **wrappers.** Built as `TelemetrySpeaker` / `TelemetryBackend` /
  `TelemetryParser` instead of editing `mic/stt/tts/explain/agent`. The
  existing voice modules are unmodified; only `run_phase0.py` changed.
- Mic meter is read live from `SttEngine.meter` instead of being recorded.
- `TelemetryParser` splits `throttled` / `backend_error` / `rejected` /
  `accepted`. This closes the ambiguous-`None` item from the open-issues
  list, and a test caught that exceptions bypassed recording.

### 10.6 Revised sequencing

| Phase | Content | Status |
|---|---|---|
| A | `voice/telemetry.py`, `voice/status.py`, `run_phase0` wiring | built, 19 new tests |
| A2 | `voice_command.json` listener in the voice process (listen / speak_test) | built, 13 tests, verified live on the C270 |
| B | `GET /api/voice/status` (auth-split) + `POST /api/voice/command` | built, 20 tests |
| C | Tab templates + `dashboard.html` | built, browser test |


---

## 11. As built

**Run it:** start the dashboard as usual, then in another terminal
`python -m voice.run_phase0`. Open the **Voice** tab (`#voice`). Sign in to
see transcripts; press **Listen now** and speak.

| Piece | File | Notes |
|---|---|---|
| Recording + wrappers | `voice/telemetry.py` | bounded rings, no edits to mic/stt/tts/explain/agent |
| Publisher | `voice/status.py` | `voice_status.json`, atomic, key *presence* only |
| Command inbox | `voice/command.py` | `listen` / `speak_test`, fixed phrase, single-flight |
| Runner wiring | `voice/run_phase0.py` | `--no-status` to turn it off |
| Routes | `web.py` (+~100 lines) | `/api/voice/status`, `/api/voice/command` |
| Tab | `templates/_voice_tab.{html,css,js}` | `textContent` only, never `innerHTML` |
| Nav | `templates/dashboard.html` | 5 one-line inserts |

**Tests (new):** `test_voice_telemetry` 13, `test_voice_status` 8,
`test_voice_command` 13, `test_web_voice` 20, `test_dashboard_ui_voice` 1
(browser). Whole suite passes. The browser test was mutation-checked: making
the transcript `innerHTML` makes it fail.

**Verified live** (scratch dir, real C270 + Deepgram): web server and voice
process share a directory; signed-out reads are redacted; a password POST
queued a listen; the voice process ran it and the result appeared in the
status; `verb:"drive"` was refused with 400.

### Still open (not part of this tab)
- The **parser lane** is not constructed by `run_phase0`, so it reads *down*
  ("not configured") until a rules engine / parse loop exists.
- Transcript, parse and latency cards fill only when something listens. Until
  voice v1 exists, **Listen now** is the producer.
- The RMS gate is a loudness gate: the meter makes that visible, it does not
  fix it. Needs a real VAD.
- 0 / 20 phrases cached: all speech still depends on the network.

---

## 12. Microphone card (added after §11)

The tab showed "no data" for every lane whenever the voice process was off —
including the microphone, which does not need that process to be checked.

| Question | Answer comes from | Needs voice process? |
|---|---|---|
| Which mic is connected? | `voice.mic.mic_inventory()` — sysfs + `arecord -l`, opens nothing | no |
| Is it working? | `voice.mic.probe_microphone()` — one 1.5 s capture, classified `working / silent / busy / missing / no_tool / error` | no |

- `GET /api/voice/mic` (open, never 500s) and `POST /api/voice/mic/test`
  (password, one at a time).
- **Deliberate change to an earlier rule:** §2 said `web.py` must never touch
  `voice`. It now lazily imports exactly two read-only functions
  (`mic_inventory`, `probe_microphone`) inside the route handlers. A test
  enforces: no module-level `voice` import, only `voice.mic`, only those two
  names, and no reference to `MicCapture`/`SttEngine`/`listen`. If the voice
  process is mid-listen the probe reports `busy` instead of fighting for the
  device.
- "Working" means audio is arriving, not that anyone spoke. Flat zero is
  `silent` (muted/dead), never a pass. Unplugging after a good test shows
  `not found`, never a stale "working".
- Tests: `test_voice_mic_probe` (10), `test_web_voice` (+12),
  `test_dashboard_ui_voice_mic` (browser, every state stubbed).

---

## 12. Addendum — the camera link (2026-10-22)

`/api/voice/command` now accepts four verbs, not two: `listen`, `speak_test`, and
`announce_who` / `call_person {name}`. The new two only make the rover say what the camera
sees, from fixed templates; `call_person` takes the name of an *enrolled* person and nothing
else is spoken from the browser. They are still password-gated, whitelisted, single-flight
and unable to reach `control_state`. The status payload gains a `faces` lane (only when the
face watcher runs). See `docs/cam-voice-integration-plan.md`.

