"""Thresholds, command vocabulary, persona name.

These are referenced by every other module in ``voice/`` and are intentionally
import-safe (no audio, no network). The only non-constant here is the
PERSONA_NAME — change it once and every utterance updates.
"""

from __future__ import annotations

# ── Persona ──────────────────────────────────────────────────────────
# Working name. Change here; every other module reads this constant.
PERSONA_NAME = 'Tarka'

# ── Wake / STT ──────────────────────────────────────────────────────
# Wake-word model id is configured at the model layer, not here.
WAKE_WORD_DEFAULT = 'tarka'           # the literal wake phrase
STT_LANGUAGE_DEFAULT = 'en'
STT_MODEL_DEFAULT = 'tiny'            # whisper.cpp size: tiny|base|small
STT_BEAM_DEFAULT = 1

# ── Microphone (Logitech C270) ──────────────────────────────────────
# The C270 is identified by USB VID:PID, never by ALSA card number —
# card indices shift when other USB audio devices are plugged in, so
# hardcoding "card 2" breaks the day someone attaches a headset.
# voice/mic.py walks sysfs to map VID:PID -> card at runtime.
C270_USB_ID = '046d:0825'

# Capture format. 16 kHz mono S16_LE is what whisper.cpp / ctranslate2
# want natively — no resampling in Python. The C270 UAC mic exposes
# 16 kHz as its lowest rate.
MIC_SAMPLE_RATE = 16000
MIC_CHANNELS = 1
MIC_SAMPLE_WIDTH = 2                  # bytes; S16_LE

# RMS energy gate. Below this the frame is treated as room noise and
# discarded. Calibrated against a C270 ~1 m from a speaking operator:
# a normal voice peaks around RMS 800-3000, room tone sits near 100.
MIC_SPEECH_RMS = 250.0
# Consecutive loud frames needed before we call it speech. ~0.3 s at
# 50 ms frames — long enough to reject a door slam or a cough burst.
MIC_SPEECH_FRAMES = 6
# Frame length for the energy gate. 50 ms = 800 samples at 16 kHz.
MIC_FRAME_MS = 50
# Longest single capture. Commands are short; this is a runaway guard,
# not an expected duration.
MIC_MAX_SECONDS = 10.0

# ── Confirmation gate ───────────────────────────────────────────────
# How long (seconds) Tarka waits for an operator to confirm a parsed
# motion command before treating it as abandoned.
CONFIRM_TIMEOUT_S = 4.0
# Varied confirm phrases (anti-spoof).
CONFIRM_PHRASES = (
    'affirmative',
    'go ahead',
    'do it',
    'confirmed',
    'correct',
)
# Varied echo prompts. The recogniser picks one at random so a radio / TV
# saying "yes" cannot authorise motion.
MOTION_PROMPT_VARIANTS = (
    '{restate} — confirm?',
    '{restate}. Confirm?',
    'Going to {restate}. Confirm?',
)

# ── Intent confidence ───────────────────────────────────────────────
# ≥ CONFIRM_FLOOR_HIGH  → confirm + execute
# < CONFIRM_FLOOR_HIGH and ≥ CONFIRM_FLOOR_MID → confirm with a restate ("Did
#                                                  you mean …?"); slot fill.
# < CONFIRM_FLOOR_MID  → "Sorry, I didn't catch that."
CONFIRM_FLOOR_HIGH = 0.85
CONFIRM_FLOOR_MID = 0.50

# When the rules engine can't match, the LLM lane is consulted at or above
# this confidence. Below it the rover asks for clarification.
LLM_CONFIDENCE_FLOOR = 0.60

# ── Safety ──────────────────────────────────────────────────────────
# Hardware mute: when the MuteSwitch reports pressed, voice input is
# ignored and Tarka announces "Voice off." Once a session, never again
# until the switch is released.
MUTE_ANNOUNCE = 'Voice off.'
MUTE_RELEASE_ANNOUNCE = 'Voice on.'

# ── LiDAR / camera hold behaviour ───────────────────────────────────
# When the safety layer holds motion (LiDAR or camera obstacle), voice
# commands are queued, not dropped. HOLD_PROMPT is the persona's hold
# utterance template, filled with the reason.
HOLD_PROMPT = 'Holding — {reason}. Will retry when clear.'
# Max wait before Tarka re-announces the hold.
HOLD_RETRY_S = 5.0
# Phase 0 watcher debounce: don't fire lidar_hold unless the stop zone has
# stayed populated for this many seconds. Prevents a noisy single return
# from firing the phrase every poll.
HOLD_DEBOUNCE_S = 1.5
# Minimum points inside the stop zone before we say "obstacle present".
HOLD_MIN_STOP_POINTS = 1

# ── Emergency ───────────────────────────────────────────────────────
# Halt / Stop bypasses confirm and is the highest-priority interrupt.
EMERGENCY_STOP_PHRASES = (
    'stop',
    'halt',
    'emergency stop',
    'full stop',
)
EMERGENCY_STOP_ACK = 'Stopped.'

# ── Filenames the watcher expects ───────────────────────────────────
# JSON files written by the main project. The watcher reads them; it
# never writes.
CONTROL_FILENAME = 'control.json'
POINTS_FILENAME = 'points.json'
# Camera link. Written by the dashboard process (cam/facefile.py) and read here;
# CAM_WATCH is the reverse: we ask the camera to wake for a moment. The cam
# package duplicates these two names on purpose (neither side imports the other);
# tests/test_cam_voice_link.py fails if they ever drift apart.
CAM_FACES_FILENAME = 'cam_faces.json'
CAM_WATCH_FILENAME = 'cam_watch.json'

# ── Persona events that produce speech (no audio here) ──────────────
# Each one has an utterance template in PERSONA.md / persona.py.
PERSONA_EVENTS = frozenset({
    # Core persona + dialogue (Phase 0+)
    'wake_ready',          # wake-word heard, awaiting command
    'motion_authorised',   # confirmation accepted, wheels turning
    'motion_complete',     # requested displacement achieved
    'confirm_timeout',     # operator did not confirm in CONFIRM_TIMEOUT_S
    'clarify_direction',   # rule/LLM lacked a direction slot
    'clarify_distance',    # rule/LLM lacked a distance slot
    'clarify_default',     # below CONFIRM_FLOOR_MID
    'lidar_hold',          # safety paused motion (reason slot)
    'lidar_clear',         # safety released motion
    'battery_low',         # below threshold, returning (pct slot)
    'stuck',               # encoder + LiDAR + camera all unhappy
    'mute_engaged',
    'mute_released',
    'emergency_stop',
    'out_of_scope',        # persona-side "use the dashboard"

    # Phase 0 narration (read from control.json / points.json)
    'motors_enabled',      # enabled False→True  (mode slot)
    'motors_disabled',     # enabled True→False
    'mode_changed',        # while enabled, mode flipped (mode slot)
    'obstacle_stop_disabled',     # obstacle_stop True→False (password override)
    'lidar_disabled',             # lidar_enabled True→False (password override)

    # Camera link: who the rover can see
    'person_greeted',      # unprompted, an opted-in person (greeting slot)
    'person_unknown',      # unprompted, optional, off by default
    'person_called',       # operator asked the rover to call someone (name slot)
    'person_not_seen',     # ...but they are not in view (name slot)
    'who_none',            # asked who is here: nobody
    'who_known',           # ...known people only (names slot)
    'who_mixed',           # ...known people plus strangers (names, others slots)
    'who_strangers',       # ...only people it does not recognise (others slot)
    'who_blind',           # ...the camera is offline, so it cannot say

    # Confirm flow, status report and help (fixed text; all pre-recordable)
    'confirm_prompt', 'confirmed', 'cancelled', 'understood', 'standing_by',
    'status_motors_on', 'status_mode_auto', 'status_mode_manual',
    'status_lidar_clear', 'status_lidar_offline', 'status_lidar_off',
    'status_camera_online', 'status_camera_offline',
    'battery_unknown', 'position_unknown', 'controller_unreachable',
    'voice_control_off',
    'intro', 'help_summary',
    'obstacle_none', 'obstacle_stop', 'obstacle_slow',
})

# Events whose text contains a person's name. They can never be a pre-recorded
# clip (a clip named after the event would play without the name), so the phrase
# cache is bypassed for them and they are not counted as missing cache entries.
DYNAMIC_EVENTS = frozenset({'person_greeted', 'person_called', 'person_not_seen',
                            'who_known', 'who_mixed'})

# Speech that must never wait behind, or be delayed by, a greeting.
SAFETY_EVENTS = frozenset({'lidar_hold', 'emergency_stop', 'stuck', 'battery_low',
                           'obstacle_stop_disabled', 'lidar_disabled', 'mute_engaged'})
# Unprompted speech: dropped, never queued, when the speaker is busy.
DROPPABLE_EVENTS = frozenset({'person_greeted', 'person_unknown'})

# ── Camera link behaviour ───────────────────────────────────────────
FACES_FRESH_S = 3.0          # cam_faces.json older than this means the dashboard is not publishing
GREET_MIN_GAP_S = 10.0       # at most one greeting this often, whoever it is for
SAFETY_QUIET_S = 10.0        # no greeting this soon after a safety announcement
UNKNOWN_SEEN_S = 3.0         # a stranger must be in view this long before 'person_unknown'
LOOKUP_TIMEOUT_S = 6.0       # how long 'who is here?' waits for the camera to look
LOOKUP_SETTLE_S = 1.5        # ...and the least it waits before saying nobody is there
LOOKUP_WATCH_TTL_S = 8.0     # how long we ask the camera to stay awake

# ── Motion intent clamps (Phase 3, intent validator) ───────────────
# Hard limits on what a motion intent is allowed to specify, regardless
# of what the LLM says. Same clamp philosophy as the dashboard — a
# prompt-injection or hallucination can't exceed these.
MOTION_MAX_DISTANCE_M = 50.0      # nobody drives an AGV 50 m in one command
MOTION_MIN_DISTANCE_M = 0.05      # smaller is encoder noise
MOTION_VALID_DIRECTIONS = frozenset({'forward', 'back', 'left', 'right'})

# Whitelisted actions the validator accepts. The LLM is *never* trusted
# to invent a new action — anything outside this set is rejected before
# any state change. See voice/intent.py.
VALID_INTENT_ACTIONS = frozenset({
    'motion',           # args: dir, dist_m
    'stop',             # no args; interrupts, top-priority
    'query_status',     # no args; persona speaks telemetry
    'query_position',   # no args; persona speaks pose
    'unknown',          # LLM admitted it couldn't parse; dialogue clarifies
})
