"""Offline tests for the routing, detour and ledger layers.

The fixture is a small synthetic grid rather than real centreline rows, so
the graph behaviour is checkable by hand. Where a test encodes something
learned from live data, the docstring says so.

    python -m unittest discover tests
"""

from __future__ import annotations

import datetime as dt
import tempfile
import unittest
from pathlib import Path

from detour import graph as G
from detour import ledger, reroute

UTC = dt.timezone.utc
NOW = dt.datetime(2026, 9, 13, 19, 0, tzinfo=UTC)

# A 3x3 grid a few hundred metres on a side, near downtown Austin.
LON0, LAT0 = -97.7450, 30.2700
STEP = 0.0030


def at(col: int, row: int) -> tuple[float, float]:
    return (round(LON0 + col * STEP, 6), round(LAT0 + row * STEP, 6))


def segment(col1, row1, col2, row2, name, one_way="B", speed="30") -> dict:
    return {
        "segment_id": f"{name}-{col1}{row1}{col2}{row2}",
        "full_street_name": name,
        "one_way": one_way,
        "speed_limit": speed,
        "road_class": "6",
        "the_geom": {
            "type": "MultiLineString",
            "coordinates": [[list(at(col1, row1)), list(at(col2, row2))]],
        },
    }


def grid() -> list[dict]:
    rows: list[dict] = []
    for row in range(3):
        for col in range(2):
            rows.append(segment(col, row, col + 1, row, f"E {row}TH ST"))
    for col in range(3):
        for row in range(2):
            rows.append(segment(col, row, col, row + 1, f"{col} AVE"))
    return rows


def closure(points, road_names, impact="all-lanes-closed") -> dict:
    return {
        "id": "test-closure",
        "name": f"test closure on {road_names}",
        "road_names": road_names,
        "vehicle_impact": impact,
        "geometry": {"type": "LineString", "coordinates": [list(p) for p in points]},
    }


class GraphTests(unittest.TestCase):
    def setUp(self):
        self.graph = G.build(grid())

    def test_two_way_segments_become_two_directed_edges(self):
        rows = [segment(0, 0, 1, 0, "E 0TH ST", one_way="B")]
        self.assertEqual(len(G.build(rows).edges), 2)

    def test_one_way_segments_become_one(self):
        for value in ("FT", "TF"):
            with self.subTest(one_way=value):
                rows = [segment(0, 0, 1, 0, "E 0TH ST", one_way=value)]
                self.assertEqual(len(G.build(rows).edges), 1)

    def test_one_way_direction_is_respected(self):
        rows = [segment(0, 0, 1, 0, "E 0TH ST", one_way="FT")]
        graph = G.build(rows)
        self.assertIsNotNone(G.shortest_path(graph, G.node_of(at(0, 0)), G.node_of(at(1, 0))))
        self.assertIsNone(G.shortest_path(graph, G.node_of(at(1, 0)), G.node_of(at(0, 0))))

    def test_lowercase_one_way_is_normalised(self):
        """One row in the live layer carries 'b' rather than 'B'."""
        rows = [segment(0, 0, 1, 0, "E 0TH ST", one_way="b")]
        self.assertEqual(len(G.build(rows).edges), 2)

    def test_missing_one_way_defaults_to_two_way(self):
        """Two rows in the live layer carry no value at all."""
        row = segment(0, 0, 1, 0, "E 0TH ST")
        del row["one_way"]
        self.assertEqual(len(G.build([row]).edges), 2)

    def test_shortest_path_crosses_the_grid(self):
        found = G.shortest_path(self.graph, G.node_of(at(0, 0)), G.node_of(at(2, 2)))
        self.assertIsNotNone(found)
        seconds, metres, path = found
        self.assertGreater(metres, 0)
        self.assertGreater(len(path), 1)

    def test_blocked_edges_are_skipped_only_when_asked(self):
        for edge in self.graph.edges:
            if edge.name == "E 0TH ST":
                edge.blocked = True
        start, goal = G.node_of(at(0, 0)), G.node_of(at(2, 0))

        direct = G.shortest_path(self.graph, start, goal, avoid_blocked=False)
        around = G.shortest_path(self.graph, start, goal, avoid_blocked=True)
        self.assertIsNotNone(direct)
        self.assertIsNotNone(around)
        self.assertGreater(around[1], direct[1])

    def test_follow_route_stays_on_the_drawn_street(self):
        """The reason `follow_route` exists.

        Routing endpoint-to-endpoint by travel time returns the globally
        fastest path, which downtown is the interstate — so a closure on the
        user's own street never enters the baseline. Chaining legs between
        waypoints keeps the baseline on the roads they actually drive.
        """
        waypoints = [at(0, 0), at(1, 0), at(2, 0)]
        found = G.follow_route(self.graph, waypoints)
        self.assertIsNotNone(found)
        self.assertEqual(G.street_sequence(self.graph, found[2]), ["E 0TH ST"])

    def test_count_turns_counts_street_changes(self):
        waypoints = [at(0, 0), at(2, 0), at(2, 2)]
        found = G.follow_route(self.graph, waypoints)
        self.assertEqual(G.count_turns(self.graph, found[2]), 1)


class BlockageTests(unittest.TestCase):
    def setUp(self):
        self.graph = G.build(grid())

    def test_full_closure_blocks_matching_edges(self):
        result = reroute.block_closure(
            self.graph, closure([at(0, 0), at(1, 0)], "E 0TH ST")
        )
        self.assertTrue(result.applied)
        self.assertTrue(all(self.graph.edges[i].name == "E 0TH ST" for i in result.edge_indices))

    def test_lane_closure_blocks_nothing(self):
        """A lane restriction slows traffic; it does not change topology."""
        result = reroute.block_closure(
            self.graph, closure([at(0, 0), at(1, 0)], "E 0TH ST", impact="some-lanes-closed")
        )
        self.assertFalse(result.applied)
        self.assertIn("not a full closure", result.note)

    def test_geometry_on_the_wrong_street_is_refused(self):
        """No work zone record carries a verified position.

        If a permit names one street and its geometry sits on another, we
        say so rather than silently deleting the wrong road.
        """
        result = reroute.block_closure(
            self.graph, closure([at(0, 0), at(1, 0)], "COLORADO ST")
        )
        self.assertFalse(result.applied)
        self.assertFalse(result.name_matched)
        self.assertIn("E 0TH ST", result.note)

    def test_street_type_suffixes_do_not_break_matching(self):
        rows = [segment(0, 0, 1, 0, "W OLTORF ST")]
        graph = G.build(rows)
        result = reroute.block_closure(graph, closure([at(0, 0), at(1, 0)], "OLTORF STREET"))
        self.assertTrue(result.applied)

    def test_closure_far_from_any_edge_is_refused(self):
        far = [(-97.60, 30.40), (-97.599, 30.401)]
        result = reroute.block_closure(self.graph, closure(far, "E 0TH ST"))
        self.assertFalse(result.applied)
        self.assertIn("no graph edges", result.note)


class DeltaTests(unittest.TestCase):
    def setUp(self):
        self.graph = G.build(grid())
        self.waypoints = [at(0, 0), at(1, 0), at(2, 0)]

    def test_no_closures_means_no_change(self):
        result = reroute.delta(self.graph, self.waypoints, [])
        self.assertFalse(result.affected)
        self.assertEqual(result.summary(), "No change to your route.")

    def test_closure_on_the_route_produces_a_detour(self):
        result = reroute.delta(
            self.graph, self.waypoints, [closure([at(0, 0), at(1, 0)], "E 0TH ST")]
        )
        self.assertTrue(result.affected)
        self.assertGreater(result.extra_m, 0)
        self.assertIn("+", result.summary())

    def test_closure_beside_the_route_is_ignored(self):
        result = reroute.delta(
            self.graph, self.waypoints, [closure([at(0, 2), at(1, 2)], "E 2TH ST")]
        )
        self.assertFalse(result.affected)

    def test_short_trip_around_one_block_is_not_called_implausible(self):
        """A guard that fired on exactly this case during development.

        Routing around a single closed block costs several times the direct
        distance. That is correct, not a sign of bad geometry, so the ratio
        test only applies above RATIO_FLOOR_M.
        """
        result = reroute.delta(
            self.graph, [at(0, 0), at(1, 0)], [closure([at(0, 0), at(1, 0)], "E 0TH ST")]
        )
        self.assertTrue(result.affected)
        self.assertFalse(result.implausible)

    def test_unreachable_destination_is_reported(self):
        rows = [segment(0, 0, 1, 0, "E 0TH ST")]
        graph = G.build(rows)
        result = reroute.delta(
            graph, [at(0, 0), at(1, 0)], [closure([at(0, 0), at(1, 0)], "E 0TH ST")]
        )
        self.assertTrue(result.unreachable)
        self.assertIn("No way through", result.summary())


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name) / "ledger.jsonl"

    def tearDown(self):
        self.dir.cleanup()

    def test_confirmation_round_trips(self):
        ledger.confirm("abc", present=True, path=self.path)
        claims = ledger.read_all(path=self.path)
        self.assertEqual(len(claims), 1)
        self.assertEqual(claims[0].claim, "present")
        self.assertEqual(claims[0].source, "resident")

    def test_latest_observation_wins(self):
        ledger.confirm("abc", present=True, path=self.path)
        ledger.confirm("abc", present=False, path=self.path)
        seen = ledger.latest_observation("abc", now=NOW, path=self.path)
        self.assertEqual(seen.claim, "absent")

    def test_stale_observations_stop_counting(self):
        old = ledger.Claim(
            record_id="abc",
            kind="work_zone",
            claim="present",
            source="resident",
            observed_at=(NOW - dt.timedelta(days=9)).isoformat(),
        )
        ledger.append(old, path=self.path)
        self.assertIsNone(ledger.latest_observation("abc", now=NOW, path=self.path))

    def test_malformed_lines_do_not_break_the_morning_run(self):
        ledger.confirm("abc", present=True, path=self.path)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write("{ this is not json\n")
        self.assertEqual(len(ledger.read_all(path=self.path)), 1)

    def test_accuracy_tallies_resident_observations(self):
        ledger.confirm("a", present=True, path=self.path)
        ledger.confirm("b", present=False, path=self.path)
        ledger.confirm("c", present=False, path=self.path)
        self.assertEqual(
            ledger.accuracy(path=self.path), {"present": 1, "absent": 2, "total": 3}
        )

    def test_missing_ledger_is_empty_not_an_error(self):
        self.assertEqual(ledger.read_all(path=self.path), [])


class GroundTruthOverrideTests(unittest.TestCase):
    """A resident sighting outranks the permit.

    It is the only verified statement anyone holds: the city verifies a date
    on 126 of 3,853 records and a position on none of them.
    """

    def setUp(self):
        from detour import confidence as conf

        self.conf = conf
        self.zone = {
            "vehicle_impact": "all-lanes-closed",
            "description": "Replace wiring.",
            "_start": NOW - dt.timedelta(days=5),
            "_end": NOW + dt.timedelta(days=5),
        }

    def test_present_sighting_confirms(self):
        seen = ledger.Claim("x", "work_zone", "present", "resident", NOW.isoformat())
        verdict = self.conf.score_work_zone(self.zone, now=NOW, observation=seen)
        self.assertIs(verdict.level, self.conf.Confidence.CONFIRMED)
        self.assertTrue(verdict.pushable)

    def test_absent_sighting_overrides_an_open_permit(self):
        seen = ledger.Claim("x", "work_zone", "absent", "resident", NOW.isoformat())
        verdict = self.conf.score_work_zone(self.zone, now=NOW, observation=seen)
        self.assertIs(verdict.level, self.conf.Confidence.PROBABLY_OVER)
        self.assertFalse(verdict.pushable)


if __name__ == "__main__":
    unittest.main()
