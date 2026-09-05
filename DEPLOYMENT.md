# Deployment runbook

This runbook deploys only the independent AGPL Swiss Ephemeris service. It
must run separately from Umbra's private application and is called only by
Umbra's private server-side ephemeris proxy.

## Deployment boundary

```text
browser -> Umbra site -> private Umbra Worker -> private HTTPS/HTTP -> this service
```

There is no browser CORS access, no public chart endpoint, and no shared code
or runtime with the private product. Bind the reference Compose deployment to
loopback, as it does by default. On a cloud platform, remove the public
ingress entirely and allow requests only from the private Umbra Worker
network. Apply a platform rate limit as a second line of defence; the service
key remains mandatory.

## One-time AGPL release gate

Do these steps **before any external user can reach a service backed by this
project**:

1. Publish this complete repository, including `LICENSE`, build files,
   dependency pins and all changes in the running release, to a public source
   host.
2. Create an immutable release or commit URL and set it as `AGPL_SOURCE_URL`.
3. Verify that `GET /source` returns that exact URL without authentication.
4. Retain the release URL and deployed image digest together in the deployment
   record.

Until this is complete, the project may be developed and tested locally only;
it must not be exposed to network users. The service rejects `/v1/chart` in
AGPL mode if the source URL is absent. Architectural separation is an
engineering boundary, not a legal opinion on licence obligations.

## Swiss data

Prepare an authorised host directory with the required Swiss Ephemeris data.
For the current MVP date range, the local test uses `sepl_18.se1` and
`semo_18.se1`. Keep those files outside Git and outside the container image;
mount the directory read-only through `SWE_EPHEMERIS_DATA_DIR`.

The calculator deliberately fails rather than falling back to Moshier data.
`/healthz` also performs a fixed, non-user Swiss-data probe, so it is not
ready for traffic merely because a mount directory exists. When expanding the
supported year range or bodies, update this data set and repeat the integration
check.

## Reference Compose launch

```bash
cp .env.example .env
# Edit the values locally or populate them from the host secret manager.
docker compose --env-file .env up --build -d
docker compose --env-file .env ps
curl --fail http://127.0.0.1:8080/healthz
curl --fail http://127.0.0.1:8080/source
```

`docker compose config` is the first preflight; it must not show empty values
for the source URL, key, or data directory. The service key must be configured
independently on Umbra's private proxy as `SWISS_EPHEMERIS_SERVICE_KEY`.

The repository also includes a safer preflight that checks the immutable AGPL
source URL, the required data-file set, and the Compose shape without starting
a service:

```bash
scripts/preflight-deployment.sh .env
```

## Release verification

1. Run the Python unit suite and `scripts/run-local-contract-test.sh`.
2. Build the exact image and record its digest.
3. Deploy it to the private network with read-only data mount and a secret
   manager-provided key.
4. From the Umbra Worker environment, run the gated TypeScript integration
   test in `prototype/services/ephemeris/src/swiss-upstream.integration.test.ts`.
5. Confirm a request with an incorrect upstream key returns 401, and that a
   missing source URL or missing data gives no chart.

Do not log raw request payloads, birth details, coordinates, headers, or the
service key. Configure encrypted transport between the private Worker and
this service whenever the connection leaves one trusted host.
