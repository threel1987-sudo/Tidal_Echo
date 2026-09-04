#!/bin/sh
set -eu

mkdir -p /data/uploads

export RELAY_PORT="${PORT:-8080}"
export LOOP_PORT="${LOOP_PORT:-3020}"
export RELAY_DEFAULT_BRAIN="${RELAY_DEFAULT_BRAIN:-loop}"
export RELAY_LOOP_INGEST_URL="${RELAY_LOOP_INGEST_URL:-http://127.0.0.1:${LOOP_PORT}/loop/ingest}"
export RELAY_URL="${RELAY_URL:-http://127.0.0.1:${RELAY_PORT}}"

python /app/examples/api_loop.py &
loop_pid=$!

cd /app/backend
uvicorn app:app --host 0.0.0.0 --port "$RELAY_PORT" &
relay_pid=$!

cleanup() {
  kill "$loop_pid" 2>/dev/null || true
  kill "$relay_pid" 2>/dev/null || true
}

trap cleanup INT TERM EXIT

while kill -0 "$loop_pid" 2>/dev/null && kill -0 "$relay_pid" 2>/dev/null; do
  sleep 1
done

exit 1
