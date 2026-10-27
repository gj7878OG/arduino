"""Persona utterances — pure rendering from events + state.

Owns Tarka's voice. This module is intentionally side-effect free: it
turns an event name + slots into a string. The audio layer
(``voice.tts``) decides whether to play a recorded clip or synthesise.

No JSON, no audio, no I/O — fully testable in isolation. Any system
that wants Tarka to say something calls ``render_event``.
"""

from __future__ import annotations

from typing import Optional

from ._constants import (
    HOLD_PROMPT,
    PERSONA_EVENTS,
)


# Event → utterance template. Curated set; this is the *only* place
# Tarka's exact wording lives. The phrase cache in ``voice/phrases/`` is
# keyed by event name; keep event names stable so the cache stays valid.
_UTTERANCES: dict[str, str] = {
    # ── Canonical 10 (pinned by tests/test_voice_persona.py) ────────
    'wake_ready':          'Yes?',
    'motion_authorised':   'Moving.',
    'motion_complete':     'Done.',
    'emergency_stop':      'Stopped.',
    'mute_engaged':        'Voice off.',
    'mute_released':       'Voice on.',
    'confirm_timeout':     'No confirmation. Standing by.',
    'clarify_default':     "Sorry, I didn't catch that.",
    'out_of_scope':        "I can't do that from voice. Please use the dashboard.",
    # 'lidar_hold' uses the HOLD_PROMPT template — see below.

    # ── Hold family ────────────────────────────────────────────────
    'lidar_hold':          HOLD_PROMPT,        # 'Holding — {reason}. Will retry when clear.'
    'lidar_clear':         'Path clear.',

    # ── Slot-fill clarifications ───────────────────────────────────
    'clarify_direction':   'Sorry, which direction?',
    'clarify_distance':    'Distance?',

    # ── Status + fault announcements ───────────────────────────────
    'battery_low':         'Battery at {pct} percent. Returning to dock.',
    'stuck':               "I'm stuck. Need assistance.",

    # ── Phase 0 narration (read from the dashboard's control.json /
    #    points.json by voice.event_watcher) ────────────────────────
    'motors_enabled':      'Motors on. {mode} mode.',
    'motors_disabled':     'Motors off.',
    'mode_changed':        '{mode} mode.',
    'obstacle_stop_disabled':
        'Obstacle stop disabled. Password override accepted. Test bench mode active.',
    'lidar_disabled':
        'LiDAR disabled. Password override accepted. Test bench mode active.',

    # ── Camera link ────────────────────────────────────────────────
    # {greeting} is the person's own phrase or 'Hello, <name>.', composed by
    # voice.face_watcher so this module stays free of logic.
    'person_greeted':      '{greeting}',
    'person_unknown':      'Unrecognised person.',
    'person_called':       '{name}, please come to the rover.',
    'person_not_seen':     "I can't see {name}.",
    'who_none':            "I don't see anyone.",
    'who_known':           'I see {names}.',
    'who_mixed':           "I see {names} and {others} I don't recognise.",
    'who_strangers':       "I see {others} I don't recognise.",
    'who_blind':           "I can't see right now.",

    # ── Confirm flow ───────────────────────────────────────────────
    'confirm_prompt':      'Say confirm to go.',
    'confirmed':           'Confirmed.',
    'cancelled':           'Cancelled.',
    'understood':          'Understood.',
    'standing_by':         'Standing by.',

    # ── Status report pieces (composed by voice.respond from telemetry) ──
    'status_motors_on':    'Motors are on.',
    'status_mode_auto':    'Auto mode.',
    'status_mode_manual':  'Manual mode.',
    'status_lidar_clear':  'LiDAR is clear.',
    'status_lidar_offline': 'LiDAR is offline.',
    'status_lidar_off':    'LiDAR safety is off.',
    'status_camera_online': 'Camera is online.',
    'status_camera_offline': 'Camera is offline.',
    # Honest "no": there is no battery sensor and no pose estimate yet.
    'battery_unknown':     "I can't measure the battery.",
    'position_unknown':    "I don't track my position.",
    'controller_unreachable': "I can't reach the controller.",
    # Voice can't drive yet, and must never *sound* like it did.
    'voice_control_off':   "Voice control isn't switched on. Use the dashboard.",

    # ── Help ───────────────────────────────────────────────────────
    'intro':               "I'm Tarka. Say stop at any time.",
    'help_summary':        'I can move, stop, and report status.',

    # ── Obstacle ───────────────────────────────────────────────────
    'obstacle_none':       'Nothing in front of me.',
    'obstacle_stop':       'Obstacle ahead. Stopping.',
    'obstacle_slow':       'Slowing down. Obstacle nearby.',
}


def _safe_format(template: str, slots: dict) -> str:
    """Replace ``{key}`` placeholders; leave unknown keys literal so the
    template survives even when a caller forgot a slot."""
    out = template
    for k, v in slots.items():
        out = out.replace('{' + k + '}', str(v))
    return out


def render_event(event: str, **slots) -> Optional[str]:
    """Return Tarka's utterance for ``event``, or ``None`` if unknown.

    Unknown event names return ``None`` so callers can ignore them
    without importing ``PERSONA_EVENTS``. Missing slot values are
    rendered literally — keeps ``render_event`` safe to call from any
    watcher that may not have every slot.
    """
    if event not in PERSONA_EVENTS:
        return None
    template = _UTTERANCES.get(event)
    if template is None:
        return None
    return _safe_format(template, dict(slots))


def utterance_for_event(event: str, **slots) -> Optional[str]:
    """Alias for ``render_event`` — same behaviour, noun form for callers
    that prefer to read it as 'what does Tarka say for this event?'."""
    return render_event(event, **slots)


__all__ = ['render_event', 'utterance_for_event']
