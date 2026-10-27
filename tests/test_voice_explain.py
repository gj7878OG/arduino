"""Tests for voice.explain — Gemini narration.

The tests that matter most here are architectural, not behavioural:
this module must be incapable of influencing control, and must degrade
to the persona template rather than go silent.
"""

import _env  # noqa: F401

import ast
from pathlib import Path

import voice.explain as explain
from voice.explain import (
    MAX_CHARS,
    EventExplainer,
    Explanation,
    ExplainerBackend,
    sanitize,
)

_MIC_PATH = Path(explain.__file__)


# ── helpers ─────────────────────────────────────────────────────────


class FakeBackend:
    """Returns canned prose or raises, like a real backend."""

    def __init__(self, text='', error=None):
        self.text = text
        self.error = error
        self.prompts = []
        self._available = True

    @property
    def available(self):
        return self._available

    def complete(self, prompt):
        self.prompts.append(prompt)
        if self.error:
            raise self.error
        return self.text


# ── the architectural guarantee ─────────────────────────────────────


def test_explain_module_never_imports_the_intent_validator():
    """If this ever imports voice.intent, the narrator has been wired
    somewhere it must not be. ARCHITECTURE.md rule #4."""
    tree = ast.parse(_MIC_PATH.read_text())
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                imported.add(node.module)
    banned = {'voice.intent', 'voice.safety', 'control_state', 'voice.dialogue'}
    assert not (imported & banned), f'banned import found: {imported & banned}'


def test_explain_module_never_touches_the_control_writer():
    """Belt and braces: no path from prose to control_state.write_state.

    Scans the AST for real identifiers, not raw substrings — the module
    docstring legitimately *names* the things it refuses to import, and
    a docstring is a Constant node, not a Name.
    """
    tree = ast.parse(_MIC_PATH.read_text())
    referenced = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            referenced.add(node.id)
        elif isinstance(node, ast.Attribute):
            referenced.add(node.attr)
    banned = {'write_state', 'validate_intent', 'Intent', 'SpeechClient'}
    assert not (referenced & banned), \
        f'explain.py must not reference {sorted(referenced & banned)}'


def test_explanation_carries_no_action_field():
    """The return type is prose. There is nowhere to put an action."""
    e = Explanation(text='Holding.', event='lidar_hold', source='gemini')
    assert not hasattr(e, 'action')
    assert not hasattr(e, 'args')
    assert isinstance(e.text, str)


# ── sanitize ────────────────────────────────────────────────────────


def test_sanitize_strips_markdown_fences():
    assert sanitize('```\nHolding.\n```') == 'Holding.'


def test_sanitize_strips_wrapping_quotes():
    assert sanitize('"Holding."') == 'Holding.'
    assert sanitize("'Holding.'") == 'Holding.'


def test_sanitize_collapses_whitespace():
    assert sanitize('Holding\n\nnow.') == 'Holding now.'


def test_sanitize_truncates_at_word_boundary():
    out = sanitize('one two three four five six', max_chars=15)
    assert len(out) <= 16
    assert out.endswith('.')
    assert not out.endswith('..')


def test_sanitize_returns_none_for_empty():
    assert sanitize('') is None
    assert sanitize('   ') is None
    assert sanitize('"""') is None


def test_sanitize_preserves_short_prose():
    assert sanitize('Three obstacles in the stop zone.') == \
        'Three obstacles in the stop zone.'


# ── explanation fallback behaviour ──────────────────────────────────


def test_explain_falls_back_to_persona_when_no_backend():
    x = EventExplainer(backend=None)
    r = x.explain('lidar_hold', reason='obstacle')
    assert r.source == 'persona'
    assert r.text                     # never silent


def test_explain_falls_back_when_backend_unavailable():
    backend = FakeBackend(text='Three obstacles ahead.')
    backend._available = False
    r = EventExplainer(backend=backend).explain('lidar_hold', reason='obstacle')
    assert r.source == 'persona'


def test_explain_falls_back_on_backend_error():
    backend = FakeBackend(error=RuntimeError('rate limited'))
    r = EventExplainer(backend=backend).explain('lidar_hold', reason='obstacle')
    assert r.source == 'persona'
    assert r.text


def test_explain_falls_back_on_empty_backend_output():
    backend = FakeBackend(text='   ')
    r = EventExplainer(backend=backend).explain('motors_disabled', mode='auto')
    assert r.source == 'persona'


def test_explain_returns_gemini_prose_when_healthy():
    backend = FakeBackend(text='  **"Holding — three obstacles ahead."**  ')
    r = EventExplainer(backend=backend).explain('lidar_hold', reason='obstacle')
    assert r.source == 'gemini'
    assert r.text == 'Holding — three obstacles ahead.'


def test_explain_never_raises_on_bad_backend():
    """A backend returning a non-string must not explode narration."""
    class Weird:
        available = True

        def complete(self, prompt):
            return object()

    r = EventExplainer(backend=Weird()).explain('motors_disabled', mode='auto')
    assert r.source == 'persona'


# ── prompt construction ─────────────────────────────────────────────


def test_prompt_includes_event_and_state():
    backend = FakeBackend(text='Battery is low.')
    EventExplainer(backend=backend).explain('battery_low', pct=12)
    prompt = backend.prompts[0]
    assert 'battery_low' in prompt
    assert 'pct: 12' in prompt


def test_prompt_does_not_ask_for_instructions():
    """The system prompt must forbid instructing the operator."""
    assert 'never issue an instruction' in explain.SYSTEM_PROMPT.lower()


def test_prompt_forbids_inventing_facts():
    assert 'never invent' in explain.SYSTEM_PROMPT.lower()


# ── cooldown ────────────────────────────────────────────────────────


def test_cooldown_suppresses_repeat_of_same_event():
    backend = FakeBackend(text='Holding.')
    x = EventExplainer(backend=backend, cooldown_s=60.0)
    first = x.explain('lidar_hold', reason='obstacle')
    second = x.explain('lidar_hold', reason='obstacle')
    assert first.source == 'gemini'
    assert second.source == 'persona'        # suppressed, no second API call
    assert len(backend.prompts) == 1


def test_cooldown_does_not_suppress_a_different_event():
    backend = FakeBackend(text='Understood.')
    x = EventExplainer(backend=backend, cooldown_s=60.0)
    x.explain('lidar_hold', reason='obstacle')
    second = x.explain('battery_low', pct=12)
    assert second.source == 'gemini'


def test_cooldown_of_zero_always_calls():
    backend = FakeBackend(text='Understood.')
    x = EventExplainer(backend=backend, cooldown_s=0.0)
    x.explain('lidar_hold', reason='obstacle')
    assert x.explain('lidar_hold', reason='obstacle').source == 'gemini'


# ── backend plumbing ────────────────────────────────────────────────


def test_backend_is_available_before_any_call_is_made():
    """A correctly configured lane must not look broken at startup.

    Regression: ``available`` used to report False until the first real
    request, because the client was built lazily. That made the startup
    health check lie about a working Gemini key.
    """
    b = ExplainerBackend(api_key='dummy')
    assert b.available is True


def test_backend_is_unavailable_without_key():
    assert ExplainerBackend(api_key='').available is False


def test_auto_backend_reads_gemini_env_var(monkeypatch=None):
    import os
    os.environ['GEMINI_API_KEY'] = 'test-key-123'
    try:
        b = EventExplainer._auto_backend()
        assert b is not None
        assert b.api_key == 'test-key-123'
    finally:
        os.environ.pop('GEMINI_API_KEY', None)


def test_auto_backend_falls_back_to_google_env_var():
    import os
    # GEMINI_API_KEY genuinely takes precedence, so the fallback can only
    # be observed with it absent. Clearing it is the point of the test —
    # without this, a developer with both keys exported fails here.
    saved = os.environ.pop('GEMINI_API_KEY', None)
    os.environ['GOOGLE_API_KEY'] = 'test-key-456'
    try:
        b = EventExplainer._auto_backend()
        assert b is not None
        assert b.api_key == 'test-key-456'
    finally:
        os.environ.pop('GOOGLE_API_KEY', None)
        if saved is not None:
            os.environ['GEMINI_API_KEY'] = saved


def test_auto_backend_returns_none_without_key():
    import os
    saved = {k: os.environ.pop(k) for k in explain.API_KEY_ENVS
             if k in os.environ}
    try:
        assert EventExplainer._auto_backend() is None
    finally:
        os.environ.update(saved)


def test_model_is_free_tier_appropriate():
    assert 'flash' in explain.MODEL.lower()


def test_output_budget_is_bounded():
    """A runaway generation must not become an unbounded utterance."""
    assert MAX_CHARS <= 400
    assert MAX_CHARS > 0


# ── Gemini request shape (regression: narrator spoke "I") ───────────


class _FakeModels:
    def __init__(self, finish='FinishReason.STOP', text='Holding.'):
        self.kwargs, self.finish, self.text = None, finish, text

    def generate_content(self, **kwargs):
        from types import SimpleNamespace
        self.kwargs = kwargs
        cand = SimpleNamespace(finish_reason=self.finish)
        return SimpleNamespace(text=self.text, candidates=[cand])


def _backend_with(models):
    from types import SimpleNamespace
    b = ExplainerBackend(api_key='k')
    b._client = SimpleNamespace(models=models)
    return b


def test_request_disables_thinking():
    """Thinking spends the 90-token budget and truncates the narration."""
    m = _FakeModels()
    _backend_with(m).complete('x')
    assert m.kwargs['config']['thinking_config'] == {'thinking_budget': 0}


def test_truncated_narration_falls_back_to_persona_not_a_fragment():
    m = _FakeModels(finish='FinishReason.MAX_TOKENS', text='I')
    r = EventExplainer(backend=_backend_with(m)).explain('lidar_hold', reason='obstacle')
    assert r.source == 'persona' and r.text != 'I'


def test_truncation_raises_the_named_error_not_a_nameerror():
    """Guards against the fallback being reached by accident (a NameError
    is swallowed by explain() just the same)."""
    m = _FakeModels(finish='FinishReason.MAX_TOKENS', text='I')
    try:
        _backend_with(m).complete('x')
    except explain.ExplainError as exc:
        assert 'truncated' in str(exc)
        return
    raise AssertionError('expected ExplainError')


def test_complete_narration_is_used():
    r = EventExplainer(backend=_backend_with(_FakeModels(text='Holding — obstacle ahead.'))
                       ).explain('lidar_hold', reason='obstacle')
    assert r.source == 'gemini'


if __name__ == '__main__':
    _env.run_tests(globals())