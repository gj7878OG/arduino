"""Camera tab: detection overlay, enrolment panel, roster, events (Playwright).

/cam/status, /cam/detections, /api/cam/* are stubbed with Playwright routes,
so this runs with no camera, no models and no enrolled faces. It checks the
page does the right thing with what the server says:

* boxes land on the canvas where the frame coordinates say they should,
* names are shown as text (they are operator input),
* a hidden tab does not poll /cam/detections (that poll is what wakes the
  server-side analytics worker),
* enrolment progress and the roster render from the payload.
"""
import _env
import json
import os
import subprocess
import sys
import urllib.request

PORT, WS_PORT = 5077, 5078
BASE = f'http://127.0.0.1:{PORT}/'

_STATUS = {'connected': True, 'fps': 11.8, 'age_ms': 90, 'frame_seq': 412,
           'device': '/dev/video1', 'width': 640, 'height': 480, 'last_error': ''}
_SETTINGS = {'faces_enabled': True, 'objects_enabled': False, 'threshold': 0.363,
             'object_conf': 0.4, 'classes': [], 'classes_available': [],
             'limits': {'threshold': {'min': 0.2, 'max': 0.8, 'step': 0.01, 'label': 'Match threshold'}}}
_PEOPLE = {'people': [{'name': 'Alice', 'samples': 8, 'has_thumb': False, 'enrolled_at': 1}]}


def _det(**over):
    d = {'state': 'running', 'seq': 5, 'ts': 1.0, 'age_ms': 80, 'w': 640, 'h': 480,
         'faces': [{'track': 1, 'box': [160, 120, 160, 120], 'score': 0.93, 'name': 'Alice',
                    'sim': 0.71, 'known': True},
                   {'track': 2, 'box': [440, 300, 100, 100], 'score': 0.88, 'name': None,
                    'sim': 0.12, 'known': False}],
         'objects': [], 'enroll': {'active': False}, 'people': 1,
         'events': [{'t': 1792650000, 'name': 'Alice', 'sim': 0.71, 'kind': 'seen'}],
         'settings': dict(_SETTINGS), 'models': {'faces': 'ok', 'objects': 'off'},
         'timing': {'faces_ms': 22.0, 'embed_ms': 31.0, 'objects_ms': 0.0, 'hz': 9.5}}
    d.update(over)
    return d


def _json_route(page, pattern, body_fn, counter=None):
    def handle(route):
        if counter is not None:
            counter.append(route.request.url)
        route.fulfill(status=200, content_type='application/json', body=json.dumps(body_fn()))
    page.route(pattern, handle)


def _pixel_at(page, x_frac, y_frac):
    """RGBA of the overlay canvas at a fraction of its size."""
    return page.evaluate("""([fx, fy]) => {
      const c = document.getElementById('cam-overlay');
      const d = c.getContext('2d').getImageData(Math.round(c.width * fx), Math.round(c.height * fy), 1, 1).data;
      return [d[0], d[1], d[2], d[3]];
    }""", [x_frac, y_frac])


def test_overlay_panels_and_polling():
    from playwright.sync_api import sync_playwright
    env = dict(os.environ, AGV_DASHBOARD_PORT=str(PORT), AGV_DASHBOARD_WS_PORT=str(WS_PORT))
    log = open(os.path.join(_env.TMP, 'web_cam_detect.out'), 'w')
    proc = subprocess.Popen([sys.executable, os.path.join(_env.REPO, 'web.py')],
                            cwd=_env.REPO, env=env, stdout=log, stderr=subprocess.STDOUT)
    try:
        def up():
            try:
                return urllib.request.urlopen(BASE, timeout=1).status == 200
            except Exception:
                return False
        assert _env.poll(up, 20), 'web.py did not start'

        errors, det_calls = [], []
        payload = _det()
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            try:
                page = browser.new_page(viewport={'width': 1200, 'height': 900})
                page.on('pageerror', lambda e: errors.append(f'pageerror: {e}'))
                page.on('console', lambda m: m.type == 'error' and not any(
                    k in m.text.lower() for k in ('favicon', '404', 'mjpeg', 'stream', 'websocket'))
                    and errors.append(f'console: {m.text}'))
                _json_route(page, '**/cam/status', lambda: _STATUS)
                _json_route(page, '**/cam/detections', lambda: payload, det_calls)
                _json_route(page, '**/api/cam/settings', lambda: _SETTINGS)
                _json_route(page, '**/api/cam/people', lambda: _PEOPLE)

                page.goto(BASE, wait_until='domcontentloaded')

                # ── a hidden tab does not wake the analytics worker ──
                assert page.is_hidden('#tab-cam')
                page.wait_for_timeout(1200)
                assert det_calls == [], f'polled /cam/detections with the tab closed: {len(det_calls)}'

                page.click('#tabbtn-cam')
                # Route handlers only run while Playwright is being driven, so a bare
                # poll on a list would never see them fire: pump the page while waiting.
                def pumped():
                    page.wait_for_timeout(100)
                    return len(det_calls) >= 2
                assert _env.poll(pumped, 6), 'no polling once the tab is open'

                # ── face list: names as text, similarity, unknown flagged ──
                assert _env.poll(lambda: page.locator('#cam-faces-now li').count() == 2, 6)
                rows = page.locator('#cam-faces-now li').all_inner_texts()
                assert 'Alice' in rows[0] and '0.71' in rows[0], rows
                assert 'Unknown' in rows[1] and '0.12' in rows[1], rows

                # ── canvas: a green box edge for the known face, red for the unknown ──
                # Frame 640x480 fills the 4:3 wrap, so frame (x, y) is (x/640, y/480) of the canvas.
                # Probe the top edge of each box (the 2 px stroke), away from the label chip.
                def edge(fx, fy):
                    return _pixel_at(page, fx / 640, fy / 480)
                assert _env.poll(lambda: edge(280, 120)[3] > 0, 6), 'nothing drawn for the known face'
                r, g, b, a = edge(300, 120)
                assert g > r + 60 and g > b, f'known face edge not green: {(r, g, b, a)}'
                r, g, b, a = edge(500, 300)
                assert r > g + 60 and r > b + 60, f'unknown face edge not red: {(r, g, b, a)}'
                assert _pixel_at(page, .05, .95)[3] == 0, 'canvas not transparent away from boxes'

                # ── stale detections are not drawn over a newer picture ──
                payload.update(age_ms=4000)
                assert _env.poll(lambda: edge(300, 120)[3] == 0, 6), 'drew a 4 s old box'
                payload.update(age_ms=80)

                # ── events + perf line ──
                assert _env.poll(lambda: 'Alice' in page.inner_text('#cam-events'), 6)
                assert '22' in page.inner_text('#cam-perf') and '9.5 Hz' in page.inner_text('#cam-perf')

                # ── roster ──
                assert _env.poll(lambda: page.locator('#cam-roster .cam-person').count() == 1, 6)
                assert 'Alice' in page.inner_text('#cam-roster') and '8 samples' in page.inner_text('#cam-roster')

                # ── names are text, never markup ──
                evil = '<img src=x onerror=window.__pwned=1>'
                payload['faces'][0]['name'] = evil
                payload['events'][0]['name'] = evil
                assert _env.poll(lambda: evil in page.inner_text('#cam-faces-now'), 6)
                assert page.evaluate('window.__pwned') is None, 'a name was executed as HTML'
                assert page.locator('#cam-faces-now img, #cam-events img').count() == 0
                payload['faces'][0]['name'] = 'Alice'
                payload['events'][0]['name'] = 'Alice'

                # ── enrolment progress ──
                payload['enroll'] = {'active': True, 'name': 'Carol', 'want': 8, 'got': 4,
                                     'hint': 'Turn your head slightly'}
                assert _env.poll(lambda: page.is_visible('#cam-enroll-bar'), 6)
                assert 'Carol: 4/8' in page.inner_text('#cam-enroll-msg')
                assert page.get_attribute('#cam-enroll-fill', 'style').replace(' ', '').startswith('width:50%')
                assert page.is_disabled('#cam-enroll-go') and page.is_visible('#cam-enroll-cancel')
                payload['enroll'] = {'active': False}
                assert _env.poll(lambda: not page.is_visible('#cam-enroll-bar'), 6)
                assert page.is_enabled('#cam-enroll-go')

                # ── controls reflect server settings ──
                assert page.get_attribute('#cam-t-faces', 'aria-pressed') == 'true'
                assert page.get_attribute('#cam-t-objects', 'aria-pressed') == 'false'
                assert page.input_value('#cam-thr') == '0.363'.rstrip('0') or abs(float(page.input_value('#cam-thr')) - 0.363) < 0.006

                # ── model error is surfaced, not swallowed ──
                payload['models'] = {'faces': 'OSError: model missing', 'objects': 'off'}
                assert _env.poll(lambda: page.is_visible('#cam-model-note'), 6)
                assert 'model missing' in page.inner_text('#cam-model-note')

                assert not errors, errors
            finally:
                browser.close()
    finally:
        proc.terminate()
        proc.wait(timeout=10)
        log.close()


if __name__ == '__main__':
    _env.run_tests(globals())
