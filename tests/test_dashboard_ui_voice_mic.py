"""Voice tab, Microphone card (Playwright).

/api/voice/mic and /api/voice/mic/test are stubbed so each state is
deterministic whatever is plugged into this machine. What matters: the card
never claims "working" without evidence, shows a clear reason when it is not,
and still answers when the voice process is not running.
"""
import _env
import json
import os
import subprocess
import sys
import time
import urllib.request

PORT, WS_PORT = 5079, 5080
BASE = f'http://127.0.0.1:{PORT}/'
STATUS = os.path.join(_env.DASH_DIR, 'voice_status.json')

CARDS = [{'card': 0, 'id': 'WEBCAM', 'name': 'C270 HD WEBCAM', 'selected': True},
         {'card': 2, 'id': 'APE', 'name': 'NVIDIA Jetson Orin Nano APE', 'selected': False}]
PLUGGED = {'connected': True, 'arecord': True, 'expected_usb_id': '046d:0825', 'cards': CARDS,
           'device': {'card': 0, 'plughw': 'plughw:0,0', 'product': 'WEBCAM', 'usb_id': '046d:0825'},
           'last_test': None}
UNPLUGGED = {'connected': False, 'arecord': True, 'expected_usb_id': '046d:0825',
             'cards': CARDS[1:], 'device': None, 'last_test': None}


def _test(state, ok, peak, msg):
    return {'ok': ok, 'state': state, 'peak_rms': peak, 'message': msg, 't': time.time()}


def test_microphone_card():
    from playwright.sync_api import sync_playwright
    if os.path.exists(STATUS):
        os.unlink(STATUS)
    env = dict(os.environ, AGV_DASHBOARD_PORT=str(PORT), AGV_DASHBOARD_WS_PORT=str(WS_PORT))
    log = open(os.path.join(_env.TMP, 'web_voice_mic.out'), 'w')
    proc = subprocess.Popen([sys.executable, os.path.join(_env.REPO, 'web.py')],
                            cwd=_env.REPO, env=env, stdout=log, stderr=subprocess.STDOUT)
    try:
        def up():
            try:
                return urllib.request.urlopen(BASE, timeout=1).status == 200
            except Exception:
                return False
        assert _env.poll(up, 20), 'web.py did not start'

        mic = dict(UNPLUGGED)
        next_test = {}
        sent = []
        errors = []
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            try:
                page = browser.new_page()
                page.on('pageerror', lambda e: errors.append(f'pageerror: {e}'))
                page.route('**/api/voice/mic', lambda r: r.fulfill(
                    status=200, content_type='application/json', body=json.dumps(mic)))
                def test_route(r):
                    sent.append(r.request.headers)
                    result = dict(next_test)
                    mic['last_test'] = result
                    r.fulfill(status=200, content_type='application/json', body=json.dumps(result))
                page.route('**/api/voice/mic/test', test_route)
                page.goto(BASE + '#voice', wait_until='domcontentloaded')

                def chip(): return page.text_content('#mic-chip').strip()
                def lane_text(): return page.text_content('#voice-lanes')

                # ── unplugged: says so, names what it expected ──
                assert _env.poll(lambda: chip() == 'not found', 8), chip()
                head = page.text_content('#mic-headline')
                assert '046d:0825' in head and 'Re-plug' in head, head
                assert page.get_attribute('#mic-headline', 'class').endswith('bad')
                # voice process is off, yet the mic tile still has a real answer
                assert 'voice process off' in lane_text(), 'other lanes must say why they are empty'
                assert 'no data' not in lane_text()
                assert 'C270 not found' in lane_text()

                # ── plugged in, untested: connected is NOT working ──
                mic.clear(); mic.update(PLUGGED)
                assert _env.poll(lambda: chip() == 'connected', 8), chip()
                assert 'not tested yet' in page.text_content('#mic-headline')
                assert 'working' not in page.text_content('#mic-headline')
                assert 'C270 HD WEBCAM' in page.text_content('#mic-headline')
                assert page.text_content('#mic-alsa').strip() == 'plughw:0,0'
                assert page.text_content('#mic-usb').strip() == '046d:0825'
                assert page.text_content('#mic-last').strip() == 'not tested'
                sel = page.query_selector_all('#mic-cards li.sel')
                assert len(sel) == 1 and 'C270' in sel[0].text_content()
                assert len(page.query_selector_all('#mic-cards li')) == 2
                assert 'connected, not tested yet' in lane_text()

                # ── test button needs a sign-in, then reports working ──
                assert page.evaluate("Auth.login('testpw').then(r => r.ok)") is True
                next_test.update(_test('working', True, 836.0, 'Audio is arriving (peak level 836).'))
                page.click('#mic-test')
                assert _env.poll(lambda: chip() == 'working', 8), chip()
                assert 'connected and working' in page.text_content('#mic-headline')
                assert 'peak 836' in page.text_content('#mic-peak')
                assert 'test' in page.text_content('#mic-last')
                assert 'Audio is arriving' in page.text_content('#mic-test-msg')
                assert sent and sent[0].get('authorization', '').startswith('Bearer '), 'test must be authenticated'
                assert page.get_attribute('#mic-headline', 'class').endswith('ok')

                # ── flat silence is a failure, not a pass ──
                next_test.clear(); next_test.update(_test(
                    'silent', False, 0.0, 'Captured, but the signal is flat zero — microphone muted or dead.'))
                page.click('#mic-test')
                assert _env.poll(lambda: chip() == 'silent', 8), chip()
                assert 'muted or dead' in page.text_content('#mic-headline')
                assert page.get_attribute('#mic-headline', 'class').endswith('bad')

                # ── busy (voice process listening) is a retry, not a fault ──
                next_test.clear(); next_test.update(_test('busy', False, 0.0, 'Microphone is in use by another process — try again.'))
                page.click('#mic-test')
                assert _env.poll(lambda: chip() == 'busy', 8), chip()
                assert page.get_attribute('#mic-headline', 'class').endswith('warn')

                # ── unplugging after a good test must not keep saying "working" ──
                mic.clear(); mic.update(UNPLUGGED); mic['last_test'] = _test('working', True, 800.0, 'ok')
                assert _env.poll(lambda: chip() == 'not found', 8), chip()
                assert 'working' not in page.text_content('#mic-headline')

                # ── voice process up: its last capture counts as evidence ──
                mic.clear(); mic.update(PLUGGED)
                json.dump({'schema': 1, 'updated_at': time.time(), 'pid': 1, 'stale_after_s': 10,
                           'lanes': {}, 'keys': {}, 'meter': {'peak_rms': 640.0, 'threshold': 250.0},
                           'transcripts': [], 'narration': [], 'parses': [], 'latency': {},
                           'command': {'state': 'idle'}}, open(STATUS, 'w'))
                assert _env.poll(lambda: chip() == 'working', 8), chip()
                assert 'from voice capture' in page.text_content('#mic-last')
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
