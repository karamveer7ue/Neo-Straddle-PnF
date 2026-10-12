#!/usr/bin/env bash
# Start the live P&F app and the simulator together, in this terminal.
#
#   ./start.sh                    live on 8765, simulator on 8766
#   ./start.sh --restart          restart servers already running, without asking
#   ./start.sh --skip             leave servers already running alone, without asking
#   ./start.sh --live-port 9000 --sim-port 9001
#
# If a server is already running on its port, you are asked whether to
# restart it or skip it (keep it running and start only the other).
# Output from the servers started here is shown, each line tagged [live] or [sim].
# Ctrl+C stops them; if one exits, the other is stopped too.
# Restarting the live server ends its session: enter a fresh TOTP afterwards.

set -euo pipefail

cd "$(dirname "$0")"

LIVE_PORT=8765
SIM_PORT=8766
WHEN_RUNNING=ask # ask | restart | skip

usage() {
  sed -n '2,13s/^# \{0,1\}//p' "$0"
}

while [ $# -gt 0 ]; do
  case "$1" in
    --live-port) LIVE_PORT="${2:?--live-port needs a number}"; shift 2 ;;
    --sim-port) SIM_PORT="${2:?--sim-port needs a number}"; shift 2 ;;
    --restart) WHEN_RUNNING=restart; shift ;;
    --skip) WHEN_RUNNING=skip; shift ;;
    -h | --help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
done
if [ "$LIVE_PORT" = "$SIM_PORT" ]; then
  echo "The live app and the simulator need different ports." >&2
  exit 2
fi

PYTHON=.venv/bin/python
if [ ! -x "$PYTHON" ]; then
  echo "No virtualenv at .venv - create it first (see README.md, Setup)." >&2
  exit 1
fi
if [ ! -f .env ]; then
  echo "No .env file - copy .env.sample to .env and fill in your credentials." >&2
  exit 1
fi

listener() { # PIDs listening on a TCP port, if any
  lsof -tiTCP:"$1" -sTCP:LISTEN 2>/dev/null || true
}

# What to do with a server already on its port: sets DECISION to "restart" or "skip".
decide() {
  local name=$1 port=$2 pid=$3 answer
  if [ "$WHEN_RUNNING" != ask ]; then
    DECISION=$WHEN_RUNNING
    return
  fi
  if ! { : </dev/tty; } 2>/dev/null; then
    echo "The $name is already running on port $port (PID $pid) and there is no terminal to ask;" \
      "run with --restart or --skip." >&2
    exit 1
  fi
  echo "The $name is already running on port $port (PID $pid)." >/dev/tty
  [ "$name" = "live app" ] && echo "Restarting it ends its session: you will need a fresh TOTP." >/dev/tty
  while true; do
    read -r -p "[r]estart or [s]kip? " answer </dev/tty
    case "$answer" in
      r | R | restart) DECISION=restart; return ;;
      s | S | skip) DECISION=skip; return ;;
      *) echo "Please answer r or s." >&2 ;;
    esac
  done
}

stop_port() {
  local port=$1 pids
  pids=$(listener "$port")
  echo "Stopping the server on port $port (PID ${pids//$'\n'/ })..."
  # shellcheck disable=SC2086 # one or more PIDs
  kill $pids
  for _ in $(seq 50); do # up to 5s for it to let go of the port
    [ -z "$(listener "$port")" ] && return
    sleep 0.1
  done
  echo "Port $port is still in use; stop that process and try again." >&2
  exit 1
}

START_LIVE=1
START_SIM=1
DECISION=
pid=$(listener "$LIVE_PORT")
if [ -n "$pid" ]; then
  decide "live app" "$LIVE_PORT" "${pid//$'\n'/ }"
  if [ "$DECISION" = restart ]; then
    stop_port "$LIVE_PORT"
  else
    START_LIVE=0
  fi
fi
pid=$(listener "$SIM_PORT")
if [ -n "$pid" ]; then
  decide "simulator" "$SIM_PORT" "${pid//$'\n'/ }"
  if [ "$DECISION" = restart ]; then
    stop_port "$SIM_PORT"
  else
    START_SIM=0
  fi
fi

[ "$START_LIVE" -eq 0 ] && echo "Live app:  http://127.0.0.1:$LIVE_PORT (already running, left alone)"
[ "$START_SIM" -eq 0 ] && echo "Simulator: http://127.0.0.1:$SIM_PORT (already running, left alone)"
if [ "$START_LIVE" -eq 0 ] && [ "$START_SIM" -eq 0 ]; then
  echo "Nothing to start."
  exit 0
fi

export PYTHONUNBUFFERED=1
tag() { # prefix each line, flushing at once so the two outputs interleave live
  awk -v t="$1" '{ print t " " $0; fflush() }'
}

PIDS=()
if [ "$START_LIVE" -eq 1 ]; then
  "$PYTHON" -m pnf --port "$LIVE_PORT" > >(tag "[live]") 2>&1 &
  PIDS+=($!)
  echo "Live app:  http://127.0.0.1:$LIVE_PORT"
fi
if [ "$START_SIM" -eq 1 ]; then
  "$PYTHON" -m pnf_sim --port "$SIM_PORT" > >(tag "[sim] ") 2>&1 &
  PIDS+=($!)
  echo "Simulator: http://127.0.0.1:$SIM_PORT"
fi

stop() {
  trap - INT TERM EXIT
  kill "${PIDS[@]}" 2>/dev/null || true
  wait "${PIDS[@]}" 2>/dev/null || true
}
trap 'echo; echo "Stopping..."; stop; exit 0' INT TERM
trap stop EXIT

if [ ${#PIDS[@]} -eq 2 ]; then
  echo "Press Ctrl+C to stop both."
else
  echo "Press Ctrl+C to stop it."
fi

# macOS ships bash 3.2 (no `wait -n`): poll until any server started here exits.
while true; do
  for p in "${PIDS[@]}"; do
    if ! kill -0 "$p" 2>/dev/null; then
      [ ${#PIDS[@]} -eq 2 ] && echo "A server stopped; stopping the other one too." >&2
      exit 1
    fi
  done
  sleep 1
done
