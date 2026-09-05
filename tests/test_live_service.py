"""Loopback-only HTTP contract test for a running Swiss Ephemeris service."""

import json
import os
import unittest
from typing import Optional
from urllib.error import HTTPError
from urllib.request import Request, urlopen


SERVICE_URL = os.environ.get("SWISS_EPHEMERIS_INTEGRATION_URL")
SERVICE_KEY = os.environ.get("SWISS_EPHEMERIS_INTEGRATION_KEY")
EXPECTED_SOURCE_URL = os.environ.get("EXPECTED_AGPL_SOURCE_URL")


def get_json(path: str, key: Optional[str] = None):
    headers = {"x-umbra-service-key": key} if key else {}
    with urlopen(Request(f"{SERVICE_URL}{path}", headers=headers), timeout=5) as response:
        return response.status, json.loads(response.read())


def post_chart(key: str):
    request_body = {
        "version": "ephemeris-request-v1",
        "birth": {
            "localDate": "1992-05-30",
            "localTime": "09:15",
            "timeAccuracy": "exact",
            "timeZone": "Europe/Lisbon",
            "location": {"latitude": 38.7223, "longitude": -9.1393},
        },
        "bodies": ["sun", "moon", "mercury", "venus", "north_node", "south_node", "lilith_mean", "lilith_true", "chiron", "proserpina"],
        "features": ["positions", "aspects", "houses", "angles"],
        "limitations": [],
    }
    request = Request(
        f"{SERVICE_URL}/v1/chart",
        data=json.dumps(request_body).encode(),
        headers={"content-type": "application/json", "x-umbra-service-key": key},
        method="POST",
    )
    with urlopen(request, timeout=10) as response:
        return response.status, json.loads(response.read())


def post_solar_return(key: str):
    request_body = {
        "version": "solar-return-request-v1",
        "natal": {
            "localDate": "1992-05-30",
            "localTime": "09:15",
            "timeAccuracy": "exact",
            "timeZone": "Europe/Lisbon",
            "location": {"latitude": 38.7223, "longitude": -9.1393},
        },
        "returnYear": 1993,
        "returnLocation": {"latitude": 38.7223, "longitude": -9.1393},
        "returnTimeZone": "Europe/Lisbon",
        "bodies": ["sun", "moon", "north_node", "south_node"],
        "features": ["positions", "aspects", "houses", "angles"],
        "limitations": [],
    }
    request = Request(
        f"{SERVICE_URL}/v1/solar-return",
        data=json.dumps(request_body).encode(),
        headers={"content-type": "application/json", "x-umbra-service-key": key},
        method="POST",
    )
    with urlopen(request, timeout=10) as response:
        return response.status, json.loads(response.read())


def post_chart_study(key: str, study: str, inputs: dict):
    request_body = {
        "version": "chart-study-request-v1",
        "study": study,
        "inputs": inputs,
        "bodies": ["sun", "moon", "north_node", "south_node"],
        "features": ["positions", "aspects", "houses", "angles"],
        "limitations": [],
    }
    request = Request(
        f"{SERVICE_URL}/v1/chart-study",
        data=json.dumps(request_body).encode(),
        headers={"content-type": "application/json", "x-umbra-service-key": key},
        method="POST",
    )
    with urlopen(request, timeout=15) as response:
        return response.status, json.loads(response.read())


def post_astronomy_module(key: str, path: str, request_body: dict):
    request = Request(
        f"{SERVICE_URL}{path}",
        data=json.dumps(request_body).encode(),
        headers={"content-type": "application/json", "x-umbra-service-key": key},
        method="POST",
    )
    with urlopen(request, timeout=15) as response:
        return response.status, json.loads(response.read())


@unittest.skipUnless(
    SERVICE_URL and SERVICE_KEY and EXPECTED_SOURCE_URL,
    "Set loopback integration environment variables to run this test.",
)
class LiveServiceContractTests(unittest.TestCase):
    def test_health_source_authentication_and_swiss_chart(self):
        self.assertTrue(SERVICE_URL.startswith(("http://127.0.0.1:", "http://localhost:")))

        health_status, health = get_json("/healthz")
        self.assertEqual(health_status, 200)
        self.assertEqual(health, {"ok": True, "calculator": "swiss-ephemeris", "ready": True})

        source_status, source = get_json("/source")
        self.assertEqual(source_status, 200)
        self.assertEqual(source, {"license": "AGPL-3.0-only", "sourceUrl": EXPECTED_SOURCE_URL})

        with self.assertRaises(HTTPError) as unauthorized:
            post_chart("incorrect-key")
        self.assertEqual(unauthorized.exception.code, 401)

        chart_status, chart = post_chart(SERVICE_KEY)
        self.assertEqual(chart_status, 200)
        self.assertEqual(chart["version"], "ephemeris-response-v1")
        self.assertEqual(
            [position["body"] for position in chart["positions"]],
            ["sun", "moon", "mercury", "venus", "north_node", "south_node", "lilith_mean", "lilith_true", "chiron", "proserpina"],
        )
        self.assertEqual(len(chart["houses"]), 12)
        self.assertEqual(
            [angle["name"] for angle in chart["angles"]],
            ["ascendant", "midheaven"],
        )
        self.assertIn("House cusps use the Placidus house system.", chart["limitations"])

        solar_status, solar_return = post_solar_return(SERVICE_KEY)
        self.assertEqual(solar_status, 200)
        self.assertEqual(solar_return["version"], "solar-return-response-v1")
        self.assertIn("returnInstant", solar_return)
        self.assertIn("returnLocalDateTime", solar_return)
        self.assertEqual(len(solar_return["houses"]), 12)
        self.assertEqual(
            [position["body"] for position in solar_return["positions"]],
            ["sun", "moon", "north_node", "south_node"],
        )
        solar_error = abs(
            ((solar_return["positions"][0]["longitudeDegrees"] - solar_return["natalSunLongitudeDegrees"] + 180) % 360) - 180
        )
        self.assertLess(solar_error, 0.0001)

    def test_every_remaining_chart_study_uses_live_swiss_data(self):
        natal = {
            "localDate": "1992-05-30",
            "localTime": "09:15",
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
        requests = [
            ("synastry", {"first": natal, "second": {**natal, "localDate": "1991-04-20"}}),
            ("composite", {"first": natal, "second": {**natal, "localDate": "1991-04-20"}, "oppositionPolicy": "omit"}),
            ("coalescent", {"first": natal, "second": {**natal, "localDate": "1991-04-20"}, "method": "harmonic_sum"}),
            ("davison", {"first": natal, "second": {**natal, "localDate": "1991-04-20"}}),
            (
                "multichart",
                {
                    "participants": [{"id": "first", "natal": natal}, {"id": "second", "natal": {**natal, "localDate": "1991-04-20"}}],
                    "layers": [{"kind": "natal"}, {"kind": "transits", "target": moment}],
                },
            ),
            ("horary", {"moment": moment}),
            ("electional", {"candidates": [moment, {**moment, "localTime": "13:00"}]}),
            ("transits", {"natal": natal, "target": moment}),
            ("progressions", {"natal": natal, "target": moment}),
            ("directions", {"natal": natal, "target": moment, "method": "solar_arc"}),
            ("mundane", {"method": "event", "moment": moment}),
            (
                "mundane",
                {
                    "method": "ingress",
                    "year": 2026,
                    "ingress": "aries",
                    "location": natal["location"],
                    "timeZone": "Europe/Lisbon",
                },
            ),
        ]

        for study, inputs in requests:
            with self.subTest(study=study, method=inputs.get("method")):
                status, result = post_chart_study(SERVICE_KEY, study, inputs)
                self.assertEqual(status, 200)
                self.assertEqual(result["version"], "chart-study-response-v1")
                self.assertGreaterEqual(len(result["charts"]), 1)
                self.assertEqual(
                    [position["body"] for position in result["charts"][0]["positions"]],
                    ["sun", "moon", "north_node", "south_node"],
                )
                self.assertEqual(len(result["charts"][0]["houses"]), 12)

        direction_status, direction = post_chart_study(
            SERVICE_KEY,
            "directions",
            {"natal": natal, "target": moment, "method": "solar_arc"},
        )
        self.assertEqual(direction_status, 200)
        self.assertIsInstance(direction["directionArcDegrees"], float)

    def test_astrocartography_and_eclipse_modules_use_live_swiss_data(self):
        moment = {
            "localDate": "2026-09-04",
            "localTime": "12:00",
            "timeAccuracy": "exact",
            "timeZone": "Europe/Lisbon",
            "location": {"latitude": 38.7223, "longitude": -9.1393},
        }
        astro_status, astro = post_astronomy_module(
            SERVICE_KEY,
            "/v1/astrocartography",
            {
                "version": "astrocartography-request-v1",
                "moment": moment,
                "bodies": ["sun", "moon"],
                "angles": ["mc", "ic", "asc", "dsc"],
                "latitudeStepDegrees": 5,
                "limitations": [],
            },
        )
        self.assertEqual(astro_status, 200)
        self.assertEqual(astro["version"], "astrocartography-response-v1")
        self.assertEqual(len(astro["lines"]), 8)
        self.assertTrue(all(line["points"] for line in astro["lines"]))

        eclipse_status, eclipses = post_astronomy_module(
            SERVICE_KEY,
            "/v1/eclipses",
            {
                "version": "eclipse-search-request-v1",
                "start": moment,
                "kinds": ["solar", "lunar"],
                "count": 2,
                "observer": moment["location"],
                "limitations": [],
            },
        )
        self.assertEqual(eclipse_status, 200)
        self.assertEqual(eclipses["version"], "eclipse-search-response-v1")
        self.assertEqual(len(eclipses["eclipses"]), 2)
        self.assertEqual(
            sorted(eclipse["maximumInstant"] for eclipse in eclipses["eclipses"]),
            [eclipse["maximumInstant"] for eclipse in eclipses["eclipses"]],
        )

        lunar_status, lunar = post_astronomy_module(
            SERVICE_KEY,
            "/v1/lunar-calendar",
            {
                "version": "lunar-calendar-request-v1",
                "anchorLocalDate": "2026-09-05",
                "timeZone": "Europe/Lisbon",
                "daysBefore": 0,
                "daysAfter": 0,
                "limitations": [],
            },
        )
        self.assertEqual(lunar_status, 200)
        self.assertEqual(lunar["version"], "lunar-calendar-response-v1")
        self.assertEqual(len(lunar["days"]), 1)
        self.assertEqual(lunar["days"][0]["moon"]["body"], "moon")
        self.assertGreaterEqual(lunar["days"][0]["illuminationFraction"], 0)
        self.assertLessEqual(lunar["days"][0]["illuminationFraction"], 1)

        formula_status, formula = post_astronomy_module(
            SERVICE_KEY,
            "/v1/event-formula",
            {
                "version": "event-formula-request-v1",
                "birth": {**moment, "localDate": "1992-05-30", "localTime": "09:15"},
                "bodies": ["sun", "moon", "mercury", "venus", "mars", "jupiter", "saturn", "uranus", "neptune", "pluto"],
                "rulershipProfile": "traditional",
                "formula": {
                    "id": "neutral_demo",
                    "title": "Neutral structural demo",
                    "school": {
                        "id": "neutral-structural-v1",
                        "version": "1",
                        "attribution": "Umbra neutral example",
                        "licence": "neutral",
                    },
                    "operator": "all",
                    "clauses": [
                        {
                            "id": "ruler_location",
                            "sourceHouse": 1,
                            "targetHouse": 10,
                            "relation": "ruler_in_house",
                            "sourceRole": "ruler",
                        }
                    ],
                },
                "limitations": [],
            },
        )
        self.assertEqual(formula_status, 200)
        self.assertEqual(formula["version"], "event-formula-response-v1")
        self.assertEqual(len(formula["houses"]), 12)
        self.assertEqual(formula["clauseResults"][0]["clauseId"], "ruler_location")


if __name__ == "__main__":
    unittest.main()
