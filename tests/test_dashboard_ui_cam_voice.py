"""Camera tab: profile editor, greeter controls, rover voice buttons (Playwright).

Everything the page talks to is stubbed with Playwright routes except sign-in,
so no camera, voice process or models are needed. What is checked is the
page's behaviour around the things that are easy to get wrong:

* the greeter and the profile editor are behind sign-in, and nothing is sent
  to the server before it;
* the editor loads notes with the bearer token (a signed-out read hides them,
  and saving that blank back would erase them);
* the rover buttons are disabled, with the reason, when the voice process is
  down or has no camera link;
* names, roles and greetings are text, never markup;
* the page reports what the rover actually did, not just that a request left.
"""
import _env
import json
import os
import subprocess
import sys
import urllib.request

PORT, WS_PORT = 5081, 5082
BASE = f'http://127.0.0.1:{PORT}/'

_STATUS = {'connected': True, 'fps': 11.8, 'age_ms': 90, 'frame_seq': 412,
           'device': '/dev/video1', 'width': 640, 'height': 480, 'last_error': ''}
_LIMITS = {'threshold': {'min': 0.2, 'max': 0.8, 'step': 0.01},
           'greet_min_sim': {'min': 0.4, 'max': 0.8, 'step': 0.01},
           'greet_cooldown_s': {'min': 30, 'max': 3600, 'step': 10}}


def _settings(**over):
    s = {'faces_enabled': True, 'objects_enabled': False, 'threshold': 0.363, 'object_conf': 0.4,
         'classes': [], 'greeter_enabled': False, 'greet_min_sim': 0.5, 'greet_cooldown_s': 300,
         'greet_unknown': False, 'classes_available': [], 'limits': _LIMITS}
    s.update(over)
    return s


def _det():
    return {'state': 'idle', 'seq': 0, 'ts': None, 'age_ms': None, 'w': 0, 'h': 0, 'faces': [],
            'objects': [], 'enroll': {'active': False}, 'people': 2, 'events': [],
            'settings': _settings(), 'models': {'faces': 'ok', 'objects': 'off'},
            'timing': {'faces_ms': 0, 'embed_ms': 0, 'objects_ms': 0, 'hz': 0}}


def _voice(running=True, faces_state='ok', **cmd):
    if not running:
        return {'running': False, 'stale': True}
    lane = {'ok': faces_state != 'down', 'state': faces_state,
            'reason': 'no fresh cam_faces.json' if faces_state == 'down' else '', 'greeted': 3}
    return {'running': True, 'stale': False, 'lanes': {'faces': lane},
            'command': cmd or {'state': 'idle'}}


def _json(page, pattern, fn, log=None):
    def handle(route):
        req = route.request
        if log is not None:
            log.append({'method': req.method, 'url': req.url, 'auth': req.headers.get('authorization'),
                        'body': req.post_data})
        status, body = fn(req)
        route.fulfill(status=status, content_type='application/json', body=json.dumps(body))
    page.route(pattern, handle)


def _wait(page, fn, timeout=8.0):
    """Poll while pumping Playwright, so route handlers can run."""
    def go():
        page.wait_for_timeout(80)
        try:
            return fn()
        except Exception:
            return False
    return _env.poll(go, timeout, 0.0)


def test_profile_editor_greeter_and_rover_buttons():
    from playwright.sync_api import sync_playwright
    env = dict(os.environ, AGV_DASHBOARD_PORT=str(PORT), AGV_DASHBOARD_WS_PORT=str(WS_PORT))
    log = open(os.path.join(_env.TMP, 'web_cam_voice.out'), 'w')
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
        evil = '<img src=x onerror=window.__pwned=1>'
        state = {'settings': _settings(), 'voice': _voice(running=False), 'people': None}
        state['people'] = [
            {'name': 'Alice', 'samples': 8, 'has_thumb': False, 'enrolled_at': 1,
             'profile': {'role': 'Operator', 'greeting': 'Welcome back, Alice.', 'notes': '',
                         'greet': True}},
            {'name': 'Bob', 'samples': 4, 'has_thumb': False, 'enrolled_at': 2,
             'profile': {'role': evil, 'greeting': '', 'notes': '', 'greet': False}}]
        settings_posts, profile_puts, commands = [], [], []

        def people(req):
            signed = bool(req.headers.get('authorization'))
            out = json.loads(json.dumps(state['people']))
            if signed:
                out[0]['profile']['notes'] = 'REAL NOTES'      # a signed-out read hides them
            return 200, {'people': out}

        def settings(req):
            if req.method == 'POST':
                state['settings'].update(json.loads(req.post_data))
            return 200, state['settings']

        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            try:
                page = browser.new_page(viewport={'width': 1200, 'height': 1000})
                page.on('pageerror', lambda e: errors.append(f'pageerror: {e}'))
                page.on('console', lambda m: m.type == 'error' and not any(
                    k in m.text.lower() for k in ('favicon', '404', '403', '401', '409', '503',
                                                  'mjpeg', 'stream', 'websocket'))
                    and errors.append(f'console: {m.text}'))
                _json(page, '**/cam/status', lambda r: (200, _STATUS))
                _json(page, '**/cam/detections', lambda r: (200, _det()))
                _json(page, '**/api/cam/settings', settings, settings_posts)
                _json(page, '**/api/cam/people', people)
                _json(page, '**/api/voice/status', lambda r: (200, state['voice']))
                _json(page, '**/api/voice/command',
                      lambda r: (202, {'id': 'c1', 'verb': json.loads(r.post_data)['verb']}), commands)

                def put(req):
                    profile_puts.append({'url': req.url, 'auth': req.headers.get('authorization'),
                                         'body': json.loads(req.post_data)})
                    return 200, {}
                page.route('**/api/cam/people/*/profile', lambda route: (
                    put(route.request), route.fulfill(status=200, content_type='application/json', body='{}')))

                page.goto(BASE, wait_until='domcontentloaded')
                page.click('#tabbtn-cam')

                # ── voice down: the rover buttons are disabled and say why ──
                assert _wait(page, lambda: 'not running' in page.inner_text('#cam-voice-status'))
                assert page.is_disabled('#cam-say-who')
                assert 'not running' in page.get_attribute('#cam-say-who', 'title')
                assert _wait(page, lambda: page.locator('#cam-roster .cam-call').count() == 2)
                assert all(page.locator('#cam-roster .cam-call').nth(i).is_disabled() for i in range(2))

                # ── voice up with a live camera link: enabled ──
                state['voice'] = _voice(running=True)
                page.evaluate('document.getElementById("tab-cam").hidden = true; '
                              'document.getElementById("tab-cam").hidden = false')   # wake the poll
                assert _wait(page, lambda: 'camera link ok' in page.inner_text('#cam-voice-status'), 12)
                assert page.is_enabled('#cam-say-who')
                assert page.locator('#cam-roster .cam-call').first.is_enabled()

                # ── voice up but camera link down: disabled with the reason ──
                state['voice'] = _voice(running=True, faces_state='down')
                page.evaluate('document.getElementById("tab-cam").hidden = true; '
                              'document.getElementById("tab-cam").hidden = false')
                assert _wait(page, lambda: 'link down' in page.inner_text('#cam-voice-status'), 12)
                assert page.is_disabled('#cam-say-who')
                state['voice'] = _voice(running=True)
                page.evaluate('document.getElementById("tab-cam").hidden = true; '
                              'document.getElementById("tab-cam").hidden = false')
                assert _wait(page, lambda: page.is_enabled('#cam-say-who'), 12)

                # ── roster shows role and the greeted badge; names are text ──
                txt = page.inner_text('#cam-roster')
                assert 'Operator' in txt and 'greeted' in txt
                assert evil in txt and page.locator('#cam-roster img').count() == 0
                assert page.evaluate('window.__pwned') is None

                # ── greeter: nothing is sent before sign-in ──
                page.click('#cam-t-greeter')
                assert _wait(page, lambda: page.evaluate('document.getElementById("login-dlg").open'))
                assert settings_posts == [] or all(p['method'] == 'GET' for p in settings_posts), settings_posts
                assert not [p for p in settings_posts if p['method'] == 'POST'], 'sent greeter change signed out'
                page.keyboard.press('Escape')
                assert page.get_attribute('#cam-t-greeter', 'aria-pressed') == 'false'

                # ── sign in, then the greeter goes through with the bearer token ──
                assert page.evaluate("Auth.login('testpw').then(r => r.ok)") is True
                page.click('#cam-t-greeter')
                assert _wait(page, lambda: any(p['method'] == 'POST' for p in settings_posts))
                post = [p for p in settings_posts if p['method'] == 'POST'][-1]
                assert json.loads(post['body']) == {'greeter_enabled': True}
                assert post['auth'] and post['auth'].startswith('Bearer ')
                assert _wait(page, lambda: page.get_attribute('#cam-t-greeter', 'aria-pressed') == 'true')

                page.fill('#cam-gcool', '120')
                page.dispatch_event('#cam-gcool', 'change')
                assert _wait(page, lambda: any(json.loads(p['body'] or '{}') == {'greet_cooldown_s': 120.0}
                                               for p in settings_posts if p['method'] == 'POST'))
                page.check('#cam-gunk')
                assert _wait(page, lambda: any(json.loads(p['body'] or '{}') == {'greet_unknown': True}
                                               for p in settings_posts if p['method'] == 'POST'))

                # ── profile editor: loads REAL notes (signed-in read), previews, saves ──
                page.locator('#cam-roster .cam-edit').first.click()
                assert _wait(page, lambda: page.is_visible('#cam-profile-form'))
                assert _wait(page, lambda: page.input_value('#cam-profile-notes') == 'REAL NOTES'), \
                    'editor opened with the signed-out (blank) notes: saving would erase them'
                assert page.input_value('#cam-profile-role') == 'Operator'
                assert page.is_checked('#cam-profile-greet')
                assert 'Welcome back, Alice.' in page.inner_text('#cam-profile-preview')
                page.fill('#cam-profile-greeting', '')
                assert 'Hello, Alice.' in page.inner_text('#cam-profile-preview')
                page.uncheck('#cam-profile-greet')
                assert 'Will not greet' in page.inner_text('#cam-profile-preview')
                page.check('#cam-profile-greet')
                page.fill('#cam-profile-role', 'Supervisor')
                page.click('#cam-profile-save')
                assert _wait(page, lambda: len(profile_puts) == 1)
                sent = profile_puts[0]
                assert sent['url'].endswith('/api/cam/people/Alice/profile')
                assert sent['auth'].startswith('Bearer ')
                assert sent['body'] == {'role': 'Supervisor', 'greeting': '', 'notes': 'REAL NOTES',
                                        'greet': True}
                assert _wait(page, lambda: not page.is_visible('#cam-profile-form'))

                # ── rover buttons send the whitelisted verbs, and report what happened ──
                state['voice'] = _voice(running=True, id='c1', state='done', verb='announce_who',
                                        detail="said: 'I see Alice.'")
                page.click('#cam-say-who')
                assert _wait(page, lambda: len(commands) >= 1)
                assert json.loads(commands[0]['body']) == {'verb': 'announce_who'}
                assert commands[0]['auth'].startswith('Bearer ')
                assert _wait(page, lambda: "I see Alice" in page.inner_text('#toasts'), 15), \
                    'did not report what the rover said'

                page.locator('#cam-roster .cam-call').first.click()
                assert _wait(page, lambda: len(commands) >= 2)
                assert json.loads(commands[1]['body']) == {'verb': 'call_person', 'name': 'Alice'}

                assert not errors, errors
            finally:
                browser.close()
    finally:
        proc.terminate()
        proc.wait(timeout=10)
        log.close()


if __name__ == '__main__':
    _env.run_tests(globals())
