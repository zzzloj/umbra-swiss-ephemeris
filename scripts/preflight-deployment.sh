#!/bin/sh
# Validate the independently deployable AGPL service configuration without
# starting a container or contacting the calculation endpoint.
set -eu

ROOT_DIR="$(CDPATH= cd -- "$(dirname "$0")/.." && pwd)"
ENV_FILE="${1:-$ROOT_DIR/.env}"

if [ ! -f "$ENV_FILE" ]; then
  echo "Missing deployment environment file: $ENV_FILE" >&2
  exit 2
fi

set -a
. "$ENV_FILE"
set +a

case "${AGPL_SOURCE_URL:-}" in
  https://github.com/*/tree/[0-9a-f][0-9a-f]*) ;;
  *)
    echo "AGPL_SOURCE_URL must be an immutable public GitHub commit URL." >&2
    exit 2
    ;;
esac

if [ -z "${SWISS_EPHEMERIS_SERVICE_KEY:-}" ] || [ "${#SWISS_EPHEMERIS_SERVICE_KEY}" -lt 32 ]; then
  echo "SWISS_EPHEMERIS_SERVICE_KEY must be at least 32 characters." >&2
  exit 2
fi

if [ ! -d "${SWE_EPHEMERIS_DATA_DIR:-}" ]; then
  echo "SWE_EPHEMERIS_DATA_DIR must name an authorised data directory." >&2
  exit 2
fi

for data_file in sepl_18.se1 semo_18.se1 seas_18.se1 seorbel.txt; do
  if [ ! -f "$SWE_EPHEMERIS_DATA_DIR/$data_file" ]; then
    echo "Missing required Swiss Ephemeris data file: $data_file" >&2
    exit 2
  fi
done

docker compose --env-file "$ENV_FILE" -f "$ROOT_DIR/compose.yaml" config >/dev/null
echo "Preflight passed. The service may now be built and deployed to its private network."
