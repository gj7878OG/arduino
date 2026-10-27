"""Tests for voice.persona — the pure event→utterance renderer.

These tests pin the canonical 10 phrases. Their job is to fail loudly
if anyone "fixes" Tarka's wording without updating PERSONA.md and the
phrase cache.
"""

import _env  # noqa: F401

from voice.persona import render_event, utterance_for_event


# ── Canonical 10 (mirrors voice/PERSONA.md and voice/PHRASES.md) ────

def test_canonical_wake_ready():
    assert render_event('wake_ready') == 'Yes?'


def test_canonical_moving():
    assert render_event('motion_authorised') == 'Moving.'


def test_canonical_done():
    assert render_event('motion_complete') == 'Done.'


def test_canonical_stopped():
    assert render_event('emergency_stop') == 'Stopped.'


def test_canonical_voice_off():
    assert render_event('mute_engaged') == 'Voice off.'


def test_canonical_voice_on():
    assert render_event('mute_released') == 'Voice on.'


def test_canonical_lidar_hold_person_ahead():
    """The exact form of the most-fired hold phrase. The dashboard's
    points.json emits ~10 Hz; if the template drifts, this catches it."""
    assert render_event('lidar_hold', reason='person ahead') == \
        'Holding — person ahead. Will retry when clear.'


def test_canonical_out_of_scope():
    assert render_event('out_of_scope') == \
        "I can't do that from voice. Please use the dashboard."


def test_canonical_clarify_default():
    assert render_event('clarify_default') == "Sorry, I didn't catch that."


def test_canonical_confirm_timeout():
    assert render_event('confirm_timeout') == 'No confirmation. Standing by.'


# ── Slot semantics ─────────────────────────────────────────────────

def test_unknown_event_returns_none():
    assert render_event('not_an_event') is None
    assert render_event('') is None
    assert render_event('MOTION_AUTHORISED') is None    # case sensitive


def test_missing_slot_renders_literally_in_the_template():
    """A watcher that forgets a slot must not crash the persona module.
    The {placeholder} stays visible in the output, which makes the bug
    obvious in the log instead of silent."""
    out = render_event('battery_low')        # no pct slot
    assert '{pct}' in out
    assert 'Battery at' in out


def test_slot_is_filled_in():
    assert render_event('battery_low', pct='23') == \
        'Battery at 23 percent. Returning to dock.'


def test_lidar_hold_with_reason():
    assert render_event('lidar_hold', reason='obstacle ahead') == \
        'Holding — obstacle ahead. Will retry when clear.'


def test_motors_enabled_includes_mode():
    assert render_event('motors_enabled', mode='manual') == 'Motors on. manual mode.'
    assert render_event('motors_enabled', mode='auto') == 'Motors on. auto mode.'


def test_mode_changed_includes_mode():
    assert render_event('mode_changed', mode='manual') == 'manual mode.'
    assert render_event('mode_changed', mode='auto') == 'auto mode.'


def test_utterance_for_event_alias_matches():
    assert utterance_for_event('motion_authorised') == \
           render_event('motion_authorised')


def test_extra_slots_are_ignored_safely():
    """The persona only uses the slots it knows about. Spurious kwargs
    must not raise."""
    out = render_event('motion_authorised', extra='ignored', other=42)
    assert out == 'Moving.'


# ── camera link phrases ─────────────────────────────────────────────
def test_every_persona_event_has_a_template_and_renders():
    """A new event name with no template would be silently mute."""
    from voice._constants import PERSONA_EVENTS
    from voice.persona import render_event
    slots = dict(reason='x', mode='auto', pct=5, greeting='Hi', name='A', names='A', others='one person')
    for ev in PERSONA_EVENTS:
        out = render_event(ev, **slots)
        assert out and '{' not in out, ev


def test_camera_phrases():
    from voice.persona import render_event as r
    assert r('person_greeted', greeting='Welcome back, Alice.') == 'Welcome back, Alice.'
    assert r('person_called', name='Alice') == 'Alice, please come to the rover.'
    assert r('person_not_seen', name='Alice') == "I can't see Alice."
    assert r('who_none') == "I don't see anyone."
    assert r('who_known', names='Alice and Bob') == 'I see Alice and Bob.'
    assert r('who_mixed', names='Alice', others='two people') == "I see Alice and two people I don't recognise."
    assert r('who_strangers', others='one person') == "I see one person I don't recognise."
    assert r('person_unknown') == 'Unrecognised person.'


def test_blind_phrase_does_not_claim_an_empty_room():
    from voice.persona import render_event as r
    text = r('who_blind').lower()
    assert "can't see" in text and 'anyone' not in text and 'nobody' not in text


def test_name_events_are_flagged_dynamic_and_the_rest_are_not():
    from voice._constants import DYNAMIC_EVENTS, PERSONA_EVENTS
    assert DYNAMIC_EVENTS <= PERSONA_EVENTS
    assert {'person_greeted', 'person_called', 'person_not_seen', 'who_known', 'who_mixed'} == DYNAMIC_EVENTS
    assert 'who_none' not in DYNAMIC_EVENTS and 'lidar_hold' not in DYNAMIC_EVENTS


if __name__ == '__main__':
    _env.run_tests(globals())
