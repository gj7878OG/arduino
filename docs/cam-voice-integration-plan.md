# Camera ↔ Voice: a name-aware rover

Status: **implemented — phases 1, 2, 3, 4 and 6. Phase 5 is blocked** (see §5). Written 2026-10-22
after reading all of `voice/`, the camera analytics on `feat/cam-dashboard-tab` and the live system;
built the same day. The "As built" section at the end records what changed from this plan.

## 0. Goal and non-goals

Naming: this plan calls the voice persona **"the rover"**. The code still names it through one
constant (`PERSONA_NAME` in `voice/_constants.py`) plus the wake phrase and `voice/PERSONA.md`;
those are unchanged by this plan (see D5).

Goal. The rover (the voice persona) can:

1. **Greet** an enrolled person by name when they appear, using per-person info
   (role, a custom greeting) stored with their face.
2. **Say who is in view** when asked.
3. **Address a named person** ("Rushabh, please come to the rover") and say honestly if it
   cannot see them.

Non-goals (stated so nobody assumes them):

- **Not access control.** SFace on a webcam is fooled by a photo and is only as good as
  one frame. Identity here is a convenience for greetings, never a gate for anything.
- No speaker identification, no free conversation, no motion decisions based on identity.
- Gemini never sees faces or names (see §6).

## 1. What I verified (and what it means)

| # | Finding | Evidence | Consequence |
|---|---|---|---|
| 1 | Voice is a **separate process** from `web.py`; they talk only through JSON files in `/tmp/agv-dashboard`. Sockets were rejected on purpose. | `voice/command.py` header, `voice/DASHBOARD_TAB.md` §2, `voice/status.py` | The integration must be **files**, not imports or HTTP. |
| 2 | Voice today is **output-only**: `run_phase0` = event narration + a 2-verb command inbox + a status publisher. No wake word, no listen loop, no rules engine, no dialogue. | `voice/README.md` "Not yet built", `run_phase0.py` | A spoken "who is there?" **cannot exist yet**. A dashboard-triggered version can. |
| 3 | Speech is rendered from **local persona templates**. The Gemini explainer is constructed only for the status lane and is not wired to speech. | `event_watcher._fire`, `run_phase0.py:95` | Names stay out of Gemini as long as we do not route greetings through `explain()`. Enforce with a test. |
| 4 | The phrase cache has **0 clips**; every utterance is synthesised by **Deepgram (cloud, ~2.5 s)**, then played with a blocking `aplay`. A name can never be pre-baked. | `voice/phrases/` (README only), `tts.py` | **Spoken names are sent to Deepgram.** This is a privacy decision (D1). |
| 5 | Playback **blocks the watcher's single loop thread**. A 3 s greeting would delay a `lidar_hold` announcement. | `EventWatcher.run_forever`, `SystemPlayback.play` | Greetings need their own thread and must yield to safety speech. |
| 6 | `PERSONA.md` says the rover **never says the operator's name** ("It does not know it.") | `voice/PERSONA.md` | This feature **reverses a written rule**. Amend the doc deliberately (D3). |
| 7 | `/api/voice/command` is pinned to **two verbs** and a test says it "must stay two verbs wide". | `web.py:247`, `tests/test_web_voice.py:143` | New verbs widen a stated constraint (D2). |
| 8 | Camera analytics **idle** unless the Camera tab polled in the last 10 s. | `cam/analytics.py` `WATCH_TTL_S` | A greeter needs an always-on mode, which costs CPU. |
| 9 | Live timings from your running session: faces **37 ms**, embed **120 ms**, objects **62 ms**. | `GET /cam/detections` just now | Embedding is 3x slower than my earlier bench (contention with YOLO). A greeter must run faces only, at a capped rate. |
| 10 | C270 video (`uvcvideo`, If 0-1) and audio (`snd-usb-audio`, If 2-3) are separate USB interfaces. | `lsusb -t` | Dashboard camera and voice mic can run together on the same device. |
| 11 | `/tmp/agv-dashboard` is mode **775** (group-readable) and `/tmp` is volatile. | `stat` | Files containing names must be **0600**. Enrolled faces and profiles must live outside `/tmp`. |
| 12 | The voice process is **not running** now; one person (**Rushabh**, 8 samples) is enrolled. | `pgrep`, `/api/cam/people` | Nothing here has been exercised end to end. |
| 13 | Recognition accuracy across poses/lighting is **unmeasured**. My test compared an image to itself (sim 1.0). SFace's 0.363 is a recommended *operating point*, not a safe bar to say a name aloud. | earlier test | Greeting needs a stricter threshold, stability, and a measured evaluation (§8). |

## 2. Principles (taken from how this codebase already works)

1. **Files, not sockets or imports.** Atomic write (`tmp` + `os.replace`), versioned `schema`, readers tolerate missing/stale files.
2. **Staleness is a state.** Camera offline is not the same as "nobody there". The rover must never say "I don't see anyone" when it is blind.
3. **Whitelisted verbs, no browser free text.** Spoken text comes from templates plus a **validated, enrolled** name.
4. **Safety speech outranks greetings.** A greeting is droppable; a hold announcement is not.
5. **Nothing here reaches `control_state`.** Same AST-test guard `explain.py` has.
6. **Opt-in.** Greeting is off globally by default and off per person by default.

## 3. Architecture

```
web process (owns the C270 video)                voice process (owns the C270 mic + speaker)
┌───────────────────────────────┐                ┌──────────────────────────────────┐
│ CamAnalytics (faces, tracks)  │                │ FaceWatcher (own thread)         │
│   │ stable identity           │                │   reads cam_faces.json           │
│   ▼                           │                │   rules: fresh, stable, opted-in,│
│ CamFacePublisher ─────────────┼─ cam_faces.json ─►  cooldown, not during safety    │
│   (2 Hz, 0600, atomic)        │                │   ▼                              │
│                               │                │ PrioritySpeaker ─► TtsEngine     │
│ cam_watch reader ◄────────────┼─ cam_watch.json ◄─ CommandListener (new verbs)    │
│   (wakes analytics ≤30 s)     │                │                                  │
│                               │                │                                  │
│ POST /api/voice/command ──────┼─ voice_command.json ─► announce_who / call_person │
└───────────────────────────────┘                └──────────────────────────────────┘
```

Why `cam_watch.json`: analytics idle by default. For an on-demand question, the voice
process asks the camera to wake for a bounded time, then reads `cam_faces.json`. It is the
reverse direction of `voice_command.json`, with the same replay and age protections.

## 4. Data model

**Profile** (new `face_recognition/profiles.json`, 0600, atomic, gitignored; the CLI's
`faces_db.pkl` format is **not** changed):

```json
{ "Rushabh": { "role": "Operator", "greeting": "Welcome back, Rushabh.",
               "notes": "free text for operators", "greet": false, "updated_at": 1792654032 } }
```

Caps: role 40, greeting 80, notes 300 chars; control characters and newlines stripped.
`notes` never leaves the web process except to a signed-in dashboard session.
`greet` defaults **false**.

**`cam_faces.json`** (written by web, 0600):

```json
{ "schema": 1, "updated_at": 1792654100.2, "pid": 28595,
  "state": "running",                       // running | idle | camera_offline
  "greeter": {"enabled": true, "min_sim": 0.50, "cooldown_s": 300, "greet_unknown": false},
  "faces": [ {"track": 3, "name": "Rushabh", "sim": 0.71, "stable": true,
              "greet": true, "greeting": "Welcome back, Rushabh."} ],
  "unknown": 1,
  "roster": ["Rushabh"] }
```

Only what speech needs: no boxes, no images, no embeddings, no notes.

**`cam_watch.json`** (written by voice): `{id, ts, ttl_s (clamped ≤ 30), reason}`.
Ignored if older than 15 s or already seen (same rules as `voice/command.py`).

## 5. Phases

Each phase ships on its own and leaves the system working.

### Phase 1: Person profiles (web only, standalone value)
- `cam/profiles.py` `ProfileStore`; cascade delete when a person is removed; orphans dropped on load.
- `GET /api/cam/people` returns profile fields (notes redacted when signed out); `PUT /api/cam/people/<name>/profile` requires sign-in.
- UI: People card gets a "Details" editor (role, greeting phrase, notes, "Greet by name" checkbox) with a "Will say: …" preview.
- **Tests:** round-trip, caps, control chars, cascade delete, redaction, PUT auth, names/notes rendered as text only (Playwright XSS check, as in `test_dashboard_ui_cam_detect`).
- **Done when:** a profile edited in the UI survives a restart; a signed-out GET has no notes.

### Phase 2: The camera publishes who is in view
- New settings with `limits`: `greeter_enabled` (false), `greet_min_sim` (0.40–0.80, default 0.50), `greet_cooldown_s` (30–3600, 300), `greet_unknown` (false).
- **Stable identity:** a track is `stable` only after ≥ 2 consecutive embeddings agree on the same name at ≥ `greet_min_sim`. One frame never names anyone.
- **Greeter mode:** analytics stay on with the tab closed, **faces only, ≤ 3 Hz**, objects forced off. Full rate returns while the tab is watched.
- `CamFacePublisher` writes `cam_faces.json`; `state` is honest (`camera_offline` when the publisher is disconnected).
- `cam_watch.json` reader wakes analytics for ≤ 30 s.
- **Tests:** file shape and 0600 mode; stability needs two embeddings; offline state; watch wake/expiry/replay; rate cap; greeter off + tab closed ⇒ idle and no file writes.
- **Done when:** with greeter on and the tab closed the file is ≤ 1 s stale, and **CPU is measured** before/after (I have no number for this yet).

### Phase 3: The rover greets (voice side)
- Constants/persona: events `person_greeted` (`{greeting}`, default "Hello, {name}.") and `person_unknown` ("Unrecognised person.", off by default). Dynamic events are **excluded** from the phrase-cache "expected" count in `status.py`, or the lane would read "degraded" forever.
- `voice/face_watcher.py`: speaks only if the file is fresh (≤ 3 s), `greeter.enabled`, face `stable`, `sim ≥ min_sim`, person opted in; per-name cooldown, a global minimum gap, at most one greeting per tick, and **drops a greeting that is stale by the time it could play**. Text is re-sanitised voice-side (length, control chars): the file is not trusted.
- `PrioritySpeaker`: safety events take the speaker first; greetings try-acquire and drop if busy or if a safety event spoke in the last 10 s. Runs in its own thread, so `lidar_hold` is never queued behind a 3 s synthesis.
- `run_phase0` starts it; `--no-faces` turns it off. Status gets a `faces` lane (file fresh? greeter on? last greeted).
- **Tests:** fake clock; cooldown; opt-in; unknown never named; stale/offline file silent; priority drop; sanitisation; AST test that `face_watcher` imports none of `explain`, `agent`, `intent`, `control_state`.
- **Done when (live):** Rushabh appears → greeted once; leaves and returns within cooldown → silent; camera unplugged → silent and the lane turns red; a hold during a greeting is announced without waiting for it.

### Phase 4: On-demand "who's here" and "call a person" (dashboard-triggered)
- New voice verbs `announce_who` and `call_person {name}` (D2). `name` must pass `valid_name` **and** be in the roster, so nothing arbitrary is ever spoken.
- Flow: write `cam_watch.json` → wait ≤ 4 s for a fresh `cam_faces.json` newer than the request → speak a **three-state honest** result:
  - nobody: "I don't see anyone." · known: "I see Rushabh." · mixed: "I see Rushabh and one person I don't recognise."
  - blind: "I can't see right now. The camera is offline." (never "nobody" when the camera is down)
  - `call_person`: visible → "{name}, please come to the rover." · not visible → "I can't see {name}."
  - Similarity is never spoken.
- UI: **Say who's here** button; per-person **Call** button; both disabled with a reason when the voice process is down (reads `/api/voice/status`). Operator-initiated announcements may name anyone enrolled; per-person `greet` governs only unprompted greetings.
- **Tests:** verb whitelist (update the "two verbs" test on purpose); free text rejected; each honest outcome; timeout; replay protection.

### Phase 5: Spoken "who is there?" (blocked on voice v1)
Needs the wake word, listen loop, rules engine and dialogue, none of which exist. When they do: add `query_person {name?}` to `VALID_INTENT_ACTIONS` and the arg validator, add rules ("who is there", "is Rushabh there"), and call the **same** lookup as Phase 4. Review the 29 pinned rejection cases first. Not scheduled.

### Phase 6: Docs and hardening
Amend `PERSONA.md` (name policy) and `README.md`/`DASHBOARD_TAB.md`; scrub a deleted person's profile and any queued command; document the privacy notes below.

## 6. Privacy and safety

- **Spoken names go to Deepgram** (cloud TTS) because they cannot be cached. Mitigation options: accept it (D1), or move TTS to local Piper first.
- **Gemini gets no names.** Greetings never pass through `explain()`; a test asserts it.
- Names live in `/tmp/agv-dashboard` (mode 775 dir), so the files are **0600**. Profiles and the face db live under `face_recognition/`, outside `/tmp`, gitignored.
- No history is persisted: `cam_faces.json` is overwritten; the in-memory event log already exists.
- Master switch off by default; per-person opt-in off by default.
- A photo of a person will be recognised as that person. Document it; do not use identity for anything that matters.

## 7. Risks

| Risk | Mitigation |
|---|---|
| Wrong name spoken aloud | stability (2 embeds) + stricter `greet_min_sim` + cooldown + the evaluation in §8 |
| Greeter CPU on a Jetson that also runs the AGV | faces only, ≤ 3 Hz, **measure before/after**, kill-switch |
| Greeting delays a safety announcement | separate thread, priority speaker, drop-if-busy |
| Cloud TTS down → silent greeting | acceptable; logged; status lane shows it |
| Two voice processes → every greeting twice | single-instance check (pid in status) |
| Web restart wipes `/tmp` state | profiles/faces already outside `/tmp` |
| Pinned tests break | update the "two verbs" and status-count tests deliberately, in the same commits |

## 8. Accuracy evaluation (before trusting a greeting)

Nothing proves recognition is reliable yet. Before enabling the greeter for real use:
enrol ≥ 3 people; for each, ≥ 20 walk-ups at normal distance, in the room's usual light, with
glasses/no glasses where relevant; log correct / wrong-name / unknown / missed. Pick
`greet_min_sim` from the wrong-name rate (target: zero wrong names in the sample), not from
SFace's default. Record the result in the doc.

## 9. Decisions needed

- **D1 Cloud TTS for names:** accept sending names to Deepgram, or do local Piper first? *(recommend: accept for a trial, note it, revisit)*
- **D2 Widen the voice verbs:** add `announce_who` / `call_person` to the pinned two? *(recommend yes: they only read a file and speak templates; update the test)*
- **D3 Name policy:** amend `PERSONA.md` so the rover may say enrolled people's names? *(this is a deliberate reversal)*
- **D4 Unknown-person announcements:** build `person_unknown` at all, default off? *(recommend: build, default off)*

- **D5 Rename in code:** change `PERSONA_NAME`, the wake phrase and `PERSONA.md` to "rover" too, or keep the old name internally and only say "the rover" in docs? *(recommend: decide before Phase 3, since the wake word will need re-testing if it changes)*

## 10. Order and size (rough)

P1 small · P2 medium · P3 medium · P4 medium · P6 small. P1 is useful alone; P3 is the first
thing you can hear; P2 must precede it. Sizes are my guess, not measured.

## 11. As built

Decisions I took because no answer had been given (reverse any of them):
D1 accept cloud TTS for names (trial) · D2 widen the verbs to four · D3 amend `PERSONA.md` ·
D4 build `person_unknown`, default off · D5 keep the internal persona name, say "the rover" in docs.

| Phase | Where | Notes |
|---|---|---|
| 1 Profiles | `cam/profiles.py`, `cam/_atomic.py`, `/api/cam/people[...]/profile` | notes redacted when signed out; `greet` must be a real boolean; deleting a person deletes their profile (a new person of the same name inherits nothing) |
| 2 Camera → file | `cam/facefile.py`, `CamAnalytics` | `stable` = 2 separate agreeing embeddings; greeter mode ≤ 3 Hz, faces only; `cam_watch.json` wake with replay/age/ttl guards; `cam_faces.json` is 0600 |
| 3 Greeting | `voice/face_watcher.py`, `voice/speaker.py`, `voice/cam_lookup.py` | own thread; `PrioritySpeaker` outermost so the Voice tab's lane still unwraps; `--no-faces` |
| 4 On demand | `announce_who`, `call_person` in `voice/command.py` + `/api/voice/command` | name re-checked against the camera's roster on the voice side too |
| 6 Docs | `voice/PERSONA.md` ("People and names"), `voice/README.md`, `voice/DASHBOARD_TAB.md` | |

Deviations from the plan above:

- **Dropped greetings.** The plan said a greeting is dropped if "stale by the time it could play".
  As built there is no queue to go stale in: `PrioritySpeaker` does a non-blocking acquire and
  drops if busy or if a safety call spoke in the last 10 s; the watcher then retries next tick.
  A *failed synthesis* is recorded as said (otherwise it would hit the paid API every tick).
- **Greeter settings need a password**, not just the per-person edits: turning the greeter on keeps
  the camera analysing with nobody watching and lets the rover say names. Display settings stay open.
- **`who_blind` says only "I can't see right now."** The plan's "The camera is offline" was wrong when
  the cause is a failing model rather than an offline camera.
- **A stale analysis counts as blind** (`idle`), not just an offline camera.
- **UI race found by the browser test and fixed:** a slow response to an earlier greeter save reset the
  control the operator had just changed.

Measured (this Jetson, one clear face, real models):

| | CPU |
|---|---|
| greeter off, tab closed | 0% (idle, nothing analysed) |
| greeter on, tab closed (≈3 Hz) | **23%** of one core |
| tab open (≈10 Hz) | 54% of one core |

The first measurement was **91%**, not 23%: onnxruntime's default thread pool spins on all 6 cores
(82% of a core for YuNet alone at 3 Hz). Two threads with spinning off cost 14% for the same ~21 ms
inference, so `cam/yunet.py` sets that and a test pins it. SFace costs ~3 cores for ~40 ms per embed
but runs at most every 0.8–2 s (~7%).

End-to-end check (real YuNet/SFace, real files, voice modules reading only the files): the greeting
was spoken 1.6 s after the greeter was enabled and was not repeated inside the cooldown;
`announce_who` → "I see <name>."; `call_person` for an enrolled name spoke it; a name that is not
enrolled was refused and not spoken.

**Not verified:** the voice process with real audio and the real dashboard (the running dashboard was
started before these changes and holds the camera, so it must be restarted); recognition accuracy
across people, poses and lighting (§8 is still to do); behaviour on the real Deepgram TTS path for a
greeting.

