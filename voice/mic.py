"""Microphone capture from the Logitech C270 USB webcam.

The C270 is a UAC (USB Audio Class) device: it enumerates as a plain ALSA
capture card alongside the Jetson's onboard HDA/APE audio. On this machine
it shows up as ``card 2: WEBCAM [C270 HD WEBCAM], device 0: USB Audio``.

Card numbers are *not* stable — plugging in a USB headset can renumber
them. So nothing here hardcodes "card 2". ``find_alsa_card`` walks sysfs
from each ``/sys/class/sound/cardN`` up to its USB ancestor and matches
``idVendor:idProduct`` against ``C270_USB_ID``.

Capture is a plain ``arecord`` subprocess writing 16 kHz mono S16_LE —
the format whisper.cpp and ctranslate2 consume natively, so no
resampling happens in Python. Pure stdlib; no pip dependencies.

Fail-soft is the contract: a missing webcam, a busy device, or a missing
``arecord`` returns ``None`` and logs. Voice input is an enhancement, and
the rover must still run when it is unavailable.
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
import time
import wave
from dataclasses import dataclass, field
from io import BytesIO
from pathlib import Path
from typing import Optional

from ._constants import (
    C270_USB_ID,
    MIC_CHANNELS,
    MIC_FRAME_MS,
    MIC_MAX_SECONDS,
    MIC_SAMPLE_RATE,
    MIC_SAMPLE_WIDTH,
    MIC_SPEECH_FRAMES,
    MIC_SPEECH_RMS,
)

log = logging.getLogger(__name__)

_SYS_CLASS_SOUND = Path('/sys/class/sound')
_PROC_ASOUND_CARDS = Path('/proc/asound/cards')


# ── Errors ──────────────────────────────────────────────────────────


class MicUnavailable(Exception):
    """The C270 could not be found or opened. Callers degrade to log-only."""


# ── Device discovery ────────────────────────────────────────────────


@dataclass(frozen=True)
class MicDevice:
    """A resolved capture endpoint.

    ``card`` is the ALSA index and ``alsa_device`` the full ``hw:N,M``
    string. ``product`` is whatever sysfs reported, used only in logs.
    """
    card: int
    alsa_device: str
    product: str = ''
    usb_id: str = C270_USB_ID

    @property
    def plughw(self) -> str:
        """``plughw:`` variant — lets ALSA do format conversion instead of
        failing outright if the device refuses 16 kHz."""
        return f'plughw:{self.card},0'


def _usb_id_for_card(card_dir: Path) -> Optional[str]:
    """Walk from an ALSA card directory up to its USB device node.

    A card's ``device`` symlink points at the USB interface (e.g.
    ``.../1-2/1-2.4/1-2.4:1.2``); the vendor/product id files live a few
    levels up at the USB *device* node. Returns ``"046d:0825"`` or None.
    """
    device_link = card_dir / 'device'
    try:
        node = device_link.resolve(strict=True)
    except (OSError, RuntimeError):
        return None

    # Bounded walk: sysfs depth is small, but never loop on a bad symlink.
    for _ in range(8):
        if node == node.parent:
            break
        vid, pid = node / 'idVendor', node / 'idProduct'
        if vid.is_file() and pid.is_file():
            try:
                return f'{vid.read_text().strip().lower()}:{pid.read_text().strip().lower()}'
            except OSError:
                return None
        node = node.parent
    return None


def _card_name(card_dir: Path) -> str:
    """Best-effort human name from sysfs, falling back to /proc/asound/cards."""
    for candidate in ('product', 'id'):
        path = card_dir / candidate
        if path.is_file():
            try:
                name = path.read_text().strip()
                if name:
                    return name
            except OSError:
                pass
    try:
        for line in _PROC_ASOUND_CARDS.read_text().splitlines():
            # e.g. " 2 [WEBCAM         ]: USB-Audio - C270 HD WEBCAM"
            head, _, tail = line.partition(':')
            head = head.strip()
            if head.isdigit() and int(head) == _card_index_from_dir(card_dir):
                return tail.strip().lstrip('-').strip() or f'card{head}'
    except OSError:
        pass
    return card_dir.name


def _card_index_from_dir(card_dir: Path) -> int:
    try:
        return int(card_dir.name.replace('card', ''))
    except ValueError:
        return -1


def find_alsa_card(usb_id: str = C270_USB_ID) -> Optional[MicDevice]:
    """Locate the C270 by USB VID:PID. Returns None if not attached.

    Kept pure w.r.t. the filesystem (no subprocess) so tests can point it
    at a fixture tree via ``_SYS_CLASS_SOUND``.
    """
    if not _SYS_CLASS_SOUND.is_dir():
        log.debug('%s absent — no ALSA cards to scan', _SYS_CLASS_SOUND)
        return None

    for card_dir in sorted(_SYS_CLASS_SOUND.glob('card*')):
        if not card_dir.is_dir():
            continue
        found = _usb_id_for_card(card_dir)
        if found is None:
            continue
        if found != usb_id.lower():
            continue
        index = _card_index_from_dir(card_dir)
        if index < 0:
            continue
        name = _card_name(card_dir)
        log.info('found %s at ALSA card %d (%s)', usb_id, index, name)
        return MicDevice(card=index, alsa_device=f'hw:{index},0',
                         product=name, usb_id=found)
    return None


def list_capture_cards() -> list[str]:
    """Human-readable capture device list, for error messages and /voice.

    Only the ``card N:`` device lines. ``arecord -l`` also prints a banner and
    two lines of subdevice detail per device, so a machine with the Jetson's
    onboard APE codec returns ~20 entries — passing that whole list through
    produced a single ~5000-character log line that buried the actual problem.
    Callers that want structured cards already match ``_CARD_LINE``.
    """
    if shutil.which('arecord') is None:
        return []
    try:
        out = subprocess.run(['arecord', '-l'], capture_output=True,
                             text=True, timeout=5.0)
    except (OSError, subprocess.SubprocessError):
        return []
    cards: list[str] = []
    for line in out.stdout.splitlines():
        line = line.strip()
        # Keep only the device line, and only the first of each card+device
        # pair: `arecord -l` can repeat a pair when it has several subdevices.
        if _CARD_LINE.match(line):
            key = line.split(']:')[0]
            if not any(c.split(']:')[0] == key for c in cards):
                cards.append(line)
    return cards


def summarise_capture_cards(budget: int = 150) -> str:
    """One short line naming the capture devices, for a warning message.

    Capped by *characters*, not count: this machine's onboard ADMAIF entries
    are ~85 characters each, so a count-based cap still produced a paragraph.
    What the reader needs is "is the webcam there or not" — the full list is
    always available from ``/api/voice/mic``.
    """
    cards = list_capture_cards()
    if not cards:
        return 'none detected'
    shown: list[str] = []
    for card in cards:
        if shown and sum(len(c) for c in shown) + len(card) > budget:
            break
        shown.append(card)
    text = '; '.join(shown)
    if len(shown) < len(cards):
        text += ' (+%d more; full list on the dashboard)' % (len(cards) - len(shown))
    return text


# ── Signal level ────────────────────────────────────────────────────


def frame_rms(samples: bytes) -> float:
    """Root-mean-square of S16_LE ``samples``. 0.0 for empty input."""
    if not samples:
        return 0.0
    total = 0
    count = 0
    # Unpack in chunks to avoid a giant list for long captures.
    step = 2
    for i in range(0, len(samples) - (len(samples) % step), step):
        value = int.from_bytes(samples[i:i + step], 'little', signed=True)
        total += value * value
        count += 1
    if count == 0:
        return 0.0
    return (total / count) ** 0.5


@dataclass
class LevelMeter:
    """Rolling RMS over fixed frames — the crude VAD under the wake word.

    Not a neural VAD: it answers "is somebody talking right now", which is
    all the dispatcher needs to decide whether to hand a buffer to STT.
    A real Silero VAD drops in behind the same interface later.
    """
    frame_bytes: int
    threshold: float = MIC_SPEECH_RMS
    required_frames: int = MIC_SPEECH_FRAMES

    loud_streak: int = 0
    quiet_streak: int = 0
    peak_rms: float = 0.0
    _cursor: int = field(default=0, repr=False)

    @classmethod
    def for_defaults(cls, frame_ms: int = MIC_FRAME_MS) -> 'LevelMeter':
        samples_per_frame = MIC_SAMPLE_RATE * frame_ms // 1000
        return cls(frame_bytes=samples_per_frame * MIC_SAMPLE_WIDTH)

    def feed(self, wav_bytes: bytes) -> bool:
        """Consume a WAV buffer. Returns True if speech was detected."""
        pcm = read_wav_pcm(wav_bytes)
        self.peak_rms = max(self.peak_rms, frame_rms(pcm))

        self._cursor = 0
        detected = False
        while self._cursor + self.frame_bytes <= len(pcm):
            rms = frame_rms(pcm[self._cursor:self._cursor + self.frame_bytes])
            self._cursor += self.frame_bytes
            if rms >= self.threshold:
                self.loud_streak += 1
                self.quiet_streak = 0
                if self.loud_streak >= self.required_frames:
                    detected = True
            else:
                self.quiet_streak += 1
                self.loud_streak = 0
        return detected

    def reset(self) -> None:
        self.loud_streak = 0
        self.quiet_streak = 0
        self.peak_rms = 0.0
        self._cursor = 0


def read_wav_pcm(wav_bytes: bytes) -> bytes:
    """Strip the 44-byte RIFF header, returning raw S16_LE samples.

    Tolerant of extra chunks: seeks to the ``data`` chunk by walking the
    RIFF directory rather than assuming a 44-byte header.
    """
    if not wav_bytes or len(wav_bytes) < 12:
        return b''
    try:
        with wave.open(BytesIO(wav_bytes), 'rb') as w:
            return w.readframes(w.getnframes())
    except (wave.Error, EOFError):
        return b''


# ── Capture ─────────────────────────────────────────────────────────


def _duration_arg(seconds: float) -> str:
    """Format a duration for ``arecord -d``.

    ALSA parses ``-d`` as an integer number of seconds — ``'3.0'`` is a
    hard error ("invalid duration argument"). So round to the nearest
    second and clamp into [1, MIC_MAX_SECONDS]. Sub-second windows are
    not something this layer needs; the wake word takes long utterances.
    """
    return str(int(max(1, min(round(float(seconds)), MIC_MAX_SECONDS))))


@dataclass
class MicCapture:
    """Records from the C270 via ``arecord``.

    ``record()`` returns WAV bytes for a fixed duration, or None if the
    device is missing or ``arecord`` failed. ``listen()`` is the
    convenience wrapper: record, then gate on the level meter so a burst
    of room tone never reaches the STT engine.
    """
    device: Optional[MicDevice] = None
    sample_rate: int = MIC_SAMPLE_RATE
    channels: int = MIC_CHANNELS
    timeout_s: float = 10.0

    def __post_init__(self) -> None:
        if self.device is None:
            self.device = find_alsa_card()
        if self.device is None:
            log.warning('C270 (%s) not found on USB — voice input disabled. '
                        'Capture devices present: %s. Output still works.',
                        C270_USB_ID, summarise_capture_cards())

    @property
    def available(self) -> bool:
        return self.device is not None and shutil.which('arecord') is not None

    def _argv(self, seconds: float) -> list[str]:
        assert self.device is not None
        return [
            'arecord',
            '-D', self.device.plughw,
            '-f', 'S16_LE',
            '-r', str(self.sample_rate),
            '-c', str(self.channels),
            '-d', _duration_arg(seconds),
            '-t', 'wav',
            '-',
        ]

    def record(self, seconds: float = 3.0) -> Optional[bytes]:
        """Capture ``seconds`` of audio. None on any failure."""
        if self.device is None:
            return None
        if shutil.which('arecord') is None:
            log.warning('arecord not installed — cannot capture')
            return None

        seconds = max(0.1, min(float(seconds), MIC_MAX_SECONDS))
        try:
            proc = subprocess.run(
                self._argv(seconds),
                capture_output=True,
                timeout=self.timeout_s + 5.0,
                check=True,
            )
        except FileNotFoundError:
            log.warning('arecord disappeared mid-run')
            return None
        except subprocess.TimeoutExpired:
            log.warning('arecord timed out after %.1fs', seconds)
            return None
        except subprocess.CalledProcessError as exc:
            log.warning('arecord failed: %s',
                        (exc.stderr or b'').decode('utf-8', 'replace').strip())
            return None

        data = proc.stdout or b''
        if not data:
            log.warning('arecord produced no audio (mic muted?)')
            return None
        log.debug('captured %d bytes from card %d', len(data), self.device.card)
        return data

    def listen(self,
               seconds: float = 3.0,
               meter: Optional[LevelMeter] = None) -> Optional[bytes]:
        """Record and return WAV bytes only if someone actually spoke.

        ``None`` means either no audio or no speech — the caller cannot
        tell the difference, and must not care: both mean "say it again".
        """
        wav = self.record(seconds)
        if wav is None:
            return None
        meter = meter if meter is not None else LevelMeter.for_defaults()
        if not meter.feed(wav):
            log.info('captured %.1fs but no speech (peak rms %.0f < %.0f)',
                     seconds, meter.peak_rms, meter.threshold)
            return None
        log.info('speech detected (peak rms %.0f)', meter.peak_rms)
        return wav


# ── Diagnostics (dashboard Voice tab) ───────────────────────────────

_CARD_LINE = re.compile(r'^card (\d+): (\S+) \[(.*?)\], device (\d+):')
# Below this, a capture is flat digital silence: a muted or dead mic,
# not just a quiet room (room tone on the C270 sits in the tens).
SILENT_PEAK_RMS = 3.0


def mic_inventory() -> dict:
    """What is connected, without opening any capture device.

    Safe to call on every dashboard poll: it reads sysfs and runs
    ``arecord -l`` (which enumerates, it does not capture).
    """
    device = find_alsa_card()
    cards: dict[int, dict] = {}
    for line in list_capture_cards():
        m = _CARD_LINE.match(line)
        if m and int(m.group(1)) not in cards:
            idx = int(m.group(1))
            cards[idx] = {'card': idx, 'id': m.group(2), 'name': m.group(3),
                          'selected': device is not None and device.card == idx}
    return {
        'connected': device is not None,
        'arecord': shutil.which('arecord') is not None,
        'device': None if device is None else {
            'card': device.card, 'plughw': device.plughw,
            'product': device.product, 'usb_id': device.usb_id},
        'expected_usb_id': C270_USB_ID,
        'cards': [cards[k] for k in sorted(cards)],
    }


def probe_microphone(seconds: float = 1.5) -> dict:
    """Actually capture a moment of audio and say whether the mic works.

    ``state`` is one of: ``missing`` (not on USB), ``no_tool`` (arecord
    absent), ``busy`` (another process holds it — usually the voice
    process mid-listen), ``error`` (arecord failed), ``silent`` (captured
    but flat zero — muted or dead), ``working`` (real signal).

    "Working" means audio is arriving, not that anyone spoke.
    """
    inv = mic_inventory()
    base = {'t': time.time(), 'seconds': seconds, 'peak_rms': 0.0,
            'device': (inv['device'] or {}).get('plughw', '')}
    if not inv['connected']:
        return {**base, 'ok': False, 'state': 'missing',
                'message': f'No USB microphone {C270_USB_ID} found. Re-plug the C270.'}
    if not inv['arecord']:
        return {**base, 'ok': False, 'state': 'no_tool',
                'message': 'arecord is not installed (apt install alsa-utils).'}
    cap = MicCapture()
    try:
        proc = subprocess.run(cap._argv(seconds), capture_output=True,
                              timeout=seconds + 5.0)
    except subprocess.TimeoutExpired:
        return {**base, 'ok': False, 'state': 'error', 'message': 'capture timed out'}
    except OSError as exc:
        return {**base, 'ok': False, 'state': 'error', 'message': str(exc)[:160]}
    err = (proc.stderr or b'').decode('utf-8', 'replace').strip()
    if proc.returncode != 0:
        busy = 'busy' in err.lower()
        return {**base, 'ok': False, 'state': 'busy' if busy else 'error',
                'message': ('Microphone is in use by another process '
                            '(the voice process may be listening) — try again.'
                            if busy else (err[:160] or 'arecord failed'))}
    pcm = read_wav_pcm(proc.stdout or b'')
    if not pcm:
        return {**base, 'ok': False, 'state': 'error',
                'message': 'arecord produced no audio'}
    frame = max(2, int(MIC_SAMPLE_RATE * MIC_FRAME_MS / 1000) * 2)
    peak = max(frame_rms(pcm[i:i + frame]) for i in range(0, len(pcm) - frame + 1, frame)) \
        if len(pcm) >= frame else frame_rms(pcm)
    base.update(peak_rms=round(peak, 1), bytes=len(pcm))
    if peak < SILENT_PEAK_RMS:
        return {**base, 'ok': False, 'state': 'silent',
                'message': 'Captured, but the signal is flat zero — microphone '
                           'muted or dead.'}
    return {**base, 'ok': True, 'state': 'working',
            'message': f'Audio is arriving (peak level {peak:.0f}).'}


__all__ = [
    'MicDevice',
    'MicCapture',
    'MicUnavailable',
    'LevelMeter',
    'find_alsa_card',
    'list_capture_cards',
    'mic_inventory',
    'probe_microphone',
    'SILENT_PEAK_RMS',
    'frame_rms',
    'read_wav_pcm',
]