# AGV: camera line follower + Arduino

Camera (IMX219, ~1 m high, ~45 deg down) -> `vision.py` -> `decision.py` -> `motion.py` -> Arduino (`M<left>,<right>`).

## Run
1. Flash the firmware once (stops the motors on reset):
   `arduino-cli compile --fqbn arduino:avr:uno MotorControl && arduino-cli upload -p /dev/ttyACM0 --fqbn arduino:avr:uno MotorControl`
2. `./agv_run.sh` (restart `web.py` too if it was already running), open `http://<jetson>:5000`.
3. The AGV always starts **OFF**. Press **START** on the dashboard to enable the motors; **STOP** turns them off (bounded ramp, ~0.25 s).
   START asks for the dashboard password; STOP never does. The password is `AGV_DASHBOARD_PASSWORD`, else the first line of `.dashboard_password` (generated on first run; edit the file to change it, then restart `web.py`). 5 wrong guesses lock START for 30 s.
4. **Max speed** slider (10-100 %): master limit that scales every other speed (line following, curves, caution, obstacle slow, turning, search). Applies immediately, ramped by the jerk limiter; kept across restarts.

## Safety layers
- Dashboard OFF / no camera frame / stalled vision / Arduino ACK missing -> emergency (bounded) ramp to 0.
- Firmware: no valid command for 1 s -> motors ramp to 0. Firmware also slew-limits PWM itself.
- Obstacle STOP (camera) -> emergency stop. Obstacle SLOW -> half speed.
- Without firmware v2 (`ACK2` replies) the AGV refuses to drive.

## Measure first, then tune (all via `AGV_*` env vars, see `config.py`)
| Setting | Meaning |
|---|---|
| `AGV_CAM_HEIGHT_M`, `AGV_CAM_TILT_DEG` | camera pose (drives the bird's-eye warp and every distance) |
| `AGV_ROVER_WIDTH_M` | obstacle corridor width |
| `AGV_MIN_PWM` | PWM where the wheels just start to turn (stiction) |
| `AGV_V_MAX`, `AGV_V_MIN` | speed range (start low, e.g. 100/60) |
| `AGV_STEER_GAIN`, `AGV_OMEGA_MAX` | steering strength |
| `AGV_MAX_ACCEL`, `AGV_MAX_JERK` | smoothness (PWM/s^2, PWM/s^3) |
| `AGV_OBST_STOP_M`, `AGV_OBST_SLOW_M`, `AGV_OBST_DELTA_E` | obstacle distances and sensitivity |
| `AGV_RECOVERY=0` | disable the slow search when the line is lost |

Check the dashboard BEV inset (bottom right of the video): a straight line on the floor must look straight and vertical, and obstacle distances must match a tape measure. If not, fix height/tilt first.

## Limits
Obstacle detection compares the floor in front of the rover with its own colour: it can miss floor-coloured or very low objects and needs steady lighting. Start with the corridor clear.

## Yellow-line detection (how it works)
- Colour test in CIELAB, not fixed HSV: enough chroma, hue near yellow (`AGV_LINE_HUE_DEG`, `_TOL_DEG`), and at least `AGV_LINE_HUE_MARGIN_DEG` closer to yellow than the median floor pixel. Measured on the real tape (pale masking tape on a wood desk): tape Lab hue ~102 / HSV S ~85, wood hue ~80 / H 18. Cream walls and pink lights give no mask.
- Colour tracking: once the line is found, the tape's colour *relative to the floor* is remembered; if a colour cast later breaks the hue test, the line is followed by that colour (up to `AGV_TAPE_MODEL_TTL_S`). A cast present from the very first frame is not recoverable: fix the lighting or white balance (`AGV_CAMERA_WB_MODE`).
- Blob choice: long (`AGV_LINE_MIN_LENGTH_M`) and elongated (`AGV_LINE_MIN_ELONGATION`) shapes only, preferring the one near where the line just was. Round yellow objects are ignored.
- A sudden lighting step can show as an obstacle STOP; pressing OFF then ON relearns the floor.

## Steering and PWM (what the controller does)
- **Pure pursuit:** the turn is the curvature that reaches the point `AGV_LOOKAHEAD_M` ahead, and the differential PWM is `omega = kappa * v * wheel_base / 2`. The turn therefore scales with speed, which keeps the loop stable from creeping to full speed (the old fixed-gain law weaved and lost the line at high speed in simulation). Set `AGV_WHEEL_BASE_M` to the real centre-to-centre wheel distance.
- **Near-offset term** (`AGV_K_OFFSET_CURV`) pulls it onto the line; **trim** (`AGV_KI_OFFSET`) slowly cancels a weaker motor / steady drift. Both back off automatically above the tuned speed.
- **Turn priority at full PWM:** if speed + turn would exceed 255, forward speed is reduced instead of clipping one wheel.
- `AGV_STEER_MODE=p` restores the old fixed-gain law.
- Simulation results (25 cm start offset, 100 ms camera delay, +-8 % motor mismatch): late tracking error 0.4-0.7 cm on straights, about 4 cm on 25 cm-amplitude S-curves, no weaving. Real numbers depend on your chassis: measure the wheel base and `AGV_MIN_PWM` and re-tune on the floor.

## Not turning / drifting off the line (what is checked)
- **Minimum turn authority:** a skid-steer chassis does not turn until the wheels differ by a minimum amount; at a low max-speed setting the pursuit turn alone can be about 1 PWM level, i.e. nothing. The controller now grows a minimum wheel difference (`AGV_TURN_FLOOR`, up to 40 levels) only when needed.
- **Tracking monitor** (dashboard "Line tracking"): while the wheels are driving it watches whether the error to the line shrinks. `ON THE LINE`, `CORRECTING`, `NOT TURNING - BOOSTING` (error not shrinking: turn boosted up to x3 and speed reduced), `DRIFTING OFF - BOOSTING` (error growing), `OFF THE LINE` (far off for 2.5 s). It ignores curved lines and overshoot (crossing the line means the turn is strong enough) and only judges while driving.
- **Pivot to align:** if the heading is more than 45 deg off, or the lookahead point is more than 0.6 m off, it pivots in place first, then arcs back onto the line.
- **Obstacle stop switch:** the dashboard checkbox turns the camera obstacle stop off for bench tests (password needed to turn it OFF). With it enforced, people/feet/boxes in the 0.6 m zone stop the rover by design.
- **Snapshots:** when an obstacle is flagged, `logs/snapshots/` gets the raw frame and the detector views (max 12 per run) for diagnosis.
- Simulation (skid-steer chassis that needs 0.35 rad/s before it turns): the old logic never turned (error stayed 25 cm); the new logic gets back onto the line (about 2-7 cm late error). On an ideal chassis it is unchanged or better.

## Heading from the whole shape, and wide pieces
- The line's heading and position now come from the **principal axis of the whole yellow shape** whenever the line is straight or only gently curved (accurate to a fraction of a degree on diagonal pieces; per-strip centroids under-read them by 5-8 degrees). Strongly curved lines (|curvature| > 0.35 1/m) still use the strip curve fit. With fewer than 3 valid strips the axis is used too, so a short wide piece no longer reads as "straight ahead".
- Width is judged **perpendicular to the line** (area / length), not per image row, and the limit is `AGV_LINE_MAX_WIDTH_M` = 0.45 m (was 0.30), so 30 cm paper pieces and 5-15 cm tape both qualify. A shape wider than that, or not elongated (`AGV_LINE_MIN_ELONGATION`), is rejected.
- **Pivot to align** turns toward the line's *direction* (sign of its heading angle), not toward the sideways offset: a pivot fixes the heading, then the arc steering closes the offset.

## Encoder speed feedback + PID (firmware v3)

One encoder per side (the two motors of a side share a driver and an encoder).
The Arduino measures wheel speed and runs a PID per side; Python only says how
fast each side should go.

**Wiring (Uno)** — change the pins/flags at the top of `MotorControl.ino` if you wire differently:

| Signal | Pin |
|---|---|
| Left encoder A / B | D2 (INT0) / D4 |
| Right encoder A / B | D3 (INT1) / A0 |
| Encoder 5 V / GND | Uno 5 V / GND (common ground with the drivers) |

Set `ENC_INVERT_L/R` to 1 if driving forward makes the tick counter go down.
For a single-channel encoder set `ENC_QUADRATURE 0` (direction then comes from
the sign of the applied PWM). Flash with
`arduino-cli compile --upload -p /dev/ttyACM0 --fqbn arduino:avr:uno MotorControl`.

**Calibrate** (`config.py` / env): `AGV_ENC_PPR` (pulses per rev, per channel),
`AGV_ENC_GEAR` (encoder revs per wheel rev), `AGV_WHEEL_DIAMETER_M`,
`AGV_WHEEL_MAX_MPS` (speed at full PWM), `AGV_PID_KP/KI/KD` (PWM per m/s).
Use the dashboard chassis card: run "Both ▲" in open loop at 100 %, read the
*Peak seen* speed and put it in `AGV_WHEEL_MAX_MPS`; then use the *Step test*
in closed loop and raise/lower `AGV_PID_KP/KI` until rise time is short without
overshoot. Aim for at least ~1000 ticks per metre; fewer makes the speed
reading coarse.

**Safety:** the PID can only add ±100 PWM to the feed-forward, the slew limit and
1 s command timeout still apply, and a wheel that is commanded but shows no
encoder motion for 1.2 s is flagged (dashboard: red ENC) and falls back to
feed-forward only. Python uses `V<l>,<r>` only after the firmware reports v3 and
accepted the gains; with older firmware, or with the dashboard "Closed loop"
box off, it sends raw `M<l>,<r>` PWM as before.

## Dashboard sign-in (access + refresh tokens)

`POST /api/login {"password": ...}` returns a short-lived **access token**
(15 min, `AGV_ACCESS_TTL_S`) and a **refresh token** (8 h, `AGV_REFRESH_TTL_S`).
The page keeps both in `localStorage` with their absolute expiry, so reloading
the page neither signs you out nor restarts the clock; the access token is
refreshed in the background (`/api/refresh`). The refresh token is never
extended by use, so a session ends 8 h after sign-in. Sign out (`/api/logout`)
revokes it. Tokens are HMAC-signed with `.dashboard_secret` + the password, so
changing the password signs everyone out. Scripts can still send the
`X-AGV-Password` header. Protected actions: START, MANUAL mode and driving,
turning the obstacle stop off. STOP, AUTO and the speed slider need no sign-in.

Reading recordings (frames, logs, downloads, derived views) also needs sign-in
(`AGV_REC_REQUIRE_AUTH`); the page gets a 10 min file token (`?ft=`, `AGV_REC_FILE_TOKEN_TTL_S`)
for image and download links, revoked on sign-out. Deleting a recording needs it too;
record start / stop / mark and the session list do not.

## Bench test without the whole project

`./agv_motortest.sh` starts only the Arduino link and the dashboard (no camera,
no vision, no line following). Open the dashboard, press **Enable manual
access** (sign in, MANUAL + START in one click) and use the **Bench** tab:
per-side sliders and +/- buttons, raw PWM or PID speed, ramp, hold-to-spin wheel
pairs, step test, encoder zero and ticks-per-metre calibration. It cannot run
together with `./agv_run.sh` (both need the Arduino port); with the full project
running, the same controls work too.

## Recording test data (Record tab)

The dashboard's **Record** tab (or the ● Record chip at the top, and the **M**
key to mark) saves a session under `recordings/<date_time>/`: the raw camera
frames (after the software flip and green-cast fix, i.e. exactly what the
detector saw) plus a log row per saved frame. A **mark** flags any frame you
disagree with. Record start / stop / mark need no sign-in; deleting a session does.

### Recording every AUTO drive

**Switches → Drive → "Record every AUTO drive automatically"** (`record_auto`
in `control.json`, **off** by default). With it on, the dashboard starts a
recording itself the moment the rover actually begins driving — AUTO *and* the
motors on, not merely AUTO selected — and stops it when the drive ends, then
asks what to do with the session.

The trigger and the prompt are front-end; the switch itself only carries the
flag, so it needs no password (like `closed_loop`, it is a standing preference,
not a command). It is **off** by default because every drive it covers writes
frames to disk.

What it deliberately does **not** do: a recording you started by hand is never
stopped automatically, and nothing is deleted without an explicit Delete (which
does ask for sign-in). The prompt is held until the motors are actually off, so
leaving AUTO for MANUAL with the drive still live cannot throw a modal over a
live d-pad.

**What is stored:** frames (JPEG, full size, quality 88), `log.jsonl` rows (line
offset / angle / lookahead / curvature, obstacle state, the decision, speeds,
wheel PWM, encoder ticks / rpm / faults, where the line was drawn, frame age),
a faster `telemetry.jsonl`, `events.jsonl`, and `meta.json` (camera, geometry,
robot, firmware, git commit + source hashes, config snapshot).
**What is NOT stored:** the annotated frame, the views mosaic (top-down view,
yellow mask, obstacle masks) and any masks. The review page rebuilds them on
demand from the raw frame with the *current* `vision.py`. Caveat: every derived
view starts a fresh (cold) detector, so it can differ slightly from the live
decision, which had history. The **warm start** option replays up to 10 previous
saved frames (within 3 s) first, which is closer to live but still not identical.
The review page warns when the code changed since recording. Derived views are
refused ("busy: rover driving") while the motors are enabled
(`AGV_REC_DERIVED_WHILE_DRIVING=1` overrides), one render at a time, cached.

### Capture modes (select next to the rate in the Record tab)
- **Continuous** at N fps (`AGV_REC_RATE_HZ`, default 5). A problem frame (see
  triggers) is always saved too, at most once per second; marks always save.
- **Triggered**: a slow *baseline* (`AGV_REC_BASELINE_HZ`, 0.5 fps) plus a ring
  buffer in RAM of the last `AGV_REC_PRE_S` (8 s) sampled at `AGV_REC_RING_HZ`
  (10 fps, at most `AGV_REC_RING_MAX_MB` of JPEG). When a trigger fires, the ring
  is written (`save="pre"`), then the trigger frame, then frames at the ring
  rate for `AGV_REC_POST_S` (3 s, `save="post"`). Good for long drives where only
  the trouble spots matter.

Triggers (also listed in each row's `triggers` and in `events.jsonl`): `line_lost`,
`track_stuck`, `track_drifting`, `track_off_line`, `obstacle_slow`,
`obstacle_stop`, `implausible` (offset / lookahead beyond the track width,
angle > `AGV_REC_TRIG_MAX_ANGLE_DEG`, fewer than `AGV_REC_TRIG_MIN_STRIPS`
strips, or curvature beyond the limit), `jump` (offset or angle changed by more
than `AGV_REC_TRIG_JUMP_M` / `AGV_REC_TRIG_JUMP_DEG` between two found frames
< 0.5 s apart), `enc_fault`, `link_down`; `mark` is the operator's.
Throttling: each trigger has a cooldown (`AGV_REC_TRIG_COOLDOWN_S`, 1 s). A
condition that stays true is one *episode*: it fires once, and re-fires only
every `AGV_REC_TRIG_PERSIST_S` (30 s) while it lasts; it counts as a new episode
(fires again right away) once it was false for `AGV_REC_TRIG_REARM_S` (1 s). So
a stuck line cannot keep the post window open forever.

### On-disk layout (schema 1)
```
recordings/<YYYYmmdd_HHMMSS>/
  meta.json  manifest.json  summary.json (written at stop)
  shards/s0000/frames/000000.jpg ...  log.jsonl  events.jsonl  telemetry.jsonl  manifest.json
  shards/s0001/...
```
A shard is 300 s or 3000 frames (`AGV_REC_SHARD_S`, `AGV_REC_SHARD_MAX_FRAMES`).
Readers sort rows by `i` (the capture index; gaps are normal in triggered mode).
- `meta.json`: `schema`, `mode`, `capture` settings, `image`, `camera`, `geometry`
  (incl. homography), `robot`, `firmware`, `code` (git commit/dirty, `src_fingerprint`), `triggers`, `config`.
- `log.jsonl` row: `i, t, dt, t_mono, file, shard, save` (continuous / baseline /
  trigger / pre / post / mark), `triggers`, `problem`, `mark`, `line{}`,
  `obstacle{}`, `decision{}`, `wheels{}`, `enc{}`, `img{w,h,bytes}`, `luma`, `link_ok`, `vision_ms`.
- `telemetry.jsonl`: compact rows at `AGV_REC_TELEM_HZ` (10), saved even when no frame is.
- `events.jsonl`: record_start/stop, shard_open/close, mark, trigger, problem, system transitions.
- `manifest.json` (per shard and top level): file list with sha256 + sizes, counts, `complete`.
- `summary.json`: frames, problems, marks, dropped, size, duration, stop reason,
  `triggers` counts, `saved_by`, `complete`.

Full schema: "Data spec" in `PLAN_RECORDER_V1.md`.

### Crash safety and the tool
All disk work happens on one writer thread per session; the control loop only
queues a frame copy (bounded by `AGV_REC_QUEUE_MAX`; when full the frame is
dropped and counted, never waited for). Images and JSON files are written to a
temp name then renamed; JSONL is append-only and fsynced every
`AGV_REC_FSYNC_S` (2 s); each closed shard gets a manifest with sha256 checksums.
A power cut loses at most the last ~2 s of the open shard.
```
python3 recording_tool.py info   <session>   # schema, counts, triggers
python3 recording_tool.py verify <session>   # checksums + every row's image; VERIFY OK, exit 1 on problems
python3 recording_tool.py repair <session>   # after a crash: rebuild the open shard's manifest, drop *.tmp, write summary.json (complete:false)
```
`<session>` is a name under `recordings/` or a folder path.

### Review, download, replay
- **Review** (`/review/<session>`): frame scrubber, play, jump to next flagged
  frame, overlay of the logged line, derived views (above), every logged value.
- **Download** gives the whole session as a `.tar`.
- **Replay after a code change:** `python3 replay_recording.py <session>` re-runs
  the current detector on the saved frames and compares it with what was logged.
  `--out file.csv` writes a per-frame table, `--flagged` only flagged frames.
- **v0 compatibility:** old flat sessions (`frames/`, `log.jsonl`, `views/`) still
  list, review (their saved `views/` are shown directly) and replay. The old
  recorder is kept as the rollback (`AGV_REC_FORMAT=v0`).

### Access control
With `AGV_REC_REQUIRE_AUTH=1` (default) frames, logs, events, meta, downloads,
derived views and `/api/build` need sign-in; the session **list** stays open
(`AGV_REC_LIST_REQUIRE_AUTH=1` closes it) and `/review/<name>` is only an empty
shell. The review page shares the dashboard sign-in and shows its own form if
needed. Scripts: `curl -H 'X-AGV-Password: ...' http://<jetson>:5000/recordings/<s>.tar -o s.tar`
or a Bearer access token. Unauthenticated reads get 401 and never count as wrong
passwords. See also "Dashboard sign-in" above.

### Limits, disk use, tuning
Per session: 3 GB, 60 min, stop at 3 GB free disk. A frame is about 0.13 MB
(1280x720, q88): 5 fps is ~0.65 MB/s, ~2.3 GB/h. Triggered mode saves far less on
a calm run, but a noisy scene can still save several frames per second; tune
`AGV_REC_RING_HZ`, `AGV_REC_POST_S`, `AGV_REC_TRIG_COOLDOWN_S` (and
`AGV_REC_PRE_S`) or use `AGV_REC_SAVE_WIDTH` to shrink images.

| Variable | Default | Meaning |
|---|---|---|
| `AGV_REC_DIR` | `./recordings` | where sessions are stored |
| `AGV_REC_FORMAT` | `v1` | `v1` sharded recorder, `v0` legacy recorder (rollback) |
| `AGV_REC_MODE_DEFAULT` | `continuous` | capture mode when none is chosen |
| `AGV_REC_RATE_HZ` | 5 | continuous frames per second |
| `AGV_REC_MAX_GB` | 3 | size cap per session |
| `AGV_REC_MAX_MIN` | 60 | duration cap per session |
| `AGV_REC_MIN_FREE_GB` | 3 | stop when the disk has less free |
| `AGV_REC_JPEG_QUALITY` | 88 | JPEG quality |
| `AGV_REC_SAVE_WIDTH` | 0 | resize saved frames to this width (0 = full size) |
| `AGV_REC_SHARD_S` | 300 | seconds per shard |
| `AGV_REC_SHARD_MAX_FRAMES` | 3000 | frames per shard |
| `AGV_REC_FSYNC_S` | 2.0 | fsync interval for the logs |
| `AGV_REC_QUEUE_MAX` | 24 | frames waiting for the writer (full = drop) |
| `AGV_REC_PRE_S` | 8 | triggered: pre-roll seconds in the ring |
| `AGV_REC_RING_HZ` | 10 | triggered: ring / post-window frame rate |
| `AGV_REC_RING_MAX_MB` | 48 | triggered: ring memory cap |
| `AGV_REC_POST_S` | 3 | triggered: seconds saved after a trigger |
| `AGV_REC_BASELINE_HZ` | 0.5 | triggered: background frames per second |
| `AGV_REC_TELEM_HZ` | 10 | telemetry rows per second |
| `AGV_REC_TRIG_COOLDOWN_S` | 1.0 | min seconds between two fires of one trigger |
| `AGV_REC_TRIG_PERSIST_S` | 30 | a condition that stays true re-fires this often |
| `AGV_REC_TRIG_REARM_S` | 1.0 | false this long = next true is a new episode |
| `AGV_REC_TRIG_MAX_OFFSET_M` | `BEV_X_HALF_M`+0.2 | `implausible`: max offset / lookahead |
| `AGV_REC_TRIG_MAX_ANGLE_DEG` | 75 | `implausible`: max angle |
| `AGV_REC_TRIG_MIN_STRIPS` | 2 | `implausible`: min strips |
| `AGV_REC_TRIG_JUMP_M` | 0.15 | `jump`: offset change |
| `AGV_REC_TRIG_JUMP_DEG` | 25 | `jump`: angle change |
| `AGV_REC_REQUIRE_AUTH` | 1 | sign-in needed to read recordings (0 = rollback) |
| `AGV_REC_LIST_REQUIRE_AUTH` | 0 | also protect the session list |
| `AGV_REC_FILE_TOKEN_TTL_S` | 600 | lifetime of `?ft=` file links |
| `AGV_REC_DERIVED_CACHE` | 64 | cached derived images |
| `AGV_REC_DERIVED_WHILE_DRIVING` | 0 | allow derived views while motors are enabled |

Rollback: `AGV_REC_FORMAT=v0` (old recorder; no modes / telemetry / shards) and/or
`AGV_REC_REQUIRE_AUTH=0`, then restart as usual.

### Video
There is no hardware H.264 encoder on the Orin Nano (no NVENC) and software
encoding would load the CPU, so no video is recorded; per-frame JPEG is kept,
which also keeps every frame independently usable.

### Tests
`python3 tests/run_all.py` (isolated temp dirs, no Arduino, never touches the live
project) must print `ALL PASSED`; `python3 tests/run_all.py --e2e` also runs the
end-to-end recording test.

Each browser test spawns its own `web.py` on its own port, and the files run
strictly one after another, so ports are reused rather than reserved: 5071–5082
across the `test_*_ui*` files, and 5083/5084 for `test_dashboard_audit.py` — the
full-dashboard audit (every route, every front-end call, every DOM id, every
control, all seven tabs). Run it on its own any time the dashboard is live:

```
python3 tests/test_dashboard_audit.py     # also rewrites docs/dashboard-audit.md
```

One file is known to fail for a reason unrelated to the dashboard, so a red run
is not automatically a dashboard bug: `test_voice_notes.py` fails with
`'VoiceTelemetry' object has no attribute 'attach_notes'` (the method does not
exist anywhere in `voice/`).

### Training-data notes
Train from the raw frames plus the log. The detector's masks / lines are only
weak labels: correct them (use the marked frames first) before training on them.
Split train / val / test by **session**, not by frame, because neighbouring
frames are near-duplicates and would leak.
