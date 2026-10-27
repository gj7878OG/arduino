"""Tests for voice.speaker — one speaker, with priorities.

Playback blocks and a greeting is synthesised in the cloud first, so without
arbitration a greeting could hold the speaker while a LiDAR hold waits behind
it. Unprompted speech must be dropped, never queued.
"""
import _env  # noqa: F401  (must be first)
import threading
import time
from types import SimpleNamespace

from voice.speaker import DroppedPhrase, PrioritySpeaker


class Inner:
    """A speaker that can be held mid-utterance."""

    def __init__(self):
        self.said, self.gate, self.inside = [], None, threading.Event()
        self.cache = 'inner-cache'

    def speak_event(self, event, utterance):
        self.inside.set()
        if self.gate is not None:
            self.gate.wait(3)
        self.said.append(event)
        return SimpleNamespace(event=event, played=True)


def _busy(sp, inner, event='lidar_hold'):
    """Start a safety utterance that holds the speaker until released."""
    inner.gate = threading.Event()
    inner.inside.clear()
    t = threading.Thread(target=sp.speak_event, args=(event, 'x'))
    t.start()
    assert inner.inside.wait(2)
    return t


def test_a_free_speaker_speaks_greetings():
    inner = Inner()
    out = PrioritySpeaker(inner, clock=lambda: 1000.0).speak_event('person_greeted', 'Hi')
    assert out.played and inner.said == ['person_greeted']


def test_a_greeting_is_dropped_not_queued_when_the_speaker_is_busy():
    inner = Inner()
    sp = PrioritySpeaker(inner, clock=lambda: 1000.0)
    t = _busy(sp, inner, event='motors_enabled')
    out = sp.speak_event('person_greeted', 'Hi')
    inner.gate.set()
    t.join(2)
    assert isinstance(out, DroppedPhrase) and out.dropped and not out.played
    assert inner.said == ['motors_enabled'], 'the dropped greeting played later'


def test_a_greeting_is_dropped_just_after_a_safety_announcement():
    inner, now = Inner(), [1000.0]
    sp = PrioritySpeaker(inner, clock=lambda: now[0])
    sp.speak_event('lidar_hold', 'Holding')
    now[0] += 3
    assert sp.speak_event('person_greeted', 'Hi').dropped is True
    now[0] += 20
    assert getattr(sp.speak_event('person_greeted', 'Hi'), 'dropped', False) is False


def test_safety_speech_is_stamped_before_it_waits():
    """A greeting arriving while a safety call queues must drop, not sneak in."""
    inner, now = Inner(), [1000.0]
    sp = PrioritySpeaker(inner, clock=lambda: now[0])
    t = _busy(sp, inner, event='motors_enabled')
    t2 = threading.Thread(target=sp.speak_event, args=('lidar_hold', 'Holding'))
    t2.start()
    time.sleep(0.1)                                  # queued behind the first clip
    assert sp.speak_event('person_greeted', 'Hi').dropped is True
    inner.gate.set()
    t.join(2)
    t2.join(2)
    assert inner.said == ['motors_enabled', 'lidar_hold']


def test_safety_speech_waits_only_for_the_clip_in_flight():
    inner = Inner()
    sp = PrioritySpeaker(inner, clock=lambda: 1000.0)
    t = _busy(sp, inner, event='motors_enabled')
    done = []
    t2 = threading.Thread(target=lambda: done.append(sp.speak_event('lidar_hold', 'Holding')))
    t2.start()
    time.sleep(0.1)
    assert not done                                  # not talking over the clip
    inner.gate.set()
    t.join(2)
    t2.join(2)
    assert done and inner.said[-1] == 'lidar_hold'


def test_operator_answers_wait_their_turn_rather_than_drop():
    inner = Inner()
    sp = PrioritySpeaker(inner, clock=lambda: 1000.0)
    t = _busy(sp, inner, event='motors_enabled')
    out = []
    t2 = threading.Thread(target=lambda: out.append(sp.speak_event('who_known', 'I see Alice.')))
    t2.start()
    time.sleep(0.1)
    inner.gate.set()
    t.join(2)
    t2.join(2)
    assert out and out[0].played and inner.said == ['motors_enabled', 'who_known']


def test_unknown_person_announcements_are_droppable_too():
    inner = Inner()
    sp = PrioritySpeaker(inner, clock=lambda: 1000.0)
    t = _busy(sp, inner, event='motors_enabled')
    assert sp.speak_event('person_unknown', 'Unrecognised person.').dropped is True
    inner.gate.set()
    t.join(2)


def test_attributes_pass_through_so_the_status_lane_still_works():
    inner = Inner()
    assert PrioritySpeaker(inner).cache == 'inner-cache'
    assert PrioritySpeaker(inner)._speaker is inner


if __name__ == '__main__':
    _env.run_tests(globals())
