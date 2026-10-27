"""Which speaker Tarka talks out of, chosen from the dashboard.

Why this exists: ``aplay`` with no ``-D`` follows whatever PulseAudio calls
the default sink. On a machine that has a Bluetooth speaker paired, that
default is the Bluetooth device — and when it drops off the bus, ``aplay``
still exits 0 while the audio goes nowhere. The rover appears to speak and
nobody hears it. Observed on this bench: ``bluez_output.1E_D2_C5_C4_A1_6E.1``
(ZEB-COUNTY) vanished from the sink list for exactly as long as a clip
played.

So the destination is explicit, published for the dashboard to render, and
set by writing a small file the voice process polls — the same pattern as
``voice_command.json``. The dashboard never touches audio itself; it relays a
string this module then validates.

``aplay -D`` cannot take a PulseAudio sink name (verified: "Unknown PCM").
``PULSE_SINK=<name>`` can, and is what :func:`env_for` produces. Nothing is
ever passed through a shell: the value goes into an environment dict handed
to ``subprocess`` as a list.

Safety: a device string reaches a subprocess environment, so it is never
taken on trust. It must match a sink that is *currently enumerated*, and
must match :data:`DEVICE_RE` before it is written to disk at all.

Nothing here imports from the main project.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger(__name__)

OUTPUT_FILENAME = 'voice_output.json'
AUTO = 'auto'

# Belt and braces on top of the enumerated-allowlist check. PulseAudio sink
# names are alphanumerics plus '.', '_' and '-'; ALSA hardware names also use
# ':' and ',' ("plughw:1,3"), so a comma has to be allowed or every hardware
# device would be silently rejected.
DEVICE_RE = re.compile(r'^[A-Za-z0-9._:,=-]{1,120}$')

_ENUM_TIMEOUT_S = 4.0
DEFAULT_STATE = 'unknown'


def _run(args: list[str]) -> Optional[str]:
    try:
        done = subprocess.run(args, capture_output=True, text=True,
                              timeout=_ENUM_TIMEOUT_S, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        log.debug('%s failed: %s', args[0], exc)
        return None
    if done.returncode != 0:
        return None
    return done.stdout or ''


# ── enumeration ─────────────────────────────────────────────────────


def _parse_pactl(text: str) -> list[dict]:
    """Parse ``pactl list sinks``.

    Field order is *not* guaranteed — a real sink prints ``State:`` before
    ``Name:`` — so each ``Sink #N`` block is collected and flushed whole
    rather than emitted on first sight of one field.
    """
    out: list[dict] = []
    block: dict = {}

    def flush():
        if block.get('name'):
            out.append(_device(block['name'],
                               block.get('desc') or block['name'],
                               block.get('state') or DEFAULT_STATE))
        block.clear()

    for raw in text.splitlines():
        if not raw.startswith('\t'):
            if raw.strip().startswith('Sink #'):
                flush()
            continue
        line = raw.strip()
        if line.startswith('Name:'):
            block['name'] = line.split(':', 1)[1].strip()
        elif line.startswith('Description:'):
            block['desc'] = line.split(':', 1)[1].strip()
        elif line.startswith('State:'):
            block['state'] = line.split(':', 1)[1].strip()
    flush()
    return out


_APLAY_LINE = re.compile(r'^card\s+(\d+):\s*.*?,\s*device\s+(\d+):\s*(.*)$')


def _parse_aplay(text: str) -> list[dict]:
    """Fallback when pactl is absent: parse ``aplay -l``.

    Card names contain spaces ("NVIDIA Jetson Orin Nano HDA"), so the numbers
    are pulled out with a pattern rather than by splitting on whitespace.
    """
    out: list[dict] = []
    for raw in text.splitlines():
        line = raw.strip()
        m = _APLAY_LINE.match(line)
        if not m:
            continue
        card, dev, desc = m.group(1), m.group(2), m.group(3).strip()
        out.append(_device(f'plughw:{card},{dev}',
                           f'{desc or "device"} (hw {card},{dev})',
                           DEFAULT_STATE, kind='alsa'))
    return out


def _device(name: str, desc: str, state: str, kind: str = '') -> dict:
    """Classify a device so the dashboard can group the dropdown sensibly."""
    if not kind:
        if name.startswith('bluez_'):
            kind = 'bluetooth'
        elif 'analog' in name or name.startswith(('hw:', 'plughw:', 'sysdefault')):
            kind = 'analog'
        elif 'usb' in name:
            kind = 'usb'
        elif name.startswith('alsa_output.platform-'):
            kind = 'hdmi'
        else:
            kind = 'audio'
    return {'id': name, 'label': desc, 'kind': kind, 'state': state}


def list_outputs() -> dict:
    """Every output the machine has right now, plus which one is default.

    ``{'outputs': [...], 'default': id, 'source': 'pactl'|'aplay'|'none'}``.
    Never raises: audio enumeration is a dashboard nicety and must not be
    able to stop the rover speaking through whatever default it already has.
    """
    if shutil.which('pactl'):
        text = _run(['pactl', 'list', 'sinks'])
        devices = _parse_pactl(text) if text else []
        if devices:
            default = (_run(['pactl', 'get-default-sink']) or '').strip()
            return {'outputs': devices, 'default': default, 'source': 'pactl'}
    if shutil.which('aplay'):
        text = _run(['aplay', '-l'])
        if text:
            return {'outputs': _parse_aplay(text), 'default': '', 'source': 'aplay'}
    return {'outputs': [], 'default': '', 'source': 'none'}


# ── the selection ───────────────────────────────────────────────────


class OutputSelector:
    """Reads and writes ``voice_output.json`` in the telemetry directory."""

    def __init__(self, out_dir: str, clock=time.time) -> None:
        self._path = Path(out_dir) / OUTPUT_FILENAME
        self._clock = clock
        self._lock = threading.Lock()

    @property
    def path(self) -> Path:
        return self._path

    def read(self) -> str:
        """The saved device id, or ``auto``. A corrupt file reads as ``auto``."""
        try:
            with self._path.open('r', encoding='utf-8') as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return AUTO
        device = data.get('device') if isinstance(data, dict) else None
        return device if isinstance(device, str) and device else AUTO

    def write(self, device: str, allowed: Optional[list[str]] = None) -> tuple[bool, str]:
        """Save a device after validating it. ``(ok, message)``.

        ``allowed`` is the live sink list. Passing it makes this an
        allowlist: a name that is not currently a real output is refused, so a
        stale or hand-edited file cannot push an arbitrary string into a
        subprocess environment.
        """
        if not isinstance(device, str):
            return False, 'device must be a string'
        device = device.strip()
        if device in ('', AUTO):
            device = AUTO
        else:
            if not DEVICE_RE.match(device):
                return False, 'device name has characters no output device uses'
            if allowed is not None and device not in allowed:
                return False, 'device is not one of the current outputs'
        payload = {'device': device, 'updated_at': self._clock()}
        with self._lock:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix('.tmp')
            with tmp.open('w', encoding='utf-8') as fh:
                json.dump(payload, fh)
            os.replace(tmp, self._path)
        return True, device

    # ── the one place a device becomes a subprocess input ──────────

    def env_for(self, device: Optional[str] = None) -> dict:
        """Environment overrides for a playback call.

        ``aplay -D`` cannot name a PulseAudio sink; ``PULSE_SINK`` can. An
        unknown or ``auto`` device yields ``{}``, which leaves the machine's
        own default in place — the current behaviour, kept as the fallback.
        """
        device = self.read() if device is None else device
        if not device or device == AUTO or not DEVICE_RE.match(device):
            return {}
        return {'PULSE_SINK': device}

    def describe(self, outputs: Optional[dict] = None) -> dict:
        """What the dashboard renders: the list, the current pick, and whether
        that pick is actually present right now."""
        listing = outputs if outputs is not None else list_outputs()
        device = self.read()
        known = {o['id'] for o in listing['outputs']}
        if device == AUTO:
            state = 'auto'
        elif device in known:
            state = 'connected'
        else:
            state = 'missing'
        default = listing.get('default', '')
        # The default sink can name a device that is not currently present —
        # a paired Bluetooth speaker that dropped off the bus. Say so rather
        # than showing a healthy green lane over a silent speaker.
        phantom = bool(default) and default not in known
        return {
            'outputs': listing['outputs'],
            'default': default,
            'source': listing.get('source', 'none'),
            'selected': device,
            'state': state,
            'default_present': not phantom,
            'ok': bool(known) and not (phantom and state == 'auto'),
        }


__all__ = ['OutputSelector', 'list_outputs', 'env_for', 'probe_playback',
           'sink_status', 'default_clip', 'AUTO', 'OUTPUT_FILENAME', 'DEVICE_RE']


# ── is anything actually coming out? ────────────────────────────────

_ACTIVE_STATES = ('RUNNING', 'PREPARED')
_REPEAT_S = 3.0

# Preferred probe clips, best-sounding first. Anything pre-recorded will do;
# the point is that a *real* cached clip is used, so a green probe means the
# same audio Tarka actually plays works.
_PROBE_CLIPS = ('lidar_clear.wav', 'lidar_hold.wav', 'motors_enabled.wav')


def default_clip() -> Optional[Path]:
    """A cached phrase to test the speaker with, or ``None`` if none exist.

    Uses the real phrase cache on purpose: a synthesised tone would prove the
    mixer works without proving the rover's own audio is playable.
    """
    phrases = Path(__file__).resolve().parent / 'phrases'
    for name in _PROBE_CLIPS:
        candidate = phrases / name
        if candidate.is_file():
            return candidate
    try:
        return next(iter(sorted(phrases.glob('*.wav'))), None)
    except OSError:
        return None


def _long_enough(path: Path, tmp_dir: Optional[str] = None) -> Path:
    """A copy of ``path`` repeated until it is long enough to observe.

    One short clip is over before the first ``pactl`` subprocess returns, so
    the sink never visibly leaves ``SUSPENDED`` and a perfectly good speaker
    reads as broken. Repeating it gives the poll something to see.
    """
    try:
        import io
        import wave
        with wave.open(str(path), 'rb') as w:
            params = w.getparams()
            frames = w.readframes(w.getnframes())
        duration = w.getnframes() / float(params.framerate or 1)
        if duration >= _REPEAT_S:
            return path
        repeats = max(2, int(_REPEAT_S / max(duration, 0.05)) + 1)
        buf = io.BytesIO()
        with wave.open(buf, 'wb') as out:
            out.setparams(params)
            out.writeframes(frames * repeats)
        import tempfile
        fd, tmp = tempfile.mkstemp(suffix='.wav', dir=tmp_dir)
        with os.fdopen(fd, 'wb') as fh:
            fh.write(buf.getvalue())
        return Path(tmp)
    except Exception as exc:                      # keep the original clip
        log.debug('could not lengthen test clip: %s', exc)
        return path


def sink_status(device: str) -> dict:
    """State, mute and volume of one sink.

    ``aplay`` exiting 0 only means PulseAudio accepted the stream. A paired
    Bluetooth speaker that is powered off keeps its ``bluez_output`` sink
    listed, accepts audio, and produces nothing — so "played" is not proof
    and these facts are.

    State comes from parsing ``pactl list sinks``: this PulseAudio has no
    ``pactl get-sink-state``.
    """
    empty = {'state': 'unknown', 'muted': None, 'volume_pct': None,
             'name': device, 'present': False}
    if not device:
        return empty
    text = _run(['pactl', 'list', 'sinks'])
    state = next((o['state'] for o in _parse_pactl(text or '')
                  if o['id'] == device), '')
    if not state:
        return empty
    muted = (_run(['pactl', 'get-sink-mute', device]) or '').strip()
    vol = _run(['pactl', 'get-sink-volume', device]) or ''
    pct = None
    m = re.search(r'(\d+)%', vol)
    if m:
        pct = int(m.group(1))
    return {'state': state,
            'muted': muted.lower() == 'yes' if muted else None,
            'volume_pct': pct, 'name': device, 'present': True}


def probe_playback(device: str, clip: Optional[Path] = None,
                   timeout_s: float = 8.0) -> dict:
    """Play a clip on ``device`` and report whether audio could be heard.

    The verdict comes from watching the sink while the clip runs: a live sink
    leaves ``SUSPENDED`` for ``RUNNING``. A sink that never moves is not
    consuming audio, whatever the exit code said.

    ``auto`` is resolved to the machine's default sink *before* probing.
    Without that, the probe would look for a sink named "auto", never find
    one, and report every healthy default speaker as silent.
    """
    requested = device or AUTO
    target = device if device and device != AUTO else ''
    if not target:
        target = (_run(['pactl', 'get-default-sink']) or '').strip()
    tool = shutil.which('aplay') or shutil.which('paplay')
    result = {'device': target or requested, 'requested': requested,
              'tool': tool or '', 'played': False,
              'became_active': False, 'audible': False, 'reason': '',
              'sink': sink_status(target)}
    if tool is None:
        result['reason'] = 'no audio player is installed'
        return result
    clip = Path(clip) if clip else default_clip()
    if clip is None or not clip.is_file():
        result['reason'] = 'no test clip available'
        return result

    env = dict(os.environ)
    argv = [tool]
    if target and target != AUTO and DEVICE_RE.match(target):
        env['PULSE_SINK'] = target
        if os.path.basename(tool) == 'aplay':
            # Same trap as voice.tts: aplay's default PCM is raw ALSA, so
            # PULSE_SINK is ignored and no sink input is ever created — the
            # probe would then watch a sink that was never asked to play.
            argv += ['-D', 'pulse']
    long_clip = _long_enough(clip)
    cleanup = long_clip if long_clip != clip else None
    try:
        proc = subprocess.Popen(argv + ['-q', str(long_clip)], env=env,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError as exc:
        if cleanup:
            cleanup.unlink(missing_ok=True)
        result['reason'] = f'could not start {tool}: {exc}'
        return result

    stopped_by_us = False
    started = time.monotonic()
    deadline = started + timeout_s
    try:
        while proc.poll() is None and time.monotonic() < deadline:
            if target:
                text = _run(['pactl', 'list', 'sinks'])
                state = next((o['state'] for o in _parse_pactl(text or '')
                              if o['id'] == target), '')
                if state in _ACTIVE_STATES:
                    result['became_active'] = True
                    break
            time.sleep(0.05)
        # Always reap the child. Breaking out above leaves aplay running, and
        # an unreaped one holds the device, so the next probe fails with
        # "Device or resource busy" and a good speaker looks broken.
        try:
            proc.wait(timeout=max(0.5, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            # We stopped it on purpose: the clip is longer than the probe.
            # Killing it is not a playback failure, so do not read the kill
            # signal as one.
            stopped_by_us = True
            proc.kill()
            proc.wait(timeout=2.0)
    finally:
        if cleanup:
            cleanup.unlink(missing_ok=True)
    result['played'] = proc.returncode == 0 or stopped_by_us
    if target:
        result['sink'] = sink_status(target)
    sink = result['sink']

    if not result['played']:
        result['reason'] = f'{tool} failed on this clip'
    elif sink.get('muted'):
        result['reason'] = 'that speaker is muted'
    elif sink.get('volume_pct') == 0:
        result['reason'] = 'that speaker is at 0% volume'
    elif not target:
        result['reason'] = 'no default speaker is set on this machine'
    elif not result['became_active'] and target.startswith('bluez_'):
        result['reason'] = ('that Bluetooth speaker is paired but not connected — '
                            'power it on, or pick another speaker')
    elif not result['became_active']:
        result['reason'] = 'the speaker never started playing'
    else:
        result['audible'] = True
        result['reason'] = 'the speaker took the audio'
    return result