"""Tests for event clustering and the nightly-sweep artifact.

The sweep is the important one. On the day this was written, 90 of 131
degraded signals shared the timestamp 09:00:59 UTC across a 26 km bounding
box, every one with `second == 59`. Without detecting that, single-linkage
clustering chains most of the city into one meaningless "event" — and worse,
every duration derived from those timestamps is wrong.
"""

from __future__ import annotations

import datetime as dt
import unittest

from detour import cluster

UTC = dt.timezone.utc
NOW = dt.datetime(2026, 9, 13, 21, 0, tzinfo=UTC)


def signal(name, lon, lat, minutes_ago, state="Communication issue", signal_id=None):
    return {
        "signal_id": signal_id or name,
        "location_name": name,
        "operation_text": state,
        "location": {"type": "Point", "coordinates": [lon, lat]},
        "_since": NOW - dt.timedelta(minutes=minutes_ago),
    }


class ClusterTests(unittest.TestCase):
    def test_adjacent_simultaneous_failures_are_one_event(self):
        signals = [
            signal("RIVERSIDE / ALAMEDA", -97.7400, 30.2470, 60),
            signal("RIVERSIDE / BARTON SPRINGS", -97.7450, 30.2480, 60),
            signal("CONGRESS / RIVERSIDE", -97.7430, 30.2490, 60),
        ]
        events = cluster.cluster_signals(signals)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].size, 3)
        self.assertEqual(events[0].spread_minutes, 0)

    def test_different_states_do_not_merge(self):
        signals = [
            signal("A", -97.7400, 30.2470, 60),
            signal("B", -97.7405, 30.2472, 60, state="Unscheduled (Conflict) flash"),
        ]
        self.assertEqual(len(cluster.cluster_signals(signals)), 2)

    def test_distant_failures_do_not_merge(self):
        signals = [
            signal("DOWNTOWN", -97.7430, 30.2690, 60),
            signal("FAR NORTH", -97.7500, 30.4500, 60),
        ]
        self.assertEqual(len(cluster.cluster_signals(signals)), 2)

    def test_failures_hours_apart_do_not_merge(self):
        signals = [
            signal("A", -97.7400, 30.2470, 30),
            signal("B", -97.7405, 30.2472, 400),
        ]
        self.assertEqual(len(cluster.cluster_signals(signals)), 2)

    def test_single_linkage_chains_along_a_corridor(self):
        """A joins B, B joins C, so all three are one event.

        Right shape for a fault propagating along a run of cabinets.
        """
        signals = [
            signal("A", -97.7400, 30.2470, 60),
            signal("B", -97.7480, 30.2470, 58),
            signal("C", -97.7560, 30.2470, 56),
        ]
        events = cluster.cluster_signals(signals)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].size, 3)


class SweepDetectionTests(unittest.TestCase):
    def _citywide_batch(self, count=12):
        """Many signals, one instant, spread across the whole city."""
        out = []
        for i in range(count):
            out.append(
                signal(f"SWEPT {i}", -97.95 + i * 0.03, 30.15 + i * 0.03, 720,
                       signal_id=f"s{i}")
            )
        return out

    def test_a_citywide_simultaneous_stamp_is_a_sweep(self):
        signals = self._citywide_batch()
        detected = cluster.batch_timestamps(signals)
        self.assertEqual(len(detected), 1)
        self.assertIn(signals[0]["_since"], detected)

    def test_a_tight_real_cluster_is_not_a_sweep(self):
        """Seven signals on one corridor must survive.

        This is the live Riverside Drive case: same minute, genuinely one
        event. Detecting sweeps must not swallow it.
        """
        signals = [
            signal(f"RIVERSIDE {i}", -97.7400 + i * 0.002, 30.2470, 60)
            for i in range(7)
        ]
        self.assertEqual(cluster.batch_timestamps(signals), set())
        events = cluster.cluster_signals(signals)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].size, 7)

    def test_a_small_dispersed_group_is_not_a_sweep(self):
        """Below the member threshold, spread alone is not enough."""
        signals = [
            signal("A", -97.95, 30.15, 720),
            signal("B", -97.65, 30.45, 720),
        ]
        self.assertEqual(cluster.batch_timestamps(signals), set())

    def test_swept_signals_are_excluded_from_clustering(self):
        signals = self._citywide_batch() + [
            signal("REAL A", -97.7400, 30.2470, 60),
            signal("REAL B", -97.7420, 30.2472, 60),
        ]
        events = cluster.cluster_signals(signals)
        self.assertEqual(len(events), 1, "only the genuine pair should remain")
        self.assertEqual(events[0].size, 2)

    def test_onset_is_not_trusted_for_swept_records(self):
        signals = self._citywide_batch()
        batch = cluster.batch_timestamps(signals)
        self.assertFalse(cluster.onset_is_real(signals[0], batch))
        real = signal("REAL", -97.7400, 30.2470, 5)
        self.assertTrue(cluster.onset_is_real(real, batch))


class NotableTests(unittest.TestCase):
    def test_a_cluster_is_always_notable(self):
        signals = [
            signal("A", -97.7400, 30.2470, 60 * 24 * 400),
            signal("B", -97.7420, 30.2472, 60 * 24 * 400),
        ]
        events = cluster.cluster_signals(signals)
        self.assertEqual(len(cluster.notable(events, now=NOW)), 1)

    def test_a_stale_lone_failure_is_not(self):
        """One signal in this feed has been unreachable since July 2021.

        Re-reporting it every morning is how a brief becomes wallpaper.
        """
        events = cluster.cluster_signals([signal("OLD", -97.74, 30.24, 60 * 24 * 400)])
        self.assertEqual(cluster.notable(events, now=NOW), [])

    def test_a_fresh_lone_failure_is(self):
        events = cluster.cluster_signals([signal("NEW", -97.74, 30.24, 30)])
        self.assertEqual(len(cluster.notable(events, now=NOW)), 1)


if __name__ == "__main__":
    unittest.main()
