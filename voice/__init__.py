"""Bidirectional speech for the AGV.

Voice is an *input/output channel*, not a control surface. It reads from the
mic (wake word + STT), parses intent, and — for motion — goes through the
same ``control_state`` write path the dashboard uses. Outgoing speech goes
through a local TTS engine driven by a small persona module.

Split by concern, mirroring ``lidar/``:

  ``voice.wake``        wake-word listener, always-on
  ``voice.mic``         C270 USB microphone capture + speech gate
  ``voice.stt``         Whisper wrapper (faster-whisper / whisper.cpp)
  ``voice.intents``     rules engine — primary command lane
  ``voice.agent``       optional LLM-as-parser — secondary lane
  ``voice.dialogue``    turn-taking, confirm gate, slot filling
  ``voice.safety``      voice-side clamps, mute, LiDAR override
  ``voice.tts``         Piper wrapper, pre-baked phrase cache
  ``voice.persona``     renders persona-appropriate utterances from
                        intents and state (pure module — no audio I/O)

Phase 0 ships only the read-side narration stack (no mic, no control
writes). Future phases add input.

Public API is re-exported here. Submodules import from each other freely.
"""

from __future__ import annotations

from ._constants import (
    C270_USB_ID,
    CONFIRM_PHRASES,
    CONFIRM_TIMEOUT_S,
    CONTROL_FILENAME,
    EMERGENCY_STOP_ACK,
    EMERGENCY_STOP_PHRASES,
    HOLD_DEBOUNCE_S,
    HOLD_MIN_STOP_POINTS,
    HOLD_PROMPT,
    LLM_CONFIDENCE_FLOOR,
    MIC_CHANNELS,
    MIC_FRAME_MS,
    MIC_MAX_SECONDS,
    MIC_SAMPLE_RATE,
    MIC_SPEECH_FRAMES,
    MIC_SPEECH_RMS,
    MOTION_MAX_DISTANCE_M,
    MOTION_MIN_DISTANCE_M,
    MOTION_PROMPT_VARIANTS,
    MOTION_VALID_DIRECTIONS,
    MUTE_ANNOUNCE,
    MUTE_RELEASE_ANNOUNCE,
    PERSONA_EVENTS,
    PERSONA_NAME,
    POINTS_FILENAME,
    STT_BEAM_DEFAULT,
    STT_LANGUAGE_DEFAULT,
    STT_MODEL_DEFAULT,
    VALID_INTENT_ACTIONS,
    WAKE_WORD_DEFAULT,
)
from .agent import (
    AgentBackend,
    AgentParser,
    GeminiBackend,
    LocalBackend,
    ParseError,
    load_api_key,
)
from .event_watcher import EventWatcher, Speaker
from .explain import (
    EventExplainer,
    Explanation,
    ExplainerBackend,
    sanitize,
)
from .intent import Intent, validate_intent
from .mic import (
    LevelMeter,
    MicCapture,
    MicDevice,
    MicUnavailable,
    find_alsa_card,
    frame_rms,
    list_capture_cards,
    summarise_capture_cards,
)
from .persona import render_event, utterance_for_event
from .stt import (
    DEEPGRAM_API_KEY_ENV,
    DEEPGRAM_STT_MODEL,
    STT_CONFIDENCE_FLOOR,
    STT_LANGUAGE,
    STT_MODEL,
    DeepgramSttBackend,
    GoogleSttBackend,
    OfflineSttBackend,
    SttBackend,
    SttEngine,
    SttError,
    Transcript,
    default_backend,
)
from .tts import (
    AUDIO_EXTS,
    PHRASES_DIR,
    DeepgramTtsBackend,
    PhraseCache,
    SilentTts,
    SpokenPhrase,
    SystemPlayback,
    TtsEngine,
)

__all__ = [
    # Constants
    'PERSONA_NAME', 'PERSONA_EVENTS',
    'CONFIRM_PHRASES', 'CONFIRM_TIMEOUT_S', 'MOTION_PROMPT_VARIANTS',
    'EMERGENCY_STOP_PHRASES', 'EMERGENCY_STOP_ACK',
    'HOLD_PROMPT', 'HOLD_DEBOUNCE_S', 'HOLD_MIN_STOP_POINTS',
    'MUTE_ANNOUNCE', 'MUTE_RELEASE_ANNOUNCE',
    'WAKE_WORD_DEFAULT', 'STT_LANGUAGE_DEFAULT', 'STT_MODEL_DEFAULT',
    'STT_BEAM_DEFAULT',
    'C270_USB_ID', 'MIC_SAMPLE_RATE', 'MIC_CHANNELS',
    'MIC_SPEECH_RMS', 'MIC_SPEECH_FRAMES', 'MIC_FRAME_MS',
    'MIC_MAX_SECONDS',
    'CONTROL_FILENAME', 'POINTS_FILENAME',
    'LLM_CONFIDENCE_FLOOR',
    'MOTION_MAX_DISTANCE_M', 'MOTION_MIN_DISTANCE_M',
    'MOTION_VALID_DIRECTIONS', 'VALID_INTENT_ACTIONS',
    # Phase 0 modules
    'EventWatcher', 'Speaker',
    'TtsEngine', 'SilentTts', 'SpokenPhrase',
    'PhraseCache', 'SystemPlayback',
    'AUDIO_EXTS', 'PHRASES_DIR',
    # Microphone input (C270)
    'MicCapture', 'MicDevice', 'MicUnavailable', 'LevelMeter',
    'find_alsa_card', 'list_capture_cards', 'summarise_capture_cards',
    'frame_rms',
    # Speech-to-text (Deepgram primary, Google fallback)
    'SttEngine', 'SttBackend', 'SttError', 'Transcript', 'default_backend',
    'DeepgramSttBackend', 'GoogleSttBackend', 'OfflineSttBackend',
    'DEEPGRAM_API_KEY_ENV', 'DEEPGRAM_STT_MODEL',
    'STT_MODEL', 'STT_LANGUAGE', 'STT_CONFIDENCE_FLOOR',
    'DeepgramTtsBackend',
    # Gemini narration (words only — never control)
    'EventExplainer', 'Explanation', 'ExplainerBackend', 'sanitize',
    'render_event', 'utterance_for_event',
    # Phase 3 modules
    'Intent', 'validate_intent',
    'AgentBackend', 'AgentParser',
    'GeminiBackend', 'LocalBackend',
    'ParseError', 'load_api_key',
]
