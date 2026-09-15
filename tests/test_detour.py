"""Offline tests for the Phase 1 logic.

Nothing here touches the network. Fixtures are trimmed copies of real rows
observed in the live feeds, including their defects — the mangled inch mark
in a duct bank description and the literal "unknown" direction are both
things the city actually publishes.

    python -m unittest discover tests
"""

from __future__ import annotations

import datetime as dt
import unittest

from detour import confidence as conf
from detour import describe, geo, severity, sources

UTC = dt.timezone.utc
NOW = dt.datetime(2026, 9, 13, 19, 0, tzinfo=UTC)


def zone(**overrides) -> dict:
    base = {
        "road_names": "W OLTORF ST",
        "vehicle_impact": "some-lanes-closed",
        "direction": "eastbound",
        "critical_corridor": False,
        "are_workers_present": False,
        "is_end_date_verified": False,
        "description": "Replace wiring from P7 to P8.",
        "_start": NOW - dt.timedelta(days=10),
        "_end": NOW + dt.timedelta(days=10),
    }
    base.update(overrides)
    return base


class TimestampTests(unittest.TestCase):
    def test_utc_suffix_is_respected(self):
        parsed = sources.parse_ts("2026-09-13T18:48:26.000Z", naive_is_central=False)
        self.assertEqual(parsed, dt.datetime(2026, 9, 13, 18, 48, 26, tzinfo=UTC))

    def test_naive_signal_time_is_read_as_central(self):
        """The signal feed publishes local time with no offset.

        14:10 Central in September is CDT, so 19:10 UTC. Reading it as UTC
        would claim a signal had been flashing five hours longer than it had.
        """
        parsed = sources.parse_ts("2026-09-13T14:10:26.000", naive_is_central=True)
        self.assertEqual(parsed, dt.datetime(2026, 9, 13, 19, 10, 26, tzinfo=UTC))

    def test_central_offset_switches_with_dst(self):
        winter = dt.datetime(2026, 1, 15, 12, 0)
        summer = dt.datetime(2026, 7, 15, 12, 0)
        self.assertEqual(sources._central_offset_hours(winter), -6)
        self.assertEqual(sources._central_offset_hours(summer), -5)

    def test_garbage_returns_none(self):
        self.assertIsNone(sources.parse_ts("not a date", naive_is_central=False))
        self.assertIsNone(sources.parse_ts(None, naive_is_central=False))


class ConfidenceTests(unittest.TestCase):
    def test_verified_end_date_is_confirmed(self):
        verdict = conf.score_work_zone(zone(is_end_date_verified=True), now=NOW)
        self.assertIs(verdict.level, conf.Confidence.CONFIRMED)
        self.assertTrue(verdict.pushable)

    def test_workers_present_is_confirmed_on_a_working_day(self):
        # NOW is a Sunday, so this deliberately uses a Wednesday: a crew
        # check-in only counts when a crew could plausibly be there. See
        # tests/test_workcal.py.
        wednesday = dt.datetime(2026, 9, 16, 19, 0, tzinfo=UTC)
        verdict = conf.score_work_zone(
            zone(are_workers_present=True, _end=wednesday + dt.timedelta(days=10)),
            now=wednesday,
        )
        self.assertIs(verdict.level, conf.Confidence.CONFIRMED)

    def test_workers_present_does_not_confirm_out_of_hours(self):
        verdict = conf.score_work_zone(zone(are_workers_present=True), now=NOW)
        self.assertIsNot(verdict.level, conf.Confidence.CONFIRMED)

    def test_year_long_permit_window_is_not_evidence_about_today(self):
        verdict = conf.score_work_zone(
            zone(_start=NOW - dt.timedelta(days=190), _end=NOW + dt.timedelta(days=175)),
            now=NOW,
        )
        self.assertIs(verdict.level, conf.Confidence.PROBABLY_OVER)

    def test_closeout_phrasing_beats_an_open_window(self):
        """Observed in live data: an active permit whose own notes say the
        crew already left."""
        verdict = conf.score_work_zone(
            zone(description="***THIS PROJECT HAS CLEARED THE ROW.*** Replace wiring."),
            now=NOW,
        )
        self.assertIs(verdict.level, conf.Confidence.PROBABLY_OVER)

    def test_future_permit_is_not_active(self):
        verdict = conf.score_work_zone(
            zone(_start=NOW + dt.timedelta(days=30), _end=NOW + dt.timedelta(days=60)),
            now=NOW,
        )
        self.assertIs(verdict.level, conf.Confidence.PROBABLY_OVER)

    def test_plain_active_permit_is_reported_and_not_pushable(self):
        verdict = conf.score_work_zone(zone(), now=NOW)
        self.assertIs(verdict.level, conf.Confidence.REPORTED)
        self.assertFalse(verdict.pushable)

    def test_conflict_flash_is_confirmed_but_comms_loss_is_not(self):
        flashing = {
            "operation_text": "Unscheduled (Conflict) flash",
            "_since": NOW - dt.timedelta(hours=6),
        }
        dark = {
            "operation_text": "Communication issue",
            "_since": NOW - dt.timedelta(days=1880),
        }
        self.assertIs(conf.score_signal(flashing, now=NOW).level, conf.Confidence.CONFIRMED)
        self.assertIs(conf.score_signal(dark, now=NOW).level, conf.Confidence.REPORTED)


class SeverityTests(unittest.TestCase):
    def test_full_closure_is_blocking(self):
        self.assertIs(
            severity.work_zone_tier(zone(vehicle_impact="all-lanes-closed")),
            severity.Tier.BLOCKING,
        )

    def test_probably_over_never_reaches_blocking(self):
        """The anti-crying-wolf rule.

        One alert about a closure that is not there costs more trust than
        ten missed lane restrictions.
        """
        verdict = conf.Verdict(conf.Confidence.PROBABLY_OVER, ["stale"])
        self.assertIs(
            severity.combine(severity.Tier.BLOCKING, verdict), severity.Tier.SLOWING
        )

    def test_reported_keeps_its_tier_but_cannot_push(self):
        """Only 126 of 3,853 records carry a verified date, so demoting
        everything unverified would leave the top tier permanently empty."""
        verdict = conf.Verdict(conf.Confidence.REPORTED, ["unverified"])
        self.assertIs(
            severity.combine(severity.Tier.BLOCKING, verdict), severity.Tier.BLOCKING
        )
        self.assertFalse(verdict.pushable)


class DescribeTests(unittest.TestCase):
    def test_strips_permit_boilerplate_and_annotations(self):
        raw = (
            "Temporary use of Right of Way Permit has been issued for this location. "
            "Details: ***PER COORDINATION WITH 2025-032743 RW*** EXTENDED PER Ryan Mooney "
            "WR 197389 - AE will replace wiring from P7 to P8. Work will be behind the curb."
        )
        cleaned = describe.clean(raw)
        self.assertNotIn("Permit has been issued", cleaned)
        self.assertNotIn("***", cleaned)
        self.assertNotIn("Mooney", cleaned)
        self.assertIn("replace wiring", cleaned)

    def test_drops_the_citys_mangled_characters(self):
        """The feed publishes U+00BF where a dash or inch mark was mangled."""
        cleaned = describe.clean("Installing a 9x5¿ duct bank in two sections.")
        self.assertNotIn("¿", cleaned)
        self.assertIn("duct bank", cleaned)

    def test_strips_extension_ledgers(self):
        raw = (
            "27 extension from Aug 26, 2026 to Sep 21, 2026 "
            "46 extension from Jul 11, 2026 to Aug 25, 2026 "
            "Install 44 lf w/12in casing and remove manhole."
        )
        cleaned = describe.clean(raw)
        self.assertNotIn("extension from", cleaned)
        self.assertIn("casing", cleaned)

    def test_empty_input_is_safe(self):
        self.assertEqual(describe.clean(None), "")
        self.assertEqual(describe.clean("   "), "")


class GeoTests(unittest.TestCase):
    def test_haversine_matches_a_known_distance(self):
        # Texas Capitol to the UT Tower, ~1.6 km apart.
        capitol = (-97.7404, 30.2747)
        tower = (-97.7394, 30.2861)
        self.assertAlmostEqual(geo.haversine_m(capitol, tower) / 1000, 1.27, delta=0.15)

    def test_point_on_segment_has_zero_distance(self):
        a, b = (-97.75, 30.27), (-97.73, 30.27)
        self.assertLess(geo.point_to_segment_m((-97.74, 30.27), a, b), 1.0)

    def test_distance_clamps_to_segment_ends(self):
        a, b = (-97.75, 30.27), (-97.74, 30.27)
        beyond = (-97.70, 30.27)
        self.assertAlmostEqual(
            geo.point_to_segment_m(beyond, a, b), geo.haversine_m(beyond, b), delta=2.0
        )

    def test_densify_fills_long_legs(self):
        path = [(-97.75, 30.27), (-97.70, 30.27)]
        dense = geo.densify(path, max_gap_m=100)
        self.assertGreater(len(dense), 40)
        self.assertAlmostEqual(
            geo.path_length_m(dense), geo.path_length_m(path), delta=1.0
        )

    def test_coords_of_handles_every_shape_the_portal_serves(self):
        self.assertEqual(
            geo.coords_of({"type": "Point", "coordinates": [-97.7, 30.2]}),
            [(-97.7, 30.2)],
        )
        self.assertEqual(
            len(geo.coords_of({"type": "MultiLineString",
                               "coordinates": [[[-97.7, 30.2], [-97.6, 30.3]]]})),
            2,
        )
        self.assertEqual(geo.coords_of(None), [])


if __name__ == "__main__":
    unittest.main()
