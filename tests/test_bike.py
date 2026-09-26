"""Tests for the cyclist view.

The asymmetry tests matter most. The whole reason this module exists is
that `vehicle_impact` is written from a car, so the tests that pin down
when we are allowed to disagree with it are the ones protecting the claim.
"""

from __future__ import annotations

import unittest

from detour import bike, geo
from detour.graph import Edge, Graph, build, node_of, shortest_path
from detour.severity import Tier


def line(*pairs):
    return {"type": "LineString", "coordinates": [list(p) for p in pairs]}


def facility_row(coords, facility, comfort=None, street=""):
    return {
        "the_geom": line(*coords),
        "bicycle_facility": facility,
        "bike_level_of_comfort": comfort,
        "line_type": "On-Street",
        "full_street_name": street,
    }


class ComfortDecoding(unittest.TestCase):
    def test_ranked_codes(self):
        self.assertEqual(bike.comfort_rank("H"), 3)
        self.assertEqual(bike.comfort_rank("HP"), 3)
        self.assertEqual(bike.comfort_rank("HU"), 2)
        self.assertEqual(bike.comfort_rank("M"), 2)
        self.assertEqual(bike.comfort_rank("L"), 1)

    def test_undecoded_codes_stay_unranked(self):
        """We decline to guess rather than inventing a rating."""
        for code in bike.UNRANKED_CODES:
            self.assertIsNone(bike.comfort_rank(code), code)

    def test_missing_rating_is_not_a_bad_rating(self):
        self.assertIsNone(bike.comfort_rank(None))
        self.assertIsNone(bike.comfort_rank(""))

    def test_case_and_whitespace_tolerated(self):
        self.assertEqual(bike.comfort_rank(" hp "), 3)


class FacilityClassing(unittest.TestCase):
    def test_protected_lane_is_separated(self):
        self.assertTrue(bike.is_separated("Bike Lane - Protected One-Way"))
        self.assertTrue(bike.is_separated("Trail - Paved"))

    def test_paint_is_not_separation(self):
        self.assertFalse(bike.is_separated("Bike Lane"))
        self.assertFalse(bike.is_separated("Sharrows"))

    def test_wide_curb_lane_is_not_dedicated_infrastructure(self):
        """2,544 rows citywide. A wide lane is a lane, not a bike facility."""
        self.assertFalse(bike.is_dedicated("Wide Curb Lane"))
        self.assertFalse(bike.is_dedicated("Shoulder"))
        self.assertFalse(bike.is_dedicated(None))


class Indexing(unittest.TestCase):
    def test_dedicated_only_filters_plain_street(self):
        rows = [
            facility_row([(-97.74, 30.27), (-97.739, 30.27)], "Bike Lane"),
            facility_row([(-97.74, 30.28), (-97.739, 30.28)], "Wide Curb Lane"),
            facility_row([(-97.74, 30.29), (-97.739, 30.29)], ""),
        ]
        self.assertEqual(len(bike.index_facilities(rows)), 1)
        self.assertEqual(len(bike.index_facilities(rows, dedicated_only=False)), 3)

    def test_degenerate_geometry_dropped(self):
        rows = [facility_row([(-97.74, 30.27)], "Bike Lane")]
        self.assertEqual(bike.index_facilities(rows), [])


class TextDetection(unittest.TestCase):
    def test_matches_explicit_bike_lane_closure(self):
        self.assertTrue(bike.affects_cyclists({"description": "Bike lane closed for paving"}))
        self.assertTrue(bike.affects_cyclists({"description": "Bicycle detour in place"}))

    def test_matches_shoulder_closure(self):
        self.assertTrue(bike.affects_cyclists({"description": "Shoulder closed, no flaggers"}))

    def test_does_not_match_unrelated_text(self):
        self.assertFalse(bike.affects_cyclists({"description": "Waterline replacement"}))
        self.assertFalse(bike.affects_cyclists({}))

    def test_naming_a_bike_is_not_warning_a_cyclist(self):
        """A live record: it names a bike and warns a rider of nothing."""
        self.assertFalse(bike.affects_cyclists(
            {"description": "Muniz will electrify the existing bike station on E 2nd St."}
        ))

    def test_word_boundary_is_a_real_word_boundary(self):
        r"""Guards the \b vs \x08 mistake this project already made once.

        A previous regex in this codebase shipped a literal backspace where
        a word boundary was intended, and its test passed for the wrong
        reason. Assert on the compiled pattern, not just on behaviour.
        """
        self.assertIn("\\b", bike.CYCLIST.pattern)
        self.assertNotIn("\x08", bike.CYCLIST.pattern)


class Retiering(unittest.TestCase):
    """The core claim: we are allowed to disagree with `vehicle_impact`."""

    def setUp(self):
        self.protected = bike.index_facilities([
            facility_row([(-97.75, 30.27), (-97.74, 30.27)],
                         "Bike Lane - Protected One-Way", "H", "GUADALUPE ST")
        ])
        self.painted = bike.index_facilities([
            facility_row([(-97.75, 30.27), (-97.74, 30.27)], "Bike Lane", "M")
        ])

    def zone(self, impact, description=""):
        return {
            "geometry": line((-97.748, 30.2701), (-97.742, 30.2701)),
            "vehicle_impact": impact,
            "description": description,
        }

    def test_partial_closure_on_protected_lane_is_blocking(self):
        impact = bike.zone_impact(self.zone("some-lanes-closed"), self.protected)
        self.assertIsNotNone(impact)
        self.assertEqual(impact.tier, Tier.BLOCKING)
        self.assertTrue(impact.separated)

    def test_partial_closure_on_paint_is_slowing_not_blocking(self):
        impact = bike.zone_impact(self.zone("some-lanes-closed"), self.painted)
        self.assertEqual(impact.tier, Tier.SLOWING)
        self.assertFalse(impact.separated)

    def test_full_closure_is_never_softened(self):
        impact = bike.zone_impact(self.zone("all-lanes-closed"), self.painted)
        self.assertEqual(impact.tier, Tier.BLOCKING)

    def test_records_whether_the_city_mentioned_bikes(self):
        silent = bike.zone_impact(self.zone("some-lanes-closed"), self.protected)
        self.assertFalse(silent.said_so)
        spoken = bike.zone_impact(
            self.zone("some-lanes-closed", "Bike lane closed"), self.protected
        )
        self.assertTrue(spoken.said_so)

    def test_prose_uses_english_not_database_labels(self):
        """"Bike Lane - Protected One-Way" is precise and reads terribly."""
        impact = bike.zone_impact(self.zone("some-lanes-closed"), self.protected)
        self.assertEqual(impact.label, "protected bike lane")
        self.assertNotIn("Protected One-Way", impact.note)
        self.assertIn("protected bike lane", impact.note)

    def test_unknown_facility_still_gets_a_readable_label(self):
        self.assertEqual(bike.facility_label("Some New Thing"), "some new thing")
        self.assertEqual(bike.facility_label(None), "bike route")

    def test_no_facility_nearby_returns_none(self):
        far = {
            "geometry": line((-97.90, 30.50), (-97.89, 30.50)),
            "vehicle_impact": "all-lanes-closed",
        }
        self.assertIsNone(bike.zone_impact(far, self.protected))


class StressModel(unittest.TestCase):
    def test_separation_beats_everything(self):
        self.assertEqual(bike.stress_multiplier(55, "Bike Lane - Protected Two-Way"), 1.0)

    def test_fast_road_without_facility_is_heavily_penalised(self):
        quiet = bike.stress_multiplier(25, None)
        arterial = bike.stress_multiplier(50, None)
        self.assertGreater(arterial, quiet * 4)

    def test_city_rating_overrides_the_speed_guess(self):
        """A street the city rated comfortable is not punished for its limit."""
        self.assertLess(
            bike.stress_multiplier(45, None, "H"),
            bike.stress_multiplier(45, None, None),
        )

    def test_unrated_street_falls_back_rather_than_assuming_the_worst(self):
        self.assertEqual(
            bike.stress_multiplier(30, None, None),
            bike.stress_multiplier(30, None, "SS"),
        )

    def test_unpaved_trail_is_slower_but_not_stressful(self):
        self.assertLess(
            bike.cycling_speed_ms("Trail - Unpaved"), bike.cycling_speed_ms("Trail - Paved")
        )


class RoutingHonesty(unittest.TestCase):
    """Perceived cost steers the route; reported duration stays real."""

    def graph_of(self, stress_on_direct):
        graph = Graph()
        a, b, c = (0.0, 0.0), (0.01, 0.0), (0.005, 0.01)
        direct = [(0.0, 0.0), (0.01, 0.0)]
        graph.add(Edge("direct", "FAST RD", node_of(a), node_of(b),
                       1000.0, 100.0, direct, stress=stress_on_direct))
        graph.add(Edge("up", "QUIET A", node_of(a), node_of(c),
                       800.0, 80.0, [(0.0, 0.0), (0.005, 0.01)], stress=1.0))
        graph.add(Edge("down", "QUIET B", node_of(c), node_of(b),
                       800.0, 80.0, [(0.005, 0.01), (0.01, 0.0)], stress=1.0))
        graph.finalise()
        return graph, node_of(a), node_of(b)

    def test_low_stress_detour_is_chosen_when_the_direct_road_is_hostile(self):
        graph, start, goal = self.graph_of(stress_on_direct=4.5)
        seconds, metres, indices = shortest_path(graph, start, goal)
        self.assertEqual(len(indices), 2)
        self.assertAlmostEqual(metres, 1600.0)

    def test_direct_road_wins_when_it_is_comfortable(self):
        graph, start, goal = self.graph_of(stress_on_direct=1.0)
        _, metres, indices = shortest_path(graph, start, goal)
        self.assertEqual(len(indices), 1)
        self.assertAlmostEqual(metres, 1000.0)

    def test_reported_duration_is_real_time_not_perceived_time(self):
        """The bug this guards: quoting a rider 40 minutes for a 25 minute ride."""
        graph, start, goal = self.graph_of(stress_on_direct=4.5)
        seconds, _, _ = shortest_path(graph, start, goal)
        self.assertAlmostEqual(seconds, 160.0)

    def test_driving_mode_leaves_cost_untouched(self):
        rows = [{
            "the_geom": line((-97.75, 30.27), (-97.74, 30.27)),
            "full_street_name": "GUADALUPE ST",
            "speed_limit": "30",
            "one_way": "B",
            "segment_id": "1",
        }]
        graph = build(rows)
        self.assertTrue(all(edge.stress == 1.0 for edge in graph.edges))

    def test_bike_mode_marks_a_hostile_street(self):
        rows = [{
            "the_geom": line((-97.75, 30.27), (-97.74, 30.27)),
            "full_street_name": "LAMAR BLVD",
            "speed_limit": "45",
            "one_way": "B",
            "segment_id": "1",
        }]
        graph = build(rows, mode="bike")
        self.assertGreater(graph.edges[0].stress, 2.0)

    def test_bike_mode_credits_a_protected_lane(self):
        rows = [{
            "the_geom": line((-97.75, 30.27), (-97.74, 30.27)),
            "full_street_name": "GUADALUPE ST",
            "speed_limit": "45",
            "one_way": "B",
            "segment_id": "1",
        }]
        facilities = bike.index_facilities([
            facility_row([(-97.75, 30.27), (-97.74, 30.27)],
                         "Bike Lane - Protected One-Way", "H", "GUADALUPE ST")
        ])
        graph = build(rows, mode="bike", facilities=facilities)
        self.assertEqual(graph.edges[0].stress, 1.0)

    def test_bike_mode_without_facilities_still_builds(self):
        rows = [{
            "the_geom": line((-97.75, 30.27), (-97.74, 30.27)),
            "full_street_name": "GUADALUPE ST",
            "speed_limit": "30",
            "one_way": "B",
            "segment_id": "1",
        }]
        graph = build(rows, mode="bike", facilities=None)
        self.assertEqual(len(graph.edges), 2)

    def test_freeway_is_dropped_from_the_bike_graph(self):
        """You cannot legally ride IH 35, so it is not an edge."""
        rows = [{
            "the_geom": line((-97.75, 30.27), (-97.74, 30.27)),
            "full_street_name": "N IH 35 SB",
            "road_class": "1",
            "speed_limit": "65",
            "one_way": "FT",
            "segment_id": "1",
        }]
        self.assertEqual(len(build(rows).edges), 1)
        self.assertEqual(len(build(rows, mode="bike").edges), 0)

    def test_service_road_is_kept(self):
        """Frontage roads are legal and often carry the only bike lane."""
        rows = [{
            "the_geom": line((-97.75, 30.27), (-97.74, 30.27)),
            "full_street_name": "N IH 35 SVRD SB",
            "road_class": "6",
            "speed_limit": "40",
            "one_way": "FT",
            "segment_id": "1",
        }]
        self.assertEqual(len(build(rows, mode="bike").edges), 1)

    def test_parallel_trail_does_not_credit_a_road(self):
        """The observed failure: a shared-use path 11.9 m from the mainlane."""
        rows = [{
            "the_geom": line((-97.75, 30.27), (-97.74, 30.27)),
            "full_street_name": "S CONGRESS AVE",
            "road_class": "4",
            "speed_limit": "40",
            "one_way": "B",
            "segment_id": "1",
        }]
        trail = bike.index_facilities([{
            "the_geom": line((-97.75, 30.2701), (-97.74, 30.2701)),
            "bicycle_facility": "Trail - Paved",
            "bike_level_of_comfort": "HP",
            "line_type": "Off-Street",
            "full_street_name": "",
        }])
        graph = build(rows, mode="bike", facilities=trail)
        self.assertGreater(graph.edges[0].stress, 2.0)

    def test_adjacent_street_lane_does_not_credit_this_street(self):
        """A bike lane on the frontage road is not a bike lane on the freeway."""
        rows = [{
            "the_geom": line((-97.75, 30.27), (-97.74, 30.27)),
            "full_street_name": "N LAMAR BLVD",
            "road_class": "4",
            "speed_limit": "45",
            "one_way": "B",
            "segment_id": "1",
        }]
        other = bike.index_facilities([
            facility_row([(-97.75, 30.2701), (-97.74, 30.2701)],
                         "Bike Lane - Protected One-Way", "H", "GUADALUPE ST")
        ])
        graph = build(rows, mode="bike", facilities=other)
        self.assertGreater(graph.edges[0].stress, 2.0)

    def test_unnamed_facility_may_still_match_on_street(self):
        """35% carry no street name; geometry alone has to carry those."""
        rows = [{
            "the_geom": line((-97.75, 30.27), (-97.74, 30.27)),
            "full_street_name": "N LAMAR BLVD",
            "road_class": "4",
            "speed_limit": "45",
            "one_way": "B",
            "segment_id": "1",
        }]
        unnamed = bike.index_facilities([
            facility_row([(-97.75, 30.2700), (-97.74, 30.2700)],
                         "Bike Lane - Protected One-Way", "H", "")
        ])
        graph = build(rows, mode="bike", facilities=unnamed)
        self.assertEqual(graph.edges[0].stress, 1.0)

    def test_cycling_duration_is_slower_than_driving(self):
        rows = [{
            "the_geom": line((-97.75, 30.27), (-97.74, 30.27)),
            "full_street_name": "GUADALUPE ST",
            "speed_limit": "30",
            "one_way": "B",
            "segment_id": "1",
        }]
        drive = build(rows).edges[0].seconds
        ride = build(rows, mode="bike").edges[0].seconds
        self.assertGreater(ride, drive)


if __name__ == "__main__":
    unittest.main()
