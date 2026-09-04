# Swiss Ephemeris licensing notice

This independent project is an intentionally separate calculation service. It
is licensed as `AGPL-3.0-only`; the full licence text is in [LICENSE](./LICENSE).
It uses `pysweph`, a Python binding that links the Swiss Ephemeris C library.
Swiss Ephemeris is offered by Astrodienst under a dual licence: GNU AGPLv3 or
the Swiss Ephemeris Professional License. Its original notice is preserved in
[UPSTREAM-SWISS-EPHEMERIS-NOTICE.txt](./UPSTREAM-SWISS-EPHEMERIS-NOTICE.txt).

The default service configuration is `SWISS_EPHEMERIS_LICENSE_MODE=agpl`.
Before any network user is allowed to use that configuration, set
`AGPL_SOURCE_URL` to a durable public URL where the complete corresponding
source for this service, its modifications, build instructions, and applicable
licence notices can be obtained. The `/source` endpoint supplies the same
offer; `/v1/chart` refuses calculation requests until that URL exists.

Alternatively, set `SWISS_EPHEMERIS_LICENSE_MODE=professional` only after a
Swiss Ephemeris Professional License covering this service has been obtained
and record a non-secret `SWISS_EPHEMERIS_LICENSE_REFERENCE`. The commercial
licence decision is not supplied or validated by this code.

The Umbra web application must not import this package, its Python environment,
or the Swiss Ephemeris binary. It talks to this service only through a trusted
backend, never directly from a browser. This technical separation does not by
itself determine the legal status of any combined deployment.

Sources:

- https://www.astro.com/swisseph/swisseph.htm
- https://www.gnu.org/licenses/agpl-3.0.html
- https://pypi.org/project/pysweph/
