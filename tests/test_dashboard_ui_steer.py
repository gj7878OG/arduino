"""Drive-tab steering sliders, in a real browser (Playwright).

The unit tests in test_steer_tuning.py prove the clamps and that the knobs
move the commanded turn. They cannot prove the wiring: that the slider the
operator actually drags reaches control.json, and that the value coming back
out of the server is what the slider then shows. A silently unwired slider
looks perfect right up until the rover refuses to change behaviour, which is
the exact failure this whole feature exists to fix.

So this drives the real page against a real web.py: drag each slider, read
control.json back off disk, and check the "applied:" readout agrees. It also
checks the browser raised no page errors, which is how a broken fetch would
otherwise show up as a slider that quietly does nothing.
"""
import _env  # noqa: F401  (must be first)
import json
import os
import subprocess
import sys
import urllib.request

import control_state

PORT, WS_PORT = 5087, 5088
BASE = f'http://127.0.0.1:{PORT}/'
CTL = os.path.join(_env.DASH_DIR, 'control.json')


def _poll(fn, timeout=8.0):
    return _env.poll(fn, timeout)


def _ctl():
    with open(CTL, 'r', encoding='utf-8') as fh:
        return json.load(fh)


def test_steering_sliders_reach_control_json():
    from playwright.sync_api import sync_playwright

    control_state.write_state(turn_floor_base=0, lookahead_m=1.0, path=CTL)

    env = dict(os.environ, AGV_DASHBOARD_PORT=str(PORT), AGV_DASHBOARD_WS_PORT=str(WS_PORT))
    log = open(os.path.join(_env.TMP, 'web_steer.out'), 'w')
    proc = subprocess.Popen([sys.executable, os.path.join(_env.REPO, 'web.py')],
                            cwd=_env.REPO, env=env, stdout=log, stderr=subprocess.STDOUT)
    try:
        def up():
            try:
                return urllib.request.urlopen(BASE, timeout=1).status == 200
            except Exception:
                return False
        assert _poll(up, 20), 'web.py did not start'

        errors = []
        with sync_playwright() as pw:
            browser = pw.chromium.launch(args=['--no-sandbox'])
            page = browser.new_page()
            page.on('pageerror', lambda e: errors.append(str(e)))
            page.goto(BASE, wait_until='domcontentloaded')
            _poll(lambda: page.locator('#floor-range').count() > 0, 10)

            # The page must pull the server's range, not trust the markup.
            # (The DOM normalises 1.00 to "1", so compare numerically.)
            assert page.locator('#floor-range').get_attribute('max') == '40'
            assert float(page.locator('#look-range').get_attribute('max')) == 1.0
            assert float(page.locator('#look-range').get_attribute('min')) == 0.30

            # Drag the minimum-turn floor and let the debounce fire.
            page.locator('#floor-range').fill('30')
            page.locator('#floor-range').dispatch_event('change')
            assert _poll(lambda: _ctl().get('turn_floor_base') == 30.0, 10), \
                f'floor slider did not reach control.json: {_ctl().get("turn_floor_base")}'

            # And the lookahead.
            page.locator('#look-range').fill('0.55')
            page.locator('#look-range').dispatch_event('change')
            assert _poll(lambda: _ctl().get('lookahead_m') == 0.55, 10), \
                f'lookahead slider did not reach control.json: {_ctl().get("lookahead_m")}'

            # Both values are independent: setting one must not disturb the other.
            assert _ctl().get('turn_floor_base') == 30.0

            # The "applied:" readouts agree with what is stored, so a second
            # browser (or a restart) shows the truth rather than a stale thumb.
            _poll(lambda: '30 PWM' in (page.locator('#floor-applied').inner_text() or ''), 10)
            assert '30 PWM' in page.locator('#floor-applied').inner_text()
            assert '0.55 m' in page.locator('#look-applied').inner_text()

            browser.close()

        assert not errors, f'page errors: {errors}'
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()