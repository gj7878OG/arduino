"""Tests for agv_run.sh — the script that starts and stops the whole rover.

The script is the only thing that decides whether the rover comes up with a
dashboard, a drive loop and a voice runner, and it is the only thing that
guarantees they stop together. Neither is visible to the Python test suite,
so it is checked two ways:

* **Statically** — that every child is launched, reaped and stopped.
* **By running the real shell predicates against real processes.** The
  ``_is_*`` functions are what decides whether an orphan gets killed on the
  next run, and a predicate that is too loose reaps innocent processes while
  one that is too narrow leaves a second drive loop commanding the wheels.

Nothing here starts the rover: no ``web.py``, no ``agv_drive_arduino.py``.
The processes spawned are a real voice runner (harmless, dry-run, pointed at a
temp dir) and an unrelated Python sleeping in a loop.
"""

import _env  # noqa: F401

import atexit
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

SCRIPT = Path(_env.REPO) / 'agv_run.sh'
SRC = SCRIPT.read_text()

# The predicates, extracted from the script rather than duplicated here: a
# copy would test itself and agree with a broken script.
PREDICATES = ('_argv', '_is_dashboard', '_is_agv', '_is_voice')


# ── extracting the shell functions ──────────────────────────────────


def _function(name: str) -> str:
    """The body of ``name() { ... }``, found by counting braces."""
    start = SRC.index('\n%s() {' % name)
    i = SRC.index('{', start)
    depth, j = 0, i
    while j < len(SRC):
        if SRC[j] == '{':
            depth += 1
        elif SRC[j] == '}':
            depth -= 1
            if depth == 0:
                return SRC[start + 1:j + 1]
        j += 1
    raise AssertionError('unterminated %s in agv_run.sh' % name)


def _classify(pid: int) -> dict:
    """Ask the real predicates in a real shell which child a pid is."""
    script = 'HERE=%s\n' % _env.REPO
    # Newline-joined: each extracted function has no trailing newline, and
    # `}next() {` on one line is a syntax error, not a definition.
    script += '\n'.join(_function(name) for name in PREDICATES) + '\n'
    script += ('\nfor f in _is_dashboard _is_agv _is_voice; do\n'
               '  if $f %d; then echo "$f=yes"; else echo "$f=no"; fi\n'
               'done\n' % pid)
    out = subprocess.run(['bash', '-c', script], capture_output=True,
                         text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    return {line.split('=')[0]: line.split('=')[1] == 'yes'
            for line in out.stdout.split()}


# ── launching harmless stand-in processes ───────────────────────────


_SPAWNED = []


def _spawn(argv, cwd=None, **kw):
    proc = subprocess.Popen(argv, cwd=cwd or _env.REPO, **kw)
    _SPAWNED.append(proc)
    atexit.register(lambda: (proc.poll() is None and proc.kill()))
    return proc


def _wait_for_proc(pid: str) -> bool:
    """True once the command line shows up, so we classify the right pid."""
    for _ in range(100):
        try:
            with open('/proc/%s/cmdline' % pid, 'rb') as fh:
                if fh.read():
                    return True
        except OSError:
            return False
        time.sleep(0.05)
    return False


def _python_pids_of(cmdline_fragment: str) -> list:
    found = []
    for entry in os.listdir('/proc'):
        if not entry.isdigit():
            continue
        try:
            with open('/proc/%s/cmdline' % entry, 'rb') as fh:
                argv = fh.read().decode(errors='replace').split('\0')
        except OSError:
            continue
        if any(cmdline_fragment in a for a in argv) and 'agv_run_sh' not in argv:
            found.append(int(entry))
    return found


def _stop_spawned():
    for proc in _SPAWNED:
        if proc.poll() is None:
            proc.kill()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
    del _SPAWNED[:]


# ── the script is at all runnable ───────────────────────────────────


def test_the_script_is_valid_bash():
    out = subprocess.run(['bash', '-n', str(SCRIPT)], capture_output=True,
                         text=True, timeout=30)
    assert out.returncode == 0, out.stderr


def test_the_script_starts_all_three_children():
    """Voice is only useful if it is actually running with the rover."""
    assert '/usr/bin/python3 web.py' in SRC
    assert '/usr/bin/python3 agv_drive_arduino.py' in SRC
    assert '/usr/bin/python3 -m voice.run_phase0' in SRC


def test_voice_failure_does_not_stop_the_rover_from_driving():
    """No audio stack must not mean no rover.

    The dashboard is fatal because losing the UI means driving blind; audio is
    not, so the voice block must warn rather than exit.
    """
    block = SRC[SRC.index('if [[ "${AGV_VOICE:-1}" == "0" ]]; then'):]
    block = block[:block.index('\nfi\n') + 4]
    assert 'exit 1' not in block, block
    assert 'WARNING' in block, 'a voice failure must be visible, not silent'


def test_voice_can_be_turned_off_without_editing_the_script():
    assert 'AGV_VOICE:-1' in SRC
    assert 'AGV_VOICE_DRY_RUN:-0' in SRC
    assert '--dry-run' in SRC


def test_the_exit_trap_stops_voice_before_the_dashboard():
    """Order matters: the dashboard owns the speaker choice the runner read."""
    trap = SRC[SRC.index('_on_exit() {'):]
    trap = trap[:trap.index('\n}\n')]
    agv, voice, web = (trap.index('Stopping AGV'), trap.index('Stopping voice'),
                       trap.index('Stopping dashboard'))
    assert agv < voice < web, 'stop order is AGV, then voice, then dashboard'
    for name in ('AGV_PID', 'VOICE_PID', 'WEB_PID'):
        assert '[[ -n "$%s" ]]' % name in trap, name


def test_a_voice_runner_left_behind_by_kill_9_is_reaped():
    """Otherwise two runners race on voice_command.json and double every
    announcement."""
    assert '_is_voice "$pid"' in SRC
    assert SRC.count('_is_voice "$pid"') == 1


# ── the predicates, against real processes ──────────────────────────


def test_a_real_voice_runner_is_recognised_as_voice():
    tmp = tempfile.mkdtemp(prefix='agv-voice-test-')
    _spawn([sys.executable, '-m', 'voice.run_phase0',
            '--telemetry-dir', tmp, '--dry-run'],
           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    pids = _python_pids_of('voice.run_phase0')
    assert pids, 'the voice runner did not start; nothing to classify'
    try:
        got = _classify(pids[0])
        assert got['_is_voice'] is True, got
        # And only voice: a predicate that matches every python would reap
        # the dashboard and the drive loop along with it.
        assert got['_is_dashboard'] is False, got
        assert got['_is_agv'] is False, got
    finally:
        _stop_spawned()


def test_an_unrelated_python_process_is_reaped_by_none_of_them():
    """The whole point of the cwd + argv check: leave innocents alone."""
    proc = _spawn([sys.executable, '-c', 'import time; time.sleep(30)'],
                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    _wait_for_proc(str(proc.pid))
    try:
        got = _classify(proc.pid)
        assert got == {'_is_dashboard': False, '_is_agv': False,
                       '_is_voice': False}, got
    finally:
        _stop_spawned()


def test_a_process_from_another_directory_is_never_reaped():
    """Same argv, wrong cwd — a checkout somewhere else is not ours to kill."""
    elsewhere = tempfile.mkdtemp(prefix='agv-elsewhere-')
    proc = _spawn([sys.executable, '-m', 'voice.run_phase0',
                   '--telemetry-dir', elsewhere, '--dry-run'],
                  cwd=elsewhere,
                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    pids = _python_pids_of('voice.run_phase0')
    assert pids
    try:
        assert _classify(proc.pid)['_is_voice'] is False, \
            'a runner from another checkout must survive a reap'
    finally:
        _stop_spawned()


def test_a_dead_pid_is_not_a_child_of_anything():
    """Reaping walks every pid; most are already gone. This must not throw."""
    dead = subprocess.Popen(['true'])
    dead.wait()
    got = _classify(dead.pid)
    assert got == {'_is_dashboard': False, '_is_agv': False,
                   '_is_voice': False}


def test_this_test_process_is_not_reaped():
    """Guards against a predicate that degenerated into "matches python"."""
    got = _classify(os.getpid())
    assert got['_is_dashboard'] is False
    assert got['_is_agv'] is False
    assert got['_is_voice'] is False


if __name__ == '__main__':
    _env.run_tests(globals())