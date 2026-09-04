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
        "bodies": ["sun", "moon", "mercury", "venus", "north_node"],
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
            ["sun", "moon", "mercury", "venus", "north_node"],
        )
        self.assertEqual(len(chart["houses"]), 12)
        self.assertEqual(
            [angle["name"] for angle in chart["angles"]],
            ["ascendant", "midheaven"],
        )
        self.assertIn("House cusps use the Placidus house system.", chart["limitations"])


if __name__ == "__main__":
    unittest.main()
