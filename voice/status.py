"""voice_status.json publisher — feeds the dashboard's Voice tab.

Sibling of ``lidar/publisher.py`` and deliberately the same shape: a
background thread atomically rewrites ``<out_dir>/voice_status.json`` and
the web process just re-serves it. The dashboard never imports ``voice``
— the C270's ALSA device is exclusive, so two processes opening the mic
would race for it.

Secrets: only *whether* an API key is present is published, never its
value. ``test_voice_status`` enforces this.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import threading
import time
from typing import Any, Optional

from ._constants import MIC_SPEECH_RMS
from .record_phrases import plan as record_plan
from .telemetry import VoiceTelemetry

log = logging.getLogger(__name__)

SCHEMA = 1
STATUS_FILENAME = 'voice_status.json'
STALE_AFTER_S = 10.0
KEY_ENVS = ('DEEPGRAM_API_KEY', 'GEMINI_API_KEY', 'GOOGLE_API_KEY')
_WARN_EVERY_S = 10.0
_BACKEND_NAMES = {'DeepgramSttBackend': 'deepgram',
                  'GoogleSttBackend': 'google',
                  'OfflineSttBackend': 'offline'}


def _lane(ok: bool, reason: str = '', **extra: Any) -> dict:
    """``state`` is what the tab colours by: down / degraded / ok.

    ``ok`` with a non-empty reason is *degraded* — working but fragile
    (e.g. TTS with 0/20 phrases cached, where all speech depends on the
    network). Green would be a lie there.
    """
    state = 'down' if not ok else ('degraded' if reason else 'ok')
    return {'ok': bool(ok), 'state': state, 'reason': reason, **extra}


class VoiceStatusPublisher:
    """Builds and publishes the Voice tab payload.

    Every component is optional and probed defensively: a half-built
    stack must still publish an honest "this lane is down" rather than
    crash the publisher.
    """

    def __init__(self, telemetry: VoiceTelemetry, out_dir: str,
                 stt: Any = None, tts: Any = None, explainer: Any = None,
                 parser: Any = None, commands: Any = None, faces: Any = None,
                 outputs: Any = None,
                 interval_s: float = 0.5,
                 environ: Optional[dict] = None) -> None:
        self._telemetry = telemetry
        self._out_dir = out_dir
        self._stt = stt
        self._tts = tts
        self._explainer = explainer
        self._parser = parser
        self._commands = commands
        self._faces = faces
        self._outputs = outputs
        self._interval_s = interval_s
        self._environ = os.environ if environ is None else environ
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._last_warn = 0.0

    # ── lifecycle ────────────────────────────────────────────────────
    def start(self) -> 'VoiceStatusPublisher':
        os.makedirs(self._out_dir, exist_ok=True)
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._loop, name='voice-status-writer', daemon=True)
        self._thread.start()
        log.info('voice_status.json publisher started -> %s', self._out_path)
        return self

    def stop(self) -> None:
        if self._stop.is_set():
            return
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None and thread.is_alive():
            thread.join(timeout=2.0)

    @property
    def _out_path(self) -> str:
        return os.path.join(self._out_dir, STATUS_FILENAME)

    # ── payload ──────────────────────────────────────────────────────
    def _mic_lane(self) -> dict:
        mic = getattr(self._stt, 'mic', None)
        if mic is None:
            return _lane(False, 'no microphone configured')
        device = getattr(mic, 'device', None)
        if device is None:
            return _lane(False, 'C270 not found on USB (046d:0825)')
        arecord = shutil.which('arecord') is not None
        return _lane(arecord, '' if arecord else 'arecord not installed',
                     usb_id=getattr(device, 'usb_id', ''),
                     card=getattr(device, 'card', None),
                     device=getattr(device, 'plughw', ''),
                     product=getattr(device, 'product', ''))

    def _stt_lane(self) -> dict:
        if self._stt is None:
            return _lane(False, 'speech-to-text not configured')
        backend = getattr(self._stt, 'backend', None)
        # Unwrap telemetry proxies so the lane names the real backend.
        inner = getattr(backend, '_backend', backend)
        ok = bool(getattr(backend, 'available', False))
        reason = '' if ok else str(getattr(backend, 'unavailable_reason', '') or 'unavailable')
        return _lane(ok, reason,
                     backend=_BACKEND_NAMES.get(type(inner).__name__,
                                                type(inner).__name__),
                     class_name=type(inner).__name__,
                     confidence_floor=getattr(self._stt, 'confidence_floor', None))

    def _tts_lane(self) -> dict:
        if self._tts is None:
            return _lane(False, 'text-to-speech not configured')
        engine = getattr(self._tts, '_speaker', self._tts)
        cache = getattr(engine, 'cache', None)
        cached = len(cache) if cache is not None else 0
        # Events that carry a name cannot be pre-recorded, so they are not 'missing'.
        # Only fixed-text phrases can be pre-recorded: an event with a live
        # slot ({mode}, {pct}, {name}) would play without it. Count exactly
        # what voice.record_phrases can produce, so a full cache reads as full.
        expected = len(record_plan())
        synth = bool(getattr(engine, 'synth_available', False))
        playback = getattr(engine, '_playback', None)
        tool = getattr(playback, '_tool', None) or (
            next((t for t in ('aplay', 'paplay', 'ffplay', 'mpv')
                  if shutil.which(t)), None))
        # Degraded, not down, when only one of cache/synth works.
        ok = (cached > 0 or synth) and tool is not None
        reasons = []
        if cached < expected:
            reasons.append(f'{cached}/{expected} phrases cached')
        if not synth:
            reasons.append('synthesis unavailable')
        if tool is None:
            reasons.append('no audio player')
        return _lane(ok, '; '.join(reasons), cached=cached, expected=expected,
                     synth=synth, tool=tool or '')

    def _narrator_lane(self) -> dict:
        if self._explainer is None:
            return _lane(False, 'narrator not configured')
        ok = bool(getattr(self._explainer, 'available', False))
        backend = getattr(self._explainer, 'backend', None)
        return _lane(ok, '' if ok else 'no Gemini key — persona templates only',
                     model=getattr(backend, 'model', ''))

    def _parser_lane(self) -> dict:
        if self._parser is None:
            return _lane(False, 'parse lane not configured')
        backend = getattr(self._parser, 'backend', None)
        inner = getattr(backend, '_backend', backend)
        return _lane(True, 'only command lane — rules engine not built',
                     model=getattr(inner, 'model', ''))

    def _faces_lane(self) -> dict:
        """Is the link to the dashboard's camera alive, and has it greeted anyone?

        Only present when a face watcher is configured, so a voice process run
        with ``--no-faces`` simply has no such lane instead of a red one.
        """
        st = self._faces.state()
        state = st.get('state')
        reason = {'no_data': 'no fresh cam_faces.json — is the dashboard running?',
                  'camera_offline': 'camera offline',
                  'idle': 'camera not analysing (greeter off, nobody watching)'}.get(state, '')
        # 'idle' is normal while the greeter is off, so only the first two are faults.
        ok = state in ('running', 'idle')
        return _lane(ok, reason if state != 'idle' else '', camera=state,
                     greeter=bool(st.get('enabled')), greeted=st.get('greeted', 0),
                     last_greeted_at=st.get('last_greeted_at'))

    def _meter(self) -> dict:
        """Live read of the engine's level meter (read-only).

        Peak RMS of the *last* capture against the loudness threshold.
        This is loudness, not speech — the tab says so.
        """
        meter = getattr(self._stt, 'meter', None)
        return {'peak_rms': round(float(getattr(meter, 'peak_rms', 0.0) or 0.0), 1),
                'threshold': float(getattr(meter, 'threshold', MIC_SPEECH_RMS)
                                   or MIC_SPEECH_RMS),
                'loud_frames': int(getattr(meter, 'loud_streak', 0) or 0),
                'required_frames': int(getattr(meter, 'required_frames', 0) or 0)}

    def _output_block(self) -> Optional[dict]:
        """Where speech is going, and whether that destination still exists.

        ``None`` when no selector was configured, so a voice process run
        without one simply omits the block instead of publishing a red one.
        """
        try:
            return self._outputs.describe()
        except Exception as exc:                       # never break the tick
            log.warning('output enumeration failed: %s', exc)
            return {'outputs': [], 'default': '', 'source': 'error',
                    'selected': 'auto', 'state': 'unknown',
                    'default_present': False, 'ok': False}

    def build_payload(self) -> dict:
        snap = self._telemetry.snapshot()
        snap['meter'] = self._meter()
        lanes = {
            'mic': self._mic_lane(),
            'stt': self._stt_lane(),
            'tts': self._tts_lane(),
            'narrator': self._narrator_lane(),
            'parser': self._parser_lane(),
        }
        if self._faces is not None:
            lanes['faces'] = self._faces_lane()
        payload = {
            'schema': SCHEMA,
            'updated_at': time.time(),
            'pid': os.getpid(),
            'stale_after_s': STALE_AFTER_S,
            'lanes': lanes,
            # Booleans only — a value here would be a credential leak.
            'keys': {k: bool(str(self._environ.get(k, '')).strip())
                     for k in KEY_ENVS},
            'command': (self._commands.state() if self._commands is not None
                        else {'state': 'unavailable'}),
            **snap,
        }
        if self._outputs is not None:
            payload['output'] = self._output_block()
        return payload

    def publish_once(self) -> dict:
        out_path = self._out_path
        tmp_path = out_path + '.tmp'
        payload = self.build_payload()
        with open(tmp_path, 'w') as fh:
            json.dump(payload, fh)
        os.replace(tmp_path, out_path)
        return payload

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.publish_once()
            except Exception as exc:
                now = time.monotonic()
                if now - self._last_warn >= _WARN_EVERY_S:
                    self._last_warn = now
                    log.warning('voice_status.json writer tick failed: %s', exc)
            self._stop.wait(self._interval_s)

    def __repr__(self) -> str:
        state = 'running' if (self._thread and self._thread.is_alive()) else 'stopped'
        return f'<VoiceStatusPublisher {state} -> {self._out_path}>'


__all__ = ['VoiceStatusPublisher', 'STATUS_FILENAME', 'STALE_AFTER_S', 'SCHEMA']
