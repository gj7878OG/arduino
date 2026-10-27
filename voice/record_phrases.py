"""Record Tarka's fixed phrases into ``voice/phrases/`` (the phrase cache).

A cached clip plays locally with no network, so the announcements that
matter most — "Stopped.", "Holding — obstacle ahead." — keep working when
the internet does not. Everything else still falls back to live synthesis.

Usage (from the repo root)::

    python -m voice.record_phrases --list        # what would be recorded
    python -m voice.record_phrases               # record whatever is missing
    python -m voice.record_phrases --force       # re-record everything
    python -m voice.record_phrases --only emergency_stop --only lidar_clear

What it does for each phrase:
  1. synthesises it with Deepgram Aura (the same voice used for live speech,
     so cached and live lines sound like one character);
  2. trims leading/trailing silence so a short clip starts and ends promptly;
  3. transcribes it back and compares with the intended words — a clip that
     the speech recogniser cannot read back is not written;
  4. records the text it was made from in ``manifest.json``. Tests fail if
     the persona wording later changes without the clip being re-recorded,
     because a stale clip would say the *old* words.

Events whose text contains a live slot ({mode}, {pct}, {name}…) are never
recorded: a clip would play without the slot.
"""

from __future__ import annotations

import argparse
import io
import json
import logging
import struct
import sys
import time
import wave
from pathlib import Path
from typing import Callable, Optional

from ._constants import DYNAMIC_EVENTS, PERSONA_EVENTS
from .persona import render_event
from .tts import PHRASES_DIR
from .vocab import SYNTH_TEXT, VOCAB

log = logging.getLogger(__name__)

MANIFEST_NAME = 'manifest.json'
# lidar_hold is the one slot-bearing line that is always spoken with the same
# reason (event_watcher fires it only for 'obstacle ahead').
FIXED_SLOTS = {'lidar_hold': {'reason': 'obstacle ahead'}}
MIN_MATCH = 0.80
MIN_CLIP_S = 0.12
MAX_CLIP_S = 6.0
# Quiet consonants (the 't' of 'ten', the 'f' of 'four') sit far below the
# loud vowel; a tight gate or short pad eats them and the word is misheard.
LEAD_PAD_S, TAIL_PAD_S = 0.12, 0.18
GATE_FRACTION, GATE_FLOOR = 0.012, 12


def phrase_text(event: str) -> Optional[str]:
    """The exact words for ``event`` if it can be pre-recorded, else None.

    ``event`` is a persona event name or a vocabulary key (``num_4``,
    ``unit_meters`` …) — both are cache stems.
    """
    if event in VOCAB:
        return VOCAB[event]
    if event not in PERSONA_EVENTS or event in DYNAMIC_EVENTS:
        return None
    text = render_event(event, **FIXED_SLOTS.get(event, {}))
    if not text or '{' in text or '}' in text:
        return None
    return text


def plan(only: Optional[list[str]] = None) -> dict[str, str]:
    """event -> text for every recordable phrase (optionally restricted)."""
    out = {e: phrase_text(e) for e in sorted(PERSONA_EVENTS) + sorted(VOCAB)}
    out = {e: t for e, t in out.items() if t}
    if only:
        out = {e: t for e, t in out.items() if e in only}
    return out


def trim_silence(wav_bytes: bytes) -> bytes:
    """Cut leading/trailing near-silence from a mono 16-bit WAV.

    Returns the input untouched if it is not a mono 16-bit WAV, or is silent.
    """
    try:
        with wave.open(io.BytesIO(wav_bytes), 'rb') as w:
            ch, width, rate = w.getnchannels(), w.getsampwidth(), w.getframerate()
            pcm = w.readframes(w.getnframes())
    except (wave.Error, EOFError):
        return wav_bytes
    if ch != 1 or width != 2 or not pcm:
        return wav_bytes
    samples = struct.unpack(f'<{len(pcm) // 2}h', pcm[:len(pcm) // 2 * 2])
    win = max(1, rate // 100)                                # 10 ms
    levels = [max(abs(s) for s in samples[i:i + win]) for i in range(0, len(samples), win)]
    peak = max(levels)
    if peak == 0:
        return wav_bytes
    gate = max(GATE_FLOOR, peak * GATE_FRACTION)
    loud = [i for i, v in enumerate(levels) if v >= gate]
    start = max(0, loud[0] * win - int(LEAD_PAD_S * rate))
    end = min(len(samples), (loud[-1] + 1) * win + int(TAIL_PAD_S * rate))
    out = io.BytesIO()
    with wave.open(out, 'wb') as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(rate)
        w.writeframes(struct.pack(f'<{end - start}h', *samples[start:end]))
    return out.getvalue()


# Single words are read back as their homophones ("two" -> "to"). That is the
# recogniser being sensible, not the clip being wrong.
_HOMOPHONES = {'2': {'to', 'too'}, '4': {'for', 'fore'}, '8': {'ate'}, '1': {'won'},
               '0': {'oh', 'o'}, '100': {'hundred'}, 'meter': {'metre', 'meters', 'metres'},
               'meters': {'meter', 'metres', 'metre'}, 'centimeters': {'centimetres', 'centimeter'},
               'point': {'pint', 'points'}}


def readback_match(expected: str, heard: str) -> float:
    """Similarity of what the recogniser heard to the intended words, 0..1."""
    from .voice_check import normalize, similarity
    score = similarity(expected, heard)
    if score >= MIN_MATCH:
        return score
    ne, nh = _as_number(expected), _as_number(heard)
    if ne is not None and ne == nh:                          # "thirteen" == "13."
        return 1.0
    e, h = normalize(expected), normalize(heard)
    return 1.0 if h in _HOMOPHONES.get(e, ()) else score


def _as_number(text: str) -> Optional[float]:
    """The number a short utterance says, however spelled, else None."""
    from .intents import _parse_number, tokenize
    tokens = tokenize(text)
    got = _parse_number(tokens, 0) if tokens else None
    return got[0] if got and got[1] == len(tokens) else None


def wav_seconds(wav_bytes: bytes) -> float:
    with wave.open(io.BytesIO(wav_bytes), 'rb') as w:
        return w.getnframes() / float(w.getframerate())


def load_manifest(directory: Path) -> dict:
    try:
        data = json.loads((directory / MANIFEST_NAME).read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def record_all(directory: Path,
               synth: Callable[[str], Optional[bytes]],
               transcribe: Optional[Callable[[bytes], str]],
               events: dict[str, str],
               force: bool = False,
               voice: str = '',
               match: Optional[Callable[[str, str], float]] = None,
               sleep: Callable[[float], None] = time.sleep,
               synth_text: Optional[dict] = None) -> dict[str, dict]:
    """Record ``events`` into ``directory``. Returns per-event outcome rows.

    Outcome ``status``: recorded | skipped (already there) | failed.
    ``transcribe`` may be None to skip the read-back check.
    """
    directory.mkdir(parents=True, exist_ok=True)
    manifest = load_manifest(directory)
    results: dict[str, dict] = {}
    for event, text in events.items():
        path = directory / f'{event}.wav'
        have = manifest.get(event, {})
        if path.is_file() and not force and have.get('text') == text:
            results[event] = {'status': 'skipped', 'text': text}
            continue
        row: dict = {'text': text}
        try:
            raw = synth((synth_text or {}).get(event, text))
            if not raw:
                raise RuntimeError('synthesis returned nothing')
            clip = trim_silence(raw)
            secs = wav_seconds(clip)
            if not (MIN_CLIP_S <= secs <= MAX_CLIP_S):
                raise RuntimeError(f'clip is {secs:.2f}s (allowed {MIN_CLIP_S:g}–{MAX_CLIP_S:g}s)')
            row.update(seconds=round(secs, 2), bytes=len(clip))
            if transcribe is not None:
                heard = transcribe(clip)
                score = (match or (lambda a, b: 1.0))(text, heard)
                row.update(heard=heard, match=round(score, 2))
                if score < MIN_MATCH:
                    raise RuntimeError(f'read-back mismatch: heard {heard!r} (match {score:.2f})')
            tmp = path.with_suffix('.wav.tmp')
            tmp.write_bytes(clip)
            tmp.replace(path)                                # never leave a half clip
            manifest[event] = {'text': text, 'voice': voice, 'seconds': row['seconds'],
                               'bytes': row['bytes'], 'heard': row.get('heard', ''),
                               'recorded': time.strftime('%Y-%m-%d')}
            row['status'] = 'recorded'
        except Exception as exc:
            row.update(status='failed', error=str(exc)[:160])
        results[event] = row
        sleep(0.3)                                           # be polite to the API
    (directory / MANIFEST_NAME).write_text(json.dumps(manifest, indent=2, sort_keys=True) + '\n')
    return results


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Record Tarka's fixed phrases.")
    ap.add_argument('--list', action='store_true', help='show what would be recorded and exit')
    ap.add_argument('--only', action='append', help='record just this event (repeatable)')
    ap.add_argument('--force', action='store_true', help='re-record even if a clip exists')
    ap.add_argument('--no-verify', action='store_true', help='skip the speech-to-text read-back')
    args = ap.parse_args(argv)

    events = plan(args.only)
    if args.only and set(args.only) - set(events):
        print('Not recordable (unknown, or has a live slot): '
              + ', '.join(sorted(set(args.only) - set(events))), file=sys.stderr)
        return 2
    manifest = load_manifest(PHRASES_DIR)
    if args.list:
        for e, t in events.items():
            have = (PHRASES_DIR / f'{e}.wav').is_file()
            stale = have and manifest.get(e, {}).get('text') != t
            print(f'  {"STALE" if stale else "have " if have else "MISS "} {e:24} {t}')
        return 0

    from .stt import DeepgramSttBackend, SttError
    from .tts import DeepgramTtsBackend
    from .mic import read_wav_pcm

    tts = DeepgramTtsBackend()
    if not tts.available:
        print(f'Deepgram unavailable: {tts.unavailable_reason}', file=sys.stderr)
        return 2
    stt = DeepgramSttBackend()

    def transcribe(wav: bytes) -> str:
        try:
            return stt.transcribe(read_wav_pcm(wav)).text
        except SttError as exc:
            raise RuntimeError(f'read-back failed: {exc}')

    results = record_all(PHRASES_DIR, tts.synthesize,
                         None if args.no_verify else transcribe, events, args.force,
                         voice=tts.voice, match=readback_match, synth_text=SYNTH_TEXT)
    for e, r in results.items():
        extra = f'{r.get("seconds", "")}s' if r['status'] == 'recorded' else r.get('error', '')
        print(f'  {r["status"]:9} {e:24} {extra}')
    failed = [e for e, r in results.items() if r['status'] == 'failed']
    print(f'\n{sum(r["status"] == "recorded" for r in results.values())} recorded, '
          f'{sum(r["status"] == "skipped" for r in results.values())} already there, '
          f'{len(failed)} failed')
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())
