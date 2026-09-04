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

    def test_extended_factors_use_the_declared_swiss_identifiers(self):
        with patch.object(
            service.swe,
            "calc_ut",
            return_value=((25.0, 0, 0, -0.02, 0, 0), service.swe.FLG_SWIEPH, ""),
        ) as calculated:
            for body in ("lilith_mean", "lilith_true", "chiron"):
                self.assertEqual(service.position_for(2451545.0, body).body, body)

        self.assertEqual(
            [call.args[1] for call in calculated.call_args_list],
            [service.swe.MEAN_APOG, service.swe.OSCU_APOG, service.swe.CHIRON],
        )

    def test_proserpina_is_an_explicit_orbital_element_factor(self):
        with patch.object(
            service.swe,
            "calc_ut",
            return_value=((25.0, 0, 0, -0.02, 0, 0), 0, ""),
        ) as calculated:
            result = service.position_for(2451545.0, "proserpina")

        self.assertEqual(result.body, "proserpina")
        self.assertEqual(calculated.call_args.args[1], service.swe.PROSERPINA)

    def test_extended_factors_are_accepted_by_chart_and_study_contracts(self):
        birth = {
            "localDate": "1992-05-30",
            "localTime": "09:30",
            "timeAccuracy": "exact",
            "timeZone": "Europe/Lisbon",
            "location": {"latitude": 38.7223, "longitude": -9.1393},
        }
        factors = ["lilith_mean", "lilith_true", "chiron", "proserpina"]
        chart = service.ChartRequest(
            version="ephemeris-request-v1",
            birth=birth,
            bodies=factors,
            features=["positions"],
        )
        study = service.ChartStudyRequest(
            version="chart-study-request-v1",
            study="transits",
            inputs={"natal": birth, "target": birth},
            bodies=factors,
            features=["positions"],
        )

        self.assertEqual(chart.bodies, factors)
        self.assertEqual(study.bodies, factors)

    def test_capabilities_disclose_data_requirements_and_unimplemented_swiss_surface(self):
        configured = service.Settings(
            service_key=None,
            ephemeris_path=None,
            licence_mode="agpl",
            agpl_source_url=None,
            professional_licence_reference=None,
        )
        with patch.object(service, "settings", return_value=configured), patch.object(
            service,
            "factor_is_available",
            side_effect=[True, True, True, False],
        ):
            result = service.capabilities()

        factors = {factor["id"]: factor for factor in result["extendedFactors"]}
        self.assertTrue(factors["chiron"]["available"])
        self.assertFalse(factors["proserpina"]["available"])
        self.assertIn("seorbel.txt", factors["proserpina"]["requires"])
        self.assertTrue(any("Fixed-star" in item for item in result["notImplemented"]))
        self.assertIn("directions:solar_arc", result["implemented"]["chartStudies"])
        self.assertIn("composite:shortest_arc_midpoints", result["implemented"]["chartStudies"])
        self.assertIn("astrocartography:mc+ic+asc+dsc", result["implemented"]["chartStudies"])
        self.assertIn("eclipse_search:global+observer_visibility", result["implemented"]["chartStudies"])
        self.assertIn("event_formula:versioned_house_graph_rules", result["implemented"]["chartStudies"])

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

    def test_composite_uses_shortest_arc_midpoints_and_exposes_exact_oppositions(self):
        first = [
            service.Position(body="sun", longitudeDegrees=350, sign="Pisces", degreeInSign=20, retrograde=False),
            service.Position(body="moon", longitudeDegrees=0, sign="Aries", degreeInSign=0, retrograde=False),
        ]
        second = [
            service.Position(body="sun", longitudeDegrees=10, sign="Aries", degreeInSign=10, retrograde=False),
            service.Position(body="moon", longitudeDegrees=180, sign="Libra", degreeInSign=0, retrograde=False),
        ]

        positions, ambiguities = service.midpoint_composite(first, second, "omit")
        selected, selected_ambiguities = service.midpoint_composite(first, second, "first")

        self.assertEqual([position.longitudeDegrees for position in positions], [0])
        self.assertEqual(ambiguities[0].body, "moon")
        self.assertEqual([position.longitudeDegrees for position in selected], [0, 0])
        self.assertEqual(selected_ambiguities, [])

    def test_coalescent_harmonic_sum_is_not_a_midpoint(self):
        first = [service.Position(body="sun", longitudeDegrees=350, sign="Pisces", degreeInSign=20, retrograde=False)]
        second = [service.Position(body="sun", longitudeDegrees=20, sign="Aries", degreeInSign=20, retrograde=False)]

        result = service.harmonic_sum_coalescent(first, second)

        self.assertEqual(result[0].longitudeDegrees, 10)
        self.assertEqual(result[0].sign, "Aries")

    def test_davison_midpoint_requires_exact_times_and_uses_shortest_geographic_arc(self):
        first = {
            "localDate": "2000-01-01",
            "localTime": "00:00",
            "timeAccuracy": "exact",
            "timeZone": "UTC",
            "location": {"latitude": 10, "longitude": 170},
        }
        second = {
            "localDate": "2000-01-03",
            "localTime": "00:00",
            "timeAccuracy": "exact",
            "timeZone": "UTC",
            "location": {"latitude": 30, "longitude": -170},
        }

        instant, location = service.davison_midpoint_context(
            service.DavisonInputs(first=first, second=second)
        )

        self.assertEqual(instant, datetime(2000, 1, 2, tzinfo=timezone.utc))
        self.assertEqual(location.latitude, 20)
        self.assertEqual(location.longitude, 180)
        with self.assertRaises(ValueError):
            service.DavisonInputs(first={**first, "timeAccuracy": "approximate"}, second=second)

    def test_multichart_rejects_ambiguous_participant_and_layer_sets(self):
        natal = {
            "localDate": "2000-01-01",
            "localTime": "00:00",
            "timeAccuracy": "exact",
            "timeZone": "UTC",
            "location": {"latitude": 10, "longitude": 20},
        }
        with self.assertRaises(ValueError):
            service.MultiChartInputs(
                participants=[{"id": "one", "natal": natal}, {"id": "one", "natal": natal}],
                layers=[{"kind": "natal"}],
            )
        with self.assertRaises(ValueError):
            service.MultiChartInputs(
                participants=[{"id": "one", "natal": natal}],
                layers=[{"kind": "transits"}],
            )

    def test_astrocartography_returns_map_ready_angular_lines(self):
        instant = datetime(2026, 9, 4, 12, tzinfo=timezone.utc)
        with patch.object(service.swe, "sidtime", return_value=0), patch.object(
            service,
            "equatorial_coordinates",
            return_value=(90, 0),
        ):
            lines = service.astrocartography_lines(instant, ["sun"], ["mc", "ic", "asc", "dsc"], 10)

        self.assertEqual([(line.body, line.angle) for line in lines], [("sun", "mc"), ("sun", "ic"), ("sun", "asc"), ("sun", "dsc")])
        self.assertTrue(all(point.longitude == 90 for point in lines[0].points))
        self.assertTrue(all(point.longitude == -90 for point in lines[1].points))
        self.assertEqual(lines[2].points[0].longitude, 0)
        self.assertEqual(lines[3].points[0].longitude, -180)

    def test_eclipse_search_merges_requested_kinds_chronologically(self):
        start = {
            "localDate": "2026-01-01",
            "localTime": "00:00",
            "timeAccuracy": "exact",
            "timeZone": "UTC",
            "location": {"latitude": 0, "longitude": 0},
        }
        request = service.EclipseSearchRequest(
            version="eclipse-search-request-v1",
            start=start,
            kinds=["solar", "lunar"],
            count=2,
        )
        responses = iter(
            [
                (service.swe.ECL_PARTIAL, (2451547.0,) + (0.0,) * 9),
                (service.swe.ECL_TOTAL, (2451546.0,) + (0.0,) * 9),
                (service.swe.ECL_PARTIAL, (2451547.0,) + (0.0,) * 9),
                (service.swe.ECL_PARTIAL, (2451576.0,) + (0.0,) * 9),
            ]
        )
        with patch.object(service, "next_eclipse", side_effect=lambda *_: next(responses)):
            events = service.eclipse_events(request)

        self.assertEqual([event.kind for event in events], ["lunar", "solar"])
        self.assertEqual([event.classification for event in events], ["total", "partial"])

    def test_neutral_formula_engine_returns_structural_evidence_not_an_event_label(self):
        bodies = [
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
        ]
        longitudes = [130, 160, 190, 70, 10, 220, 250, 280, 310, 340]
        positions = [
            service.Position(
                body=body,
                longitudeDegrees=longitude,
                sign=service.SIGN_NAMES[int(longitude // 30)],
                degreeInSign=longitude % 30,
                retrograde=False,
            )
            for body, longitude in zip(bodies, longitudes)
        ]
        houses = [service.HouseCusp(number=index + 1, longitudeDegrees=index * 30) for index in range(12)]
        formula = service.EventFormula(
            id="structure_demo",
            title="Structural test",
            school={
                "id": "neutral-structural-v1",
                "version": "1",
                "attribution": "Umbra neutral example",
                "licence": "neutral",
            },
            operator="all",
            clauses=[
                {
                    "id": "ruler_location",
                    "sourceHouse": 1,
                    "targetHouse": 1,
                    "relation": "ruler_in_house",
                    "sourceRole": "ruler",
                },
                {
                    "id": "measured_aspect",
                    "sourceHouse": 1,
                    "targetHouse": 3,
                    "relation": "aspect",
                    "sourceRole": "ruler",
                    "targetRole": "occupant",
                },
            ],
        )

        elements, clauses = service.formula_clause_results(
            formula,
            positions,
            houses,
            service.major_aspects(positions),
            "traditional",
        )

        self.assertTrue(any(element.body == "mars" and element.role == "ruler" for element in elements))
        self.assertTrue(all(clause.matched for clause in clauses))
        self.assertEqual(clauses[0].evidence[0].relation, "ruler_in_house")
        self.assertEqual(clauses[1].evidence[0].aspect.kind, "sextile")

    def test_formula_request_requires_all_ten_planets_and_known_time(self):
        birth = {
            "localDate": "1992-05-30",
            "localTime": "09:30",
            "timeAccuracy": "exact",
            "timeZone": "Europe/Lisbon",
            "location": {"latitude": 38.7223, "longitude": -9.1393},
        }
        formula = {
            "id": "plain",
            "title": "Plain structural rule",
            "school": {
                "id": "neutral-structural-v1",
                "version": "1",
                "attribution": "Umbra neutral example",
                "licence": "neutral",
            },
            "clauses": [
                {
                    "id": "one",
                    "sourceHouse": 1,
                    "targetHouse": 10,
                    "relation": "ruler_in_house",
                }
            ],
        }
        with self.assertRaises(ValueError):
            service.EventFormulaRequest(
                version="event-formula-request-v1",
                birth=birth,
                bodies=["sun", "moon"],
                rulershipProfile="traditional",
                formula=formula,
            )
        with self.assertRaises(ValueError):
            service.EventFormulaRequest(
                version="event-formula-request-v1",
                birth={**birth, "localTime": None, "timeAccuracy": "unknown"},
                bodies=list(service.FORMULA_REQUIRED_BODIES),
                rulershipProfile="traditional",
                formula=formula,
            )

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
