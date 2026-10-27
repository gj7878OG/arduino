#!/usr/bin/env bash
# Run the AGV that drives the Arduino chassis, from /home/deep/arduino/.
# Starts the web dashboard + the drive orchestrator + the voice runner and
# stops ALL THREE together.
# The Arduino serial port defaults to /dev/ttyACM0.
#
# Usage:
#   ./agv_run.sh                              # foreground (Ctrl+C to stop)
#   AGV_ARDUINO_PORT=/dev/ttyUSB0 ./agv_run.sh   # different port
#   AGV_CAMERA_SENSOR_ID=1 ./agv_run.sh           # CAM1 instead of CAM0
#   AGV_SHOW_WINDOW=1 ./agv_run.sh                # local OpenCV preview
#   AGV_VOICE=0 ./agv_run.sh                      # no voice (log-only AGV)
#   AGV_VOICE_DRY_RUN=1 ./agv_run.sh              # voice, but no audio out
#
# Stopping - every one of these stops the dashboard, the drive loop AND voice:
#   Ctrl+C                              # in the terminal running this
#   kill <agv_run.sh pid>               # SIGTERM, from anywhere (incl. SSH)
#   the AGV exiting on its own           # teardown follows automatically
#   kill -9 <agv_run.sh pid>            # NOT trap-able. Nothing can be; the
#                                       # next ./agv_run.sh reaps the orphans.
#
# This script's own pid is written to logs/run.pid, so from another terminal
# or over SSH the stop is just:   kill $(cat /home/deep/arduino/logs/run.pid)
# (Do not reach for `pgrep -f agv_run.sh` - the pattern also matches the shell
# you typed it into, so the signal lands on the wrong process.)
#
# Recorder rollback switches (defaults are AGV_REC_FORMAT=v1, AGV_REC_REQUIRE_AUTH=1):
#   AGV_REC_FORMAT=v0  ./agv_run.sh          # old single-file recorder
#   AGV_REC_REQUIRE_AUTH=0 ./agv_run.sh      # recordings endpoints without sign-in

set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
cd "$HERE"
export PYTHONPATH="${HERE}${PYTHONPATH:+:$PYTHONPATH}"
export AGV_DASHBOARD_DIR="${AGV_DASHBOARD_DIR:-/tmp/agv-dashboard}"
export AGV_SHOW_WINDOW="${AGV_SHOW_WINDOW:-0}"

LOG_DIR="${HERE}/logs"
mkdir -p "$LOG_DIR"
WEB_PID=""
AGV_PID=""
VOICE_PID=""
WEB_PID_FILE="$LOG_DIR/web.pid"
WEB_LOG="$LOG_DIR/web.log"
VOICE_LOG="$LOG_DIR/voice.log"
RUN_PID_FILE="$LOG_DIR/run.pid"
echo $$ >"$RUN_PID_FILE"

# argv of a pid, one entry per line (empty when the pid is gone)
_argv() { tr '\0' '\n' <"/proc/$1/cmdline" 2>/dev/null || true; }

# True only for a process started as `python web.py` from $HERE. The
# relative argv[1] plus the cwd keeps the test harnesses (which spawn
# web.py by absolute path) out of the sweep.
_is_dashboard() {
  local a0 a1 cwd
  kill -0 "$1" 2>/dev/null || return 1
  cwd="$(readlink -f "/proc/$1/cwd" 2>/dev/null || true)"
  [[ "$cwd" == "$HERE" ]] || return 1
  a0="$(_argv "$1" | sed -n 1p)"
  a1="$(_argv "$1" | sed -n 2p)"
  [[ "$a0" == *python3 || "$a0" == *python ]] || return 1
  [[ "$a1" == "web.py" ]]
}

# Same test for a drive loop orphaned by `kill -9` on this script. Leaving
# one behind is the worst case: wheels still commanded, dashboard gone.
_is_agv() {
  local a0 a1 cwd
  kill -0 "$1" 2>/dev/null || return 1
  cwd="$(readlink -f "/proc/$1/cwd" 2>/dev/null || true)"
  [[ "$cwd" == "$HERE" ]] || return 1
  a0="$(_argv "$1" | sed -n 1p)"
  a1="$(_argv "$1" | sed -n 2p)"
  [[ "$a0" == *python3 || "$a0" == *python ]] || return 1
  [[ "$a1" == "agv_drive_arduino.py" ]]
}

# The voice runner is launched as `-m voice.run_phase0`, so argv[1] is '-m'
# and the module name is argv[2]. Orphaned voice is quieter than an orphaned
# AGV but still wrong: two runners would race on voice_command.json and double
# every announcement.
_is_voice() {
  local a0 a1 a2 cwd
  kill -0 "$1" 2>/dev/null || return 1
  cwd="$(readlink -f "/proc/$1/cwd" 2>/dev/null || true)"
  [[ "$cwd" == "$HERE" ]] || return 1
  a0="$(_argv "$1" | sed -n 1p)"
  a1="$(_argv "$1" | sed -n 2p)"
  a2="$(_argv "$1" | sed -n 3p)"
  [[ "$a0" == *python3 || "$a0" == *python ]] || return 1
  [[ "$a1" == "-m" && "$a2" == "voice.run_phase0" ]]
}

# TERM, wait up to 5s, then KILL. Always by explicit pid: `pkill -f` also
# matches the shell running this script and kills it mid-way.
_kill_pid() {
  local pid="$1" i
  kill -0 "$pid" 2>/dev/null || return 0
  kill -TERM "$pid" 2>/dev/null || true
  for i in {1..50}; do
    kill -0 "$pid" 2>/dev/null || return 0
    sleep 0.1
  done
  echo "  pid $pid ignored SIGTERM for 5s - sending SIGKILL" >&2
  kill -KILL "$pid" 2>/dev/null || true
}

# `kill -9` on this script bypasses the EXIT trap and orphans both children:
# the dashboard keeps port 5000 bound (so the next run came up with no UI and
# no hint why) and the drive loop keeps commanding the wheels with no UI to
# stop it. Sweep both before starting fresh.
reap_stale_dashboards() {
  local d pid
  for d in /proc/[0-9]*; do
    pid="${d#/proc/}"
    if [[ "$pid" == "$$" ]]; then
      continue
    fi
    if _is_agv "$pid"; then
      echo "Reaping orphaned AGV from an earlier run (pid=$pid)"
      _kill_pid "$pid"
    fi
    if _is_dashboard "$pid"; then
      echo "Reaping orphaned dashboard from an earlier run (pid=$pid)"
      _kill_pid "$pid"
    fi
    if _is_voice "$pid"; then
      echo "Reaping orphaned voice runner from an earlier run (pid=$pid)"
      _kill_pid "$pid"
    fi
  done
  rm -f "$WEB_PID_FILE"
}

_on_exit() {
  local rc=$?
  trap - EXIT INT TERM
  # The AGV first: it owns the wheels and closes the LiDAR port on its way
  # out, so let it finish before the dashboard goes away.
  if [[ -n "$AGV_PID" ]] && kill -0 "$AGV_PID" 2>/dev/null; then
    echo "Stopping AGV (agv_drive_arduino.py pid=$AGV_PID)"
    _kill_pid "$AGV_PID"
  fi
  # Voice after the AGV so it cannot narrate a rover that has already stopped,
  # and before the dashboard goes away: the dashboard owns the speaker choice
  # this runner reads at startup.
  if [[ -n "$VOICE_PID" ]] && kill -0 "$VOICE_PID" 2>/dev/null; then
    echo "Stopping voice runner (pid=$VOICE_PID)"
    _kill_pid "$VOICE_PID"
  fi
  if [[ -n "$WEB_PID" ]] && kill -0 "$WEB_PID" 2>/dev/null; then
    echo "Stopping dashboard (web.py pid=$WEB_PID)"
    _kill_pid "$WEB_PID"
  fi
  rm -f "$WEB_PID_FILE" "$RUN_PID_FILE"
  exit "$rc"
}

# INT/TERM just leave a well-defined status; the EXIT trap does the teardown.
trap _on_exit EXIT
trap 'trap - INT; exit 130' INT
trap 'trap - TERM; exit 143' TERM

echo "Starting Arduino-driven AGV from $HERE"
echo "  AGV_ARDUINO_PORT = ${AGV_ARDUINO_PORT:-/dev/ttyACM0}"
echo "  AGV_DASHBOARD_DIR= $AGV_DASHBOARD_DIR"
echo "  AGV_CAMERA_SENSOR_ID = ${AGV_CAMERA_SENSOR_ID:-0}"
echo "  voice            = $([[ "${AGV_VOICE:-1}" == "0" ]] && echo 'off (AGV_VOICE=0)' || echo 'on')"
echo "  PYTHONPATH       = $PYTHONPATH"

reap_stale_dashboards

# Offset into web.log before launching, so the readiness probe below only
# looks at what this run wrote.
WEB_LOG_OFF=0
if [[ -f "$WEB_LOG" ]]; then
  WEB_LOG_OFF="$(wc -c <"$WEB_LOG")"
  WEB_LOG_OFF="${WEB_LOG_OFF//[[:space:]]/}"
fi

# TelemetryPublisher writes status.json + frame.jpg into AGV_DASHBOARD_DIR;
# the dashboard reads from the same dir.
/usr/bin/python3 web.py >>"$WEB_LOG" 2>&1 &
WEB_PID=$!
echo "$WEB_PID" >"$WEB_PID_FILE"
echo "web.py pid=$WEB_PID"

# Wait for the socket to bind, so a port clash (or any other crash) fails
# loudly here instead of leaving you driving blind.
_ready=0
for _ in {1..150}; do
  if ! kill -0 "$WEB_PID" 2>/dev/null; then
    break
  fi
  if tail -c "+$((WEB_LOG_OFF + 1))" "$WEB_LOG" | grep -q 'Running on'; then
    _ready=1
    break
  fi
  sleep 0.1
done
if [[ "$_ready" -ne 1 ]]; then
  echo "ERROR: dashboard failed to start. Tail of $WEB_LOG:" >&2
  tail -n 15 "$WEB_LOG" >&2 || true
  exit 1
fi

/usr/bin/python3 agv_drive_arduino.py >>"$LOG_DIR/agv.log" 2>&1 &
AGV_PID=$!

# Voice runs as its own process because the C270's ALSA capture device is
# exclusive: the dashboard must never open the mic. It reads control.json,
# points.json and cam_faces.json that the two processes above publish, and
# speaks through the speaker selected on the Voice tab.
#
# A voice failure is a warning, never fatal. The rover has to be able to drive
# with no audio stack installed at all, so this must not abort the run the way
# a dashboard failure does - there, losing the UI means driving blind.
VOICE_ARGS=(--telemetry-dir "$AGV_DASHBOARD_DIR")
[[ "${AGV_VOICE_DRY_RUN:-0}" == "1" ]] && VOICE_ARGS+=(--dry-run)

if [[ "${AGV_VOICE:-1}" == "0" ]]; then
  echo "voice runner disabled (AGV_VOICE=0)"
else
  /usr/bin/python3 -m voice.run_phase0 "${VOICE_ARGS[@]}" >>"$VOICE_LOG" 2>&1 &
  VOICE_PID=$!
  echo "voice.run_phase0 pid=$VOICE_PID"

  # Give it a moment to publish voice_status.json. Unattended audio is a
  # silent failure: the rover would drive and narrate nothing with no sign of
  # it anywhere, so say so here rather than letting it pass unnoticed.
  _voiceready=0
  for _ in {1..50}; do
    kill -0 "$VOICE_PID" 2>/dev/null || break
    if [[ -f "$AGV_DASHBOARD_DIR/voice_status.json" ]]; then
      _voiceready=1
      break
    fi
    sleep 0.1
  done
  if [[ "$_voiceready" -eq 1 ]]; then
    echo "voice is live; speaker + phrase cache in $VOICE_LOG"
  elif kill -0 "$VOICE_PID" 2>/dev/null; then
    echo "WARNING: voice published no voice_status.json in 5s - narration may be off. Tail of $VOICE_LOG:" >&2
    tail -n 10 "$VOICE_LOG" >&2 || true
  else
    echo "WARNING: voice runner died at startup. Tail of $VOICE_LOG:" >&2
    tail -n 10 "$VOICE_LOG" >&2 || true
    VOICE_PID=""
  fi
fi

# `wait` (not a foreground call) is what makes the trap usable: bash defers
# traps until a *foreground* command finishes, so a plain foreground launch
# would make Ctrl+C and `kill` hang until the AGV stopped by itself. `wait`
# returns immediately on a trapped signal, and the EXIT trap then tears down
# both children.
if wait "$AGV_PID"; then
  AGV_RC=0
else
  AGV_RC=$?
fi
exit "$AGV_RC"