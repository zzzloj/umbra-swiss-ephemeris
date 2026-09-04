# SPDX-License-Identifier: AGPL-3.0-only

"""Private Swiss Ephemeris calculation service for Umbra.

This process intentionally contains the AGPL-bound calculator. It returns
astronomical data only and must be reached through a trusted backend.
"""

from __future__ import annotations

import hmac
import os
import re
from calendar import monthrange
from dataclasses import dataclass
from datetime import date, datetime, time, timezone
from functools import lru_cache
from itertools import combinations
from pathlib import Path
from typing import Literal, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import swisseph as swe
from fastapi import Depends, FastAPI, Header
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator


api = FastAPI(
    title="Umbra Swiss Ephemeris service",
    version="1.0.0-draft",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)

BODY_CODES = {
    "sun": swe.SUN,
    "moon": swe.MOON,
    "mercury": swe.MERCURY,
    "venus": swe.VENUS,
    "mars": swe.MARS,
    "jupiter": swe.JUPITER,
    "saturn": swe.SATURN,
    "uranus": swe.URANUS,
    "neptune": swe.NEPTUNE,
    "pluto": swe.PLUTO,
    "north_node": swe.TRUE_NODE,
    "lilith_mean": swe.MEAN_APOG,
    "lilith_true": swe.OSCU_APOG,
    "chiron": swe.CHIRON,
    "proserpina": swe.PROSERPINA,
}
BodyId = Literal[
    "sun",
    "moon",
    "mercury",
    "venus",
    "mars",
    "jupiter",
    "saturn",
    "uranus",
    "neptune",
    "pluto",
    "north_node",
    "south_node",
    "lilith_mean",
    "lilith_true",
    "chiron",
    "proserpina",
]

# `proserpina` is calculated from the explicit Swiss Ephemeris orbital-elements
# source. It is not a physical body and should not be represented as one.
ORBITAL_ELEMENT_FACTORS = frozenset({"proserpina"})
EXTENDED_FACTOR_CAPABILITIES = (
    {
        "id": "lilith_mean",
        "label": "Lilith · mean lunar apogee",
        "kind": "lunar_orbital_point",
        "requires": "Swiss Ephemeris lunar data",
    },
    {
        "id": "lilith_true",
        "label": "Lilith · osculating lunar apogee",
        "kind": "lunar_orbital_point",
        "requires": "Swiss Ephemeris lunar data",
    },
    {
        "id": "chiron",
        "label": "Chiron",
        "kind": "minor_body",
        "requires": "Swiss Ephemeris asteroid data (seas_*.se1)",
    },
    {
        "id": "proserpina",
        "label": "Proserpina · h57",
        "kind": "hypothetical_factor",
        "requires": "Swiss Ephemeris orbital-elements file (seorbel.txt)",
    },
)
UNIMPLEMENTED_SWISS_CAPABILITIES = (
    "Additional asteroids: Ceres, Pallas, Juno, Vesta, Pholus, and numbered minor planets.",
    "Fixed-star positions and star-based rises, settings, and transits.",
    "Other lunar nodes and apsides: Mean Node, interpolated apogee, and Priapus.",
    "Alternative house systems and additional chart points such as Vertex and Equatorial Ascendant.",
    "Sidereal zodiac modes and declared ayanamsha choices.",
    "Eclipses, occultations, planetary phenomena, heliacal events, and rise/set/transit times.",
    "Equatorial, horizontal, heliocentric, topocentric, and declination coordinate products.",
    "Planetary nodes, apsides, orbital elements, and distances.",
)
SIGN_NAMES = (
    "Aries",
    "Taurus",
    "Gemini",
    "Cancer",
    "Leo",
    "Virgo",
    "Libra",
    "Scorpio",
    "Sagittarius",
    "Capricorn",
    "Aquarius",
    "Pisces",
)
ASPECTS = {
    "conjunction": 0,
    "sextile": 60,
    "square": 90,
    "trine": 120,
    "opposition": 180,
}
MAJOR_ASPECT_ORB_DEGREES = 6.0


class ServiceError(Exception):
    """A safe error whose message does not contain birth data."""

    def __init__(self, status_code: int, code: str, message: str):
        self.status_code = status_code
        self.code = code
        self.message = message


@api.exception_handler(ServiceError)
async def handle_service_error(_, error: ServiceError):
    return JSONResponse(
        status_code=error.status_code,
        content={"error": {"code": error.code, "message": error.message}},
        headers={"cache-control": "no-store"},
    )


@dataclass(frozen=True)
class Settings:
    service_key: Optional[str]
    ephemeris_path: Optional[Path]
    licence_mode: Literal["agpl", "professional"]
    agpl_source_url: Optional[str]
    professional_licence_reference: Optional[str]


@lru_cache
def settings() -> Settings:
    mode = os.getenv("SWISS_EPHEMERIS_LICENSE_MODE", "agpl").lower()
    if mode not in {"agpl", "professional"}:
        mode = "agpl"
    raw_path = os.getenv("SWE_EPHE_PATH")
    return Settings(
        service_key=os.getenv("SWISS_EPHEMERIS_SERVICE_KEY"),
        ephemeris_path=Path(raw_path) if raw_path else None,
        licence_mode=mode,
        agpl_source_url=os.getenv("AGPL_SOURCE_URL"),
        professional_licence_reference=os.getenv(
            "SWISS_EPHEMERIS_LICENSE_REFERENCE"
        ),
    )


class Location(BaseModel):
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    label: Optional[str] = Field(default=None, max_length=160)


class Birth(BaseModel):
    localDate: str
    localTime: Optional[str] = None
    timeAccuracy: Literal["exact", "approximate", "unknown"]
    timeZone: str
    location: Location

    @field_validator("localDate")
    @classmethod
    def valid_local_date(cls, value: str) -> str:
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            raise ValueError("localDate must use YYYY-MM-DD")
        try:
            date.fromisoformat(value)
        except ValueError as error:
            raise ValueError("localDate must be a real YYYY-MM-DD date") from error
        return value

    @field_validator("timeZone")
    @classmethod
    def valid_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as error:
            raise ValueError("timeZone must be a valid IANA timezone") from error
        return value

    @model_validator(mode="after")
    def time_matches_accuracy(self) -> "Birth":
        if self.timeAccuracy == "unknown":
            if self.localTime is not None:
                raise ValueError("unknown birth time must not include localTime")
            return self
        if self.localTime is None:
            raise ValueError("known birth time requires localTime")
        if not re.fullmatch(r"\d{2}:\d{2}", self.localTime):
            raise ValueError("localTime must use HH:mm")
        try:
            time.fromisoformat(self.localTime)
        except ValueError as error:
            raise ValueError("localTime must use HH:mm") from error
        return self


class ChartRequest(BaseModel):
    version: Literal["ephemeris-request-v1"]
    birth: Birth
    bodies: list[BodyId] = Field(min_length=1, max_length=16)
    features: list[Literal["positions", "aspects", "houses", "angles"]] = Field(
        min_length=1, max_length=4
    )
    limitations: list[str] = Field(default_factory=list, max_length=32)

    @field_validator("limitations")
    @classmethod
    def limits_are_short_text(cls, values: list[str]) -> list[str]:
        if any(len(value) > 500 for value in values):
            raise ValueError("each limitation must be 500 characters or shorter")
        return values

    @model_validator(mode="after")
    def features_match_birth_precision(self) -> "ChartRequest":
        if len(set(self.bodies)) != len(self.bodies):
            raise ValueError("bodies must not contain duplicates")
        if len(set(self.features)) != len(self.features):
            raise ValueError("features must not contain duplicates")
        if "positions" not in self.features:
            raise ValueError("positions are required for every chart request")
        if self.birth.timeAccuracy == "unknown" and (
            "houses" in self.features or "angles" in self.features
        ):
            raise ValueError("houses and angles require a known or approximate birth time")
        return self


class Position(BaseModel):
    body: str
    longitudeDegrees: float
    sign: str
    degreeInSign: float
    retrograde: bool


class Aspect(BaseModel):
    between: tuple[str, str]
    kind: str
    exactAngleDegrees: Literal[0, 60, 90, 120, 180]
    orbDegrees: float


class HouseCusp(BaseModel):
    number: int = Field(ge=1, le=12)
    longitudeDegrees: float = Field(ge=0, lt=360)


class ChartAngle(BaseModel):
    name: Literal["ascendant", "midheaven"]
    longitudeDegrees: float = Field(ge=0, lt=360)
    sign: str
    degreeInSign: float = Field(ge=0, lt=30)


class ChartResponse(BaseModel):
    version: Literal["ephemeris-response-v1"] = "ephemeris-response-v1"
    calculatedAt: str
    request: ChartRequest
    positions: list[Position]
    aspects: Optional[list[Aspect]] = None
    houses: Optional[list[HouseCusp]] = None
    angles: Optional[list[ChartAngle]] = None
    limitations: list[str]


class SolarReturnRequest(BaseModel):
    version: Literal["solar-return-request-v1"]
    natal: Birth
    returnYear: int = Field(ge=1600, le=2600)
    returnLocation: Location
    returnTimeZone: str
    bodies: list[BodyId] = Field(min_length=1, max_length=16)
    features: list[Literal["positions", "aspects", "houses", "angles"]] = Field(
        min_length=1, max_length=4
    )
    limitations: list[str] = Field(default_factory=list, max_length=32)

    @field_validator("returnTimeZone")
    @classmethod
    def valid_return_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as error:
            raise ValueError("returnTimeZone must be a valid IANA timezone") from error
        return value

    @model_validator(mode="after")
    def solar_return_requires_precise_natal_data(self) -> "SolarReturnRequest":
        if self.natal.timeAccuracy != "exact":
            raise ValueError("solar return requires an exact natal birth time")
        if len(set(self.bodies)) != len(self.bodies):
            raise ValueError("bodies must not contain duplicates")
        if len(set(self.features)) != len(self.features):
            raise ValueError("features must not contain duplicates")
        if "positions" not in self.features:
            raise ValueError("positions are required for every solar return")
        if any(len(value) > 500 for value in self.limitations):
            raise ValueError("each limitation must be 500 characters or shorter")
        return self


class SolarReturnResponse(BaseModel):
    version: Literal["solar-return-response-v1"] = "solar-return-response-v1"
    calculatedAt: str
    request: SolarReturnRequest
    returnInstant: str
    returnLocalDateTime: str
    natalSunLongitudeDegrees: float
    positions: list[Position]
    aspects: Optional[list[Aspect]] = None
    houses: Optional[list[HouseCusp]] = None
    angles: Optional[list[ChartAngle]] = None
    limitations: list[str]


class Moment(Birth):
    """An explicitly timed, placed moment used by non-natal chart studies."""

    @model_validator(mode="after")
    def moment_requires_exact_time(self) -> "Moment":
        if self.timeAccuracy != "exact":
            raise ValueError("a chart-study moment requires an exact local time")
        return self


class ChartStudyRequest(BaseModel):
    """Versioned envelope for a named astrological construction.

    Its `inputs` payload is parsed into a method-specific model in the endpoint
    below. This retains an explicit boundary between the available methods
    rather than treating every study as a natal chart with a new label.
    """

    version: Literal["chart-study-request-v1"]
    study: Literal[
        "synastry",
        "horary",
        "electional",
        "transits",
        "progressions",
        "directions",
        "mundane",
    ]
    inputs: dict[str, object]
    bodies: list[BodyId] = Field(min_length=1, max_length=16)
    features: list[Literal["positions", "aspects", "houses", "angles"]] = Field(
        min_length=1, max_length=4
    )
    limitations: list[str] = Field(default_factory=list, max_length=32)

    @model_validator(mode="after")
    def validate_method_envelope(self) -> "ChartStudyRequest":
        if len(set(self.bodies)) != len(self.bodies):
            raise ValueError("bodies must not contain duplicates")
        if len(set(self.features)) != len(self.features):
            raise ValueError("features must not contain duplicates")
        if "positions" not in self.features:
            raise ValueError("positions are required for every chart study")
        if any(len(value) > 500 for value in self.limitations):
            raise ValueError("each limitation must be 500 characters or shorter")
        return self


class SynastryInputs(BaseModel):
    first: Birth
    second: Birth


class MomentInputs(BaseModel):
    moment: Moment


class ElectionalInputs(BaseModel):
    candidates: list[Moment] = Field(min_length=2, max_length=12)


class TransitInputs(BaseModel):
    natal: Birth
    target: Moment


class ProgressionInputs(BaseModel):
    natal: Birth
    target: Moment


class DirectionInputs(BaseModel):
    natal: Birth
    target: Moment
    method: Literal["solar_arc"]

    @model_validator(mode="after")
    def directions_require_precise_natal_data(self) -> "DirectionInputs":
        if self.natal.timeAccuracy != "exact":
            raise ValueError("Solar Arc directions require an exact natal birth time")
        return self


class MundaneEventInputs(BaseModel):
    method: Literal["event"]
    moment: Moment


class MundaneIngressInputs(BaseModel):
    method: Literal["ingress"]
    year: int = Field(ge=1600, le=2600)
    ingress: Literal["aries", "cancer", "libra", "capricorn"]
    location: Location
    timeZone: str

    @field_validator("timeZone")
    @classmethod
    def valid_ingress_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as error:
            raise ValueError("timeZone must be a valid IANA timezone") from error
        return value


class StudyChart(BaseModel):
    id: str
    instant: str
    localDateTime: str
    positions: list[Position]
    aspects: Optional[list[Aspect]] = None
    houses: Optional[list[HouseCusp]] = None
    angles: Optional[list[ChartAngle]] = None
    limitations: list[str]


class CrossAspect(BaseModel):
    firstBody: str
    secondBody: str
    kind: str
    exactAngleDegrees: Literal[0, 60, 90, 120, 180]
    orbDegrees: float


class ChartStudyResponse(BaseModel):
    version: Literal["chart-study-response-v1"] = "chart-study-response-v1"
    calculatedAt: str
    request: ChartStudyRequest
    charts: list[StudyChart]
    crossAspects: Optional[list[CrossAspect]] = None
    directionArcDegrees: Optional[float] = None
    limitations: list[str]


def require_compliant_settings() -> Settings:
    current = settings()
    if current.licence_mode == "agpl" and not current.agpl_source_url:
        raise ServiceError(
            503,
            "agpl_source_unconfigured",
            "AGPL mode needs a public complete-corresponding-source URL before use.",
        )
    if (
        current.licence_mode == "professional"
        and not current.professional_licence_reference
    ):
        raise ServiceError(
            503,
            "professional_licence_unconfigured",
            "Professional licence mode needs a recorded licence reference before use.",
        )
    if not current.service_key:
        raise ServiceError(
            503,
            "service_key_unconfigured",
            "Service authentication has not been configured.",
        )
    return current


def authenticate(
    x_umbra_service_key: Optional[str] = Header(default=None),
) -> Settings:
    current = require_compliant_settings()
    if not x_umbra_service_key or not hmac.compare_digest(
        x_umbra_service_key, current.service_key or ""
    ):
        raise ServiceError(401, "unauthorized", "Service authentication is required.")
    return current


def utc_birth_instant(birth: Birth) -> tuple[datetime, bool]:
    local_clock = time(12, 0) if birth.localTime is None else time.fromisoformat(birth.localTime)
    local_naive = datetime.combine(date.fromisoformat(birth.localDate), local_clock)
    zone = ZoneInfo(birth.timeZone)
    first = local_naive.replace(tzinfo=zone, fold=0)
    round_trip = first.astimezone(timezone.utc).astimezone(zone).replace(tzinfo=None)
    if round_trip != local_naive:
        raise ServiceError(
            422,
            "nonexistent_local_time",
            "This local time did not exist because of a timezone transition.",
        )
    second = local_naive.replace(tzinfo=zone, fold=1)
    if first.utcoffset() != second.utcoffset():
        raise ServiceError(
            422,
            "ambiguous_local_time",
            "This local time occurred twice because of a timezone transition.",
        )
    return first.astimezone(timezone.utc), birth.localTime is None


def julian_day(instant: datetime) -> float:
    decimal_hour = (
        instant.hour
        + instant.minute / 60
        + instant.second / 3600
        + instant.microsecond / 3_600_000_000
    )
    return swe.julday(
        instant.year,
        instant.month,
        instant.day,
        decimal_hour,
        swe.GREG_CAL,
    )


def configure_ephemeris_data(current: Settings) -> None:
    if not current.ephemeris_path or not current.ephemeris_path.is_dir():
        raise ServiceError(
            503,
            "ephemeris_data_unavailable",
            "Authorised Swiss Ephemeris data files are not configured.",
        )
    swe.set_ephe_path(str(current.ephemeris_path))


def swiss_data_is_ready(current: Settings) -> bool:
    """Confirm a configured data directory can make a Swiss-data calculation.

    A directory existence check alone is not meaningful: an empty mount or a
    partial data set would otherwise be marked ready and fail only on the
    first person-facing request. This probe uses a fixed, non-user date and
    still rejects any fallback that does not report the Swiss Ephemeris flag.
    """

    try:
        configure_ephemeris_data(current)
        _, flags, _ = swe.calc_ut(2451545.0, swe.SUN, swe.FLG_SWIEPH)
    except (ServiceError, swe.Error):
        return False
    return bool(flags & swe.FLG_SWIEPH)


def position_for(jd_ut: float, body: str) -> Position:
    if body == "south_node":
        north_node = position_for(jd_ut, "north_node")
        longitude = (north_node.longitudeDegrees + 180) % 360
        sign_index = int(longitude // 30)
        return Position(
            body="south_node",
            longitudeDegrees=round(longitude, 6),
            sign=SIGN_NAMES[sign_index],
            degreeInSign=round(longitude % 30, 6),
            retrograde=north_node.retrograde,
        )
    coordinates, flags, _ = swe.calc_ut(
        jd_ut,
        BODY_CODES[body],
        swe.FLG_SWIEPH | swe.FLG_SPEED,
    )
    if body not in ORBITAL_ELEMENT_FACTORS and not flags & swe.FLG_SWIEPH:
        raise ServiceError(
            503,
            "ephemeris_data_unavailable",
            "Swiss Ephemeris data could not be used for this calculation.",
        )
    longitude = float(coordinates[0]) % 360
    sign_index = int(longitude // 30)
    return Position(
        body=body,
        longitudeDegrees=round(longitude, 6),
        sign=SIGN_NAMES[sign_index],
        degreeInSign=round(longitude % 30, 6),
        retrograde=float(coordinates[3]) < 0,
    )


def factor_is_available(current: Settings, body: str) -> bool:
    """Probe a non-user moment for a factor's configured data dependency."""

    try:
        configure_ephemeris_data(current)
        position_for(2451545.0, body)
    except (ServiceError, swe.Error):
        return False
    return True


def angle_for(name: Literal["ascendant", "midheaven"], longitude: float) -> ChartAngle:
    normalized = float(longitude) % 360
    sign_index = int(normalized // 30)
    return ChartAngle(
        name=name,
        longitudeDegrees=round(normalized, 6),
        sign=SIGN_NAMES[sign_index],
        degreeInSign=round(normalized % 30, 6),
    )


def major_aspects(positions: list[Position]) -> list[Aspect]:
    result: list[Aspect] = []
    for first, second in combinations(positions, 2):
        if {first.body, second.body} == {"north_node", "south_node"}:
            continue
        distance = abs(first.longitudeDegrees - second.longitudeDegrees)
        smaller_distance = min(distance, 360 - distance)
        for kind, exact_angle in ASPECTS.items():
            orb = abs(smaller_distance - exact_angle)
            if orb <= MAJOR_ASPECT_ORB_DEGREES:
                result.append(
                    Aspect(
                        between=(first.body, second.body),
                        kind=kind,
                        exactAngleDegrees=exact_angle,
                        orbDegrees=round(orb, 6),
                    )
                )
                break
    return result


def placidus_geometry(
    jd_ut: float, location: Location
) -> tuple[list[HouseCusp], list[ChartAngle]]:
    cusps, ascmc = swe.houses_ex(
        jd_ut,
        location.latitude,
        location.longitude,
        b"P",
        swe.FLG_SWIEPH,
    )
    return (
        [
            HouseCusp(number=index, longitudeDegrees=round(float(cusps[index]) % 360, 6))
            for index in range(1, 13)
        ],
        [
            angle_for("ascendant", ascmc[0]),
            angle_for("midheaven", ascmc[1]),
        ],
    )


def response_limitations(request: ChartRequest, time_is_unknown: bool) -> list[str]:
    values = list(dict.fromkeys(request.limitations))
    if time_is_unknown:
        values.append(
            "Birth time is unknown. Positions use local noon as a date-level reference; the Moon and aspects can vary within the day."
        )
    if request.birth.timeAccuracy == "approximate":
        values.append(
            "Birth time is approximate. Placidus house cusps and angles should be treated with reduced confidence."
        )
    if "aspects" in request.features:
        values.append("Major aspects use a fixed 6° orb.")
    if "houses" in request.features:
        values.append("House cusps use the Placidus house system.")
    if "angles" in request.features:
        values.append("Angles return direct Ascendant and Midheaven coordinates.")
    return list(dict.fromkeys(values))


def solar_longitude(jd_ut: float) -> float:
    coordinates, flags, _ = swe.calc_ut(jd_ut, swe.SUN, swe.FLG_SWIEPH)
    if not flags & swe.FLG_SWIEPH:
        raise ServiceError(
            503,
            "ephemeris_data_unavailable",
            "Swiss Ephemeris data could not be used for this calculation.",
        )
    return float(coordinates[0]) % 360


def signed_longitude_difference(current: float, target: float) -> float:
    """Return the signed shortest angular difference in [-180, 180)."""

    return (current - target + 180) % 360 - 180


def solar_return_instant(natal_instant: datetime, return_year: int) -> tuple[datetime, float]:
    """Find the exact tropical solar recurrence near the natal calendar date.

    The Sun's longitude is monotonic over the narrow search interval. We use
    Swiss Ephemeris at every evaluation and bisection only after bracketing the
    zero, which keeps the returned instant traceable to the astronomy provider.
    """

    natal_jd = julian_day(natal_instant)
    natal_longitude = solar_longitude(natal_jd)
    last_day = monthrange(return_year, natal_instant.month)[1]
    anchor = datetime(
        return_year,
        natal_instant.month,
        min(natal_instant.day, last_day),
        12,
        tzinfo=timezone.utc,
    )
    lower_jd = julian_day(anchor) - 4
    upper_jd = julian_day(anchor) + 4
    lower_delta = signed_longitude_difference(solar_longitude(lower_jd), natal_longitude)
    upper_delta = signed_longitude_difference(solar_longitude(upper_jd), natal_longitude)
    if lower_delta > 0 or upper_delta < 0:
        raise ServiceError(
            502,
            "solar_return_not_bracketed",
            "Swiss Ephemeris could not bracket the requested solar return.",
        )

    for _ in range(48):
        middle_jd = (lower_jd + upper_jd) / 2
        middle_delta = signed_longitude_difference(
            solar_longitude(middle_jd), natal_longitude
        )
        if middle_delta < 0:
            lower_jd = middle_jd
        else:
            upper_jd = middle_jd

    instant = swe.revjul((lower_jd + upper_jd) / 2, swe.GREG_CAL)
    year, month, day, hour = instant
    whole_hour = int(hour)
    minute_value = (hour - whole_hour) * 60
    whole_minute = int(minute_value)
    second_value = (minute_value - whole_minute) * 60
    return (
        datetime(
            year,
            month,
            day,
            whole_hour,
            whole_minute,
            int(second_value),
            int((second_value % 1) * 1_000_000),
            tzinfo=timezone.utc,
        ),
        natal_longitude,
    )


def solar_return_limitations(request: SolarReturnRequest) -> list[str]:
    values = list(dict.fromkeys(request.limitations))
    values.append(
        "Solar Return is the exact tropical geocentric recurrence of the natal solar longitude."
    )
    values.append(
        "Return houses and angles use the selected return location, not the natal location."
    )
    if "aspects" in request.features:
        values.append("Major aspects use a fixed 6° orb.")
    if "houses" in request.features:
        values.append("House cusps use the Placidus house system.")
    if "angles" in request.features:
        values.append("Angles return direct Ascendant and Midheaven coordinates.")
    return list(dict.fromkeys(values))


def study_moment_instant(moment: Moment) -> datetime:
    instant, is_date_level = utc_birth_instant(moment)
    if is_date_level:
        raise ServiceError(
            422,
            "study_moment_requires_exact_time",
            "This chart study requires an exact local moment.",
        )
    return instant


def utc_datetime_from_jd(jd_ut: float) -> datetime:
    year, month, day, decimal_hour = swe.revjul(jd_ut, swe.GREG_CAL)
    whole_hour = int(decimal_hour)
    minute_value = (decimal_hour - whole_hour) * 60
    whole_minute = int(minute_value)
    second_value = (minute_value - whole_minute) * 60
    whole_second = int(second_value)
    microsecond = int(round((second_value - whole_second) * 1_000_000))
    if microsecond >= 1_000_000:
        whole_second += 1
        microsecond -= 1_000_000
    return datetime(
        year,
        month,
        day,
        whole_hour,
        whole_minute,
        whole_second,
        microsecond,
        tzinfo=timezone.utc,
    )


def chart_at_instant(
    chart_id: str,
    instant: datetime,
    location: Location,
    time_zone: str,
    bodies: list[str],
    features: list[str],
    limitations: list[str],
) -> StudyChart:
    jd_ut = julian_day(instant)
    positions = [position_for(jd_ut, body) for body in bodies]
    geometry = (
        placidus_geometry(jd_ut, location)
        if "houses" in features or "angles" in features
        else None
    )
    return StudyChart(
        id=chart_id,
        instant=instant.isoformat().replace("+00:00", "Z"),
        localDateTime=instant.astimezone(ZoneInfo(time_zone)).isoformat(),
        positions=positions,
        aspects=major_aspects(positions) if "aspects" in features else None,
        houses=geometry[0] if geometry and "houses" in features else None,
        angles=geometry[1] if geometry and "angles" in features else None,
        limitations=list(dict.fromkeys(limitations)),
    )


def chart_from_birth(
    chart_id: str,
    birth: Birth,
    bodies: list[str],
    features: list[str],
    limitations: list[str],
) -> StudyChart:
    instant, is_date_level = utc_birth_instant(birth)
    effective_features = list(features)
    result_limits = list(limitations)
    if is_date_level:
        effective_features = [
            feature for feature in effective_features if feature not in {"houses", "angles"}
        ]
        result_limits.append(
            "Birth time is unknown. Houses and angles are intentionally omitted for this chart."
        )
    elif birth.timeAccuracy == "approximate":
        result_limits.append(
            "Birth time is approximate. Houses and angles are retained with reduced confidence."
        )
    if "aspects" in effective_features:
        result_limits.append("Major aspects use a fixed 6° orb.")
    if "houses" in effective_features:
        result_limits.append("House cusps use the Placidus house system.")
    if "angles" in effective_features:
        result_limits.append("Angles return direct Ascendant and Midheaven coordinates.")
    return chart_at_instant(
        chart_id,
        instant,
        birth.location,
        birth.timeZone,
        bodies,
        effective_features,
        result_limits,
    )


def cross_aspects(
    first_positions: list[Position], second_positions: list[Position]
) -> list[CrossAspect]:
    result: list[CrossAspect] = []
    for first in first_positions:
        for second in second_positions:
            distance = abs(first.longitudeDegrees - second.longitudeDegrees)
            smaller_distance = min(distance, 360 - distance)
            for kind, exact_angle in ASPECTS.items():
                orb = abs(smaller_distance - exact_angle)
                if orb <= MAJOR_ASPECT_ORB_DEGREES:
                    result.append(
                        CrossAspect(
                            firstBody=first.body,
                            secondBody=second.body,
                            kind=kind,
                            exactAngleDegrees=exact_angle,
                            orbDegrees=round(orb, 6),
                        )
                    )
                    break
    return result


def secondary_progressed_instant(natal: datetime, target: datetime) -> datetime:
    elapsed_days = (target - natal).total_seconds() / 86_400
    if elapsed_days < 0:
        raise ServiceError(
            422,
            "target_before_natal",
            "The target moment must be after the natal moment.",
        )
    progressed_jd = julian_day(natal) + elapsed_days / 365.2425
    return utc_datetime_from_jd(progressed_jd)


def rotated_position(position: Position, arc_degrees: float) -> Position:
    longitude = (position.longitudeDegrees + arc_degrees) % 360
    sign_index = int(longitude // 30)
    return Position(
        body=position.body,
        longitudeDegrees=round(longitude, 6),
        sign=SIGN_NAMES[sign_index],
        degreeInSign=round(longitude % 30, 6),
        retrograde=position.retrograde,
    )


def rotate_geometry(
    houses: Optional[list[HouseCusp]],
    angles: Optional[list[ChartAngle]],
    arc_degrees: float,
) -> tuple[Optional[list[HouseCusp]], Optional[list[ChartAngle]]]:
    directed_houses = (
        [
            HouseCusp(
                number=house.number,
                longitudeDegrees=round((house.longitudeDegrees + arc_degrees) % 360, 6),
            )
            for house in houses
        ]
        if houses is not None
        else None
    )
    directed_angles = (
        [angle_for(angle.name, angle.longitudeDegrees + arc_degrees) for angle in angles]
        if angles is not None
        else None
    )
    return directed_houses, directed_angles


INGRESS_DEGREES = {"aries": 0, "cancer": 90, "libra": 180, "capricorn": 270}
INGRESS_ANCHORS = {"aries": (3, 20), "cancer": (6, 21), "libra": (9, 22), "capricorn": (12, 21)}


def ingress_instant(year: int, ingress: str) -> datetime:
    month, day = INGRESS_ANCHORS[ingress]
    target_longitude = INGRESS_DEGREES[ingress]
    anchor = datetime(year, month, day, 12, tzinfo=timezone.utc)
    lower_jd = julian_day(anchor) - 4
    upper_jd = julian_day(anchor) + 4
    lower_delta = signed_longitude_difference(solar_longitude(lower_jd), target_longitude)
    upper_delta = signed_longitude_difference(solar_longitude(upper_jd), target_longitude)
    if lower_delta > 0 or upper_delta < 0:
        raise ServiceError(
            502,
            "ingress_not_bracketed",
            "Swiss Ephemeris could not bracket the requested ingress.",
        )
    for _ in range(48):
        middle_jd = (lower_jd + upper_jd) / 2
        middle_delta = signed_longitude_difference(
            solar_longitude(middle_jd), target_longitude
        )
        if middle_delta < 0:
            lower_jd = middle_jd
        else:
            upper_jd = middle_jd
    return utc_datetime_from_jd((lower_jd + upper_jd) / 2)


def study_inputs(request: ChartStudyRequest) -> BaseModel:
    try:
        if request.study == "synastry":
            return SynastryInputs.model_validate(request.inputs)
        if request.study == "horary":
            return MomentInputs.model_validate(request.inputs)
        if request.study == "electional":
            return ElectionalInputs.model_validate(request.inputs)
        if request.study == "transits":
            return TransitInputs.model_validate(request.inputs)
        if request.study == "progressions":
            return ProgressionInputs.model_validate(request.inputs)
        if request.study == "directions":
            return DirectionInputs.model_validate(request.inputs)
        if request.study == "mundane":
            method = request.inputs.get("method")
            if method == "event":
                return MundaneEventInputs.model_validate(request.inputs)
            if method == "ingress":
                return MundaneIngressInputs.model_validate(request.inputs)
    except ValidationError as error:
        raise ServiceError(
            422,
            "invalid_study_inputs",
            "The selected chart study needs complete, valid source facts.",
        ) from error
    raise ServiceError(
        422,
        "invalid_study_inputs",
        "The selected chart study needs a declared method and valid source facts.",
    )


@api.get("/capabilities")
def capabilities():
    """Publish the calculator surface without accepting or exposing birth data."""

    current = settings()
    return {
        "calculator": "swiss-ephemeris",
        "implemented": {
            "positionFactors": [
                "sun",
                "moon",
                "mercury",
                "venus",
                "mars",
                "jupiter",
                "saturn",
                "uranus",
                "neptune",
                "pluto",
                "north_node",
                "south_node",
                "lilith_mean",
                "lilith_true",
                "chiron",
                "proserpina",
            ],
            "chartStudies": [
                "solar_return",
                "synastry",
                "horary",
                "electional",
                "transits",
                "progressions",
                "directions:solar_arc",
                "mundane:event",
                "mundane:ingress",
            ],
        },
        "extendedFactors": [
            {**factor, "available": factor_is_available(current, factor["id"])}
            for factor in EXTENDED_FACTOR_CAPABILITIES
        ],
        "notImplemented": list(UNIMPLEMENTED_SWISS_CAPABILITIES),
    }


@api.get("/healthz")
def healthz():
    current = settings()
    licence_ready = (
        current.licence_mode == "professional"
        and bool(current.professional_licence_reference)
    ) or (current.licence_mode == "agpl" and bool(current.agpl_source_url))
    ready_for_requests = bool(
        current.service_key
        and licence_ready
        and swiss_data_is_ready(current)
    )
    return {"ok": True, "calculator": "swiss-ephemeris", "ready": ready_for_requests}


@api.get("/source")
def source_offer():
    """Expose the required AGPL source offer without revealing service secrets."""

    current = settings()
    if current.licence_mode != "agpl":
        return {"license": "professional", "sourceUrl": None}
    if not current.agpl_source_url:
        raise ServiceError(
            503,
            "agpl_source_unconfigured",
            "AGPL source availability has not been configured.",
        )
    return {"license": "AGPL-3.0-only", "sourceUrl": current.agpl_source_url}


@api.post("/v1/chart", response_model=ChartResponse)
def calculate_chart(
    request: ChartRequest,
    current: Settings = Depends(authenticate),
) -> ChartResponse:
    configure_ephemeris_data(current)
    instant, time_is_unknown = utc_birth_instant(request.birth)
    jd_ut = julian_day(instant)
    try:
        positions = [position_for(jd_ut, body) for body in request.bodies]
        geometry = (
            placidus_geometry(jd_ut, request.birth.location)
            if "houses" in request.features or "angles" in request.features
            else None
        )
        houses = geometry[0] if geometry and "houses" in request.features else None
        angles = geometry[1] if geometry and "angles" in request.features else None
    except ServiceError:
        raise
    except swe.Error as error:
        raise ServiceError(
            502,
            "swiss_ephemeris_error",
            "Swiss Ephemeris could not complete this calculation.",
        ) from error

    return ChartResponse(
        calculatedAt=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        request=request,
        positions=positions,
        aspects=major_aspects(positions) if "aspects" in request.features else None,
        houses=houses,
        angles=angles,
        limitations=response_limitations(request, time_is_unknown),
    )


@api.post("/v1/solar-return", response_model=SolarReturnResponse)
def calculate_solar_return(
    request: SolarReturnRequest,
    current: Settings = Depends(authenticate),
) -> SolarReturnResponse:
    configure_ephemeris_data(current)
    natal_instant, is_date_level = utc_birth_instant(request.natal)
    if is_date_level:
        raise ServiceError(
            422,
            "solar_return_requires_exact_time",
            "Solar Return requires an exact natal birth time.",
        )
    try:
        return_instant, natal_sun_longitude = solar_return_instant(
            natal_instant, request.returnYear
        )
        return_jd = julian_day(return_instant)
        positions = [position_for(return_jd, body) for body in request.bodies]
        geometry = (
            placidus_geometry(return_jd, request.returnLocation)
            if "houses" in request.features or "angles" in request.features
            else None
        )
        houses = geometry[0] if geometry and "houses" in request.features else None
        angles = geometry[1] if geometry and "angles" in request.features else None
    except ServiceError:
        raise
    except swe.Error as error:
        raise ServiceError(
            502,
            "swiss_ephemeris_error",
            "Swiss Ephemeris could not complete this calculation.",
        ) from error

    return SolarReturnResponse(
        calculatedAt=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        request=request,
        returnInstant=return_instant.isoformat().replace("+00:00", "Z"),
        returnLocalDateTime=return_instant.astimezone(
            ZoneInfo(request.returnTimeZone)
        ).isoformat(),
        natalSunLongitudeDegrees=round(natal_sun_longitude, 6),
        positions=positions,
        aspects=major_aspects(positions) if "aspects" in request.features else None,
        houses=houses,
        angles=angles,
        limitations=solar_return_limitations(request),
    )


@api.post("/v1/chart-study", response_model=ChartStudyResponse)
def calculate_chart_study(
    request: ChartStudyRequest,
    current: Settings = Depends(authenticate),
) -> ChartStudyResponse:
    """Calculate one named chart construction without interpreting it.

    Each branch declares its own source facts and time transformation. The
    endpoint never substitutes a natal chart for a horary, transit, ingress,
    or direction chart merely because all are represented as longitudes.
    """

    configure_ephemeris_data(current)
    inputs = study_inputs(request)
    base_limits = list(request.limitations)
    try:
        if isinstance(inputs, SynastryInputs):
            first = chart_from_birth(
                "first_natal", inputs.first, request.bodies, request.features, base_limits
            )
            second = chart_from_birth(
                "second_natal", inputs.second, request.bodies, request.features, base_limits
            )
            return ChartStudyResponse(
                calculatedAt=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                request=request,
                charts=[first, second],
                crossAspects=(
                    cross_aspects(first.positions, second.positions)
                    if "aspects" in request.features
                    else None
                ),
                limitations=list(
                    dict.fromkeys(
                        base_limits
                        + [
                            "Synastry compares recorded chart factors; it does not claim access to either person's inner state."
                        ]
                    )
                ),
            )

        if isinstance(inputs, MomentInputs):
            moment = study_moment_instant(inputs.moment)
            study_limit = (
                "Horary uses the declared moment the question is received and understood; judging rules remain editorial."
                if request.study == "horary"
                else "Mundane event charts use the declared public event or founding moment."
            )
            chart = chart_at_instant(
                request.study,
                moment,
                inputs.moment.location,
                inputs.moment.timeZone,
                request.bodies,
                request.features,
                base_limits + [study_limit],
            )
            return ChartStudyResponse(
                calculatedAt=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                request=request,
                charts=[chart],
                limitations=list(dict.fromkeys(base_limits + [study_limit])),
            )

        if isinstance(inputs, ElectionalInputs):
            charts = [
                chart_at_instant(
                    f"candidate_{index + 1}",
                    study_moment_instant(candidate),
                    candidate.location,
                    candidate.timeZone,
                    request.bodies,
                    request.features,
                    base_limits
                    + [
                        "Electional candidates are returned without an automatic rank; selection criteria belong to the declared method."
                    ],
                )
                for index, candidate in enumerate(inputs.candidates)
            ]
            return ChartStudyResponse(
                calculatedAt=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                request=request,
                charts=charts,
                limitations=list(
                    dict.fromkeys(
                        base_limits
                        + [
                            "Electional candidates are returned without an automatic rank; selection criteria belong to the declared method."
                        ]
                    )
                ),
            )

        if isinstance(inputs, TransitInputs):
            natal = chart_from_birth(
                "natal", inputs.natal, request.bodies, request.features, base_limits
            )
            target = chart_at_instant(
                "transit",
                study_moment_instant(inputs.target),
                inputs.target.location,
                inputs.target.timeZone,
                request.bodies,
                request.features,
                base_limits
                + [
                    "Transit positions use the declared target moment; cross aspects compare those positions with the natal record."
                ],
            )
            return ChartStudyResponse(
                calculatedAt=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                request=request,
                charts=[natal, target],
                crossAspects=(
                    cross_aspects(natal.positions, target.positions)
                    if "aspects" in request.features
                    else None
                ),
                limitations=list(
                    dict.fromkeys(
                        base_limits
                        + [
                            "Transit positions use the declared target moment; cross aspects compare those positions with the natal record."
                        ]
                    )
                ),
            )

        if isinstance(inputs, ProgressionInputs):
            natal = chart_from_birth(
                "natal", inputs.natal, request.bodies, request.features, base_limits
            )
            natal_instant, _ = utc_birth_instant(inputs.natal)
            target_instant = study_moment_instant(inputs.target)
            progressed_instant = secondary_progressed_instant(natal_instant, target_instant)
            effective_features = (
                [feature for feature in request.features if feature not in {"houses", "angles"}]
                if inputs.natal.timeAccuracy == "unknown"
                else request.features
            )
            progression_limit = (
                "Secondary progressions apply one mean solar day after birth for each tropical year elapsed to the target moment."
            )
            progressed = chart_at_instant(
                "secondary_progressed",
                progressed_instant,
                inputs.natal.location,
                inputs.natal.timeZone,
                request.bodies,
                effective_features,
                base_limits + [progression_limit],
            )
            return ChartStudyResponse(
                calculatedAt=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                request=request,
                charts=[natal, progressed],
                crossAspects=(
                    cross_aspects(natal.positions, progressed.positions)
                    if "aspects" in effective_features
                    else None
                ),
                limitations=list(dict.fromkeys(base_limits + [progression_limit])),
            )

        if isinstance(inputs, DirectionInputs):
            natal = chart_from_birth(
                "natal", inputs.natal, request.bodies, request.features, base_limits
            )
            natal_instant, _ = utc_birth_instant(inputs.natal)
            target_instant = study_moment_instant(inputs.target)
            progressed_instant = secondary_progressed_instant(natal_instant, target_instant)
            arc_degrees = (
                solar_longitude(julian_day(progressed_instant))
                - solar_longitude(julian_day(natal_instant))
            ) % 360
            directed_houses, directed_angles = rotate_geometry(
                natal.houses, natal.angles, arc_degrees
            )
            direction_limit = (
                "Solar Arc directions use the secondary-progressed Sun arc applied uniformly to natal positions, cusps, and angles."
            )
            directed_positions = [
                rotated_position(position, arc_degrees) for position in natal.positions
            ]
            directed = StudyChart(
                id="solar_arc_directed",
                instant=target_instant.isoformat().replace("+00:00", "Z"),
                localDateTime=target_instant.astimezone(
                    ZoneInfo(inputs.target.timeZone)
                ).isoformat(),
                positions=directed_positions,
                aspects=major_aspects(directed_positions)
                if "aspects" in request.features
                else None,
                houses=directed_houses,
                angles=directed_angles,
                limitations=list(dict.fromkeys(base_limits + [direction_limit])),
            )
            return ChartStudyResponse(
                calculatedAt=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                request=request,
                charts=[natal, directed],
                crossAspects=(
                    cross_aspects(natal.positions, directed.positions)
                    if "aspects" in request.features
                    else None
                ),
                directionArcDegrees=round(arc_degrees, 6),
                limitations=list(dict.fromkeys(base_limits + [direction_limit])),
            )

        if isinstance(inputs, MundaneEventInputs):
            moment = study_moment_instant(inputs.moment)
            mundane_limit = "Mundane event charts use the declared public event or founding moment."
            chart = chart_at_instant(
                "mundane_event",
                moment,
                inputs.moment.location,
                inputs.moment.timeZone,
                request.bodies,
                request.features,
                base_limits + [mundane_limit],
            )
            return ChartStudyResponse(
                calculatedAt=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                request=request,
                charts=[chart],
                limitations=list(dict.fromkeys(base_limits + [mundane_limit])),
            )

        if isinstance(inputs, MundaneIngressInputs):
            instant = ingress_instant(inputs.year, inputs.ingress)
            ingress_limit = (
                "Mundane ingress charts use the exact tropical solar ingress and the declared observer location."
            )
            chart = chart_at_instant(
                f"{inputs.ingress}_ingress",
                instant,
                inputs.location,
                inputs.timeZone,
                request.bodies,
                request.features,
                base_limits + [ingress_limit],
            )
            return ChartStudyResponse(
                calculatedAt=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                request=request,
                charts=[chart],
                limitations=list(dict.fromkeys(base_limits + [ingress_limit])),
            )
    except ServiceError:
        raise
    except swe.Error as error:
        raise ServiceError(
            502,
            "swiss_ephemeris_error",
            "Swiss Ephemeris could not complete this chart study.",
        ) from error

    raise ServiceError(
        422,
        "invalid_study_inputs",
        "The selected chart study needs valid source facts.",
    )
