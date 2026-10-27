"""Drive-tab switch card (Playwright): one switch, two places.

The Drive tab lists every tick box together. The encoder-PID and detector-view
boxes still live where they are used (Bench, Record) — the Drive rows are not
copies of them but the same control: a click is forwarded to the original, which
owns the API call, and the original's state is pushed back out to the Drive row
on every telemetry frame. Two rows showing one setting must never disagree.

The PID row is tickable even when the running firmware reports no encoder data:
it is a saved preference, so the operator sets it before the encoders are there,
and the row says so in words instead of greying itself out. A row that the
original has genuinely locked (detector views while recording) must still refuse
a click rather than write to it.
"""
import _env
import json
import os
import subprocess
import sys
import urllib.request

import control_state

PORT, WS_PORT = 5079, 5080
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


def _status(encoders=True, closed_wanted=None, rec_on=False):
    """A status.json from a running AGV.

    ``closed_wanted`` is left out on purpose: that stands in for the dashboard
    running on its own, where nothing publishes the choice and the page has to
    read it back out of control.json.
    """
    d = {'running': True, 'enabled': False, 'mode': 'auto',
         'fw_closed_ready': encoders, 'wheel_closed_loop': encoders,
         'stm32_connected': encoders, 'camera_connected': True,
         'rec_on': rec_on}
    if closed_wanted is not None:
        d['closed_loop_wanted'] = closed_wanted
    return d


def test_drive_switches_mirror_their_originals():
    from playwright.sync_api import sync_playwright

    control_state.write_state(closed_loop=False, path=CTL)

    env = dict(os.environ, AGV_DASHBOARD_PORT=str(PORT), AGV_DASHBOARD_WS_PORT=str(WS_PORT))
    log = open(os.path.join(_env.TMP, 'web_switches.out'), 'w')
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
                page.wait_for_timeout(1500)

                # ── the Drive tab lists every on/off setting ──
                for box in ('obst-toggle', 'lidar-toggle', 'loop-drive', 'rec-views-drive'):
                    assert page.is_visible('#' + box), f'#{box} missing from the Drive tab'
                    assert page.is_visible(f'label:has(#{box})'), \
                        f'#{box} has no label to click'

                # ── no encoder data: tickable anyway, and it says why ──
                assert not page.is_checked('#loop-toggle'), \
                    'the saved open-loop choice was not shown'
                assert not page.is_disabled('#loop-toggle'), \
                    'the PID box greys itself out without live encoder data'
                assert not page.is_disabled('#loop-drive'), \
                    'the Drive PID row greys itself out without live encoder data'
                page.check('#loop-drive')
                assert _poll(lambda: control_state.read_state(CTL)['closed_loop'] is True, 5), \
                    'the Drive PID row could not be ticked without live encoder data'
                page.uncheck('#loop-drive')

                # ── with encoders: both tabs, both directions ──
                _atomic_json(STATUS, _status())
                assert _poll(lambda: not page.is_checked('#loop-toggle'), 8), \
                    'the Bench box lost the saved open-loop choice'
                assert _poll(lambda: not (page.text_content('#loop-noenc') or '').strip(), 8), \
                    'the no-encoder note is still up with encoder support reported'
                assert _poll(lambda: not (page.text_content('#loop-drive-noenc') or '').strip(), 5), \
                    'the Drive row did not clear the no-encoder note'

                page.check('#loop-drive')                     # Drive -> Bench, ticked
                assert _poll(lambda: control_state.read_state(CTL)['closed_loop'] is True, 5), \
                    'ticking on the Drive tab did not reach the control file'
                assert _poll(lambda: page.is_checked('#loop-toggle'), 5), \
                    'the Bench box did not follow the Drive tab'

                page.uncheck('#loop-drive')                   # Drive -> Bench, unticked
                assert _poll(lambda: control_state.read_state(CTL)['closed_loop'] is False, 5), \
                    'unticking on the Drive tab did not reach the control file'
                assert _poll(lambda: not page.is_checked('#loop-toggle'), 5), \
                    'the Bench box did not follow the Drive tab'

                page.click('#tabbtn-bench')
                page.check('#loop-toggle')                    # Bench -> Drive, ticked
                page.click('#tabbtn-drive')
                assert page.is_checked('#loop-drive'), \
                    'the Drive row did not follow the Bench tab'
                assert _poll(lambda: control_state.read_state(CTL)['closed_loop'] is True, 5)

                # ── the two labels track each other too ──
                assert page.text_content('#loop-drive-label') == page.text_content('#loop-label'), \
                    f'labels differ: {page.text_content("#loop-drive-label")!r} vs ' \
                    f'{page.text_content("#loop-label")!r}'

                page.uncheck('#loop-drive')
                assert _poll(lambda: not page.is_checked('#loop-toggle'), 5), \
                    'a telemetry frame dragged the Bench box away from the original'

                # ── detector views: same switch from the Drive tab ──
                page.check('#rec-views-drive')
                assert page.is_checked('#rec-views'), \
                    'the Record tab box did not follow the Drive tab'
                # Following the original does not wait for the next telemetry frame:
                # the click, the push and the read all happen in one JS turn, which no
                # socket frame can interleave with. Nothing in telemetry rewrites
                # #rec-views, so a Drive row that only synced on the render path would
                # still read stale here.
                followed = page.evaluate("""() => {
                    const src = document.getElementById('rec-views');
                    src.checked = false; src.dispatchEvent(new Event('change'));
                    return document.getElementById('rec-views-drive').checked;
                }""")
                assert followed is False, \
                    'the Drive row only follows the original on the next telemetry frame'
                page.click('#tabbtn-record')
                page.uncheck('#rec-views')
                page.click('#tabbtn-drive')
                assert _poll(lambda: not page.is_checked('#rec-views-drive'), 5), \
                    'the Drive row did not follow the Record tab'
                page.check('#rec-views-drive')
                page.wait_for_timeout(2500)                      # past the telemetry resync
                assert page.is_checked('#rec-views') and page.is_checked('#rec-views-drive'), \
                    'the two detector-view boxes disagree after a telemetry frame'

                # ── recording locks both rows, and a locked row writes nothing ──
                _atomic_json(STATUS, _status(rec_on=True))
                assert _poll(lambda: page.is_disabled('#rec-views-drive'), 8), \
                    'the Drive row stayed clickable while recording was running'
                assert page.is_disabled('#rec-views'), 'the Record row is not locked while recording'
                before = page.is_checked('#rec-views')
                page.evaluate("""() => {
                    const src = document.getElementById('rec-views');
                    const dst = document.getElementById('rec-views-drive');
                    dst.checked = !src.checked; dst.dispatchEvent(new Event('change'));
                }""")
                assert page.is_checked('#rec-views') == before, \
                    'the locked Drive row wrote to the original anyway'
                assert page.is_checked('#rec-views-drive') == before, \
                    'the locked Drive row kept a state the original refused'

                assert not errors, errors
            finally:
                browser.close()
    finally:
        proc.terminate()
        proc.wait(timeout=10)


if __name__ == '__main__':
    _env.run_tests(dict(globals()))