# Umbra Swiss Ephemeris service

Copyright (C) 2026 Umbra contributors.

This is an independent `AGPL-3.0-only` Python/ASGI project. It is the only
Umbra component that imports Swiss Ephemeris. The closed Umbra product does
not vendor this code, its Python environment, or its calculator binary: it
may call a deployed instance only through a server-to-server HTTP interface.

It calculates tropical, geocentric ecliptic longitudes in UT for the ten
contract bodies; it can derive five major aspects with a 6° orb and Placidus
house cusps when the request asks for them. It returns measurements and
limitations only. Interpretation remains in Umbra's editorial layer.

## Source availability for AGPL deployments

`LICENSE` contains the complete GNU AGPL v3 text. The upstream Swiss Ephemeris
licence notice is preserved verbatim in
[`UPSTREAM-SWISS-EPHEMERIS-NOTICE.txt`](./UPSTREAM-SWISS-EPHEMERIS-NOTICE.txt).

Before activating an AGPL deployment, publish this repository with its build
instructions, dependency pins, notices, and the exact running changes. Set
`AGPL_SOURCE_URL` to a durable public URL for that complete corresponding
source (preferably an immutable release or commit). `GET /source` returns that
URL without requiring the service key. Until it is configured, `/v1/chart`
refuses calculations.

No public remote has been configured by this local project setup, so it must
not be activated for network users yet.

## Required configuration

```text
SWISS_EPHEMERIS_SERVICE_KEY=replace-with-a-long-random-secret
SWE_EPHE_PATH=/run/secrets/sweph-ephemerides
SWISS_EPHEMERIS_LICENSE_MODE=agpl
AGPL_SOURCE_URL=https://example.com/umbra-swiss-ephemeris-source
```

`SWE_EPHE_PATH` must be a mounted directory of authorised Swiss Ephemeris data
files. The service rejects a calculation if Swiss data is unavailable instead
of silently falling back to a lower-precision source.

For an Astrodienst Professional License deployment, set:

```text
SWISS_EPHEMERIS_LICENSE_MODE=professional
SWISS_EPHEMERIS_LICENSE_REFERENCE=internal-non-secret-reference
```

Read [AGPL-NOTICE.md](./AGPL-NOTICE.md) before starting a service available to
any external user. The separate-process architecture does not replace the
licence decision.

## Run locally

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
uvicorn app:api --host 127.0.0.1 --port 8080
```

The service accepts `POST /v1/chart` with the `ephemeris-request-v1` body and
the `x-umbra-service-key` header. `GET /healthz` is a non-sensitive readiness
probe and does not disclose birth data. In AGPL mode, `GET /source` supplies
the configured public source offer and never exposes configuration secrets.

## Test a real local calculation

With a Python virtual environment available and an authorised local directory
containing `sepl_18.se1` and `semo_18.se1`, run:

```bash
PYTHON_BIN=/path/to/python \
SWE_EPHEMERIS_DATA_DIR=/absolute/path/to/ephemeris-data \
scripts/run-local-contract-test.sh
```

This brings up the service on `127.0.0.1:18080`, verifies its source offer,
authentication, four actual Swiss-data positions, aspects, and Placidus
houses, then shuts it down. Its `file://` source offer is deliberately
restricted to a developer's loopback test and is not a substitute for the
public `AGPL_SOURCE_URL` required by a network deployment. See
[DEPLOYMENT.md](./DEPLOYMENT.md) for the container release sequence.

## Before connecting it to Umbra

1. Add a trusted product-backend proxy; never call this service from the
   browser.
2. Mount authorised Swiss ephemeris data and validate results against approved
   astronomical fixtures.
3. Publish the complete source and configure `AGPL_SOURCE_URL`, or choose a
   Professional License, before external activation.
4. Add deployment-level rate limiting, secret management, privacy retention,
   and redacted observability.
