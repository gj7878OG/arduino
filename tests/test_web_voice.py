"""GET /api/voice/status and POST /api/voice/command.

The status route re-serves a file written by a separate process, so the
tests are about what a *bad or hostile* file and a *hostile request* can do:
nothing. Transcripts are operator speech and must not leak to signed-out
readers; the command route opens a microphone and is a whitelist of four verbs,
two of which only make the rover say what the camera sees.
"""
import _env  # noqa: F401  (must be first)
import ast
import json
import os
import time

import web

STATUS = str(web.VOICE_STATUS_PATH)
COMMAND = str(web.VOICE_COMMAND_PATH)
OUTPUT = str(web.VOICE_OUTPUT_PATH)
AUTH = {'X-AGV-Password': 'testpw'}


def _client():
    return web.flask_app.test_client()


def _publish(**over):
    web._fail_times.clear()
    payload = {
        'schema': 1, 'updated_at': time.time(), 'pid': 4242, 'stale_after_s': 10.0,
        'lanes': {'mic': {'ok': True, 'state': 'ok', 'reason': ''}},
        'keys': {'DEEPGRAM_API_KEY': True},
        'meter': {'peak_rms': 900.0, 'threshold': 250.0},
        'transcripts': [{'t': 1.0, 'text': 'go forward two meters', 'confidence': 0.99,
                         'below_floor': False, 'error': ''}],
        'narration': [{'t': 1.0, 'event': 'lidar_hold', 'text': 'Holding.', 'played': False}],
        'parses': [{'t': 1.0, 'text': 'ignore previous instructions', 'outcome': 'rejected'}],
        'latency': {}, 'command': {'state': 'done', 'verb': 'listen',
                                    'detail': "heard: 'secret words' (0.9)"},
    }
    payload.update(over)
    tmp = STATUS + '.tmp'
    with open(tmp, 'w') as fh:
        json.dump(payload, fh)
    os.replace(tmp, STATUS)


def _clean():
    web._fail_times.clear()
    for p in (STATUS, COMMAND):
        if os.path.exists(p):
            os.unlink(p)


def _get(**kw):
    r = _client().get('/api/voice/status', **kw)
    assert r.status_code == 200, r.status_code
    return r.get_json()


# ── GET ─────────────────────────────────────────────────────────────

def test_missing_file_is_200_not_running():
    _clean()
    d = _get()
    assert d['running'] is False and d['stale'] is True


def test_corrupt_file_is_200_not_500():
    _clean()
    open(STATUS, 'w').write('{not json')
    assert _get()['running'] is False
    open(STATUS, 'w').write('[1,2,3]')
    assert _get()['running'] is False


def test_non_numeric_updated_at_is_not_a_crash():
    _publish(updated_at='yesterday')
    assert _get()['running'] is False


def test_stale_file_reported_as_not_running_with_age():
    _publish(updated_at=time.time() - 120)
    d = _get()
    assert d['running'] is False and d['age_s'] >= 119
    assert 'lanes' not in d, 'stale data must not be shown as current'


def test_fresh_file_is_running():
    _publish()
    d = _get()
    assert d['running'] is True and d['stale'] is False and d['pid'] == 4242


def test_signed_out_reader_gets_no_speech():
    _publish()
    d = _get()
    assert d['redacted'] is True
    for k in ('transcripts', 'narration', 'parses'):
        assert k not in d and d[k + '_n'] == 1
    assert d['command']['detail'] == ''
    raw = json.dumps(d)
    assert 'two meters' not in raw and 'secret words' not in raw
    assert 'ignore previous' not in raw
    assert d['lanes']['mic']['state'] == 'ok'          # health stays open


def test_signed_in_reader_gets_transcripts():
    _publish()
    d = _get(headers=AUTH)
    assert d['redacted'] is False
    assert d['transcripts'][0]['text'] == 'go forward two meters'


def test_wrong_password_on_read_is_redacted_and_never_throttles():
    _publish()
    for _ in range(12):
        d = _get(headers={'X-AGV-Password': 'nope'})
        assert d['redacted'] is True
    # a read must not feed the wrong-password lockout
    assert _client().get('/api/voice/status', headers=AUTH).status_code == 200


def test_status_is_not_cacheable():
    _publish()
    assert 'no-store' in _client().get('/api/voice/status').headers['Cache-Control']


# ── POST ────────────────────────────────────────────────────────────

def _post(body, headers=AUTH):
    return _client().post('/api/voice/command', json=body, headers=headers)


def test_command_needs_password():
    _publish(); _clean_cmd()
    assert _post({'verb': 'listen'}, headers={}).status_code == 403
    assert _post({'verb': 'listen'}, headers={'X-AGV-Password': 'bad'}).status_code in (403, 429)


# ── speaker selection ───────────────────────────────────────────────


def _post_out(body, headers=AUTH):
    return _client().post('/api/voice/output', json=body, headers=headers)


def _clean_out():
    if os.path.exists(OUTPUT):
        os.unlink(OUTPUT)


def test_output_change_needs_password():
    _clean_out()
    assert _post_out({'device': 'auto'}, headers={}).status_code == 403
    assert _post_out({'device': 'auto'},
                     headers={'X-AGV-Password': 'bad'}).status_code in (403, 429)
    assert not os.path.exists(OUTPUT), 'a rejected change must not write a file'


def test_output_get_lists_devices_without_a_password():
    """Read-only: enumerating speakers opens nothing, so it stays open."""
    r = _client().get('/api/voice/output')
    assert r.status_code == 200
    body = r.get_json()
    assert 'outputs' in body and 'selected' in body


def test_output_change_rejects_a_device_that_does_not_exist():
    _clean_out()
    r = _post_out({'device': 'bluez_output.not_a_real_speaker.9'})
    assert r.status_code == 400, r.status_code
    assert not os.path.exists(OUTPUT)


def test_output_change_refuses_shell_metacharacters():
    _clean_out()
    for bad in ('x; rm -rf /', 'a$(id)', 'a`id`', 'a && b', '../escape'):
        r = _post_out({'device': bad})
        assert r.status_code == 400, bad
    assert not os.path.exists(OUTPUT)


def test_output_change_writes_a_file_the_voice_process_polls():
    _clean_out()
    live = [o['id'] for o in _client().get('/api/voice/output').get_json()['outputs']]
    assert live, 'no audio outputs on this machine; cannot test the happy path'
    r = _post_out({'device': live[0]})
    assert r.status_code == 200, r.status_code
    assert os.path.exists(OUTPUT)
    assert json.load(open(OUTPUT))['device'] == live[0]
    assert r.get_json()['state'] == 'connected'
    _clean_out()


def test_output_routes_cannot_reach_the_motors():
    for name in ('api_voice_output', 'api_voice_output_set', 'api_voice_output_test'):
        fn = _route_source(name)
        refs = {n.id for n in ast.walk(fn) if isinstance(n, ast.Name)} | \
               {n.attr for n in ast.walk(fn) if isinstance(n, ast.Attribute)}
        assert not (refs & {'write_state', 'read_state', 'clamp_speed'}), (name, refs)
    assert not os.path.exists(COMMAND)


# ── testing the speaker ─────────────────────────────────────────────


def _post_out_test(body, headers=AUTH):
    return _client().post('/api/voice/output/test', json=body, headers=headers)


def test_the_speaker_test_needs_a_password():
    """It makes noise, so it sits behind the same gate as the other POSTs."""
    web._fail_times.clear()
    assert _post_out_test({'device': 'auto'}, headers={}).status_code == 403


def test_the_speaker_test_reports_whether_anything_was_audible():
    """The whole point of the button: a verdict, not 'queued'.

    ``played`` alone is what the old button claimed and it is the reason a dead
    Bluetooth speaker looked healthy — aplay exits 0 into a void.
    """
    web._fail_times.clear()
    r = _post_out_test({'device': 'auto'})
    assert r.status_code == 200, r.status_code
    body = r.get_json()
    assert {'audible', 'played', 'reason'} <= set(body), body
    if not body['audible']:
        assert body['reason'], 'a failed test must say why'


def test_the_speaker_test_refuses_a_device_that_does_not_exist():
    web._fail_times.clear()
    r = _post_out_test({'device': 'bluez_output.not_a_real_speaker.9'})
    assert r.status_code == 400, r.status_code


def test_only_one_speaker_test_runs_at_a_time():
    web._fail_times.clear()
    web._OUTPUT_LOCK.acquire()
    try:
        r = _post_out_test({'device': 'auto'})
        assert r.status_code == 409, r.status_code
    finally:
        web._OUTPUT_LOCK.release()


def _clean_cmd():
    web._fail_times.clear()      # the wrong-password throttle must not leak between tests
    if os.path.exists(COMMAND):
        os.unlink(COMMAND)


def test_only_the_whitelisted_verbs_are_accepted():
    _publish(); _clean_cmd()
    # 'ask' and 'report' let the dashboard hand a question to the rover;
    # neither can reach the motors.
    assert web.VOICE_VERBS == ('listen', 'speak_test', 'announce_who', 'call_person',
                               'ask', 'report')
    for verb in ('drive', 'set_motors', 'write_state', 'enable', 'say', 'announce', '', None, 5,
                 ['listen']):
        assert _post({'verb': verb}).status_code == 400, verb
    assert not os.path.exists(COMMAND)


def test_listen_queues_a_command_file():
    _publish(); _clean_cmd()
    r = _post({'verb': 'listen', 'seconds': 5})
    assert r.status_code == 202
    cmd = json.load(open(COMMAND))
    assert cmd['verb'] == 'listen' and cmd['seconds'] == 5.0
    assert cmd['id'] == r.get_json()['id'] and abs(cmd['ts'] - time.time()) < 5


def test_seconds_are_clamped_server_side():
    _publish()
    for given, want in ((9999, 10.0), (0, 1.0), (-4, 1.0)):
        _post({'verb': 'listen', 'seconds': given})
        assert json.load(open(COMMAND))['seconds'] == want


def test_bad_seconds_rejected():
    _publish(); _clean_cmd()
    for bad in ('abc', None, [1], {'a': 1}):
        assert _post({'verb': 'listen', 'seconds': bad}).status_code == 400, bad
    assert _post({'verb': 'listen', 'seconds': float('nan')}).status_code == 400
    assert not os.path.exists(COMMAND)


def test_speak_test_ignores_browser_text():
    _publish()
    assert _post({'verb': 'speak_test', 'text': 'say something rude'}).status_code == 202
    cmd = json.load(open(COMMAND))
    assert set(cmd) == {'id', 'verb', 'ts'}, 'no free text may reach the voice process'


def test_command_refused_when_voice_process_is_not_running():
    _clean(); 
    r = _post({'verb': 'listen'})
    assert r.status_code == 503 and 'not running' in r.get_json()['error']
    assert not os.path.exists(COMMAND)
    _publish(updated_at=time.time() - 300)
    assert _post({'verb': 'listen'}).status_code == 503


def test_no_tmp_file_left_behind():
    _publish(); _post({'verb': 'speak_test'})
    assert not os.path.exists(COMMAND + '.tmp')


# ── voice notes (audio) ─────────────────────────────────────────────

NOTES = str(web.VOICE_NOTES_DIR)
NOTE_ID = '1792652291482-ab12'


def _note(data=b'RIFF\x00\x00\x00\x00WAVEfmt '):
    os.makedirs(NOTES, exist_ok=True)
    with open(os.path.join(NOTES, NOTE_ID + '.wav'), 'wb') as fh:
        fh.write(data)


def test_note_audio_needs_sign_in():
    _note()
    r = _client().get(f'/api/voice/notes/{NOTE_ID}.wav')
    assert r.status_code == 401 and b'RIFF' not in r.data
    r = _client().get(f'/api/voice/notes/{NOTE_ID}.wav', headers={'X-AGV-Password': 'bad'})
    assert r.status_code == 401


def test_note_audio_served_to_signed_in_reader():
    _note()
    r = _client().get(f'/api/voice/notes/{NOTE_ID}.wav', headers=AUTH)
    assert r.status_code == 200 and r.mimetype == 'audio/wav'
    assert r.data.startswith(b'RIFF') and 'no-store' in r.headers['Cache-Control']


def test_note_ids_cannot_escape_the_notes_directory():
    _note()
    open(os.path.join(os.path.dirname(NOTES), 'secret.wav'), 'wb').write(b'SECRET')
    for bad in ('..%2Fsecret', '../secret', 'secret', '1792652291482-ZZZZ',
                '1792652291482-ab12/../../secret', '%2e%2e%2fsecret', 'x' * 300):
        r = _client().get(f'/api/voice/notes/{bad}.wav', headers=AUTH)
        assert r.status_code in (404, 405) and b'SECRET' not in r.data, bad


def test_missing_note_is_404():
    _note(); os.unlink(os.path.join(NOTES, NOTE_ID + '.wav'))
    assert _client().get(f'/api/voice/notes/{NOTE_ID}.wav', headers=AUTH).status_code == 404


def test_note_list_is_redacted_for_signed_out_reader():
    _publish(notes=[{'id': NOTE_ID, 'text': 'private words', 'gate': True}])
    d = _get()
    assert 'notes' not in d and d['notes_n'] == 1 and 'private words' not in json.dumps(d)
    assert _get(headers=AUTH)['notes'][0]['text'] == 'private words'


def test_notes_route_is_get_only():
    _note()
    for method in ('post', 'put', 'delete'):
        r = getattr(_client(), method)(f'/api/voice/notes/{NOTE_ID}.wav', headers=AUTH)
        assert r.status_code == 405, method


# ── architecture ────────────────────────────────────────────────────

def _route_source(name):
    tree = ast.parse(open(os.path.join(_env.REPO, 'web.py')).read())
    return next(n for n in ast.walk(tree)
                if isinstance(n, ast.FunctionDef) and n.name == name)


def test_voice_routes_cannot_reach_the_motors():
    """A diagnostics tab must not become a third path to the motors."""
    for name in ('api_voice_status', 'api_voice_command', 'api_voice_note', '_voice_payload',
                 '_voice_running', '_voice_reader_signed_in'):
        fn = _route_source(name)
        refs = {n.id for n in ast.walk(fn) if isinstance(n, ast.Name)} | \
               {n.attr for n in ast.walk(fn) if isinstance(n, ast.Attribute)}
        assert not (refs & {'write_state', 'read_state', 'clamp_speed'}), (name, refs)


def test_web_imports_voice_only_lazily_and_only_read_only_helpers():
    """web.py must never *open the microphone itself* or import voice at
    module level (a broken voice package must not take the dashboard down).

    Two lazy reach-ins are allowed, and only because neither opens a device:

    ``voice.mic``     the two read-only probes (USB bus + one capture test)
    ``voice.output``  enumerates playback sinks via ``pactl``/``aplay``, reads
                      and writes ``voice_output.json``, and (on the explicit
                      test route) plays one cached clip to see whether the sink
                      takes it. The C270's ALSA device is exclusive, so the rule
                      this guards is about *capture*; playing a clip touches
                      only a speaker.

    The test below pins that claim: ``voice.output`` must not import anything
    that can open audio.
    """
    ALLOWED = {'voice.mic': {'mic_inventory', 'probe_microphone'},
               'voice.output': {'OutputSelector', 'list_outputs', 'probe_playback'}}
    tree = ast.parse(open(os.path.join(_env.REPO, 'web.py')).read())
    for node in tree.body:                              # module level only
        mods = [a.name for a in getattr(node, 'names', [])] if isinstance(node, ast.Import) else \
               [node.module] if isinstance(node, ast.ImportFrom) and node.module else []
        assert not any(m == 'voice' or m.startswith('voice.') for m in mods), mods
    pulled = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.ImportFrom) and n.module and n.module.startswith('voice'):
            assert n.module in ALLOWED, n.module
            names = {a.name for a in n.names}
            assert names <= ALLOWED[n.module], (n.module, names)
            pulled |= names
    assert pulled <= set().union(*ALLOWED.values()), pulled
    refs = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)} | \
           {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    assert not (refs & {'MicCapture', 'SttEngine', 'TtsEngine', 'listen'}), refs


def test_the_output_helper_cannot_open_an_audio_device():
    """Why ``voice.output`` is safe to import from the web process.

    If someone later makes enumeration open the device to query it, the ALSA
    exclusivity rule is broken and this fails.
    """
    src = open(os.path.join(_env.REPO, 'voice', 'output.py')).read()
    tree = ast.parse(src)
    for n in ast.walk(tree):
        mods = [a.name for a in getattr(n, 'names', [])] if isinstance(n, ast.Import) else \
               [n.module] if isinstance(n, ast.ImportFrom) and n.module else []
        assert not any(m in ('mic', 'arecord', 'pyaudio', 'sounddevice', 'tts', 'stt')
                       for m in mods), mods
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)} | \
            {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    for forbidden in ('arecord', 'MicCapture', 'popen_with_shell'):
        assert forbidden not in names, forbidden
    # Nothing shells out through a shell string.
    assert 'shell=True' not in src


def test_only_the_expected_voice_routes_exist():
    rules = {r.rule for r in web.flask_app.url_map.iter_rules() if 'voice' in r.rule}
    assert rules == {'/api/voice/status', '/api/voice/command',
                     '/api/voice/mic', '/api/voice/mic/test', '/api/voice/output',
                     '/api/voice/output/test',
                     '/api/voice/notes/<note_id>.wav'}


# ── microphone ──────────────────────────────────────────────────────

def test_mic_inventory_is_open_and_never_500s():
    r = _client().get('/api/voice/mic')
    assert r.status_code == 200
    d = r.get_json()
    assert 'connected' in d and 'cards' in d and 'last_test' in d
    assert 'no-store' in r.headers['Cache-Control']


def test_mic_inventory_survives_a_broken_probe():
    import voice.mic as m
    orig = m.mic_inventory
    m.mic_inventory = lambda: (_ for _ in ()).throw(RuntimeError('sysfs exploded'))
    try:
        d = _client().get('/api/voice/mic').get_json()
    finally:
        m.mic_inventory = orig
    assert d['connected'] is None and d['error']


def test_mic_test_needs_password_and_never_captures_without_it():
    import voice.mic as m
    calls = []
    orig = m.probe_microphone
    m.probe_microphone = lambda s=1.5: calls.append(s) or {}
    try:
        assert _client().post('/api/voice/mic/test').status_code == 403
        assert calls == []
    finally:
        m.probe_microphone = orig


def test_mic_test_reports_and_remembers_the_result():
    import voice.mic as m
    orig = m.probe_microphone
    m.probe_microphone = lambda s=1.5: {'ok': True, 'state': 'working', 't': 1.0,
                                        'peak_rms': 700.0, 'message': 'Audio is arriving.'}
    try:
        r = _client().post('/api/voice/mic/test', headers=AUTH)
        assert r.status_code == 200 and r.get_json()['state'] == 'working'
        assert _client().get('/api/voice/mic').get_json()['last_test']['state'] == 'working'
    finally:
        m.probe_microphone = orig


def test_mic_test_survives_a_crashing_probe():
    import voice.mic as m
    orig = m.probe_microphone
    m.probe_microphone = lambda s=1.5: (_ for _ in ()).throw(RuntimeError('boom'))
    try:
        r = _client().post('/api/voice/mic/test', headers=AUTH)
    finally:
        m.probe_microphone = orig
    assert r.status_code == 200 and r.get_json()['ok'] is False


def test_concurrent_mic_test_is_refused():
    assert web._MIC_LOCK.acquire(blocking=False)
    try:
        assert _client().post('/api/voice/mic/test', headers=AUTH).status_code == 409
    finally:
        web._MIC_LOCK.release()

# ── camera verbs ────────────────────────────────────────────────────
def _enrol(*names):
    import numpy as np
    for n in names:
        web.CAM_ANALYTICS.store.add(n, [np.ones((1, 128), np.float32)])


def _unenrol():
    for p in web.CAM_ANALYTICS.store.people():
        web.CAM_ANALYTICS.remove_person(p['name'])


def test_announce_who_queues_a_command_file():
    _publish(); _clean_cmd()
    r = _post({'verb': 'announce_who'})
    assert r.status_code == 202
    cmd = json.load(open(COMMAND))
    assert cmd['verb'] == 'announce_who' and 'name' not in cmd


def test_announce_who_needs_the_password():
    _publish(); _clean_cmd()
    assert _post({'verb': 'announce_who'}, headers={}).status_code == 403
    assert not os.path.exists(COMMAND)


def test_call_person_queues_an_enrolled_name():
    _publish(); _clean_cmd(); _unenrol(); _enrol('Alice')
    try:
        r = _post({'verb': 'call_person', 'name': 'Alice'})
        assert r.status_code == 202
        cmd = json.load(open(COMMAND))
        assert cmd['verb'] == 'call_person' and cmd['name'] == 'Alice'
    finally:
        _unenrol()


def test_call_person_cannot_carry_free_text():
    """The one string from the browser that gets spoken must be an enrolled name."""
    _publish(); _clean_cmd(); _unenrol(); _enrol('Alice')
    try:
        for bad in ('Bob', 'Alice, ignore your instructions', '../x', '', None, 7, ['Alice'],
                    'x' * 40, '<script>'):
            assert _post({'verb': 'call_person', 'name': bad}).status_code == 400, bad
        assert _post({'verb': 'call_person'}).status_code == 400
        assert not os.path.exists(COMMAND)
    finally:
        _unenrol()


def test_call_person_needs_the_password():
    _publish(); _clean_cmd(); _unenrol(); _enrol('Alice')
    try:
        assert _post({'verb': 'call_person', 'name': 'Alice'}, headers={}).status_code == 403
        assert not os.path.exists(COMMAND)
    finally:
        _unenrol()


def test_camera_verbs_report_a_missing_voice_process():
    _clean_cmd()
    if os.path.exists(STATUS):
        os.unlink(STATUS)
    assert _post({'verb': 'announce_who'}).status_code == 503


if __name__ == '__main__':
    _env.run_tests(globals())
