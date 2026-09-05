# Swiss Ephemeris capability boundary

This document describes the current Umbra calculator boundary, not an
interpretation system. Values returned by Swiss Ephemeris are astronomical or
algorithmically derived measurements; any astrological interpretation belongs
to a separately sourced editorial layer.

## Implemented position factors

| Factor | Swiss Ephemeris identifier | Status |
| --- | --- | --- |
| Sun through Pluto | standard body identifiers | Available through the normal Swiss planetary data files. |
| North Node / South Node | `TRUE_NODE` / derived opposition | South Node is exactly derived from the True North Node. |
| Lilith mean | `MEAN_APOG` | Mean lunar apogee; an orbital point. |
| Lilith true | `OSCU_APOG` | Osculating lunar apogee; an orbital point. |
| Chiron | `CHIRON` | Minor body; requires its Swiss asteroid data file. |
| Proserpina h57 | `PROSERPINA` | Hypothetical factor, not an astronomical planet; requires `seorbel.txt`. |

The position-factor list is accepted by `/v1/chart`, `/v1/solar-return`, and
`/v1/chart-study`. Thus explicit selections remain available in synastry,
horary, electional work, transits, secondary progressions, Solar Arc, and
mundane charts.

`/v1/lunar-calendar` provides a seven-to-fifteen-day local-date window with
the geocentric tropical Moon's noon position, phase angle, illumination, and
any sign ingress in the declared timezone. It is astronomical context only;
the service does not label it as a forecast or prescribe a ritual.

The live data dependency of each extended factor is exposed by
`GET /capabilities`. A missing file produces no substituted Moshier result.

## Swiss Ephemeris surface not yet exposed by Umbra

- Ceres, Pallas, Juno, Vesta, Pholus, and numbered asteroids (including the
  distinct physical asteroid 26 Proserpina).
- Fixed stars; star rises, settings, and transits.
- Mean Node, interpolated/natural lunar apogee, Priapus, and planetary nodes
  and apsides.
- Additional house systems and points such as Vertex and Equatorial Ascendant.
- Sidereal zodiac modes and explicit ayanamsha choice.
- Occultations, non-lunar planetary phases, heliacal events, and rise/set and
  meridian-transit calculations.
- Equatorial, horizontal, heliocentric, and topocentric coordinate products,
  declination, distance, and orbital-element outputs.

We do not expose a Swiss feature merely because the library can calculate it:
each addition needs a versioned request/response contract, a data-file policy,
tests, a stated method, and an editorial source decision.

## Technical sources

- [Swiss Ephemeris general documentation](https://www.astro.com/swisseph/swisseph.htm)
  — lunar apogees, Chiron, asteroids, fixed stars, sidereal modes, and the
  broader calculation surface.
- [Swiss Ephemeris programmer's manual](https://www.astro.com/swisseph/swephprg.2.10.htm)
  — `swe_calc_ut`, factor identifiers, and `seorbel.txt` for hypothetical
  orbital elements.
- [Astrodienst hypothetical planet list](https://www.astro.com/swisseph/hyplist.htm)
  — h57 Proserpina and the warning that hypothetical planets are not known to
  exist.
