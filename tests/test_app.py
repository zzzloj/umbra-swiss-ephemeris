import importlib.util
import os
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

MODULE_PATH = Path(__file__).resolve().parents[1] / "app.py"
SPEC = importlib.util.spec_from_file_location("swiss_ephemeris_app", MODULE_PATH)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError("Could not load Swiss Ephemeris service module.")
service = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = service
SPEC.loader.exec_module(service)


class SwissEphemerisServiceTests(unittest.TestCase):
    def test_known_birth_time_converts_to_utc_without_losing_timezone(self):
        birth = service.Birth(
            localDate="1992-05-30",
            localTime="09:30",
            timeAccuracy="exact",
            timeZone="Europe/Lisbon",
            location={"latitude": 38.7223, "longitude": -9.1393},
        )

        instant, is_date_level = service.utc_birth_instant(birth)

        self.assertEqual(
            instant, datetime(1992, 5, 30, 8, 30, tzinfo=timezone.utc)
        )
        self.assertFalse(is_date_level)

    def test_unknown_time_uses_a_disclosed_date_level_reference(self):
        request = service.ChartRequest(
            version="ephemeris-request-v1",
            birth={
                "localDate": "1992-05-30",
                "timeAccuracy": "unknown",
                "timeZone": "Europe/Lisbon",
                "location": {"latitude": 38.7223, "longitude": -9.1393},
            },
            bodies=["sun", "moon"],
            features=["positions", "aspects"],
        )

        instant, is_date_level = service.utc_birth_instant(request.birth)
        limitations = service.response_limitations(request, is_date_level)

        self.assertEqual(instant.hour, 11)
        self.assertTrue(is_date_level)
        self.assertTrue(any("local noon" in item for item in limitations))

    def test_major_aspects_use_the_declared_six_degree_orb(self):
        positions = [
            service.Position(
                body="sun",
                longitudeDegrees=0,
                sign="Aries",
                degreeInSign=0,
                retrograde=False,
            ),
            service.Position(
                body="moon",
                longitudeDegrees=64.5,
                sign="Gemini",
                degreeInSign=4.5,
                retrograde=False,
            ),
            service.Position(
                body="mars",
                longitudeDegrees=98,
                sign="Cancer",
                degreeInSign=8,
                retrograde=False,
            ),
        ]

        aspects = service.major_aspects(positions)

        self.assertEqual(len(aspects), 1)
        self.assertEqual(aspects[0].between, ("sun", "moon"))
        self.assertEqual(aspects[0].kind, "sextile")
        self.assertEqual(aspects[0].orbDegrees, 4.5)

    def test_unknown_birth_time_cannot_request_houses(self):
        with self.assertRaises(ValueError):
            service.ChartRequest(
                version="ephemeris-request-v1",
                birth={
                    "localDate": "1992-05-30",
                    "timeAccuracy": "unknown",
                    "timeZone": "Europe/Lisbon",
                    "location": {"latitude": 38.7223, "longitude": -9.1393},
                },
                bodies=["sun"],
                features=["positions", "houses"],
            )

    def test_unknown_birth_time_cannot_request_angles(self):
        with self.assertRaises(ValueError):
            service.ChartRequest(
                version="ephemeris-request-v1",
                birth={
                    "localDate": "1992-05-30",
                    "timeAccuracy": "unknown",
                    "timeZone": "Europe/Lisbon",
                    "location": {"latitude": 38.7223, "longitude": -9.1393},
                },
                bodies=["sun", "north_node"],
                features=["positions", "angles"],
            )

    def test_direct_angles_keep_sign_and_degree_separate(self):
        ascendant = service.angle_for("ascendant", 215.25)
        midheaven = service.angle_for("midheaven", 287.5)

        self.assertEqual(ascendant.model_dump(), {
            "name": "ascendant",
            "longitudeDegrees": 215.25,
            "sign": "Scorpio",
            "degreeInSign": 5.25,
        })
        self.assertEqual(midheaven.sign, "Capricorn")
        self.assertEqual(midheaven.degreeInSign, 17.5)

    def test_south_node_is_the_exact_opposite_of_the_true_north_node(self):
        with patch.object(
            service.swe,
            "calc_ut",
            return_value=((25.0, 0, 0, -0.02, 0, 0), service.swe.FLG_SWIEPH, ""),
        ):
            south_node = service.position_for(2451545.0, "south_node")

        self.assertEqual(south_node.model_dump(), {
            "body": "south_node",
            "longitudeDegrees": 205.0,
            "sign": "Libra",
            "degreeInSign": 25.0,
            "retrograde": True,
        })

    def test_solar_return_requires_an_exact_natal_birth_time(self):
        with self.assertRaises(ValueError):
            service.SolarReturnRequest(
                version="solar-return-request-v1",
                natal={
                    "localDate": "1992-05-30",
                    "localTime": "09:30",
                    "timeAccuracy": "approximate",
                    "timeZone": "Europe/Lisbon",
                    "location": {"latitude": 38.7223, "longitude": -9.1393},
                },
                returnYear=2026,
                returnLocation={"latitude": 38.7223, "longitude": -9.1393},
                returnTimeZone="Europe/Lisbon",
                bodies=["sun"],
                features=["positions"],
            )

    def test_solar_return_solves_the_solar_recurrence_within_seconds(self):
        natal = datetime(2000, 6, 1, 12, tzinfo=timezone.utc)
        natal_jd = service.julian_day(natal)
        days_per_year = 365.2422

        with patch.object(
            service,
            "solar_longitude",
            side_effect=lambda jd: (210 + (jd - natal_jd) * 360 / days_per_year) % 360,
        ):
            returned, longitude = service.solar_return_instant(natal, 2001)

        elapsed_seconds = (returned - natal).total_seconds()
        self.assertAlmostEqual(elapsed_seconds, days_per_year * 86_400, delta=2)
        self.assertEqual(longitude, 210)

    def test_every_remaining_study_uses_a_distinct_validated_input_shape(self):
        natal = {
            "localDate": "1992-05-30",
            "localTime": "09:30",
            "timeAccuracy": "exact",
            "timeZone": "Europe/Lisbon",
            "location": {"latitude": 38.7223, "longitude": -9.1393},
        }
        moment = {
            "localDate": "2026-09-04",
            "localTime": "12:00",
            "timeAccuracy": "exact",
            "timeZone": "Europe/Lisbon",
            "location": {"latitude": 38.7223, "longitude": -9.1393},
        }
        inputs_by_study = {
            "synastry": {"first": natal, "second": natal},
            "horary": {"moment": moment},
            "electional": {"candidates": [moment, {**moment, "localTime": "13:00"}]},
            "transits": {"natal": natal, "target": moment},
            "progressions": {"natal": natal, "target": moment},
            "directions": {"natal": natal, "target": moment, "method": "solar_arc"},
            "mundane": {"method": "event", "moment": moment},
        }
        for study, inputs in inputs_by_study.items():
            request = service.ChartStudyRequest(
                version="chart-study-request-v1",
                study=study,
                inputs=inputs,
                bodies=["sun", "moon"],
                features=["positions", "aspects"],
            )
            self.assertIsNotNone(service.study_inputs(request))

    def test_directions_require_an_explicit_solar_arc_method_and_exact_natal_time(self):
        with self.assertRaises(ValueError):
            service.DirectionInputs(
                natal={
                    "localDate": "1992-05-30",
                    "localTime": "09:30",
                    "timeAccuracy": "approximate",
                    "timeZone": "Europe/Lisbon",
                    "location": {"latitude": 38.7223, "longitude": -9.1393},
                },
                target={
                    "localDate": "2026-09-04",
                    "localTime": "12:00",
                    "timeAccuracy": "exact",
                    "timeZone": "Europe/Lisbon",
                    "location": {"latitude": 38.7223, "longitude": -9.1393},
                },
                method="primary",
            )

    def test_solar_arc_rotates_positions_and_houses_by_the_same_amount(self):
        position = service.Position(
            body="sun",
            longitudeDegrees=350,
            sign="Pisces",
            degreeInSign=20,
            retrograde=False,
        )
        directed = service.rotated_position(position, 20)
        houses, angles = service.rotate_geometry(
            [service.HouseCusp(number=1, longitudeDegrees=350)],
            [service.angle_for("ascendant", 350), service.angle_for("midheaven", 80)],
            20,
        )

        self.assertEqual(directed.longitudeDegrees, 10)
        self.assertEqual(directed.sign, "Aries")
        self.assertEqual(houses[0].longitudeDegrees, 10)
        self.assertEqual(angles[0].longitudeDegrees, 10)

    def test_birth_input_format_is_not_silently_broadened(self):
        with self.assertRaises(ValueError):
            service.Birth(
                localDate="19920530",
                localTime="09:30:00",
                timeAccuracy="exact",
                timeZone="Europe/Lisbon",
                location={"latitude": 38.7223, "longitude": -9.1393},
            )

    def test_swiss_data_fallback_is_rejected(self):
        with self.assertRaises(service.ServiceError) as captured:
            service.position_for(2451545.0, "sun")

        self.assertEqual(captured.exception.code, "ephemeris_data_unavailable")

    def test_readiness_requires_a_real_swiss_data_calculation(self):
        with TemporaryDirectory() as directory:
            configured = service.Settings(
                service_key="service-key",
                ephemeris_path=Path(directory),
                licence_mode="agpl",
                agpl_source_url="https://code.example/umbra-swiss/v1",
                professional_licence_reference=None,
            )

            with patch.object(
                service.swe,
                "calc_ut",
                return_value=((0, 0, 0, 0, 0, 0), service.swe.FLG_SWIEPH, ""),
            ):
                self.assertTrue(service.swiss_data_is_ready(configured))

            with patch.object(
                service.swe,
                "calc_ut",
                return_value=((0, 0, 0, 0, 0, 0), service.swe.FLG_MOSEPH, ""),
            ):
                self.assertFalse(service.swiss_data_is_ready(configured))

    def test_agpl_source_offer_is_public_but_contains_no_secret(self):
        original_mode = os.environ.get("SWISS_EPHEMERIS_LICENSE_MODE")
        original_url = os.environ.get("AGPL_SOURCE_URL")
        try:
            os.environ["SWISS_EPHEMERIS_LICENSE_MODE"] = "agpl"
            os.environ["AGPL_SOURCE_URL"] = "https://code.example/umbra-swiss/v1"
            service.settings.cache_clear()

            offer = service.source_offer()

            self.assertEqual(offer["license"], "AGPL-3.0-only")
            self.assertEqual(offer["sourceUrl"], "https://code.example/umbra-swiss/v1")
            self.assertNotIn("serviceKey", offer)
        finally:
            if original_mode is None:
                os.environ.pop("SWISS_EPHEMERIS_LICENSE_MODE", None)
            else:
                os.environ["SWISS_EPHEMERIS_LICENSE_MODE"] = original_mode
            if original_url is None:
                os.environ.pop("AGPL_SOURCE_URL", None)
            else:
                os.environ["AGPL_SOURCE_URL"] = original_url
            service.settings.cache_clear()


if __name__ == "__main__":
    unittest.main()
