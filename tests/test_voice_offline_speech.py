"""Does the rover still speak when there is no network?

The question this file exists to answer: *if the LiDAR sees an obstacle on a
rover with no internet, does anyone hear about it?* These tests wire the real
unprompted chain — ``EventWatcher`` → ``PrioritySpeaker`` → ``TtsEngine`` → the
real ``voice/phrases/`` cache — with the synthesizer removed, so anything that
plays did so from a pre-baked clip.

They also carry an inventory check. Silence on a safety announcement is the
worst failure this package has, and it fails silently: a persona event added to
a watcher's fire list without a matching clip simply never speaks, and no other
test notices. ``test_every_safety_event_is_recorded_or_explained`` turns that
into a failure.

Playback is faked, so no audio is produced. Tests do their own patching (the
project's runner provides no fixtures).
"""

import _env  # noqa: F401

import json
import tempfile
from pathlib import Path

from voice._constants import (
    DROPPABLE_EVENTS,
    SAFETY_EVENTS,
)
from voice.event_watcher import EventWatcher
from voice.record_phrases import phrase_text
from voice.speaker import DroppedPhrase, PrioritySpeaker
from voice.tts import PHRASES_DIR, PhraseCache, TtsEngine

# Events EventWatcher._fire can raise on its own, from control.json / points.json.
WATCHER_EVENTS = ('motors_enabled', 'motors_disabled', 'mode_changed',
                  'obstacle_stop_disabled', 'lidar_disabled',
                  'lidar_hold', 'lidar_clear')

# These carry a live slot ("Motors on. {mode} mode.") so no single clip can
# cover them; they need live synthesis. Every other unprompted event must be a
# shipped clip. Adding to this list means accepting a silent rover offline.
LIVE_SYNTHESIS_EVENTS = {'motors_enabled', 'mode_changed'}

# Safety events with no clip, and why. Empty is the goal. An entry here is a
# known silence, not a solved problem — see the test's failure message.
UNWIRED_EVENTS = {
    'battery_low': 'nothing fires it; there is no battery sensor in this project, '
                   'and "Returning to dock" describes behaviour the rover does not have',
}


def _clip(event):
    return (PHRASES_DIR / f'{event}.wav').is_file()


class _RecordingPlayback:
    """Stands in for aplay. Records what it was asked to play."""

    def __init__(self):
        self.played = []

    def play(self, path):
        self.played.append(Path(path).stem)
        return True


class _OfflineSynth:
    """A synthesizer that is switched off, and complains if used.

    ``TtsEngine(synthesizer=None)`` does *not* mean "offline": the constructor
    reads ``None`` as "no synthesizer given" and builds a real Deepgram
    backend, which then speaks if a key happens to be in the environment. That
    trap is why this exists, and why these tests pass one explicitly.
    """

    available = False

    def synthesize(self, text):
        raise AssertionError(f'offline test tried to synthesise {text!r}')


def _offline_engine():
    """The real cache, no synthesizer: nothing here can reach the network."""
    playback = _RecordingPlayback()
    engine = TtsEngine(cache=PhraseCache(PHRASES_DIR), playback=playback,
                       synthesizer=_OfflineSynth())
    return engine, playback


# ── the inventory ───────────────────────────────────────────────────


def test_every_unprompted_event_has_a_clip_or_is_on_the_live_list():
    """The watcher must not be able to raise an event the rover cannot speak."""
    missing = [e for e in WATCHER_EVENTS
               if e not in LIVE_SYNTHESIS_EVENTS and not _clip(e)]
    assert not missing, (f'no clip for {missing} — the rover would stay silent. '
                         'Run: python -m voice.record_phrases')


def test_every_safety_event_is_recorded_or_explained():
    """Silence on a *safety* event is the worst bug in this package.

    Anything without a clip must be named in UNWIRED_EVENTS with a reason, so
    the exception is visible and gets a decision when it is wired up.
    """
    unaccounted = [e for e in SAFETY_EVENTS if not _clip(e) and e not in UNWIRED_EVENTS]
    assert not unaccounted, (f'safety events {unaccounted} have no clip and no '
                             'entry in UNWIRED_EVENTS. If they are unreachable, '
                             'add them there with a reason; if they can fire, '
                             'record them with voice.record_phrases')


def test_an_explained_event_really_is_not_fired_anywhere():
    """UNWIRED_EVENTS is a claim. Check it against the code that fires events."""
    import voice.event_watcher as watcher
    src = Path(watcher.__file__).read_text()
    wrongly = [e for e in UNWIRED_EVENTS if f"_fire('{e}'" in src]
    assert not wrongly, (f'{wrongly} are listed as unreachable but event_watcher '
                         'fires them — remove them from UNWIRED_EVENTS and '
                         'record a clip for each')


def test_the_live_synthesis_list_stays_small():
    """Every entry here is a rover that says nothing offline."""
    assert LIVE_SYNTHESIS_EVENTS == {'motors_enabled', 'mode_changed'}, (
        'a new event needs live synthesis to speak. Record a clip instead '
        'unless it carries a value that changes per utterance.')


def test_no_unprompted_event_still_carries_an_unfilled_slot():
    """A recorded clip plays the whole file — it cannot fill a slot later."""
    for event in WATCHER_EVENTS:
        if event in LIVE_SYNTHESIS_EVENTS:
            continue
        text = phrase_text(event)
        assert text and '{' not in text, f'{event} would be recorded as {text!r}'


# ── the actual behaviour, with no network ───────────────────────────


def _telemetry(**kw):
    control = {'enabled': False, 'mode': 'auto', 'obstacle_stop': True,
               'lidar_enabled': True}
    control.update(kw)
    return control


def test_an_obstacle_speaks_with_no_network_at_all():
    """The headline question, end to end.

    Real cache, no synthesizer, fake playback: if audio is produced it came
    from a pre-baked clip.
    """
    engine, playback = _offline_engine()
    assert not engine.synth_available, 'the engine must be offline for this test'
    speaker = PrioritySpeaker(engine)

    with tempfile.TemporaryDirectory() as d:
        p = Path(d)
        (p / 'control.json').write_text(json.dumps(_telemetry(enabled=True)))
        (p / 'points.json').write_text(json.dumps({'points': []}))
        watcher = EventWatcher(d, speaker, hold_debounce_s=0.0)
        watcher.tick()                              # baseline reading

        # An obstacle sits in the stop zone. The first tick arms the debounce;
        # the second one, with the obstacle still there, fires.
        (p / 'points.json').write_text(json.dumps(
            {'points': [{'zone': 'stop', 'x': 0.4}]}))
        watcher.tick()
        watcher.tick()

    assert playback.played == ['lidar_hold'], playback.played


def test_the_path_clear_announcement_speaks_offline_too():
    engine, playback = _offline_engine()
    speaker = PrioritySpeaker(engine)

    with tempfile.TemporaryDirectory() as d:
        p = Path(d)
        (p / 'control.json').write_text(json.dumps(_telemetry(enabled=True)))
        (p / 'points.json').write_text(
            json.dumps({'points': [{'zone': 'stop', 'x': 0.4}]}))
        watcher = EventWatcher(d, speaker, hold_debounce_s=0.0)
        watcher.tick()                              # baseline reading
        watcher.tick()                              # arms the debounce
        watcher.tick()                              # ...fires the hold
        (p / 'points.json').write_text(json.dumps({'points': []}))
        watcher.tick()                              # ...then it clears

    assert playback.played == ['lidar_hold', 'lidar_clear'], playback.played


def test_an_offline_motors_on_announcement_is_silent_not_truncated():
    """'Motors on. auto mode.' cannot be pre-recorded, so offline it must say
    nothing at all — never half the sentence."""
    engine, playback = _offline_engine()
    speaker = PrioritySpeaker(engine)

    with tempfile.TemporaryDirectory() as d:
        p = Path(d)
        (p / 'control.json').write_text(json.dumps(_telemetry()))
        (p / 'points.json').write_text(json.dumps({'points': []}))
        watcher = EventWatcher(d, speaker)
        watcher.tick()                              # baseline
        (p / 'control.json').write_text(json.dumps(_telemetry(enabled=True)))
        watcher.tick()

    assert playback.played == []
    # And the engine reports the miss honestly rather than claiming it spoke.
    said = engine.speak('motors_enabled', 'Motors on. auto mode.')
    assert said.played is False and said.cache_key is None


def test_a_greeting_is_dropped_rather_than_delaying_an_obstacle():
    """The priority rule that matters: unprompted speech yields to safety."""
    engine, playback = _offline_engine()
    speaker = PrioritySpeaker(engine)

    with speaker.exclusive():                       # the speaker is busy
        dropped = speaker.speak_event('person_greeted', 'Hello, Rushabh.')
    assert isinstance(dropped, DroppedPhrase) and dropped.dropped
    assert playback.played == [], 'a greeting must never reach playback'

    # ...and a safety announcement is not dropped while it waits.
    got = speaker.speak_event('lidar_hold', 'Holding — obstacle ahead.')
    assert getattr(got, 'played', False) is True
    assert playback.played == ['lidar_hold']


def test_the_safety_lane_reports_degraded_when_only_cached_phrases_work():
    """The dashboard must not show a green TTS lane when synthesis is gone.

    Cached phrases still play, so the lane is working-but-fragile, not down.
    """
    from voice.status import VoiceStatusPublisher
    from voice.telemetry import VoiceTelemetry
    engine, _ = _offline_engine()
    with tempfile.TemporaryDirectory() as d:
        lane = VoiceStatusPublisher(VoiceTelemetry(), d, tts=engine) \
            .publish_once()['lanes']['tts']
    assert lane['ok'] is True
    assert lane['state'] == 'degraded', lane
    assert 'synthesis unavailable' in lane['reason']


def test_dro_ppable_events_are_a_subset_of_the_safety_events_we_test():
    """Guard against the two lists drifting apart."""
    assert DROPPABLE_EVENTS & SAFETY_EVENTS == frozenset()


if __name__ == '__main__':
    _env.run_tests(globals())