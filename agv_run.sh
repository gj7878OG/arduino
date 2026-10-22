#!/usr/bin/env bash
# Run the AGV that drives the Arduino chassis, from /home/deep/arduino/.
# Starts the web dashboard + the drive orchestrator and stops BOTH together.
# The Arduino serial port defaults to /dev/ttyACM0.
#
# Usage:
#   ./agv_run.sh                              # foreground (Ctrl+C to stop)
#   AGV_ARDUINO_PORT=/dev/ttyUSB0 ./agv_run.sh   # different port
#   AGV_CAMERA_SENSOR_ID=1 ./agv_run.sh           # CAM1 instead of CAM0
#   AGV_SHOW_WINDOW=1 ./agv_run.sh                # local OpenCV preview
#
# Stopping - every one of these stops the dashboard AND the drive loop:
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
WEB_PID_FILE="$LOG_DIR/web.pid"
WEB_LOG="$LOG_DIR/web.log"
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