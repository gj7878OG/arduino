"""Phrase cache + playback + optional Piper synthesis.

Phase 0 layout:

  ``PhraseCache``     loads ``.wav`` / ``.opus`` / ``.mp3`` / ``.ogg``
                      / ``.flac`` from ``voice/phrases/`` and indexes
                      them by filename stem. Read at startup so a
                      missing or unplayable file shows up at boot, not
                      mid-operation.

  ``SystemPlayback``  plays a file with whichever ALSA / Pulse / ffmpeg
                      tool is on the system. Detection order:
                      ``aplay``, ``paplay``, ``ffplay``, ``mpv``.
                      Pure subprocess — no pip dependencies.

  ``TtsEngine``       cache-first output. Falls back to Piper synthesis
                      when the phrase isn't cached. If Piper isn't
                      installed, returns ``played=False`` and logs.

  ``SilentTts``       drop-in replacement used by tests. Records the
                      ``SpokenPhrase`` objects that ``TtsEngine`` would
                      have produced — assertions check the list.

Nothing in this module imports from the main project.
"""

from __future__ import annotations

import logging
import os
import shutil
import struct
import tempfile
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional, Protocol

from ._constants import DYNAMIC_EVENTS

log = logging.getLogger(__name__)

PHRASES_DIR = Path(__file__).parent / 'phrases'
AUDIO_EXTS = {'.wav', '.opus', '.mp3', '.ogg', '.flac'}


# Tools tried for playback, in order. The first hit is used for the
# lifetime of the process — audible glitch when swapping.
_PLAYBACK_TOOLS = ('aplay', 'paplay', 'ffplay', 'mpv')


@dataclass
class SpokenPhrase:
    """One utterance the engine chose to render.

    ``played`` is False when the cache missed *and* Piper wasn't
    available — i.e. nobody heard anything. Tests assert on ``event``
    and ``utterance``; production code logs on ``played=False`` to
    surface broken phrase coverage.
    """
    event: str
    utterance: str
    cache_key: Optional[str]     # None if no cached file matched
    played: bool


class _Playback(Protocol):
    def play(self, path: Path) -> bool: ...


class SystemPlayback:
    """Pick the first available audio tool and use it for every play call.

    If no tool is found, ``play()`` logs a warning and returns False —
    the voice module degrades to log-only narration, which is the same
    behaviour SilentTts has for tests. This way, missing audio hardware
    never breaks the event watcher.
    """

    def __init__(self, timeout_s: float = 5.0, device: Optional[Callable[[], dict]] = None) -> None:
        self.timeout_s = timeout_s
        # Callable returning the environment for a play call, e.g. the
        # PULSE_SINK override from voice.output. None = machine default.
        self._device = device
        self._tool: Optional[str] = None
        self._lock = threading.Lock()

    def play(self, path: Path) -> bool:
        with self._lock:
            tool = self._tool or self._pick_tool()
        if tool is None:
            return False
        env = None
        if self._device is not None:
            try:
                env = self._device() or None
            except Exception as exc:            # never break playback
                log.warning('audio device lookup failed: %s', exc)
        argv = [tool]
        if env and env.get('PULSE_SINK') and os.path.basename(tool) == 'aplay':
            # aplay's default PCM is raw ALSA, so PULSE_SINK is ignored and
            # the stream never reaches the sound server at all. Verified on
            # this machine: `aplay -q clip.wav` creates no sink input, while
            # `aplay -D pulse` with PULSE_SINK set lands on the named sink.
            # Without this, the speaker chosen on the dashboard would change
            # nothing about where the audio goes.
            argv += ['-D', 'pulse']
        argv.append(str(path))
        try:
            subprocess.run(
                argv,
                check=True,
                capture_output=True,
                timeout=self.timeout_s,
                env=env,
            )
            return True
        except FileNotFoundError:
            log.warning('audio tool %s disappeared mid-run', tool)
            self._tool = None
            return False
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            log.warning('%s failed for %s: %s', tool, path, exc)
            return False

    def _pick_tool(self) -> Optional[str]:
        for tool in _PLAYBACK_TOOLS:
            if shutil.which(tool) is not None:
                log.info('voice playback using %s', tool)
                self._tool = tool
                return tool
        return False or None       # last-resort, never raise


class PhraseCache:
    """Files in PHRASES_DIR keyed by stem.

    Stem → file path. Hidden files and non-audio files are ignored.
    Re-instantiating rebuilds; this is intentional — the cache is
    loaded at startup, and a missing phrase on disk is a deployment
    bug, not a runtime surprise.
    """

    def __init__(self, directory: Path = PHRASES_DIR) -> None:
        self.directory = Path(directory)
        self._cache: dict[str, Path] = {}
        self._load()

    def _load(self) -> None:
        if not self.directory.is_dir():
            log.warning('phrase directory %s does not exist', self.directory)
            return
        for path in self.directory.iterdir():
            if not path.is_file():
                continue
            if path.suffix.lower() in AUDIO_EXTS and not path.name.startswith('.'):
                self._cache[path.stem] = path

    def has(self, key: str) -> bool:
        return key in self._cache

    def path_for(self, key: str) -> Optional[Path]:
        return self._cache.get(key)

    def keys(self) -> list[str]:
        return sorted(self._cache)

    def __contains__(self, key: str) -> bool:
        return key in self._cache

    def __len__(self) -> int:
        return len(self._cache)


class TtsEngine:
    """Cache-first audio output. Piper is an optional fallback.

    ``speak_event(event, utterance)`` is the only method most callers
    need; it derives the cache key from the event name, plays the
    cached clip if present, and only falls back to Piper when both
    the cache misses *and* Piper is installed.
    """

    def __init__(self,
                 cache: Optional[PhraseCache] = None,
                 playback: Optional[_Playback] = None,
                 synthesizer: Optional['DeepgramTtsBackend'] = None) -> None:
        self.cache = cache if cache is not None else PhraseCache()
        self._playback = playback if playback is not None else SystemPlayback()
        self._synthesizer = (synthesizer if synthesizer is not None
                             else DeepgramTtsBackend())
        self._piper: bool = self._probe_piper()

    @property
    def piper_available(self) -> bool:
        return self._piper

    @property
    def synth_available(self) -> bool:
        """True when a dynamic utterance can be spoken (Deepgram key set)."""
        return self._synthesizer is not None and self._synthesizer.available

    def _probe_piper(self) -> bool:
        try:
            import piper  # noqa: F401
            log.info('piper available; live synthesis enabled')
            return True
        except ImportError:
            log.info('piper not installed; deepgram synthesis %s',
                     'enabled' if self.synth_available else 'disabled '
                     '(cached phrases still play)')
            return False

    def speak(self,
              cache_key: str,
              fallback_text: Optional[str] = None,
              event: str = '') -> SpokenPhrase:
        """Play or synthesise an utterance. Always returns a SpokenPhrase.

        Decision order:
          1. cache_key is in cache -> play it.
          2. Piper available + fallback_text given -> synthesise+play.
          3. otherwise -> log and return played=False.
        """
        # A clip named after a dynamic event would play without the person's name.
        path = None if event in DYNAMIC_EVENTS else self.cache.path_for(cache_key)
        if path is not None:
            played = self._playback.play(path)
            if not played:
                log.warning('cached phrase %r failed to play', cache_key)
            return SpokenPhrase(event=event,
                                utterance=fallback_text or '',
                                cache_key=cache_key,
                                played=played)
        if self.synth_available and fallback_text:
            played = self._speak_synthesized(fallback_text)
            if played:
                return SpokenPhrase(event=event, utterance=fallback_text,
                                    cache_key=None, played=True)
            log.warning('synthesis failed for %r; nothing spoken', event)

        log.debug('no audio for key=%r text=%r', cache_key, fallback_text)
        return SpokenPhrase(event=event,
                            utterance=fallback_text or '',
                            cache_key=None,
                            played=False)

    def _speak_synthesized(self, text: str) -> bool:
        """Synthesise, write to a temp WAV, play, clean up.

        The temp file is removed in a finally block so a crash mid-say
        never litters /tmp on a device that runs for weeks.
        """
        audio = self._synthesizer.synthesize(text)
        if not audio:
            return False
        return self.play_audio(audio)

    def play_audio(self, audio: bytes) -> bool:
        """Play WAV bytes through the engine's playback, then delete the file.

        Used for audio assembled at speaking time (spliced number clips) as
        well as synthesised speech. False means nothing played.
        """
        if not audio:
            return False
        tmp = None
        try:
            with tempfile.NamedTemporaryFile(suffix='.wav', delete=False) as fh:
                fh.write(audio)
                tmp = fh.name
            return self._playback.play(Path(tmp))
        finally:
            if tmp:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
        log.debug('no audio for key=%r text=%r', cache_key, fallback_text)
        return SpokenPhrase(event=event,
                            utterance=fallback_text or '',
                            cache_key=None,
                            played=False)

    def speak_event(self, event: str, utterance: str) -> SpokenPhrase:
        """Convenience: cache key = event name; utterance as fallback."""
        return self.speak(event, utterance, event=event)


class SilentTts:
    """Stand-in for TtsEngine used in tests.

    Records every ``speak_event`` invocation as a ``SpokenPhrase``;
    never produces audio. Tests assert on ``self.spoken`` to verify
    what the persona emitted, without needing aplay / Piper.
    """

    def __init__(self) -> None:
        self.spoken: list[SpokenPhrase] = []

    def speak(self,
              cache_key: str,
              fallback_text: Optional[str] = None,
              event: str = '') -> SpokenPhrase:
        s = SpokenPhrase(event=event,
                         utterance=fallback_text or '',
                         cache_key=cache_key,
                         played=True)
        self.spoken.append(s)
        return s

    def speak_event(self, event: str, utterance: str) -> SpokenPhrase:
        return self.speak(event, utterance, event=event)

    def clear(self) -> None:
        self.spoken.clear()


def _repair_wav_header(data: bytes) -> bytes:
    """Rewrite RIFF/data chunk sizes to match the bytes actually received.

    Deepgram's ``/v1/speak`` is a streaming endpoint: it streams the
    ``fmt `` chunk then begins ``data`` before the length is known, so it
    ships a placeholder size (observed: 2147418112 for a 69 KB file).
    Python's ``wave`` module trusts that field and reports a nonsense
    frame count, which makes ``readframes()`` read to EOF by accident
    rather than by design — it happens to work until it doesn't.

    Fixing the two size fields in place makes the file a valid WAV
    regardless of what the sender declared. If the bytes are not a
    RIFF/WAVE with a 16-byte fmt chunk, they're returned untouched.
    """
    if len(data) < 44 or data[:4] != b'RIFF' or data[8:12] != b'WAVE':
        return data
    if data[12:16] != b'fmt ':
        return data

    fmt_size = struct.unpack('<I', data[16:20])[0]
    data_off = 12 + 8 + fmt_size
    if data[20 + fmt_size:24 + fmt_size] != b'data':
        return data
    if len(data) < data_off + 8:
        return data

    payload = len(data) - (data_off + 8)
    out = bytearray(data)
    struct.pack_into('<I', out, 4, len(data) - 8)        # RIFF size
    struct.pack_into('<I', out, data_off + 4, payload)    # data size
    return bytes(out)


class DeepgramTtsBackend:
    """Deepgram Aura text-to-speech. Used for dynamic utterances the
    phrase cache misses.

    This is the *fallback* synthesis path, not the primary one. Cached
    phrases still play locally through ``SystemPlayback`` with no
    network at all, because a safety announcement must never depend on
    a round-trip. Aura is for narration the cache can't pre-bake.

    Shares one API key with speech-to-text (``DEEPGRAM_API_KEY``); the
    free tier covers both. Construction never raises — a missing key
    leaves ``available`` False.
    """

    URL = 'https://api.deepgram.com/v1/speak'

    def __init__(self,
                 api_key: Optional[str] = None,
                 voice: str = 'aura-2-thalia-en',
                 url: Optional[str] = None,
                 timeout_s: float = 10.0,
                 api_key_env: str = 'DEEPGRAM_API_KEY') -> None:
        self.voice = voice
        self.url = url or self.URL
        self.timeout_s = timeout_s
        key = api_key if api_key is not None else os.environ.get(api_key_env, '')
        self._api_key = (key or '').strip()
        self._unavailable_reason = ''
        if not self._api_key:
            self._unavailable_reason = f'no API key — set ${api_key_env}'

    @property
    def available(self) -> bool:
        return bool(self._api_key)

    @property
    def unavailable_reason(self) -> str:
        return self._unavailable_reason

    def synthesize(self, text: str) -> Optional[bytes]:
        """Return WAV bytes for ``text``, or None on any failure.

        Asks for ``container=wav`` rather than Deepgram's default MP3:
        ``SystemPlayback`` tries ``aplay`` first and aplay cannot decode
        MP3, so an MP3 response would work on some machines and go
        silent on this one. WAV keeps one playback path for everything.
        Payload size is irrelevant at phrase length.
        """
        if not self._api_key or not text.strip():
            return None
        try:
            import requests
            response = requests.post(
                self.url,
                params={
                    'model': self.voice,
                    'container': 'wav',
                    'encoding': 'linear16',
                },
                headers={
                    'Authorization': f'Token {self._api_key}',
                    'Content-Type': 'application/json',
                },
                json={'text': text},
                timeout=self.timeout_s,
            )
            if response.status_code != 200:
                log.warning('deepgram tts HTTP %s: %s',
                            response.status_code, response.text[:200])
                return None
            return _repair_wav_header(response.content)
        except Exception as exc:
            log.warning('deepgram tts failed: %s', exc)
            return None


__all__ = [
    'PhraseCache',
    'TtsEngine',
    'SilentTts',
    'SystemPlayback',
    'SpokenPhrase',
    'DeepgramTtsBackend',
    'AUDIO_EXTS',
    'PHRASES_DIR',
]
