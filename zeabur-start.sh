#!/bin/sh
set -eu

mkdir -p /data/uploads

export RELAY_PORT="${PORT:-8080}"
export LOOP_PORT="${LOOP_PORT:-3020}"
export RELAY_LOOP_INGEST_URL="${RELAY_LOOP_INGEST_URL:-http://127.0.0.1:${LOOP_PORT}/loop/ingest}"
export RELAY_URL="${RELAY_URL:-http://127.0.0.1:${RELAY_PORT}}"

python /app/examples/api_loop.py &
loop_pid=$!

cleanup() {
  kill "$loop_pid" 2>/dev/null || true
}
trap cleanup INT TERM EXIT

cd /app/backend
exec uvicorn app:app --host 0.0.0.0 --port "$RELAY_PORT"
