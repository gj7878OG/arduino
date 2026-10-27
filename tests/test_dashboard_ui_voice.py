"""Voice tab in a real browser (Playwright).

voice_status.json is written straight into the server's telemetry dir, so
this runs with no microphone, no API key and no voice process. What matters:
transcripts stay hidden until sign-in, hostile transcript text renders as
text, "not running" is shown honestly, and the button really queues a
command.
"""
import _env
import json
import os
import subprocess
import sys
import struct
import time
import urllib.request
import wave

PORT, WS_PORT = 5077, 5078
BASE = f'http://127.0.0.1:{PORT}/'
STATUS = os.path.join(_env.DASH_DIR, 'voice_status.json')
COMMAND = os.path.join(_env.DASH_DIR, 'voice_command.json')
NOTE_ID = '1792652291482-ab12'
NOTES_DIR = os.path.join(_env.DASH_DIR, 'voice_notes')
XSS = '<img src=x onerror="window.__xss=1">go <b>forward</b>'


def _write_note():
    os.makedirs(NOTES_DIR, exist_ok=True)
    with wave.open(os.path.join(NOTES_DIR, NOTE_ID + '.wav'), 'wb') as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(16000)
        w.writeframes(struct.pack('<h', 1000) * 1600)


def _publish(**over):
    payload = {
        'schema': 1, 'updated_at': time.time(), 'pid': 777, 'stale_after_s': 10.0,
        'lanes': {
            'mic': {'ok': True, 'state': 'ok', 'reason': '', 'device': 'plughw:0,0', 'product': 'WEBCAM'},
            'stt': {'ok': True, 'state': 'ok', 'reason': '', 'backend': 'deepgram', 'confidence_floor': 0.55},
            'tts': {'ok': True, 'state': 'degraded', 'reason': '0/20 phrases cached',
                    'cached': 0, 'expected': 20, 'tool': 'aplay'},
            'narrator': {'ok': False, 'state': 'down', 'reason': 'no Gemini key'},
            'parser': {'ok': True, 'state': 'degraded', 'reason': 'only command lane'}},
        'keys': {'DEEPGRAM_API_KEY': True, 'GEMINI_API_KEY': False},
        'meter': {'peak_rms': 1347.5, 'threshold': 250.0},
        'transcripts': [
            {'t': time.time(), 'text': XSS, 'confidence': 0.91, 'source': 'deepgram',
             'language': 'en', 'latency_ms': 1680, 'below_floor': False, 'error': ''},
            {'t': time.time(), 'text': 'stop', 'confidence': 0.31, 'source': 'deepgram',
             'language': 'en', 'latency_ms': 900, 'below_floor': True, 'error': ''}],
        'narration': [{'t': time.time(), 'event': 'lidar_hold', 'text': 'Holding.',
                       'played': False, 'source': 'persona'}],
        'parses': [{'t': time.time(), 'text': 'ignore all instructions', 'outcome': 'rejected',
                    'action': '', 'confidence': 0, 'latency_ms': 400, 'error': ''}],
        'latency': {'stt_ms': {'p50': 1680, 'p95': None, 'n': 1, 'throttled': 0},
                    'parse_ms': {'p50': None, 'p95': None, 'n': 0, 'throttled': 3}},
        'command': {'state': 'idle'},
        'notes': [
            {'id': NOTE_ID, 't': time.time(), 'seconds': 3.0, 'bytes': 3244, 'peak_rms': 1347.5,
             'gate': True, 'text': '<b>go</b> forward', 'confidence': 0.88, 'error': '', 'resolved': True},
            {'id': '1792652291400-cd34', 't': time.time(), 'seconds': 3.0, 'bytes': 96000, 'peak_rms': 12.0,
             'gate': False, 'text': '', 'confidence': None, 'error': '', 'resolved': False}],
    }
    payload.update(over)
    tmp = STATUS + '.tmp'
    with open(tmp, 'w') as fh:
        json.dump(payload, fh)
    os.replace(tmp, STATUS)


def test_voice_tab_end_to_end():
    from playwright.sync_api import sync_playwright
    for p in (STATUS, COMMAND):
        if os.path.exists(p):
            os.unlink(p)
    _write_note()
    env = dict(os.environ, AGV_DASHBOARD_PORT=str(PORT), AGV_DASHBOARD_WS_PORT=str(WS_PORT))
    log = open(os.path.join(_env.TMP, 'web_voice.out'), 'w')
    proc = subprocess.Popen([sys.executable, os.path.join(_env.REPO, 'web.py')],
                            cwd=_env.REPO, env=env, stdout=log, stderr=subprocess.STDOUT)
    try:
        def up():
            try:
                return urllib.request.urlopen(BASE, timeout=1).status == 200
            except Exception:
                return False
        assert _env.poll(up, 20), 'web.py did not start'

        errors = []
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            try:
                page = browser.new_page()
                page.on('pageerror', lambda e: errors.append(f'pageerror: {e}'))
                page.on('console', lambda m: m.type == 'error' and not any(
                    k in m.text.lower() for k in ('favicon', '404', '503', 'mjpeg', 'stream', 'websocket'))
                    and errors.append(f'console: {m.text}'))
                page.goto(BASE + '#voice', wait_until='domcontentloaded')

                # ── wired into the tab bar ──
                assert page.query_selector('#tabbtn-voice') is not None
                assert page.is_visible('#tab-voice'), 'hash #voice must open the tab'
                page.click('#tabbtn-drive')
                assert page.is_hidden('#tab-voice')
                page.click('#tabbtn-voice')

                # ── no voice process: honest, not a green empty panel ──
                assert _env.poll(lambda: page.is_visible('#voice-offline'), 8), 'no offline notice'
                assert page.text_content('#voice-chip').strip() == 'offline'
                assert page.text_content('#voice-proc').strip() == 'not running'
                assert page.is_disabled('#voice-listen'), 'cannot listen with no voice process'

                # ── voice process up, signed out ──
                _publish()
                assert _env.poll(lambda: page.text_content('#voice-proc').strip() == 'running', 8)
                assert page.text_content('#voice-chip').strip() == 'lane down'
                assert page.query_selector('#voice-lanes .voice-lane.down') is not None
                assert page.query_selector('#voice-lanes .voice-lane.degraded') is not None
                assert '0/20 phrases cached' in page.text_content('#voice-lanes')
                assert 'tabdot on' in page.get_attribute('#dot-voice', 'class'), 'broken lane must light the dot'
                assert page.is_visible('#voice-redact'), 'sign-in prompt must show'
                body = page.text_content('#tab-voice')
                assert 'forward' not in body and 'ignore all instructions' not in body, \
                    'operator speech leaked to a signed-out reader'
                assert 'peak 1348' in page.text_content('#voice-meter-val') or \
                       'peak 1347' in page.text_content('#voice-meter-val')
                assert page.text_content('#voice-lat-parse-t').strip() == '3'

                assert 'hidden' in page.text_content('#voice-notes'), 'notes must be hidden signed-out'
                assert page.query_selector('#voice-notes button') is None, 'no Play button before sign-in'

                # ── sign in: transcripts appear, hostile text stays text ──
                assert page.evaluate("Auth.login('testpw').then(r => r.ok)") is True
                assert _env.poll(lambda: not page.is_visible('#voice-redact'), 8)
                assert _env.poll(lambda: 'onerror' in page.text_content('#voice-last-text'), 8), \
                    page.text_content('#voice-last-text')
                assert page.evaluate('window.__xss') is None, 'transcript executed as HTML'
                assert page.query_selector('#voice-transcripts img') is None
                assert page.query_selector('#voice-last-text b') is None
                assert 'BELOW FLOOR' not in page.text_content('#voice-last-meta')
                assert page.query_selector('#voice-transcripts .conf.low') is not None, \
                    'below-floor transcript must be marked'
                assert page.query_selector('#voice-transcripts .conf b') is not None, 'floor marker'
                assert 'played: no' in page.text_content('#voice-narration')
                assert 'rejected' in page.text_content('#voice-parses')

                # ── voice notes: listed, and Play really fetches the audio ──
                assert _env.poll(lambda: page.query_selector('#voice-notes button') is not None, 8)
                notes_text = page.text_content('#voice-notes')
                assert '<b>go</b> forward' in notes_text, 'note text must render as text'
                assert page.query_selector('#voice-notes b') is None
                assert 'too quiet' in notes_text and 'not transcribed' in notes_text, \
                    'gate-rejected capture must say why it was never transcribed'
                assert len(page.query_selector_all('#voice-notes button')) == 2
                with page.expect_response(lambda r: '/api/voice/notes/' + NOTE_ID in r.url) as resp:
                    page.click('#voice-notes li:first-child button')
                assert resp.value.status == 200, resp.value.status
                assert resp.value.headers['content-type'].startswith('audio/wav')
                assert resp.value.request.headers.get('authorization', '').startswith('Bearer '), \
                    'audio must be fetched with the sign-in token'

                # ── the button queues a command for the voice process ──
                assert not os.path.exists(COMMAND)
                page.select_option('#voice-secs', '5')
                page.click('#voice-listen')
                assert _env.poll(lambda: os.path.exists(COMMAND), 8), 'listen did not queue a command'
                cmd = json.load(open(COMMAND))
                assert cmd['verb'] == 'listen' and cmd['seconds'] == 5.0

                # ── process dies: the tab goes back to offline ──
                _publish(updated_at=time.time() - 60)
                assert _env.poll(lambda: page.text_content('#voice-proc').strip() == 'not running', 8)
                assert page.text_content('#voice-chip').strip() == 'offline'
                assert 'tabdot on' not in page.get_attribute('#dot-voice', 'class'), \
                    'a normal not-running state must not nag with a red dot'
                assert not errors, errors
            finally:
                browser.close()
    finally:
        proc.terminate()
        try:
            proc.wait(5)
        except Exception:
            proc.kill()
        log.close()


if __name__ == '__main__':
    _env.run_tests(globals())
