"""Phase 0 entry point: TTS-only, no mic, no control writes.

Reads the main project's control.json + points.json and plays (or logs)
Tarka's phrases in response to transitions. Run this *alongside* the
AGV / dashboard; you'll hear Tarka narrate enable transitions, mode
changes, password-gated overrides, and LiDAR holds.

This module does NOT touch the main project. It polls read-only JSON
files the project writes anyway. Stop with Ctrl-C.

Usage::

    python -m voice.run_phase0               # use AGV_DASHBOARD_DIR / default
    python -m voice.run_phase0 --dry-run     # log only, no audio
    python -m voice.run_phase0 --poll 0.5    # 2 Hz polling
"""

from __future__ import annotations

import argparse
import logging
import os
import sys

from config import TELEMETRY_DIR

from voice.cam_lookup import CamLookup
from voice.ask import Asker
from voice.command import CommandListener
from voice.event_watcher import EventWatcher
from voice.explain import EventExplainer
from voice.face_watcher import FaceWatcher
from voice.speaker import PrioritySpeaker
from voice.rover_state import read_rover_state
from voice.speech_plan import PlanSpeaker
from voice.output import OutputSelector
from voice.status import VoiceStatusPublisher
from voice.stt import SttEngine
from voice.telemetry import TelemetryBackend, TelemetrySpeaker, VoiceTelemetry
from voice.tts import SilentTts, SystemPlayback, TtsEngine


log = logging.getLogger(__name__)


def build_argparser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description='Voice phase 0 runner.')
    ap.add_argument(
        '--telemetry-dir',
        default=os.environ.get('AGV_DASHBOARD_DIR') or TELEMETRY_DIR,
        help='Directory containing control.json + points.json '
             '(default: $AGV_DASHBOARD_DIR or %s)' % TELEMETRY_DIR,
    )
    ap.add_argument(
        '--poll', type=float, default=0.2,
        help='Poll interval in seconds (default: 0.2)',
    )
    ap.add_argument(
        '--dry-run', action='store_true',
        help="Don't play audio; log utterances only.",
    )
    ap.add_argument(
        '--no-status', action='store_true',
        help="Don't publish voice_status.json for the dashboard Voice tab.",
    )
    ap.add_argument(
        '--no-faces', action='store_true',
        help="Don't read cam_faces.json: no greetings, no who-is-here answers.",
    )
    ap.add_argument(
        '-v', '--verbose', action='store_true',
        help='Enable debug-level logging.',
    )
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_argparser().parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format='%(asctime)s %(levelname)-7s %(name)s: %(message)s',
        datefmt='%H:%M:%S',
    )

    # Read once here: the dashboard writes the choice, and the engine asks
    # for it per play so a change takes effect without a restart.
    selector = OutputSelector(args.telemetry_dir)
    if args.dry_run:
        speaker = SilentTts()
        log.info('dry-run mode: utterances logged, no audio playback')
    else:
        chosen = selector.read()
        log.info('voice output device: %s', chosen)
        speaker = TtsEngine(playback=SystemPlayback(device=selector.env_for))

    publisher = None
    commands = None
    faces = None
    lookup = None if args.no_faces else CamLookup(args.telemetry_dir)
    if not args.no_status:
        telemetry = VoiceTelemetry()
        speaker = TelemetrySpeaker(speaker, telemetry)
    # One speaker for every thread: safety speech outranks greetings, and an
    # unprompted greeting is dropped rather than queued behind anything.
    speaker = PrioritySpeaker(speaker)
    if not args.no_faces:
        faces = FaceWatcher(args.telemetry_dir, speaker).start()
    if not args.no_status:
        # Constructed for lane health only — phase 0 never listens, so
        # nothing here opens the mic.
        stt = SttEngine()
        stt.backend = TelemetryBackend(stt.backend, telemetry,
                                       stt.confidence_floor)
        # Answers questions from the rover's own published state. Spliced
        # from cached clips, so it speaks with no network (the speech-to-text
        # upload is the only cloud step).
        asker = Asker(stt, PlanSpeaker(speaker, telemetry),
                      lambda: read_rover_state(args.telemetry_dir), telemetry)
        commands = CommandListener(args.telemetry_dir, stt=stt,
                                   speaker=speaker, lookup=lookup,
                                   asker=asker).start()
        publisher = VoiceStatusPublisher(
            telemetry, args.telemetry_dir, stt=stt, tts=speaker,
            explainer=EventExplainer(), commands=commands, faces=faces,
            outputs=selector).start()

    watcher = EventWatcher(
        telemetry_dir=args.telemetry_dir,
        speaker=speaker,
        poll_interval_s=args.poll,
    )
    log.info('voice phase 0 watcher starting (telemetry_dir=%s)',
             args.telemetry_dir)
    try:
        watcher.run_forever()
    finally:
        if publisher is not None:
            publisher.stop()
        if commands is not None:
            commands.stop()
        if faces is not None:
            faces.stop()
    return 0


if __name__ == '__main__':
    sys.exit(main())
