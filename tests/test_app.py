import importlib.util
import os
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

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
