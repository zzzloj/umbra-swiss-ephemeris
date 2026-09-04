#!/bin/sh
# Run an actual Swiss-data HTTP calculation on loopback only. This is not a
# network deployment and uses a local file source offer because the developer
# already has this repository checked out. Never reuse this configuration for
# a service available to other users.
set -eu

ROOT_DIR="$(CDPATH= cd -- "$(dirname "$0")/.." && pwd)"
PYTHON_BIN="${PYTHON_BIN:-$ROOT_DIR/.venv/bin/python}"
SERVICE_URL="${SWISS_EPHEMERIS_INTEGRATION_URL:-http://127.0.0.1:18080}"
SWE_DATA_DIR="${SWE_EPHEMERIS_DATA_DIR:?Set SWE_EPHEMERIS_DATA_DIR to a directory containing sepl_18.se1 and semo_18.se1.}"
SERVICE_KEY="umbra-local-contract-test-key"
LOG_FILE="${TMPDIR:-/tmp}/umbra-swiss-ephemeris-contract-test.log"

case "$SERVICE_URL" in
  http://127.0.0.1:*|http://localhost:*) ;;
  *)
    echo "Refusing non-loopback integration URL: $SERVICE_URL" >&2
    exit 2
    ;;
esac

if [ ! -f "$SWE_DATA_DIR/sepl_18.se1" ] || [ ! -f "$SWE_DATA_DIR/semo_18.se1" ]; then
  echo "Missing sepl_18.se1 or semo_18.se1 in $SWE_DATA_DIR" >&2
  exit 2
fi

case "$SERVICE_URL" in
  http://127.0.0.1:*) HOST=127.0.0.1; PORT="${SERVICE_URL##*:}" ;;
  http://localhost:*) HOST=127.0.0.1; PORT="${SERVICE_URL##*:}" ;;
esac

cleanup() {
  if [ -n "${SERVICE_PID:-}" ]; then
    kill "$SERVICE_PID" 2>/dev/null || true
    wait "$SERVICE_PID" 2>/dev/null || true
  fi
}
trap cleanup EXIT INT TERM

cd "$ROOT_DIR"
SWISS_EPHEMERIS_LICENSE_MODE=agpl \
AGPL_SOURCE_URL="file://$ROOT_DIR" \
SWISS_EPHEMERIS_SERVICE_KEY="$SERVICE_KEY" \
SWE_EPHE_PATH="$SWE_DATA_DIR" \
"$PYTHON_BIN" -m uvicorn app:api --host "$HOST" --port "$PORT" >"$LOG_FILE" 2>&1 &
SERVICE_PID=$!

attempt=0
until curl --fail --silent "$SERVICE_URL/healthz" >/dev/null; do
  attempt=$((attempt + 1))
  if [ "$attempt" -ge 30 ]; then
    echo "Local service did not become ready; log follows:" >&2
    sed -n '1,160p' "$LOG_FILE" >&2
    exit 1
  fi
  sleep 1
done

SWISS_EPHEMERIS_INTEGRATION_URL="$SERVICE_URL" \
SWISS_EPHEMERIS_INTEGRATION_KEY="$SERVICE_KEY" \
EXPECTED_AGPL_SOURCE_URL="file://$ROOT_DIR" \
"$PYTHON_BIN" tests/test_live_service.py
