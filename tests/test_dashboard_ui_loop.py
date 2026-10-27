"""Dashboard closed-loop tick box (Playwright): the operator's choice sticks.

Untick "Closed loop" to compare raw PWM against the encoder PID, press STOP
(or reset the drive test) and the box used to re-tick itself within two
seconds — the server reset the preference to the config default on every
stop and mode change, and the page force-posted ``closed_loop: true`` on
every test reset. Both are gone; the box now shows the saved choice.

Runs with only web.py up, so this also covers the dashboard-alone case: with
no AGV publishing telemetry, the page has to read the choice out of
control.json instead of assuming "true".
"""
import _env
import json
import os
import subprocess
import sys
import urllib.request

import control_state

PORT, WS_PORT = 5077, 5078
BASE = f'http://127.0.0.1:{PORT}/'
CTL = os.path.join(_env.DASH_DIR, 'control.json')
STATUS = os.path.join(_env.DASH_DIR, 'status.json')


def _poll(fn, timeout=8.0):
    return _env.poll(fn, timeout)


def _atomic_json(path, obj):
    tmp = path + '.tmp'
    with open(tmp, 'w') as fh:
        json.dump(obj, fh)
    os.replace(tmp, path)


def _status():
    """A status.json from a running AGV: firmware reports closed-loop
    support, so the tick box is enabled and can be clicked.

    It deliberately does NOT carry closed_loop_wanted — this stands in for
    the dashboard being run on its own, where nothing publishes the choice
    and the page has to read it back out of control.json.
    """
    return {'running': True, 'enabled': False, 'mode': 'auto',
            'fw_closed_ready': True, 'wheel_closed_loop': True,
            'stm32_connected': True, 'camera_connected': True}


def test_closed_loop_choice_sticks():
    from playwright.sync_api import sync_playwright

    # yesterday's choice: open loop, left off before the motors were stopped
    control_state.write_state(closed_loop=False, path=CTL)

    env = dict(os.environ, AGV_DASHBOARD_PORT=str(PORT), AGV_DASHBOARD_WS_PORT=str(WS_PORT))
    log = open(os.path.join(_env.TMP, 'web_loop.out'), 'w')
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
            browser = pw.chromium.launch()
            try:
                page = browser.new_page()
                page.on('pageerror', lambda e: errors.append(f'pageerror: {e}'))
                page.on('console', lambda m: m.type == 'error' and not any(
                    k in m.text.lower() for k in ('favicon', '404', 'mjpeg', 'stream', 'websocket'))
                    and errors.append(f'console: {m.text}'))
                page.goto(BASE, wait_until='domcontentloaded')
                page.click('#tabbtn-bench')
                _atomic_json(STATUS, _status())

                # ── the saved choice is what the box shows ──
                assert _poll(lambda: not page.is_disabled('#loop-toggle'), 8), \
                    'tick box still disabled with encoder support reported'
                assert _poll(lambda: not page.is_checked('#loop-toggle'), 8), \
                    'the saved open-loop choice was not shown'

                # ── it can be changed, and the page follows its own choice ──
                page.check('#loop-toggle')
                assert _poll(lambda: control_state.read_state(CTL)['closed_loop'] is True, 5), \
                    'ticking the box did not reach the control file'
                page.wait_for_timeout(2500)          # past the 2 s telemetry resync
                assert page.is_checked('#loop-toggle'), 'the box did not hold its new choice'

                # ── resetting the drive test must not re-arm the PID ──
                # This button used to POST closed_loop:true on every press.
                page.evaluate("document.getElementById('dt-reset').click()")
                page.wait_for_timeout(2500)
                assert page.is_checked('#loop-toggle'), 'reset unticked the box'
                assert control_state.read_state(CTL)['closed_loop'] is True

                page.uncheck('#loop-toggle')
                assert _poll(lambda: control_state.read_state(CTL)['closed_loop'] is False, 5), \
                    'unticking the box did not reach the control file'
                page.wait_for_timeout(2500)
                assert not page.is_checked('#loop-toggle'), 'the box re-ticked itself'

                page.evaluate("document.getElementById('dt-reset').click()")
                page.wait_for_timeout(2500)
                assert not page.is_checked('#loop-toggle'), 'reset re-ticked the box'
                assert control_state.read_state(CTL)['closed_loop'] is False, \
                    'reset re-armed the encoder PID on disk'

                # ── a full page reload comes back to the same choice ──
                page.reload(wait_until='domcontentloaded')
                _atomic_json(STATUS, _status())
                assert _poll(lambda: not page.is_checked('#loop-toggle'), 8), \
                    'the choice did not survive a reload'
                assert not errors, errors
            finally:
                browser.close()
    finally:
        proc.terminate()
        proc.wait(timeout=10)


if __name__ == '__main__':
    import _env
    _env.run_tests(dict(globals()))