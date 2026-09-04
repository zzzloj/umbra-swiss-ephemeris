# Umbra Swiss Ephemeris service

Copyright (C) 2026 Umbra contributors.

This is an independent `AGPL-3.0-only` Python/ASGI project. It is the only
Umbra component that imports Swiss Ephemeris. The closed Umbra product does
not vendor this code, its Python environment, or its calculator binary: it
may call a deployed instance only through a server-to-server HTTP interface.

It calculates tropical, geocentric ecliptic longitudes in UT for the ten
contract planets plus the True lunar node axis. The South Node is derived as
the exact opposition to the True North Node. It also accepts mean Lilith
(mean lunar apogee), True Lilith (osculating lunar apogee), Chiron, and
Proserpina h57. Proserpina h57 is an explicitly labelled hypothetical factor,
not an astronomically confirmed planet, and requires `seorbel.txt` in the
configured data directory. These extended factors are opt-in at the product
boundary and can be used in every chart study, including transits, secondary
progressions, and Solar Arc directions.

The service can derive five major aspects with a 6° orb, Placidus house cusps,
and direct Ascendant/Midheaven coordinates when the request asks for them. It
returns measurements and limitations only. It can also calculate a Solar
Return: the instant in a requested year when the tropical geocentric Sun
returns to the natal Sun longitude. A Solar Return requires an exact natal
time; its houses and angles use the selected return location. Interpretation
remains in Umbra's editorial layer. The same isolated service also supports
the remaining named chart studies: synastry, horary, electional candidates,
transits, secondary progressions, explicitly selected Solar Arc directions,
and mundane event or seasonal ingress charts. Each calculation keeps its own
source moment and method in the returned data.

Relationship constructions are deliberately separate. `composite` calculates
shortest-arc midpoints of matching natal longitudes; exact oppositions must use
a declared policy and a composite never pretends to have an event time,
houses, angles, or retrograde state. `coalescent` currently offers only an
explicit `harmonic_sum` formula (sum matching longitudes modulo 360°), because
the name has no universal calculation standard. `davison` is the distinct
time-space midpoint chart: midpoint in UTC time and shortest-arc geographic
midpoint from two exact natal records. `multichart` is a non-merging container
of one to six named natal records and their separately labelled natal,
transit, secondary-progression, Solar Arc, and Solar Return layers.

The service also exposes two astronomical modules. `POST /v1/astrocartography`
returns map-ready MC, IC, ASC, and DSC line geometry from equatorial Swiss
Ephemeris coordinates at one exact moment. `POST /v1/eclipses` returns the
next globally occurring solar and/or lunar eclipses in UTC, with optional
observer-at-maximum geometry. Neither endpoint interprets a location or an
eclipse, recommends actions, or predicts outcomes.

## Source availability for AGPL deployments

`LICENSE` contains the complete GNU AGPL v3 text. The upstream Swiss Ephemeris
licence notice is preserved verbatim in
[`UPSTREAM-SWISS-EPHEMERIS-NOTICE.txt`](./UPSTREAM-SWISS-EPHEMERIS-NOTICE.txt).

Before activating an AGPL deployment, publish this repository with its build
instructions, dependency pins, notices, and the exact running changes. Set
`AGPL_SOURCE_URL` to a durable public URL for that complete corresponding
source (preferably an immutable release or commit). `GET /source` returns that
URL without requiring the service key. Until it is configured, `/v1/chart`,
`/v1/solar-return`, `/v1/chart-study`, `/v1/astrocartography`, and
`/v1/eclipses` refuse calculations.

The complete public source project is hosted at
[`github.com/zzzloj/umbra-swiss-ephemeris`](https://github.com/zzzloj/umbra-swiss-ephemeris).
For a network deployment, configure `AGPL_SOURCE_URL` with the exact immutable
commit URL that corresponds to the deployed image; do not use a moving branch
URL.

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
`POST /v1/solar-return` with the `solar-return-request-v1` body; both require
the `x-umbra-service-key` header. `POST /v1/chart-study` accepts the
`chart-study-request-v1` body for the named non-natal methods; `POST
/v1/astrocartography` accepts `astrocartography-request-v1`; and `POST
/v1/eclipses` accepts `eclipse-search-request-v1`. Each has the same
authentication requirement. `GET /healthz` is a non-sensitive readiness probe
and does not disclose birth data. In AGPL mode, `GET /source` supplies the
configured public source offer and never exposes configuration secrets.
`GET /capabilities` is also non-sensitive: it discloses the supported factor
catalogue, whether the current data mount can calculate each extended factor,
and the Swiss Ephemeris surface not yet exposed by Umbra.

## Test a real local calculation

With a Python virtual environment available and an authorised local directory
containing `sepl_18.se1`, `semo_18.se1`, `seas_18.se1`, and `seorbel.txt`, run:

```bash
PYTHON_BIN=/path/to/python \
SWE_EPHEMERIS_DATA_DIR=/absolute/path/to/ephemeris-data \
scripts/run-local-contract-test.sh
```

This brings up the service on `127.0.0.1:18080`, verifies its source offer,
authentication, four actual Swiss-data positions, aspects, Placidus houses,
one real Solar Return recurrence, every named chart study, astrocartography
line geometry, and an eclipse-search chronology, then shuts it down. Its
`file://` source offer is deliberately
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
