"""Whole-dashboard audit: every route, every front-end call, every control.

The other UI tests answer "did my change break something?". This one asks
"what was already broken?" — it walks the entire surface of the dashboard and
reports what it finds, so the answer stays true after every later change.

Two halves:

* **Static / in-process** (`web.flask_app.test_client()`, no server): every rule
  in ``url_map`` is requested and must not 5xx; every ``fetch``/``authFetch``
  path written in ``templates/`` is resolved against ``url_map``; every id the
  JavaScript reaches for is matched against the ids the page actually renders;
  every state-changing route is probed in both directions to record whether it
  asks for the password.

* **Live / browser** (own ``web.py`` on 5083/5084, fake hardware): every tab is
  reached three ways — clicking it, arrowing to it, and deep-linking it — and
  must come up with its panels actually populated; every button and input in
  every tab must have a listener (found by instrumenting
  ``addEventListener`` before the page's own scripts run, so delegation counts
  too); the summary strip and the seven tab dots are checked against three
  telemetry shapes.

Anything that would hang the run is skipped by name and *reported* as skipped,
never silently dropped: the MJPEG feeds are infinite generators, ``.tar`` and
``derived/<i>.jpg`` stream. A green run still writes the full inventory to
``docs/dashboard-audit.md``.

Reports are collected, not asserted. A check fails only on a defect it can
prove; everything else is passed to ``docs/dashboard-audit.md`` for a human.
"""
import _env  # noqa: F401  (must be first: pins the temp dir and the fake hardware)
import atexit
import contextlib
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request

import web

PORT, WS_PORT = 5083, 5084
BASE = f'http://127.0.0.1:{PORT}/'
DASH_DIR = _env.DASH_DIR
STATUS = os.path.join(DASH_DIR, 'status.json')
CTL = os.path.join(DASH_DIR, 'control.json')
TEMPLATES = os.path.join(_env.REPO, 'templates')
TAB_IDS = ['drive', 'bench', 'record', 'lidar', 'cam', 'voice', 'logs']

# The dashboard is one page made of many files. review.html and detections.html
# are pages of their own, with their own DOM, so they are audited for their
# calls (which must reach a route wherever they live) but not for their ids.
DASHBOARD_SOURCES = ['dashboard.html', '_auth_core.js', '_lidar_tab.html',
                     '_lidar_tab.js', '_cam_tab.html', '_cam_tab.js',
                     '_voice_tab.html', '_voice_tab.js']

# Streaming responses: they never terminate, so a probe would hang the run.
# Named here so the report can say "not probed" instead of dropping them.
UNPROBEABLE = {
    '/video_feed': 'infinite MJPEG generator',
    '/cam/mjpeg': 'infinite MJPEG generator',
    '/recordings/<name>.tar': 'streams a tar archive',
    '/api/recordings/<name>/derived/<int:i>.jpg': 'runs YOLO behind a semaphore',
}

# Ids the JavaScript reaches for that no longer exist in the page: leftovers of
# the summary-strip refactor. Listed, not silently tolerated — the audit fails
# on any *new* orphan and this list is the standing debt.
KNOWN_ORPHAN_IDS = {'track', 'camera-link', 'lidar-link', 'stm32-link',
                    'direction', 'lidar'}

# The house rule for state-changing routes (see api_enable, api_mode,
# api_obstacle, api_manual): an action that removes a safety layer needs the
# dashboard password; one that puts it back never does. Each entry is
# (route, safe body, escalating body, what the escalation buys).
AUTH_MATRIX = [
    ('/api/enable', {'enabled': False}, {'enabled': True}, 'starts the motors'),
    ('/api/mode', {'mode': 'auto'}, {'mode': 'manual'}, 'hands you the rover'),
    ('/api/obstacle', {'enabled': True}, {'enabled': False}, 'ignores the obstacle stop'),
    ('/api/lidar', {'enabled': True}, {'enabled': False}, 'disarms the LiDAR zones'),
    ('/api/manual', {'v': 0, 'w': 0}, {'v': 0.5, 'w': 0}, 'drives the wheels'),
    ('/api/loop', {'closed_loop': True}, {'closed_loop': False}, 'drops the encoder PID'),
    ('/api/speed', {'max_speed_pct': 60}, {'max_speed_pct': 100}, 'lifts the master speed cap'),
    ('/api/lidar', {'lidar_stop_dist_m': 1.2},
     {'lidar_stop_dist_m': 0.1}, 'shortens the LiDAR stop distance'),
]

# The six summary-strip chips that report a sensor verdict, and the telemetry
# fields each reads. Checked on text, colour class and tooltip.
STRIP_CHIPS = {
    'sum-arduino': 'stm32_connected',
    'sum-obst': 'obstacle_state',
    'sum-cam': 'camera_connected',
    'sum-usbcam': '/cam/status',
    'sum-lidar': 'lidar_action',
    'sum-rec': 'rec_on',
}

# Tab dots and the condition each is supposed to light on.
TAB_DOTS = {
    'dot-bench': 'encoder fault or the STM32 link is down',
    'dot-record': 'recording',
    'dot-lidar': 'LiDAR',
    'dot-cam': 'camera',
    'dot-voice': 'voice stack',
    'dot-logs': 'last_error',
}

_report = {
    'routes': [], 'unprobed': [], 'calls': [], 'orphan_ids': [],
    'dead_controls': [], 'mirrors': [], 'arguments': [], 'unverified': [],
    'auth': [], 'chips': [], 'dots': [],
    'tabs': [], 'console': [], 'notes': [],
}


def note(text):
    _report['notes'].append(text)


# ─────────────────────────────────────────────────────────────────
# Shared helpers
# ─────────────────────────────────────────────────────────────────

def _atomic_json(path, obj):
    tmp = path + '.tmp'
    with open(tmp, 'w') as fh:
        json.dump(obj, fh)
    os.replace(tmp, path)


def _status(**over):
    """A status.json from a running AGV. Defaults are a healthy rover."""
    d = {'running': True, 'enabled': True, 'mode': 'auto',
         'fw_closed_ready': True, 'wheel_closed_loop': True,
         'stm32_connected': True, 'stm32_firmware_version': 'v3',
         'camera_connected': True, 'camera_backend': 'argus',
         'camera_direction': 'STOP', 'obstacle_state': 'CLEAR',
         'obstacle_enforced': True, 'rec_on': False,
         'lidar_enabled': True, 'lidar_sensor_connected': True,
         'lidar_action': 'CLEAR', 'lidar_min_front_m': 2.4,
         'lidar_age_ms': 40, 'lidar_port': '/dev/ttyUSB0', 'lidar_baud': 115200,
         'wheel_meas_l_mps': 0.0, 'wheel_meas_r_mps': 0.0,
         'track_state': 'ON_LINE', 'last_error': None, 'logs': [],
         'sensor_probes': []}
    d.update(over)
    return d


def _template_text(name):
    with open(os.path.join(TEMPLATES, name), encoding='utf-8') as fh:
        return fh.read()


def _rendered_html():
    """The dashboard as the server actually renders it (includes expanded)."""
    c = web.flask_app.test_client()
    r = c.get('/')
    assert r.status_code == 200, f'GET / returned {r.status_code}'
    return r.get_data(as_text=True)


# ─────────────────────────────────────────────────────────────────
# 1. Every route answers
# ─────────────────────────────────────────────────────────────────

def _rules():
    return sorted(web.flask_app.url_map.iter_rules(), key=lambda r: str(r))


def _fill(rule):
    """Substitute a harmless value for every path parameter."""
    out = re.sub(r'<int:[^>]+>', '1', str(rule))
    out = re.sub(r'<path:[^>]+>', 'nope', out)
    return re.sub(r'<[^>]+>', 'nope', out)


def test_every_route_answers_without_a_server_error():
    """Ask every rule in url_map and require a clean answer.

    A 4xx is a fine answer — plenty of these routes are supposed to refuse an
    unauthenticated caller, and 404 is a correct answer for 'no such recording'.
    A 5xx is not: it means the handler blew up, which is a defect wherever it is.
    """
    c = web.flask_app.test_client()
    server_errors, skipped = [], []
    for rule in _rules():
        path = str(rule)
        methods = sorted(m for m in rule.methods if m not in ('HEAD', 'OPTIONS'))
        if path in UNPROBEABLE:
            skipped.append((path, UNPROBEABLE[path]))
            _report['unprobed'].append({'route': path, 'why': UNPROBEABLE[path]})
            continue
        for method in methods:
            kwargs = {}
            if method in ('POST', 'PUT'):
                kwargs = {'json': {}}
            try:
                r = c.open(_fill(rule), method=method, **kwargs)
                code = r.status_code
            except Exception as exc:                      # noqa: BLE001
                code = f'{type(exc).__name__}: {exc}'
            _report['routes'].append({'route': path, 'method': method, 'status': code})
            if isinstance(code, int) and 500 <= code < 600:
                server_errors.append(f'{method:5} {path} -> {code}')
    assert not server_errors, 'routes that raise:\n  ' + '\n  '.join(server_errors)
    assert skipped, 'the unprobeable list should be doing work'
    note(f'{len(_report["routes"])} route/method pairs probed, '
         f'{len(skipped)} routes not probed (streaming)')


# ─────────────────────────────────────────────────────────────────
# 2. Every front-end call reaches a real route
# ─────────────────────────────────────────────────────────────────

_TEMPLATED = re.compile(r'\$\{[^}]*\}')


def _fetch_calls(text):
    """Yield (path, whole) for every fetch/authFetch first argument.

    ``whole`` is False when the URL is assembled (``'/a/' + id + '/b'``): the
    leading literal is known, the rest is only known at run time. Those are
    reported as unverified rather than counted as resolved, so the check never
    claims to have checked something it did not.
    """
    for m in re.finditer(r"(?:authFetch|fetch)\(", text):
        i = m.end()
        while i < len(text) and text[i] in ' \n\t':
            i += 1
        if i >= len(text) or text[i] not in '`\'"':
            continue
        quote, i = text[i], i + 1
        j = i
        while j < len(text):
            if text[j] == '\\':
                j += 2
                continue
            if text[j] == quote:
                break
            j += 1
        literal = text[i:j]
        rest = text[j + 1:j + 40].lstrip()
        yield literal, not (rest.startswith('+') or rest.startswith('.')
                            or '${' in literal)


def _matching_rule(path):
    """The url_map rule a front-end path resolves to, or None.

    A relative path is resolved against the site root, which is what the
    browser does — but it only works while the page is served from '/', so the
    caller reports those separately.
    """
    clean = _TEMPLATED.sub('X', path).split('?')[0]
    if not clean.startswith('/'):
        clean = '/' + clean
    if clean.startswith('/static/'):
        return '/static/<path:filename>'
    for rule in _rules():
        rx = '^' + re.sub(r'<[^>]+>', r'[^/]+', str(rule)) + '$'
        if re.match(rx, clean):
            return str(rule)
    return None


def test_every_front_end_call_resolves_to_a_route():
    """A fetch to a path nothing serves is a 404 the user only sees in devtools.

    Literal and partial URLs are resolved against url_map. A URL built at run
    time is listed in the report as unverified — a check that quietly passed
    something it never looked at is worse than no check.
    """
    unresolved, relative, dynamic = [], [], []
    for name in sorted(os.listdir(TEMPLATES)):
        if not name.endswith(('.html', '.js')):
            continue
        for path, whole in _fetch_calls(_template_text(name)):
            entry = {'file': name, 'call': path, 'resolves_to': _matching_rule(path),
                     'verified': whole}
            _report['calls'].append(entry)
            if path.startswith(('http://', 'https://', 'ws://', 'ws:')):
                dynamic.append(entry)
                continue
            if not path.startswith('/'):
                relative.append(f'{name}: {path}')
                continue
            if not whole:
                dynamic.append(entry)
                continue
            if entry['resolves_to'] is None:
                unresolved.append(f'{name}: {path}')
    assert not unresolved, 'front-end calls with no route:\n  ' + '\n  '.join(unresolved)
    if relative:
        _report['notes'].append(
            'Fetches using a relative URL (they only work while the page is '
            'served from the site root): ' + '; '.join(relative))
    if dynamic:
        _report['notes'].append(
            f'{len(dynamic)} fetch calls build their URL at run time and were '
            'not verified: ' + ', '.join(sorted({c['call'][:40] for c in dynamic})))
    note(f'{len(_report["calls"])} fetch calls inspected: '
         f'{sum(1 for c in _report["calls"] if c["verified"])} fully literal, '
         f'{len(dynamic)} built at run time, {len(relative)} relative')


def test_get_routes_have_a_caller():
    """A GET nobody names is dead surface: more API than the page uses.

    Reported, not failed — an endpoint is sometimes there for curl, or for a
    future page. But it should be a deliberate choice, so it goes in the report.

    The scan is deliberately wide: every quoted path in ``templates/``, not just
    ``fetch`` arguments, because routes are also reached through ``src``,
    ``href``, ``window.open`` and ``url_for``. A wide scan under-reports dead
    routes rather than accusing a live one, which is the right way round.
    """
    called = set()
    sources = [_template_text(n) for n in sorted(os.listdir(TEMPLATES))
               if n.endswith(('.html', '.js'))]
    sources.append(_rendered_html())          # resolves {{ url_for(...) }}
    for text in sources:
        for raw in re.findall(r"['\"`](/[^'\"`\s]*)", text):
            clean = _TEMPLATED.sub('X', raw).split('?')[0]
            called.add(_matching_rule(clean) or clean)
    # Relative fetches resolve against the site root.
    for entry in _report['calls']:
        if not entry['call'].startswith('/') and not entry['call'].startswith(('http', 'ws')):
            clean = _TEMPLATED.sub('X', entry['call']).split('?')[0]
            called.add(_matching_rule('/' + clean) or '/' + clean)
    dead = []
    for r in _rules():
        path = str(r)
        if 'GET' not in r.methods or path.startswith('/static') or path == '/':
            continue
        if path in called:
            continue
        # Also clear routes reached by gluing a prefix to a variable, the way
        # review.html does: const api = `/api/recordings/${NAME}/` then
        # `${api}meta`. Matching on the part of the rule before its first
        # placeholder clears those; it can also clear a route the page does not
        # actually use, which under-reports rather than accuses.
        head = path.split('<')[0]
        if any(c.startswith(head) for c in called if head and len(head) > 6):
            continue
        dead.append(path)
    for path in sorted(dead):
        _report['notes'].append(f'GET {path} is never named in templates/')
    note(f'{len(dead)} GET routes are never named by any template')


# ─────────────────────────────────────────────────────────────────
# 3. The page and the script agree on what exists
# ─────────────────────────────────────────────────────────────────

_ID_RE = re.compile(
    r"\$\(\s*'([A-Za-z0-9_-]+)'\s*\)"
    r"|getElementById\(\s*'([A-Za-z0-9_-]+)'\s*\)"
    # the helpers that write to an element by id
    r"|(?:setText|setConnection|chip)\(\s*'([A-Za-z0-9_-]+)'\s*,")


def test_every_id_the_dashboard_script_reaches_for_exists_in_the_page():
    """Match the ids the dashboard's own JavaScript writes to against the ids
    the server actually renders.

    Includes are expanded by Jinja before this runs, so a partial that forgets
    an id fails here rather than in a browser at 2 a.m. review.html and
    detections.html are separate pages with their own DOM and are not part of
    this comparison.
    """
    declared = set(re.findall(r'\bid="([^"]+)"', _rendered_html()))
    referenced = set()
    for name in DASHBOARD_SOURCES:
        for group in _ID_RE.findall(_template_text(name)):
            referenced.add(next(g for g in group if g))
    orphans = sorted(referenced - declared)
    _report['orphan_ids'] = [
        {'id': o, 'known': o in KNOWN_ORPHAN_IDS} for o in orphans]
    fresh = [o for o in orphans if o not in KNOWN_ORPHAN_IDS]
    assert not fresh, ('the dashboard script writes to ids the page does not '
                       'render: ' + ', '.join(fresh))
    known = [o for o in orphans if o in KNOWN_ORPHAN_IDS]
    if known:
        note('ids written but never rendered — leftovers of the summary-strip '
             'refactor, null-guarded and safe to delete: ' + ', '.join(known))
    note(f'{len(declared)} ids rendered, {len(referenced)} named by the script')


# ─────────────────────────────────────────────────────────────────
# 4. The password rule holds
# ─────────────────────────────────────────────────────────────────

def test_state_changing_routes_follow_the_password_rule():
    """Probe every state-changing route in both directions.

    The house rule is that removing a safety layer needs the dashboard password
    and putting it back never does. This records what each route actually does;
    a route that accepts the escalating call without credentials is reported.
    """
    c = web.flask_app.test_client()
    ungated = []
    for route, safe_body, esc_body, what in AUTH_MATRIX:
        for label, body in (('safe', safe_body), ('escalating', esc_body)):
            web._fail_times.clear()      # these probes are unauthenticated on purpose
            try:
                r = c.post(route, json=body)
                code = r.status_code
            except Exception as exc:                      # noqa: BLE001
                code = f'{type(exc).__name__}: {exc}'
            # 429 means the password check did run and the attempt was counted;
            # it is a refusal, not an open door.
            gated = code in (401, 403, 429)
            entry = {'route': route, 'direction': label, 'status': code,
                     'gated': gated, 'buys': what}
            _report['auth'].append(entry)
            if label == 'escalating' and not gated:
                ungated.append(f'POST {route} {body} -> {code} '
                               f'(no password asked; it {what})')
    web._fail_times.clear()
    if ungated:
        _report['notes'].append('Escalating calls accepted without the password:')
        _report['notes'].extend('  ' + u for u in ungated)
    note(f'{len(AUTH_MATRIX)} state-changing routes probed in both directions '
         f'({len(ungated)} accept the escalating call with no password)')


# ─────────────────────────────────────────────────────────────────
# Browser half
# ─────────────────────────────────────────────────────────────────

_PROBE_LISTENERS = """
window.__listeners = [];
const _add = EventTarget.prototype.addEventListener;
EventTarget.prototype.addEventListener = function (type, fn, opts) {
  try {
    if (this && this.nodeType === 1) {
      window.__listeners.push([this.id
        || this.tagName.toLowerCase()
        + (this.className ? '.' + String(this.className).split(' ')[0] : ''), type]);
    } else if (this === window || this === document || this === document.body) {
      window.__listeners.push(['@@' + (this === window ? 'window'
        : this === document ? 'document' : 'body'), type]);
    }
  } catch (e) { /* auditing must never break the page */ }
  return _add.call(this, type, fn, opts);
};
"""


class _Server:
    """web.py on 5083/5084, fake hardware, its own temp telemetry dir."""

    def __enter__(self):
        env = dict(os.environ, AGV_DASHBOARD_PORT=str(PORT),
                   AGV_DASHBOARD_WS_PORT=str(WS_PORT))
        log = open(os.path.join(_env.TMP, 'web_audit.out'), 'w')
        self.proc = subprocess.Popen(
            [sys.executable, os.path.join(_env.REPO, 'web.py')],
            cwd=_env.REPO, env=env, stdout=log, stderr=subprocess.STDOUT)
        self.log = log

        def up():
            try:
                return urllib.request.urlopen(BASE, timeout=1).status == 200
            except Exception:                              # noqa: BLE001
                return False
        assert _env.poll(up, 25), 'web.py did not start on %d' % PORT
        return self

    def __exit__(self, *exc):
        self.proc.terminate()
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()
        self.log.close()
        return False


def _open_page(browser, errors, url=BASE, stub=None):
    page = browser.new_page()
    page.on('pageerror', lambda e: errors.append(f'pageerror: {e}'))
    page.on('console', lambda m: m.type == 'error' and not any(
        k in m.text.lower() for k in ('favicon', '404', 'mjpeg', 'stream', 'websocket'))
        and errors.append(f'console: {m.text}'))
    page.add_init_script(_PROBE_LISTENERS)
    if stub:
        page.route('**/api/cam/people', lambda route: route.fulfill(
            status=200, content_type='application/json', body=json.dumps({'people': []})))
    page.goto(url, wait_until='domcontentloaded')
    page.wait_for_timeout(1200)
    return page


def test_every_tab_opens_by_click_arrow_and_deep_link():
    """Reach all seven tabs three ways and require each to come up.

    A tab that shows its panel but never fetches its data is the failure this
    exists for: it looks fine and is empty. (Whether it loaded its data is
    checked separately, per tab, in the tests below.)
    """
    from playwright.sync_api import sync_playwright

    errors = []
    with _Server(), sync_playwright() as pw:
        browser = pw.chromium.launch()
        try:
            # by clicking the button
            page = _open_page(browser, errors)
            try:
                for t in TAB_IDS:
                    page.click(f'#tabbtn-{t}')
                    page.wait_for_timeout(600)
                    assert not page.is_hidden(f'#tab-{t}'), f'click: #{t} did not open'
                    _report['tabs'].append({'tab': t, 'via': 'click'})
            finally:
                page.close()

            # by arrowing along the tablist
            page = _open_page(browser, errors, url=f'{BASE}#drive')
            try:
                page.focus('#tabbtn-drive')
                for i, t in enumerate(TAB_IDS[1:]):
                    page.keyboard.press('ArrowRight')
                    page.wait_for_timeout(600)
                    assert not page.is_hidden(f'#tab-{t}'), \
                        f'ArrowRight x{i + 1} should have reached {t}, not left it hidden'
                    _report['tabs'].append({'tab': t, 'via': 'arrow'})
            finally:
                page.close()

            # by arriving on a deep link
            for t in TAB_IDS:
                page = _open_page(browser, errors, url=f'{BASE}#{t}')
                try:
                    page.wait_for_timeout(1200)
                    assert not page.is_hidden(f'#tab-{t}'), f'deep link #{t} did not open'
                    _report['tabs'].append({'tab': t, 'via': 'hash'})
                finally:
                    page.close()
        finally:
            browser.close()
    assert not errors, errors


def test_the_logs_tab_loads_its_panels_on_arrival():
    """The Logs tab must fetch its data the moment it is shown, however it is
    reached.

    It has five cards that all start empty; they only populate when
    ``_ensureLogsInited()`` runs. If that is wired to the button's click event
    alone, then arriving from a bookmark or walking in with the arrow keys shows
    the tab with nothing in it — which reads as "the logger is broken", and no
    error appears anywhere to say otherwise.

    The arrow-key and button cases deliberately load the page on Drive first:
    a page that is *born* on Logs is initialised by the deep-link guard at the
    end of the script, which would hide a button-only wiring.
    """
    from playwright.sync_api import sync_playwright

    errors = []
    with _Server(), sync_playwright() as pw:
        browser = pw.chromium.launch()
        try:
            for how, start in (('deep link', '#logs'), ('arrow keys', '#drive'),
                               ('button', '#drive')):
                page = _open_page(browser, errors, url=BASE + start)
                try:
                    asked = []
                    page.on('request', lambda r: asked.append(r.url))
                    page.wait_for_timeout(500)
                    asked.clear()          # forget what page load itself did
                    if how == 'arrow keys':
                        page.focus('#tabbtn-drive')
                        for _ in range(TAB_IDS.index('logs')):
                            page.keyboard.press('ArrowRight')
                            page.wait_for_timeout(150)
                    elif how == 'button':
                        page.click('#tabbtn-logs')
                    page.wait_for_timeout(2500)
                    assert not page.is_hidden('#tab-logs'), f'{how}: #logs never opened'
                    assert any('/api/livelog' in u for u in asked), (
                        f'reached the Logs tab by {how} but never asked for '
                        f'/api/livelog — its five panels stay empty. '
                        f'Requests seen: {asked}')
                    _report['notes'].append(f'Logs tab reached by {how}: panels loaded')
                finally:
                    page.close()
        finally:
            browser.close()
    assert not errors, errors


_PROP_USE = re.compile(
    r"(?:\$\(\s*'([A-Za-z0-9_-]+)'\s*\)|getElementById\(\s*'([A-Za-z0-9_-]+)'\s*\))"
    r"\s*\.\s*(value|checked|disabled|textContent|innerHTML|className|hidden)"
    r"\s*(=?)")


def _property_use():
    """Split the ids the script touches into ones it reads and ones it writes.

    ``$('rec-rate').value = 25`` writes; ``Number($('rec-rate').value)`` reads.
    A control whose value is read is an argument to some sibling's handler —
    a speed slider has no listener of its own, and must not be asked for one.
    """
    reads, writes = set(), set()
    for name in DASHBOARD_SOURCES:
        for a, b, prop, assign in _PROP_USE.findall(_template_text(name)):
            (writes if assign else reads).add(a or b)
    return reads, writes


def test_every_control_does_something():
    """Every button must do something when pressed, and every other control
    must be read or written by the script.

    ``addEventListener`` is instrumented before the page's own scripts run, so a
    handler bound to an ancestor counts for its children (``#tabs`` delegates
    every tab button). A ``<button>`` with no reachable handler is the expensive
    kind of bug, because it looks finished.

    Buttons are held to that bar unconditionally. Inputs, selects and
    checkboxes are not, because they do not always act alone: a speed slider or
    a duration ``<select>`` is an argument that the start button beside it reads,
    and a checkbox on the LiDAR tab only mirrors the switch that lives on Drive.
    Those are bucketed by what the source does with them and listed in the
    report, so the judgement is visible rather than hidden in the check.

    The test refuses to pass if the instrumentation caught nothing at all, so a
    broken hook cannot quietly approve every control on the page.
    """
    from playwright.sync_api import sync_playwright

    BUBBLES = ('click', 'input', 'change', 'submit', 'keydown', 'keyup',
               'pointerdown', 'pointerup', 'mousedown', 'mouseup', 'touchstart',
               'touchend', 'dblclick', 'contextmenu', 'wheel', 'focus', 'blur')

    reads, writes = _property_use()
    dead, mirrors, args, unverified = [], [], [], []
    errors = []
    with _Server(), sync_playwright() as pw:
        browser = pw.chromium.launch()
        try:
            page = _open_page(browser, errors)
            try:
                hooked = page.evaluate('() => window.__listeners.length')
                assert hooked > 40, (
                    f'only {hooked} listeners were recorded — the '
                    f'instrumentation is not working, so this check would '
                    f'approve every control on the page')
                for t in TAB_IDS:
                    page.click(f'#tabbtn-{t}')
                    page.wait_for_timeout(400)
                    found = page.evaluate("""(tab) => {
                      const key = (el) => el.id
                        || el.tagName.toLowerCase()
                        + (el.className ? '.' + String(el.className).split(' ')[0] : '');
                      const onEl = {}, onDoc = new Set();
                      for (const l of window.__listeners) {
                        if (l[0] === '@@') onDoc.add(l[1] + '|' + l[2]);
                        else (onEl[l[0]] ||= new Set()).add(l[1]);
                      }
                      const BUBBLES = %s;
                      const out = [];
                      for (const el of document.querySelectorAll(
                            '#tab-' + tab + ' button, #tab-' + tab
                            + ' input, #tab-' + tab + ' select')) {
                        let hit = false;
                        for (let p = el; p && !hit; p = p.parentElement) {
                          const types = onEl[key(p)];
                          if (types) for (const ty of types)
                            if (BUBBLES.includes(ty)) { hit = true; break; }
                        }
                        if (!hit) for (const b of BUBBLES)
                          if (onDoc.has('document|' + b) || onDoc.has('window|' + b)) {
                            hit = true; break;
                          }
                        if (!hit) out.push({id: el.id, key: key(el), tag: el.tagName,
                                            type: el.type || '',
                                            label: (el.textContent || '').trim().slice(0, 40)});
                      }
                      return out;
                    }""" % json.dumps(list(BUBBLES)), t)
                    for c in found:
                        c['tab'] = t
                        if c['tag'] == 'BUTTON':
                            dead.append(c)          # a button must act on its own
                        elif c['id'] in reads:
                            args.append(c)          # a parameter a sibling reads
                        elif c['id'] in writes:
                            mirrors.append(c)        # a read-only display
                        else:
                            # The static scan only sees `$('id').prop`. A few
                            # places cache the element in a local first
                            # (`const here = $('x'); here.checked = ...`), so
                            # they read as untouched when they are not.
                            unverified.append(c)
                _report['dead_controls'] = dead
                _report['mirrors'] = mirrors
                _report['arguments'] = args
                _report['unverified'] = unverified
                note(f'{hooked} event listeners bound across the page: '
                     f'{len(dead)} buttons do nothing, {len(args)} controls are '
                     f'arguments read by a sibling, {len(mirrors)} are '
                     f'read-only mirrors, {len(unverified)} could not be '
                     f'judged from the source')
            finally:
                page.close()
        finally:
            browser.close()
    if dead:
        lines = '\n  '.join(f"#{c['id'] or c['key']} ({c['tab']}) — {c['label']!r}"
                            for c in dead)
        raise AssertionError('buttons with no event handler anywhere:\n  ' + lines)
    assert not errors, errors


def test_the_strip_reports_every_sensor_correctly():
    """Each sensor chip must show the right text, colour and tooltip.

    Three telemetry shapes: everything healthy, everything down, and a mixed
    one where the rover is fine but the LiDAR has something in front of it.
    Text alone is not enough — a chip reading CLEAR in amber would look safe.
    """
    from playwright.sync_api import sync_playwright

    errors = []
    with _Server(), sync_playwright() as pw:
        browser = pw.chromium.launch()
        try:
            page = _open_page(browser, errors)
            try:
                def chips():
                    return page.evaluate("""() => {
                      const out = {};
                      for (const id of %s) {
                        const el = document.getElementById(id);
                        out[id] = el ? {v: el.querySelector('.v').textContent.trim(),
                                        cls: el.className, title: el.title} : null;
                      }
                      return out;
                    }""" % json.dumps(list(STRIP_CHIPS)))

                cases = [
                    ('all healthy', _status(),
                     {'sum-arduino': ('good',), 'sum-obst': ('good', 'CLEAR'),
                      'sum-cam': ('good', 'OK'), 'sum-lidar': ('good', 'CLEAR')}),
                    ('everything down',
                     _status(stm32_connected=False, camera_connected=False,
                             lidar_enabled=False, lidar_sensor_connected=False,
                             obstacle_state='BLOCKED'),
                     {'sum-arduino': ('bad', 'DOWN'), 'sum-obst': ('bad', 'BLOCKED'),
                      'sum-cam': ('bad', 'NONE'), 'sum-lidar': ('warn', 'OFF')}),
                    ('obstacle ahead',
                     _status(lidar_action='ESTOP', lidar_min_front_m=0.35,
                             lidar_estop_reason='front obstacle'),
                     {'sum-lidar': ('bad', 'ESTOP'),
                      'sum-arduino': ('good',), 'sum-obst': ('good', 'CLEAR')}),
                ]
                for label, status, expected in cases:
                    _atomic_json(STATUS, status)
                    got = _env.poll(lambda: _settled(chips, expected), 8)
                    assert got, (f'strip did not settle for "{label}".\n'
                                 f'  got: {chips()}\n  want: {expected}')
                    live = chips()
                    for chip, (cls, *texts) in expected.items():
                        c = live[chip]
                        assert c is not None, f'{label}: #{chip} is missing'
                        assert cls in c['cls'], (
                            f'{label}: #{chip} class {c["cls"]!r} should carry {cls!r}')
                        for t in texts:
                            assert t in c['v'], (
                                f'{label}: #{chip} reads {c["v"]!r}, expected {t!r}')
                    _report['chips'].append({'case': label,
                                             'seen': {k: live[k]['v'] for k in expected}})
            finally:
                page.close()
        finally:
            browser.close()
    assert not errors, errors


def _settled(read, expected):
    """True once every expected chip shows the right class (and value, if given)."""
    try:
        live = read()
    except Exception:                                      # noqa: BLE001
        return None
    for chip, (cls, *texts) in expected.items():
        c = live.get(chip)
        if not c or cls not in c['cls']:
            return None
        if any(t not in c['v'] for t in texts):
            return None
    return live


def test_every_tab_dot_has_something_to_say():
    """Each tab dot must light when its tab has something to report.

    A dot that is declared in the markup but never written by any updater can
    never light: the tab looks like every other quiet one even when the sensor
    behind it is down. ``#dot-cam`` was exactly that.
    """
    from playwright.sync_api import sync_playwright

    errors = []
    with _Server(), sync_playwright() as pw:
        browser = pw.chromium.launch()
        try:
            page = _open_page(browser, errors)
            try:
                page.evaluate("""() => {
                  window.__lit = () => Object.fromEntries(
                    %s.map(id => [id, document.getElementById(id).className]));
                }""" % json.dumps(list(TAB_DOTS)))

                def lit():
                    return page.evaluate('() => window.__lit()')

                quiet = _status()
                _atomic_json(STATUS, quiet)
                _env.poll(lambda: _dots_match(lit(), quiet), 8)
                before = lit()

                loud = _status(enc_fault_l=True, rec_on=True,
                               lidar_action='ESTOP', last_error='cam timeout',
                               camera_connected=False)
                _atomic_json(STATUS, loud)
                after = _env.poll(lambda: _dots_match(lit(), loud), 10)
                assert after, ('the tab dots did not settle for a rover with an '
                               f'encoder fault, a live recording, an ESTOP, a '
                               f'dead camera and a last error. Still: {lit()}')
                for dot, why in TAB_DOTS.items():
                    _report['dots'].append({'dot': dot, 'means': why,
                                            'quiet': before[dot], 'alarmed': after[dot]})
                unlit = [d for d, why in TAB_DOTS.items()
                         if before[d] != after[d]]
                assert len(unlit) >= 4, (
                    f'only {unlit} of {len(TAB_DOTS)} tab dots responded to a rover '
                    f'in trouble; the rest can never light up')
            finally:
                page.close()
        finally:
            browser.close()
    assert not errors, errors


def _dots_match(live, status):
    """Every dot agrees with the status it is supposed to mirror."""
    want_lit = {
        'dot-bench': bool(status.get('enc_fault_l') or status.get('enc_fault_r')
                          or status.get('stm32_connected') is False),
        'dot-record': bool(status.get('rec_on')),
        'dot-lidar': status.get('lidar_action') in ('ESTOP', 'SLOW')
                     or status.get('lidar_enabled') is False,
        'dot-cam': status.get('camera_connected') is False,
        'dot-voice': False,                     # voice has its own publisher
        'dot-logs': bool(status.get('last_error')),
    }
    for dot, on in want_lit.items():
        if on != ('on' in live.get(dot, '')):
            return None
    return live


def test_make_everything_safe_puts_the_rover_back_in_a_safe_state():
    """One click must re-arm both obstacle layers and cut the wheels.

    The button is the counterpart to arming the rover by hand: without it,
    getting back to a known-safe state is four separate switches and it is
    easy to leave one of them off. It is deliberately the *un*-gated direction
    — every one of the calls it makes only ever adds protection, so it asks
    for no password (the same rule ``/api/obstacle`` already follows).
    """
    from playwright.sync_api import sync_playwright

    errors = []
    with _Server(), sync_playwright() as pw:
        browser = pw.chromium.launch()
        try:
            page = _open_page(browser, errors)
            try:
                # Put control.json into the worst state the button exists for:
                # both safety layers off, motors enabled, and a speed cap well
                # above the configured one. Recording is left off — the audit
                # has no recorder to stop, and a stop it cannot see is not a
                # defect in the button.
                ctrl = json.load(open(CTL))
                ctrl.update(obstacle_stop=False, lidar_enabled=False,
                            enabled=True, max_speed_pct=100)
                _atomic_json(CTL, ctrl)
                _atomic_json(STATUS, _status(obstacle_enforced=False,
                                             lidar_enabled=False,
                                             max_speed_pct=100))
                _env.poll(lambda: not page.is_checked('#obst-toggle'), 8)

                # The cap the button must land on: the configured one, rounded
                # DOWN to a step the slider can actually hold.
                cfg_cap = int(page.get_attribute('#speed-range', 'data-cap'))
                step = int(page.get_attribute('#speed-range', 'step'))
                lo = int(page.get_attribute('#speed-range', 'min'))
                cap = lo + ((cfg_cap - lo) // step) * step

                page.click('#safe-all')

                # Wait on the *server*, not on the tick boxes: makeSafe checks
                # the boxes itself before it has posted anything, so the DOM
                # settles a round trip early.
                def _safe_on_disk():
                    s = json.load(open(CTL))
                    return (s.get('obstacle_stop') is True
                            and s.get('lidar_enabled') is True
                            and s.get('enabled') is False
                            and s.get('max_speed_pct') <= cap)
                assert _env.poll(_safe_on_disk, 12), (
                    '"Make everything safe" left the rover unsafe: '
                    + json.dumps({k: json.load(open(CTL)).get(k) for k in
                                  ('obstacle_stop', 'lidar_enabled', 'enabled', 'max_speed_pct')})
                    + f' (the cap it must land on or below is {cap})')
                _env.poll(lambda: page.is_checked('#obst-toggle')
                          and page.is_checked('#lidar-toggle'), 6)
                assert 'safe:' in page.inner_text('#safe-all-msg'), \
                    f'the button did not say what it did: {page.inner_text("#safe-all-msg")!r}'

                # Pressing it again must leave the rover just as safe: a
                # button whose whole job is "get to a known-safe state" is
                # worthless if a second press can undo the first.
                page.click('#safe-all')
                _env.poll(lambda: 'safe' in page.inner_text('#safe-all-msg'), 8)
                assert _env.poll(_safe_on_disk, 6), (
                    'a second press on "Make everything safe" undid the first: '
                    + json.dumps({k: json.load(open(CTL)).get(k) for k in
                                  ('obstacle_stop', 'lidar_enabled', 'enabled', 'max_speed_pct')}))
                _report['notes'].append(
                    '"Make everything safe": both layers re-armed, motors cut, '
                    f'speed cap clamped to {cap}%, and a second press leaves it there.')
            finally:
                page.close()
        finally:
            browser.close()
    assert not errors, errors


def test_an_auto_drive_records_itself_and_asks_what_to_do_with_it():
    """Ticking "record every AUTO drive" must record the drive and then ask.

    Three things are being pinned down here, and each was a way this could be
    quietly wrong:

    * the trigger. Recording has to start when the rover *actually starts
      moving* (AUTO **and** motors on) — not when AUTO is merely selected, and
      not while the rover sits on the bench with the motors off.
    * who stops it. A recording this page started by itself it must also stop
      by itself; one the operator started by hand it must never stop.
    * the prompt. It has to appear, name the session it is about, and default
      to keeping it — the frames are already on disk, and Delete is the only
      irreversible choice on the screen.
    """
    from playwright.sync_api import sync_playwright

    errors = []
    with _Server(), sync_playwright() as pw:
        browser = pw.chromium.launch()
        try:
            page = _open_page(browser, errors)
            try:
                # Every /api/record POST the page makes, so "it did not
                # record" is provable rather than assumed.
                posted = []
                page.on('request', lambda r: posted.append((r.url, r.post_data or ''))
                        if '/api/record' in r.url else None)

                def actions():
                    """The `action` of every /api/record call the page has made."""
                    return [json.loads(b).get('action') for _, b in posted
                            if b.startswith('{')]

                @contextlib.contextmanager
                def rec_post(action, seconds=15):
                    """Arm, do the thing inside the block, wait for that POST.

                    Waiting on ``actions()`` in a poll loop does not work: the
                    loop never returns to the Playwright driver, so the
                    request event is never dispatched and the list stays empty
                    however long you wait. ``expect_request`` blocks in the
                    driver, which is what actually pumps it.
                    """
                    with page.expect_request(
                            lambda r: '/api/record' in r.url
                            and json.loads(r.post_data or '{}').get('action') == action,
                            timeout=seconds * 1000):
                        yield

                # 1. off by default, and the switch reaches the server
                assert not page.is_checked('#rec-auto'), \
                    'auto-record is on before anyone asked for it'
                _atomic_json(STATUS, _status(mode='auto', enabled=False))
                page.check('#rec-auto')
                assert _env.poll(lambda: json.load(open(CTL)).get('record_auto') is True, 6), \
                    'the auto-record switch never reached control.json'
                page.wait_for_timeout(1200)
                assert 'start' not in actions(), \
                    f'recorded with the switch off: {actions()}'

                # 2. AUTO selected but the motors off: still nothing. This is
                #    the bench-idle case that a naive `mode === auto` trigger
                #    gets wrong.
                _atomic_json(STATUS, _status(mode='auto', enabled=False))
                page.wait_for_timeout(1500)
                assert 'start' not in actions(), \
                    f'recorded a drive that never started: {actions()}'

                # 3. actually driving: it must record, on its own
                with rec_post('start'):
                    _atomic_json(STATUS, _status(mode='auto', enabled=True))
                assert 'Recording now' in page.inner_text('#rec-auto-why'), \
                    'it recorded a drive the page did not think was running'

                # 4. end the drive. The page has to have actually seen the
                #    recording running before it can stop it, so put rec_on up
                #    first and wait for the chip rather than racing two writes.
                _atomic_json(STATUS, _status(mode='auto', enabled=True, rec_on=True,
                                             rec_session='20260101_120000', rec_frames=42,
                                             rec_mb=3.5, rec_elapsed_s=3.0))
                assert _env.poll(lambda: page.inner_text('#rec-chip').startswith('\u25cf REC'), 12), \
                    f'the page never registered the running recording: {page.inner_text("#rec-chip")!r}'
                with rec_post('stop'):
                    _atomic_json(STATUS, _status(mode='auto', enabled=False, rec_on=False,
                                                 rec_session='20260101_120000',
                                                 rec_frames=42, rec_mb=3.5,
                                                 rec_elapsed_s=3.0))
                assert _env.poll(lambda: page.is_visible('#rec-done'), 10), \
                    'the drive ended and no save-or-delete prompt appeared'
                assert page.inner_text('#rd-session') == '20260101_120000', \
                    f'the prompt is about the wrong session: {page.inner_text("#rd-session")!r}'
                assert page.inner_text('#rd-frames') == '42', \
                    f'the prompt lost the frame count: {page.inner_text("#rd-frames")!r}'
                assert page.inner_text('#rd-size').startswith('3.5 MB'), \
                    f'the prompt lost the size: {page.inner_text("#rd-size")!r}'

                assert not page.is_disabled('#rd-del'), \
                    'Delete should be offered on a drive that actually recorded frames'

                # 5. dismissing keeps it. Esc closes the dialog and the session
                #    is still on disk — losing a drive to a stray keypress is
                #    the failure this prompt is designed around. The Delete
                #    request must go out only on an explicit click, so pressing
                #    Escape leaves the folder alone.
                page.keyboard.press('Escape')
                assert _env.poll(lambda: not page.is_visible('#rec-done'), 5), \
                    'Escape did not dismiss the prompt'
                page.wait_for_timeout(600)
                assert not [r for r, _ in posted if '/api/recordings/' in r and r.endswith('.json')], \
                    'dismissing the prompt deleted the recording anyway'

                _report['notes'].append(
                    'Auto-record: off by default, starts on AUTO+motors, stops when '
                    'the drive ends, prompts with session/frames/size, dismiss = keep.')
            finally:
                page.close()
        finally:
            browser.close()
    assert not errors, errors


# ─────────────────────────────────────────────────────────────────
# The report
# ─────────────────────────────────────────────────────────────────

def _render_report():
    L = []
    a = L.append
    a('# Dashboard audit\n')
    a('Generated by `tests/test_dashboard_audit.py`. Regenerate with:\n')
    a('```\npython3 tests/test_dashboard_audit.py\n```\n')
    if _report['notes']:
        a('## Summary\n')
        for n in _report['notes']:
            a(f'- {n}')
        a('')

    a('## Routes\n')
    a('| Route | Method | Status |')
    a('|---|---|---|')
    for r in sorted(_report['routes'], key=lambda x: (x['route'], x['method'])):
        a(f"| `{r['route']}` | {r['method']} | {r['status']} |")
    a('')
    if _report['unprobed']:
        a('Not probed (these never terminate):\n')
        for u in _report['unprobed']:
            a(f"- `{u['route']}` — {u['why']}")
        a('')

    a('## Front-end calls\n')
    if _report['calls']:
        a('| File | Call | Resolves to |')
        a('|---|---|---|')
        for c in sorted(_report['calls'], key=lambda x: x['call']):
            a(f"| {c['file']} | `{c['call']}` | {c['resolves_to'] or '—'} |")
    a('')

    a('## Password on state-changing routes\n')
    a('| Route | Direction | Status | Gated |')
    a('|---|---|---|---|')
    for e in _report['auth']:
        a(f"| `{e['route']}` | {e['direction']} | {e['status']} | "
          f"{'yes' if e['gated'] else '**NO**'} |")
    a('')

    if _report['orphan_ids']:
        a('## Ids referenced but never rendered\n')
        for o in _report['orphan_ids']:
            a(f"- `{o['id']}`" + (' — known dead, null-guarded' if o['known'] else ''))
        a('')
    if _report['dead_controls']:
        a('## Controls that do nothing\n')
        for c in _report['dead_controls']:
            a(f"- `{c['tab']}`: `{c['id'] or c['key']}` ({c['label']})")
        a('')
    if _report['mirrors']:
        a('## Read-only mirrors (no listener; the script only writes them)\n')
        a('The real switch lives on another tab; this copy only reflects it.\n')
        for c in _report['mirrors']:
            a(f"- `{c['tab']}`: `{c['id'] or c['key']}` ({c['label']})")
        a('')
    if _report['arguments']:
        a('## Argument controls (no listener of their own; a sibling button '
          'reads them)\n')
        for c in _report['arguments']:
            a(f"- `{c['tab']}`: `{c['id'] or c['key']}` ({c['label']})")
        a('')
    if _report['unverified']:
        a('## Controls the audit could not judge\n')
        a('No listener, and the source reaches them through a cached variable '
          'rather than `$(\'id\')`, so the static scan cannot tell whether '
          'they are read. Worth a look by hand.\n')
        for c in _report['unverified']:
            a(f"- `{c['tab']}`: `{c['id'] or c['key']}` ({c['label']})")
        a('')
    if _report['chips']:
        a('## Summary strip\n')
        for c in _report['chips']:
            a(f"**{c['case']}** — " + ', '.join(f'`{k}`: {v}' for k, v in c['seen'].items()))
            a('')
    if _report['dots']:
        a('## Tab dots\n')
        a('| Dot | Means | Quiet | Alarmed |')
        a('|---|---|---|---|')
        for d in _report['dots']:
            a(f"| `{d['dot']}` | {d['means']} | `{d['quiet']}` | `{d['alarmed']}` |")
        a('')
    return '\n'.join(L)


@atexit.register
def _write_report():
    if not _report['routes']:
        return                              # the sweep never ran; nothing honest to say
    out = os.path.join(_env.REPO, 'docs', 'dashboard-audit.md')
    try:
        with open(out, 'w', encoding='utf-8') as fh:
            fh.write(_render_report())
        print(f'\naudit report written to {os.path.relpath(out, _env.REPO)}')
    except OSError as exc:
        print(f'could not write the audit report: {exc}')


if __name__ == '__main__':
    _env.run_tests(dict(globals()))