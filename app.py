# SPDX-License-Identifier: AGPL-3.0-only

"""Private Swiss Ephemeris calculation service for Umbra.

This process intentionally contains the AGPL-bound calculator. It returns
astronomical data only and must be reached through a trusted backend.
"""

from __future__ import annotations

import hmac
import math
import os
import re
from calendar import monthrange
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
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
    version="1.1.0-draft",
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
FORMULA_REQUIRED_BODIES = frozenset(
    {
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
    }
)

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
    "Occultations, planetary phenomena, heliacal events, and rise/set/transit times.",
    "Horizontal, heliocentric, topocentric, and declination coordinate products beyond astrocartography line inputs.",
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
        "composite",
        "coalescent",
        "davison",
        "multichart",
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


class CompositeInputs(BaseModel):
    """Two natal records for a shortest-arc midpoint composite."""

    first: Birth
    second: Birth
    oppositionPolicy: Literal["omit", "first", "second"] = "omit"


class CoalescentInputs(BaseModel):
    """A deliberately named, non-standard harmonic-sum construction.

    Coalescent has no single accepted calculation standard. Requiring the
    method field ensures an interface cannot market a convenient midpoint as a
    different traditional technique.
    """

    first: Birth
    second: Birth
    method: Literal["harmonic_sum"]


class DavisonInputs(BaseModel):
    """A time-space midpoint relationship chart, commonly called Davison."""

    first: Birth
    second: Birth

    @model_validator(mode="after")
    def davison_requires_exact_source_times(self) -> "DavisonInputs":
        if self.first.timeAccuracy != "exact" or self.second.timeAccuracy != "exact":
            raise ValueError("Davison requires an exact recorded time for both natal inputs")
        return self


class MultiChartParticipant(BaseModel):
    id: str = Field(min_length=1, max_length=48, pattern=r"^[a-zA-Z0-9_-]+$")
    natal: Birth


class MultiChartLayer(BaseModel):
    """One explicitly declared layer, reused for each named participant."""

    kind: Literal["natal", "transits", "progressions", "directions", "solar_return"]
    target: Optional[Moment] = None
    method: Optional[Literal["solar_arc"]] = None
    returnYear: Optional[int] = Field(default=None, ge=1600, le=2600)
    returnLocation: Optional[Location] = None
    returnTimeZone: Optional[str] = None

    @field_validator("returnTimeZone")
    @classmethod
    def valid_return_timezone(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return value
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as error:
            raise ValueError("returnTimeZone must be a valid IANA timezone") from error
        return value

    @model_validator(mode="after")
    def layer_has_its_own_source_facts(self) -> "MultiChartLayer":
        if self.kind in {"transits", "progressions", "directions"} and self.target is None:
            raise ValueError(f"{self.kind} layer requires an exact target moment")
        if self.kind == "directions" and self.method != "solar_arc":
            raise ValueError("directions layer requires method solar_arc")
        if self.kind == "solar_return" and (
            self.returnYear is None
            or self.returnLocation is None
            or self.returnTimeZone is None
        ):
            raise ValueError(
                "solar_return layer requires returnYear, returnLocation, and returnTimeZone"
            )
        return self


class MultiChartInputs(BaseModel):
    participants: list[MultiChartParticipant] = Field(min_length=1, max_length=6)
    layers: list[MultiChartLayer] = Field(min_length=1, max_length=5)

    @model_validator(mode="after")
    def participants_and_layers_are_distinct(self) -> "MultiChartInputs":
        participant_ids = [participant.id for participant in self.participants]
        layer_ids = [layer.kind for layer in self.layers]
        if len(set(participant_ids)) != len(participant_ids):
            raise ValueError("multichart participant ids must be unique")
        if len(set(layer_ids)) != len(layer_ids):
            raise ValueError("multichart layers must not repeat a kind")
        return self


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


class DerivedPosition(BaseModel):
    """A synthetic longitude has no meaningful instantaneous retrograde flag."""

    body: str
    longitudeDegrees: float = Field(ge=0, lt=360)
    sign: str
    degreeInSign: float = Field(ge=0, lt=30)


class MidpointAmbiguity(BaseModel):
    body: str
    firstLongitudeDegrees: float = Field(ge=0, lt=360)
    secondLongitudeDegrees: float = Field(ge=0, lt=360)


class DerivedChart(BaseModel):
    """A constructed set of longitudes, intentionally not presented as an event chart."""

    id: str
    construction: Literal["midpoint_composite", "harmonic_sum_coalescent"]
    positions: list[DerivedPosition]
    aspects: Optional[list[Aspect]] = None
    midpointAmbiguities: list[MidpointAmbiguity] = Field(default_factory=list)
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
    derivedCharts: Optional[list[DerivedChart]] = None
    crossAspects: Optional[list[CrossAspect]] = None
    directionArcDegrees: Optional[float] = None
    limitations: list[str]


class AstrocartographyRequest(BaseModel):
    version: Literal["astrocartography-request-v1"]
    moment: Moment
    bodies: list[BodyId] = Field(min_length=1, max_length=16)
    angles: list[Literal["mc", "ic", "asc", "dsc"]] = Field(
        min_length=1, max_length=4
    )
    latitudeStepDegrees: int = Field(default=2, ge=1, le=10)
    limitations: list[str] = Field(default_factory=list, max_length=32)

    @model_validator(mode="after")
    def astrocartography_request_is_finite(self) -> "AstrocartographyRequest":
        if len(set(self.bodies)) != len(self.bodies):
            raise ValueError("bodies must not contain duplicates")
        if len(set(self.angles)) != len(self.angles):
            raise ValueError("angles must not contain duplicates")
        if any(len(value) > 500 for value in self.limitations):
            raise ValueError("each limitation must be 500 characters or shorter")
        return self


class AstrocartographyPoint(BaseModel):
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)


class AstrocartographyLine(BaseModel):
    body: str
    angle: Literal["mc", "ic", "asc", "dsc"]
    points: list[AstrocartographyPoint] = Field(min_length=1)


class AstrocartographyResponse(BaseModel):
    version: Literal["astrocartography-response-v1"] = "astrocartography-response-v1"
    calculatedAt: str
    request: AstrocartographyRequest
    lines: list[AstrocartographyLine]
    limitations: list[str]


class EclipseSearchRequest(BaseModel):
    version: Literal["eclipse-search-request-v1"]
    start: Moment
    kinds: list[Literal["solar", "lunar"]] = Field(min_length=1, max_length=2)
    count: int = Field(default=6, ge=1, le=12)
    observer: Optional[Location] = None
    limitations: list[str] = Field(default_factory=list, max_length=32)

    @model_validator(mode="after")
    def eclipse_search_is_finite(self) -> "EclipseSearchRequest":
        if len(set(self.kinds)) != len(self.kinds):
            raise ValueError("kinds must not contain duplicates")
        if any(len(value) > 500 for value in self.limitations):
            raise ValueError("each limitation must be 500 characters or shorter")
        return self


class EclipseObserverVisibility(BaseModel):
    visible: bool
    maximumVisible: bool
    magnitude: Optional[float] = None
    sarosSeries: Optional[int] = None
    sarosMember: Optional[int] = None


class EclipseEvent(BaseModel):
    kind: Literal["solar", "lunar"]
    classification: Literal["total", "annular", "partial", "hybrid", "penumbral"]
    maximumInstant: str
    contacts: dict[str, str]
    observerVisibility: Optional[EclipseObserverVisibility] = None


class EclipseSearchResponse(BaseModel):
    version: Literal["eclipse-search-response-v1"] = "eclipse-search-response-v1"
    calculatedAt: str
    request: EclipseSearchRequest
    eclipses: list[EclipseEvent]
    limitations: list[str]


class LunarCalendarRequest(BaseModel):
    """A short local-date window for public astronomical moon context."""

    version: Literal["lunar-calendar-request-v1"]
    anchorLocalDate: str
    timeZone: str
    daysBefore: int = Field(default=3, ge=0, le=7)
    daysAfter: int = Field(default=3, ge=0, le=7)
    limitations: list[str] = Field(default_factory=list, max_length=32)

    @field_validator("anchorLocalDate")
    @classmethod
    def valid_anchor_local_date(cls, value: str) -> str:
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            raise ValueError("anchorLocalDate must use YYYY-MM-DD")
        try:
            date.fromisoformat(value)
        except ValueError as error:
            raise ValueError("anchorLocalDate must be a real YYYY-MM-DD date") from error
        return value

    @field_validator("timeZone")
    @classmethod
    def valid_lunar_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as error:
            raise ValueError("timeZone must be a valid IANA timezone") from error
        return value

    @model_validator(mode="after")
    def lunar_calendar_window_is_finite(self) -> "LunarCalendarRequest":
        if any(len(value) > 500 for value in self.limitations):
            raise ValueError("each limitation must be 500 characters or shorter")
        return self


class LunarIngress(BaseModel):
    instant: str
    fromSign: str
    toSign: str


class LunarCalendarDay(BaseModel):
    localDate: str
    referenceInstant: str
    moon: Position
    phaseAngleDegrees: float = Field(ge=0, lt=360)
    illuminationFraction: float = Field(ge=0, le=1)
    ingress: Optional[LunarIngress] = None


class LunarCalendarResponse(BaseModel):
    version: Literal["lunar-calendar-response-v1"] = "lunar-calendar-response-v1"
    calculatedAt: str
    request: LunarCalendarRequest
    days: list[LunarCalendarDay]
    limitations: list[str]


class FormulaSchoolReference(BaseModel):
    """Provenance travels with a rule set instead of being implied by a label."""

    id: str = Field(min_length=1, max_length=80, pattern=r"^[a-z0-9][a-z0-9_-]*$")
    version: str = Field(min_length=1, max_length=40)
    attribution: str = Field(min_length=1, max_length=240)
    licence: Literal["neutral", "user_supplied", "licensed"]


class FormulaClause(BaseModel):
    id: str = Field(min_length=1, max_length=80, pattern=r"^[a-zA-Z0-9_-]+$")
    sourceHouse: int = Field(ge=1, le=12)
    targetHouse: int = Field(ge=1, le=12)
    relation: Literal["ruler_in_house", "aspect"]
    sourceRole: Literal["ruler", "occupant", "any"] = "any"
    targetRole: Literal["ruler", "occupant", "any"] = "any"

    @model_validator(mode="after")
    def relation_has_valid_roles(self) -> "FormulaClause":
        if self.relation == "ruler_in_house" and self.sourceRole not in {
            "ruler",
            "any",
        }:
            raise ValueError("ruler_in_house requires a ruler or any source role")
        return self


class EventFormula(BaseModel):
    id: str = Field(min_length=1, max_length=80, pattern=r"^[a-zA-Z0-9_-]+$")
    title: str = Field(min_length=1, max_length=160)
    school: FormulaSchoolReference
    operator: Literal["all", "any"] = "all"
    clauses: list[FormulaClause] = Field(min_length=1, max_length=24)

    @model_validator(mode="after")
    def formula_clause_ids_are_unique(self) -> "EventFormula":
        ids = [clause.id for clause in self.clauses]
        if len(set(ids)) != len(ids):
            raise ValueError("formula clause ids must be unique")
        return self


class EventFormulaRequest(BaseModel):
    version: Literal["event-formula-request-v1"]
    birth: Birth
    bodies: list[BodyId] = Field(min_length=10, max_length=16)
    rulershipProfile: Literal["traditional", "modern"]
    formula: EventFormula
    limitations: list[str] = Field(default_factory=list, max_length=32)

    @model_validator(mode="after")
    def formula_request_has_complete_graph_factors(self) -> "EventFormulaRequest":
        if self.birth.timeAccuracy == "unknown":
            raise ValueError("event formulas require a known or approximate birth time")
        if len(set(self.bodies)) != len(self.bodies):
            raise ValueError("bodies must not contain duplicates")
        missing = set(FORMULA_REQUIRED_BODIES) - set(self.bodies)
        if missing:
            raise ValueError("event formulas require all ten contract planets")
        if any(len(value) > 500 for value in self.limitations):
            raise ValueError("each limitation must be 500 characters or shorter")
        return self


class FormulaHouseElement(BaseModel):
    house: int = Field(ge=1, le=12)
    body: str
    role: Literal["ruler", "occupant"]


class FormulaEvidence(BaseModel):
    relation: Literal["ruler_in_house", "aspect"]
    sourceHouse: int = Field(ge=1, le=12)
    sourceBody: str
    sourceRole: Literal["ruler", "occupant"]
    targetHouse: int = Field(ge=1, le=12)
    targetBody: Optional[str] = None
    targetRole: Optional[Literal["ruler", "occupant", "location"]] = None
    aspect: Optional[Aspect] = None


class FormulaClauseResult(BaseModel):
    clauseId: str
    matched: bool
    evidence: list[FormulaEvidence]


class EventFormulaResponse(BaseModel):
    version: Literal["event-formula-response-v1"] = "event-formula-response-v1"
    calculatedAt: str
    request: EventFormulaRequest
    positions: list[Position]
    houses: list[HouseCusp]
    aspects: list[Aspect]
    houseElements: list[FormulaHouseElement]
    clauseResults: list[FormulaClauseResult]
    matched: bool
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


def utc_iso(instant: datetime) -> str:
    return instant.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def local_day_bounds(local_day: date, zone: ZoneInfo) -> tuple[datetime, datetime]:
    """Return the declared civil-day bounds as UTC instants.

    Calendar context is organised by the person's local dates, not fixed 24h
    UTC segments. ZoneInfo applies the correct offset on either side of a
    daylight-saving change; the public reference value remains local noon.
    """

    start = datetime.combine(local_day, time.min, tzinfo=zone).astimezone(timezone.utc)
    end = datetime.combine(
        local_day + timedelta(days=1), time.min, tzinfo=zone
    ).astimezone(timezone.utc)
    return start, end


def lunar_ingress_between(start: datetime, end: datetime) -> Optional[LunarIngress]:
    """Find the one Moon sign ingress that can occur in a civil day.

    The Moon cannot traverse two full signs in this bounded interval. We first
    bracket a sign change at the local-day edges, then bisect against Swiss
    Ephemeris positions. Returning no ingress means the sign did not change
    during that local date, not that a transit was inferred from a phase.
    """

    start_jd = julian_day(start)
    end_jd = julian_day(end)
    first = position_for(start_jd, "moon")
    final = position_for(end_jd, "moon")
    if first.sign == final.sign:
        return None

    lower_jd = start_jd
    upper_jd = end_jd
    for _ in range(40):
        middle_jd = (lower_jd + upper_jd) / 2
        if position_for(middle_jd, "moon").sign == first.sign:
            lower_jd = middle_jd
        else:
            upper_jd = middle_jd

    return LunarIngress(
        instant=utc_iso(utc_datetime_from_jd(upper_jd)),
        fromSign=first.sign,
        toSign=final.sign,
    )


def lunar_calendar_days(request: LunarCalendarRequest) -> list[LunarCalendarDay]:
    """Calculate one compact moon week at local noon for each civil date."""

    zone = ZoneInfo(request.timeZone)
    anchor = date.fromisoformat(request.anchorLocalDate)
    result: list[LunarCalendarDay] = []
    for offset in range(-request.daysBefore, request.daysAfter + 1):
        local_day = anchor + timedelta(days=offset)
        local_noon = datetime.combine(local_day, time(12), tzinfo=zone).astimezone(
            timezone.utc
        )
        jd_ut = julian_day(local_noon)
        moon = position_for(jd_ut, "moon")
        sun = position_for(jd_ut, "sun")
        phase_angle = (moon.longitudeDegrees - sun.longitudeDegrees) % 360
        illumination = (1 - math.cos(math.radians(phase_angle))) / 2
        start, end = local_day_bounds(local_day, zone)
        result.append(
            LunarCalendarDay(
                localDate=local_day.isoformat(),
                referenceInstant=utc_iso(local_noon),
                moon=moon,
                phaseAngleDegrees=round(phase_angle, 6),
                illuminationFraction=round(illumination, 6),
                ingress=lunar_ingress_between(start, end),
            )
        )
    return result


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


TRADITIONAL_SIGN_RULERS = {
    "Aries": ("mars",),
    "Taurus": ("venus",),
    "Gemini": ("mercury",),
    "Cancer": ("moon",),
    "Leo": ("sun",),
    "Virgo": ("mercury",),
    "Libra": ("venus",),
    "Scorpio": ("mars",),
    "Sagittarius": ("jupiter",),
    "Capricorn": ("saturn",),
    "Aquarius": ("saturn",),
    "Pisces": ("jupiter",),
}
MODERN_SIGN_RULERS = {
    **TRADITIONAL_SIGN_RULERS,
    "Scorpio": ("pluto",),
    "Aquarius": ("uranus",),
    "Pisces": ("neptune",),
}


def house_for_longitude(longitude: float, houses: list[HouseCusp]) -> int:
    """Assign a longitude to the explicitly returned Placidus house intervals."""

    if len(houses) != 12:
        raise ServiceError(
            502,
            "formula_houses_unavailable",
            "Formula evaluation needs twelve calculated house cusps.",
        )
    by_number = {house.number: house.longitudeDegrees for house in houses}
    if len(by_number) != 12:
        raise ServiceError(
            502,
            "formula_houses_unavailable",
            "Formula evaluation needs twelve distinct house cusps.",
        )
    for number in range(1, 13):
        start = by_number[number]
        end = by_number[1 if number == 12 else number + 1]
        span = (end - start) % 360
        distance = (longitude - start) % 360
        if span > 0 and distance < span:
            return number
    raise ServiceError(
        502,
        "formula_house_assignment_failed",
        "Formula evaluation could not assign a factor to a calculated house.",
    )


def sign_for_longitude(longitude: float) -> str:
    return SIGN_NAMES[int((longitude % 360) // 30)]


def formula_house_elements(
    positions: list[Position],
    houses: list[HouseCusp],
    rulership_profile: Literal["traditional", "modern"],
) -> list[FormulaHouseElement]:
    """Create transparent house occupants and cusp rulers for a rule graph."""

    positions_by_body = {position.body: position for position in positions}
    result: list[FormulaHouseElement] = []
    for position in positions:
        result.append(
            FormulaHouseElement(
                house=house_for_longitude(position.longitudeDegrees, houses),
                body=position.body,
                role="occupant",
            )
        )
    ruler_map = (
        TRADITIONAL_SIGN_RULERS
        if rulership_profile == "traditional"
        else MODERN_SIGN_RULERS
    )
    for house in houses:
        sign = sign_for_longitude(house.longitudeDegrees)
        for ruler in ruler_map[sign]:
            if ruler not in positions_by_body:
                raise ServiceError(
                    422,
                    "formula_ruler_missing",
                    "Formula evaluation needs the complete declared rulership profile.",
                )
            result.append(FormulaHouseElement(house=house.number, body=ruler, role="ruler"))
    return result


def formula_role_matches(
    element: FormulaHouseElement, requested: Literal["ruler", "occupant", "any"]
) -> bool:
    return requested == "any" or element.role == requested


def formula_aspect_lookup(aspects: list[Aspect]) -> dict[frozenset[str], Aspect]:
    return {frozenset(aspect.between): aspect for aspect in aspects}


def formula_clause_results(
    formula: EventFormula,
    positions: list[Position],
    houses: list[HouseCusp],
    aspects: list[Aspect],
    rulership_profile: Literal["traditional", "modern"],
) -> tuple[list[FormulaHouseElement], list[FormulaClauseResult]]:
    elements = formula_house_elements(positions, houses, rulership_profile)
    positions_by_body = {position.body: position for position in positions}
    aspect_by_bodies = formula_aspect_lookup(aspects)
    results: list[FormulaClauseResult] = []
    for clause in formula.clauses:
        sources = [
            element
            for element in elements
            if element.house == clause.sourceHouse
            and formula_role_matches(element, clause.sourceRole)
        ]
        evidence: list[FormulaEvidence] = []
        if clause.relation == "ruler_in_house":
            for source in sources:
                if source.role != "ruler":
                    continue
                if house_for_longitude(
                    positions_by_body[source.body].longitudeDegrees, houses
                ) == clause.targetHouse:
                    evidence.append(
                        FormulaEvidence(
                            relation="ruler_in_house",
                            sourceHouse=clause.sourceHouse,
                            sourceBody=source.body,
                            sourceRole=source.role,
                            targetHouse=clause.targetHouse,
                            targetBody=source.body,
                            targetRole="location",
                        )
                    )
        else:
            targets = [
                element
                for element in elements
                if element.house == clause.targetHouse
                and formula_role_matches(element, clause.targetRole)
            ]
            for source in sources:
                for target in targets:
                    if source.body == target.body:
                        continue
                    aspect = aspect_by_bodies.get(frozenset({source.body, target.body}))
                    if aspect is not None:
                        evidence.append(
                            FormulaEvidence(
                                relation="aspect",
                                sourceHouse=clause.sourceHouse,
                                sourceBody=source.body,
                                sourceRole=source.role,
                                targetHouse=clause.targetHouse,
                                targetBody=target.body,
                                targetRole=target.role,
                                aspect=aspect,
                            )
                        )
        results.append(
            FormulaClauseResult(
                clauseId=clause.id,
                matched=bool(evidence),
                evidence=evidence,
            )
        )
    return elements, results


def event_formula_limitations(request: EventFormulaRequest) -> list[str]:
    values = list(request.limitations)
    values.extend(
        [
            "The engine evaluates only the declared structural clauses. A match is not a prediction, probability, diagnosis, or statement that an event will occur.",
            "Formula attribution, version, and licence are returned with the rule. The service ships no proprietary or school-specific formula catalogue.",
            f"House rulers use the explicitly selected {request.rulershipProfile} profile; formula schools can provide separately versioned profiles later.",
            "Major aspects use a fixed 6° orb.",
            "House cusps use the Placidus house system.",
        ]
    )
    if request.birth.timeAccuracy == "approximate":
        values.append(
            "Birth time is approximate. House-based formula evidence has reduced confidence."
        )
    return list(dict.fromkeys(values))


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


def derived_position(body: str, longitude: float) -> DerivedPosition:
    normalized = float(longitude) % 360
    sign_index = int(normalized // 30)
    return DerivedPosition(
        body=body,
        longitudeDegrees=round(normalized, 6),
        sign=SIGN_NAMES[sign_index],
        degreeInSign=round(normalized % 30, 6),
    )


def derived_aspects(positions: list[DerivedPosition]) -> list[Aspect]:
    """Use the declared aspect geometry without fabricating a motion flag."""

    return major_aspects(
        [
            Position(
                body=position.body,
                longitudeDegrees=position.longitudeDegrees,
                sign=position.sign,
                degreeInSign=position.degreeInSign,
                retrograde=False,
            )
            for position in positions
        ]
    )


def midpoint_longitude(
    first: float,
    second: float,
    opposition_policy: Literal["omit", "first", "second"],
) -> Optional[float]:
    """Calculate a shortest-arc midpoint while exposing a 180° ambiguity.

    A pair separated by exactly 180° has two equally short paths. The caller
    has to disclose whether it omits that synthetic factor or chooses a stated
    source-side convention.
    """

    shortest = (second - first + 180) % 360 - 180
    if math.isclose(abs(shortest), 180, abs_tol=1e-8):
        if opposition_policy == "omit":
            return None
        return first if opposition_policy == "first" else second
    return (first + shortest / 2) % 360


def midpoint_composite(
    first: list[Position],
    second: list[Position],
    opposition_policy: Literal["omit", "first", "second"],
) -> tuple[list[DerivedPosition], list[MidpointAmbiguity]]:
    positions: list[DerivedPosition] = []
    ambiguities: list[MidpointAmbiguity] = []
    second_by_body = {position.body: position for position in second}
    for first_position in first:
        second_position = second_by_body[first_position.body]
        longitude = midpoint_longitude(
            first_position.longitudeDegrees,
            second_position.longitudeDegrees,
            opposition_policy,
        )
        if longitude is None:
            ambiguities.append(
                MidpointAmbiguity(
                    body=first_position.body,
                    firstLongitudeDegrees=first_position.longitudeDegrees,
                    secondLongitudeDegrees=second_position.longitudeDegrees,
                )
            )
            continue
        positions.append(derived_position(first_position.body, longitude))
    return positions, ambiguities


def harmonic_sum_coalescent(
    first: list[Position], second: list[Position]
) -> list[DerivedPosition]:
    """Return an explicitly labelled harmonic-sum coalescent variant only."""

    second_by_body = {position.body: position for position in second}
    return [
        derived_position(
            first_position.body,
            first_position.longitudeDegrees
            + second_by_body[first_position.body].longitudeDegrees,
        )
        for first_position in first
    ]


def shortest_arc_geographic_midpoint(first: float, second: float) -> float:
    """Geographic longitude midpoint with the same exposed antipode guard."""

    midpoint = midpoint_longitude(first % 360, second % 360, "omit")
    if midpoint is None:
        raise ServiceError(
            422,
            "davison_longitude_ambiguous",
            "Davison cannot infer one geographic midpoint from exactly opposite longitudes.",
        )
    return midpoint - 360 if midpoint > 180 else midpoint


def davison_midpoint_context(inputs: DavisonInputs) -> tuple[datetime, Location]:
    first_instant, first_is_date_level = utc_birth_instant(inputs.first)
    second_instant, second_is_date_level = utc_birth_instant(inputs.second)
    if first_is_date_level or second_is_date_level:
        raise ServiceError(
            422,
            "davison_requires_exact_times",
            "Davison requires an exact recorded time for both natal inputs.",
        )
    instant = first_instant + (second_instant - first_instant) / 2
    location = Location(
        latitude=(inputs.first.location.latitude + inputs.second.location.latitude) / 2,
        longitude=shortest_arc_geographic_midpoint(
            inputs.first.location.longitude, inputs.second.location.longitude
        ),
        label="Geographic midpoint of the two declared natal locations",
    )
    return instant, location


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


def equatorial_coordinates(jd_ut: float, body: str) -> tuple[float, float]:
    """Return apparent right ascension and declination in degrees."""

    if body == "south_node":
        north_ra, north_declination = equatorial_coordinates(jd_ut, "north_node")
        return (north_ra + 180) % 360, -north_declination
    coordinates, flags, _ = swe.calc_ut(
        jd_ut,
        BODY_CODES[body],
        swe.FLG_SWIEPH | swe.FLG_EQUATORIAL,
    )
    if body not in ORBITAL_ELEMENT_FACTORS and not flags & swe.FLG_SWIEPH:
        raise ServiceError(
            503,
            "ephemeris_data_unavailable",
            "Swiss Ephemeris data could not be used for this calculation.",
        )
    return float(coordinates[0]) % 360, float(coordinates[1])


def geographic_longitude(value: float) -> float:
    normalized = (value + 180) % 360 - 180
    return round(normalized, 6)


def astrocartography_lines(
    instant: datetime,
    bodies: list[str],
    angles: list[str],
    latitude_step: int,
) -> list[AstrocartographyLine]:
    """Sample geographic angular lines from Swiss equatorial coordinates.

    MC/IC are meridians of right ascension. ASC/DSC are sampled from the
    spherical horizon equation. The response is map-ready geometry, not an
    interpretation or a location recommendation.
    """

    jd_ut = julian_day(instant)
    greenwich_sidereal_degrees = swe.sidtime(jd_ut) * 15
    latitudes = list(range(-80, 81, latitude_step))
    if latitudes[-1] != 80:
        latitudes.append(80)
    lines: list[AstrocartographyLine] = []
    for body in bodies:
        right_ascension, declination = equatorial_coordinates(jd_ut, body)
        declination_radians = math.radians(declination)
        for angle in angles:
            if angle == "mc":
                longitude = geographic_longitude(right_ascension - greenwich_sidereal_degrees)
                points = [
                    AstrocartographyPoint(latitude=latitude, longitude=longitude)
                    for latitude in latitudes
                ]
            elif angle == "ic":
                longitude = geographic_longitude(
                    right_ascension + 180 - greenwich_sidereal_degrees
                )
                points = [
                    AstrocartographyPoint(latitude=latitude, longitude=longitude)
                    for latitude in latitudes
                ]
            else:
                points = []
                for latitude in latitudes:
                    ratio = -math.tan(math.radians(latitude)) * math.tan(
                        declination_radians
                    )
                    if ratio < -1 or ratio > 1:
                        continue
                    hour_angle = math.degrees(math.acos(ratio))
                    local_sidereal = (
                        right_ascension - hour_angle
                        if angle == "asc"
                        else right_ascension + hour_angle
                    )
                    points.append(
                        AstrocartographyPoint(
                            latitude=latitude,
                            longitude=geographic_longitude(
                                local_sidereal - greenwich_sidereal_degrees
                            ),
                        )
                    )
            if points:
                lines.append(AstrocartographyLine(body=body, angle=angle, points=points))
    return lines


def eclipse_classification(kind: str, flags: int) -> str:
    if flags & swe.ECL_ANNULAR_TOTAL:
        return "hybrid"
    if kind == "solar" and flags & swe.ECL_ANNULAR:
        return "annular"
    if flags & swe.ECL_TOTAL:
        return "total"
    if kind == "lunar" and flags & swe.ECL_PENUMBRAL:
        return "penumbral"
    return "partial"


def eclipse_contacts(kind: str, values: tuple[float, ...]) -> dict[str, str]:
    labels = (
        ("partialBegins", 2),
        ("partialEnds", 3),
        ("totalityBegins", 4),
        ("totalityEnds", 5),
    )
    if kind == "lunar":
        labels += (("penumbraBegins", 6), ("penumbraEnds", 7))
    result: dict[str, str] = {}
    for label, index in labels:
        if index < len(values) and values[index] > 0:
            result[label] = utc_datetime_from_jd(values[index]).isoformat().replace(
                "+00:00", "Z"
            )
    return result


def eclipse_observer_visibility(
    kind: str, jd_ut: float, observer: Location
) -> EclipseObserverVisibility:
    geoposition = (observer.longitude, observer.latitude, 0.0)
    if kind == "solar":
        flags, attributes = swe.sol_eclipse_how(jd_ut, geoposition, swe.FLG_SWIEPH)
    else:
        flags, attributes = swe.lun_eclipse_how(jd_ut, geoposition, swe.FLG_SWIEPH)
    magnitude = float(attributes[8]) if len(attributes) > 8 and attributes[8] >= 0 else None
    series = int(attributes[9]) if len(attributes) > 9 and attributes[9] >= 0 else None
    member = int(attributes[10]) if len(attributes) > 10 and attributes[10] >= 0 else None
    above_horizon = bool(flags) and len(attributes) > 6 and attributes[6] > 0
    return EclipseObserverVisibility(
        visible=above_horizon,
        maximumVisible=above_horizon,
        magnitude=round(magnitude, 6) if magnitude is not None else None,
        sarosSeries=series,
        sarosMember=member,
    )


def next_eclipse(kind: str, start_jd: float) -> tuple[int, tuple[float, ...]]:
    if kind == "solar":
        return swe.sol_eclipse_when_glob(start_jd, swe.FLG_SWIEPH)
    return swe.lun_eclipse_when(start_jd, swe.FLG_SWIEPH)


def eclipse_events(request: EclipseSearchRequest) -> list[EclipseEvent]:
    start = study_moment_instant(request.start)
    next_jd_by_kind = {kind: julian_day(start) for kind in request.kinds}
    events: list[EclipseEvent] = []
    while len(events) < request.count:
        candidates: list[tuple[str, int, tuple[float, ...]]] = []
        for kind, start_jd in next_jd_by_kind.items():
            flags, times = next_eclipse(kind, start_jd)
            candidates.append((kind, flags, times))
        kind, flags, times = min(candidates, key=lambda candidate: candidate[2][0])
        maximum_jd = times[0]
        events.append(
            EclipseEvent(
                kind=kind,
                classification=eclipse_classification(kind, flags),
                maximumInstant=utc_datetime_from_jd(maximum_jd)
                .isoformat()
                .replace("+00:00", "Z"),
                contacts=eclipse_contacts(kind, times),
                observerVisibility=(
                    eclipse_observer_visibility(kind, maximum_jd, request.observer)
                    if request.observer
                    else None
                ),
            )
        )
        next_jd_by_kind[kind] = maximum_jd + 1
    return events


def multichart_charts(
    inputs: MultiChartInputs,
    bodies: list[str],
    features: list[str],
    base_limits: list[str],
) -> list[StudyChart]:
    """Build separately labelled layers; never merge people into a pseudo-chart."""

    charts: list[StudyChart] = []
    for participant in inputs.participants:
        for layer in inputs.layers:
            layer_id = f"{participant.id}:{layer.kind}"
            layer_limits = base_limits + [
                f"Multichart layer belongs to participant '{participant.id}' and remains separate from every other participant and layer."
            ]
            if layer.kind == "natal":
                charts.append(
                    chart_from_birth(
                        layer_id,
                        participant.natal,
                        bodies,
                        features,
                        layer_limits,
                    )
                )
                continue

            if layer.kind == "transits":
                assert layer.target is not None
                target_instant = study_moment_instant(layer.target)
                charts.append(
                    chart_at_instant(
                        layer_id,
                        target_instant,
                        layer.target.location,
                        layer.target.timeZone,
                        bodies,
                        features,
                        layer_limits
                        + [
                            f"Transit layer is keyed to '{participant.id}:natal'; compare it only with that participant's natal record."
                        ],
                    )
                )
                continue

            if layer.kind == "progressions":
                assert layer.target is not None
                natal_instant, is_date_level = utc_birth_instant(participant.natal)
                target_instant = study_moment_instant(layer.target)
                effective_features = (
                    [
                        feature
                        for feature in features
                        if feature not in {"houses", "angles"}
                    ]
                    if is_date_level
                    else features
                )
                charts.append(
                    chart_at_instant(
                        layer_id,
                        secondary_progressed_instant(natal_instant, target_instant),
                        participant.natal.location,
                        participant.natal.timeZone,
                        bodies,
                        effective_features,
                        layer_limits
                        + [
                            "Secondary progressions apply one mean solar day after birth for each tropical year elapsed to the target moment."
                        ],
                    )
                )
                continue

            if layer.kind == "directions":
                assert layer.target is not None
                if participant.natal.timeAccuracy != "exact":
                    raise ServiceError(
                        422,
                        "directions_require_exact_natal_time",
                        "Solar Arc layers require an exact natal time for every included participant.",
                    )
                natal_chart = chart_from_birth(
                    f"{participant.id}:natal_source",
                    participant.natal,
                    bodies,
                    features,
                    layer_limits,
                )
                natal_instant, _ = utc_birth_instant(participant.natal)
                target_instant = study_moment_instant(layer.target)
                arc_degrees = (
                    solar_longitude(
                        julian_day(secondary_progressed_instant(natal_instant, target_instant))
                    )
                    - solar_longitude(julian_day(natal_instant))
                ) % 360
                directed_houses, directed_angles = rotate_geometry(
                    natal_chart.houses, natal_chart.angles, arc_degrees
                )
                directed_positions = [
                    rotated_position(position, arc_degrees)
                    for position in natal_chart.positions
                ]
                charts.append(
                    StudyChart(
                        id=layer_id,
                        instant=target_instant.isoformat().replace("+00:00", "Z"),
                        localDateTime=target_instant.astimezone(
                            ZoneInfo(layer.target.timeZone)
                        ).isoformat(),
                        positions=directed_positions,
                        aspects=major_aspects(directed_positions)
                        if "aspects" in features
                        else None,
                        houses=directed_houses,
                        angles=directed_angles,
                        limitations=list(
                            dict.fromkeys(
                                layer_limits
                                + [
                                    f"Solar Arc layer uses an arc of {round(arc_degrees, 6)}° applied to participant '{participant.id}'."
                                ]
                            )
                        ),
                    )
                )
                continue

            assert layer.kind == "solar_return"
            assert layer.returnYear is not None
            assert layer.returnLocation is not None
            assert layer.returnTimeZone is not None
            if participant.natal.timeAccuracy != "exact":
                raise ServiceError(
                    422,
                    "solar_return_requires_exact_natal_time",
                    "Solar Return layers require an exact natal time for every included participant.",
                )
            natal_instant, _ = utc_birth_instant(participant.natal)
            return_instant, natal_sun = solar_return_instant(
                natal_instant, layer.returnYear
            )
            charts.append(
                chart_at_instant(
                    layer_id,
                    return_instant,
                    layer.returnLocation,
                    layer.returnTimeZone,
                    bodies,
                    features,
                    layer_limits
                    + [
                        f"Solar Return layer repeats participant '{participant.id}' natal solar longitude {round(natal_sun, 6)}° at the selected return location."
                    ],
                )
            )
    return charts


def study_inputs(request: ChartStudyRequest) -> BaseModel:
    try:
        if request.study == "synastry":
            return SynastryInputs.model_validate(request.inputs)
        if request.study == "composite":
            return CompositeInputs.model_validate(request.inputs)
        if request.study == "coalescent":
            return CoalescentInputs.model_validate(request.inputs)
        if request.study == "davison":
            return DavisonInputs.model_validate(request.inputs)
        if request.study == "multichart":
            return MultiChartInputs.model_validate(request.inputs)
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
                "composite:shortest_arc_midpoints",
                "coalescent:harmonic_sum",
                "davison:time_space_midpoint",
                "multichart:natal+transits+progressions+directions+solar_return",
                "horary",
                "electional",
                "transits",
                "progressions",
                "directions:solar_arc",
                "mundane:event",
                "mundane:ingress",
                "astrocartography:mc+ic+asc+dsc",
                "eclipse_search:global+observer_visibility",
                "event_formula:versioned_house_graph_rules",
            ],
            "lunarCalendar": [
                "phase_angle",
                "illumination",
                "moon_sign_at_local_noon",
                "local_sign_ingress",
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


@api.post("/v1/astrocartography", response_model=AstrocartographyResponse)
def calculate_astrocartography(
    request: AstrocartographyRequest,
    current: Settings = Depends(authenticate),
) -> AstrocartographyResponse:
    """Return map geometry for angular planetary lines at one exact moment."""

    configure_ephemeris_data(current)
    try:
        instant = study_moment_instant(request.moment)
        lines = astrocartography_lines(
            instant,
            request.bodies,
            request.angles,
            request.latitudeStepDegrees,
        )
    except ServiceError:
        raise
    except swe.Error as error:
        raise ServiceError(
            502,
            "swiss_ephemeris_error",
            "Swiss Ephemeris could not complete this astrocartography calculation.",
        ) from error
    limitations = list(
        dict.fromkeys(
            request.limitations
            + [
                "Astrocartography lines are sampled angular geometry from the declared exact moment, not place recommendations.",
                "MC and IC are right-ascension meridians; ASC and DSC use sampled horizon intersections at each returned latitude.",
                f"ASC and DSC line geometry is sampled every {request.latitudeStepDegrees}° of latitude and may have polar gaps.",
            ]
        )
    )
    return AstrocartographyResponse(
        calculatedAt=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        request=request,
        lines=lines,
        limitations=limitations,
    )


@api.post("/v1/eclipses", response_model=EclipseSearchResponse)
def search_eclipses(
    request: EclipseSearchRequest,
    current: Settings = Depends(authenticate),
) -> EclipseSearchResponse:
    """Find chronologically next global eclipses, with optional observer geometry."""

    configure_ephemeris_data(current)
    try:
        events = eclipse_events(request)
    except ServiceError:
        raise
    except swe.Error as error:
        raise ServiceError(
            502,
            "swiss_ephemeris_error",
            "Swiss Ephemeris could not complete this eclipse search.",
        ) from error
    limitations = list(
        dict.fromkeys(
            request.limitations
            + [
                "Eclipse events are astronomical timings in UTC. This service does not assign personal meaning or predict outcomes.",
                "Observer visibility, when requested, is evaluated at the event maximum for the declared location; it is not a full local circumstances report.",
            ]
        )
    )
    return EclipseSearchResponse(
        calculatedAt=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        request=request,
        eclipses=events,
        limitations=limitations,
    )


@api.post("/v1/lunar-calendar", response_model=LunarCalendarResponse)
def lunar_calendar(
    request: LunarCalendarRequest,
    current: Settings = Depends(authenticate),
) -> LunarCalendarResponse:
    """Return local-date lunar measurements without personal interpretation."""

    configure_ephemeris_data(current)
    try:
        days = lunar_calendar_days(request)
    except ServiceError:
        raise
    except swe.Error as error:
        raise ServiceError(
            502,
            "swiss_ephemeris_error",
            "Swiss Ephemeris could not complete this lunar calendar.",
        ) from error
    limitations = list(
        dict.fromkeys(
            request.limitations
            + [
                "Each row is calculated at local noon for its declared civil date; the Moon can move substantially within a day.",
                "Sign ingress is a geocentric tropical zodiac boundary crossing in the declared timezone.",
                "Phase angle and illumination are astronomical context, not a personal forecast or instruction.",
            ]
        )
    )
    return LunarCalendarResponse(
        calculatedAt=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        request=request,
        days=days,
        limitations=limitations,
    )


@api.post("/v1/event-formula", response_model=EventFormulaResponse)
def evaluate_event_formula(
    request: EventFormulaRequest,
    current: Settings = Depends(authenticate),
) -> EventFormulaResponse:
    """Evaluate a user-supplied, versioned formula against a natal house graph.

    The endpoint intentionally accepts formula definitions as data. This keeps
    the calculator extensible for independently licensed schools while avoiding
    a hidden or inferred formula catalogue in the service.
    """

    configure_ephemeris_data(current)
    try:
        instant, is_date_level = utc_birth_instant(request.birth)
        if is_date_level:
            raise ServiceError(
                422,
                "formula_requires_known_birth_time",
                "Event formula evaluation requires a known or approximate birth time.",
            )
        jd_ut = julian_day(instant)
        positions = [position_for(jd_ut, body) for body in request.bodies]
        houses, _ = placidus_geometry(jd_ut, request.birth.location)
        aspects = major_aspects(positions)
        elements, clause_results = formula_clause_results(
            request.formula,
            positions,
            houses,
            aspects,
            request.rulershipProfile,
        )
    except ServiceError:
        raise
    except swe.Error as error:
        raise ServiceError(
            502,
            "swiss_ephemeris_error",
            "Swiss Ephemeris could not complete this event formula evaluation.",
        ) from error
    matched = (
        all(result.matched for result in clause_results)
        if request.formula.operator == "all"
        else any(result.matched for result in clause_results)
    )
    return EventFormulaResponse(
        calculatedAt=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        request=request,
        positions=positions,
        houses=houses,
        aspects=aspects,
        houseElements=elements,
        clauseResults=clause_results,
        matched=matched,
        limitations=event_formula_limitations(request),
    )


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
        if isinstance(inputs, CompositeInputs):
            first = chart_from_birth(
                "first_natal", inputs.first, request.bodies, request.features, base_limits
            )
            second = chart_from_birth(
                "second_natal", inputs.second, request.bodies, request.features, base_limits
            )
            positions, ambiguities = midpoint_composite(
                first.positions, second.positions, inputs.oppositionPolicy
            )
            composite_limitations = list(
                dict.fromkeys(
                    base_limits
                    + [
                        "Composite positions use shortest-arc midpoints of each matching pair of natal longitudes.",
                        "A composite is a derived longitude set, not an astronomical event chart: houses, angles, and retrograde states are intentionally not generated.",
                        f"Exactly opposite pairs use the declared opposition policy: {inputs.oppositionPolicy}.",
                    ]
                )
            )
            derived = DerivedChart(
                id="midpoint_composite",
                construction="midpoint_composite",
                positions=positions,
                aspects=derived_aspects(positions)
                if "aspects" in request.features
                else None,
                midpointAmbiguities=ambiguities,
                limitations=composite_limitations,
            )
            return ChartStudyResponse(
                calculatedAt=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                request=request,
                charts=[first, second],
                derivedCharts=[derived],
                limitations=composite_limitations,
            )

        if isinstance(inputs, CoalescentInputs):
            first = chart_from_birth(
                "first_natal", inputs.first, request.bodies, request.features, base_limits
            )
            second = chart_from_birth(
                "second_natal", inputs.second, request.bodies, request.features, base_limits
            )
            positions = harmonic_sum_coalescent(first.positions, second.positions)
            coalescent_limitations = list(
                dict.fromkeys(
                    base_limits
                    + [
                        "Coalescent is calculated only with the explicitly selected harmonic_sum formula: each pair of ecliptic longitudes is added modulo 360°.",
                        "Coalescent does not have one universal calculation standard. This result must be labelled with its formula rather than treated as a time-space midpoint chart.",
                        "This derived longitude set has no astronomical event time, houses, angles, or retrograde states.",
                    ]
                )
            )
            derived = DerivedChart(
                id="harmonic_sum_coalescent",
                construction="harmonic_sum_coalescent",
                positions=positions,
                aspects=derived_aspects(positions)
                if "aspects" in request.features
                else None,
                limitations=coalescent_limitations,
            )
            return ChartStudyResponse(
                calculatedAt=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                request=request,
                charts=[first, second],
                derivedCharts=[derived],
                limitations=coalescent_limitations,
            )

        if isinstance(inputs, DavisonInputs):
            instant, location = davison_midpoint_context(inputs)
            davison_limit = (
                "Davison uses the midpoint in UTC time and the shortest-arc geographic midpoint of two exact natal records; it is a distinct time-space midpoint method, not a composite or coalescent calculation."
            )
            chart = chart_at_instant(
                "davison_time_space_midpoint",
                instant,
                location,
                "UTC",
                request.bodies,
                request.features,
                base_limits + [davison_limit],
            )
            return ChartStudyResponse(
                calculatedAt=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                request=request,
                charts=[chart],
                limitations=list(dict.fromkeys(base_limits + [davison_limit])),
            )

        if isinstance(inputs, MultiChartInputs):
            multichart_limit = (
                "Multichart is a container of separately labelled participant layers. It does not average people, derive a relationship chart, or rank outcomes."
            )
            charts = multichart_charts(
                inputs,
                request.bodies,
                request.features,
                base_limits + [multichart_limit],
            )
            return ChartStudyResponse(
                calculatedAt=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                request=request,
                charts=charts,
                limitations=list(dict.fromkeys(base_limits + [multichart_limit])),
            )

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
