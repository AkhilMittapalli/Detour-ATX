"""Tests for the work-calendar signal.

This module exists because the Verifier reasoned its way to the distinction
unprompted — asked about a permit whose window had just opened, it answered
"unclear" because the day was a Sunday. Encoding it deterministically means
the model stops paying to rediscover it, and the rules baseline gets better.
"""

from __future__ import annotations

import datetime as dt
import unittest

from detour import confidence as conf
from detour import workcal

UTC = dt.timezone.utc


def utc(y, m, d, h, mi=0):
    return dt.datetime(y, m, d, h, mi, tzinfo=UTC)


class CentralTimeTests(unittest.TestCase):
    def test_summer_is_cdt(self):
        # 19:00 UTC in September is 14:00 CDT.
        self.assertEqual(workcal.to_central(utc(2026, 9, 13, 19)).hour, 14)

    def test_winter_is_cst(self):
        # 19:00 UTC in January is 13:00 CST.
        self.assertEqual(workcal.to_central(utc(2026, 1, 13, 19)).hour, 13)


class HolidayTests(unittest.TestCase):
    def test_fixed_date_holidays(self):
        self.assertEqual(workcal.federal_holiday(dt.date(2026, 7, 4)), "Independence Day")
        self.assertEqual(workcal.federal_holiday(dt.date(2026, 12, 25)), "Christmas Day")

    def test_floating_holidays(self):
        # Labor Day 2026 is Monday 7 September.
        self.assertEqual(workcal.federal_holiday(dt.date(2026, 9, 7)), "Labor Day")

    def test_an_ordinary_day_is_not_a_holiday(self):
        self.assertIsNone(workcal.federal_holiday(dt.date(2026, 9, 16)))


class CrewPlausibilityTests(unittest.TestCase):
    def test_sunday_is_not_plausible(self):
        """The case the agent flagged. 2026-09-13 is a Sunday."""
        plausible, reason = workcal.crew_plausible(utc(2026, 9, 13, 19))
        self.assertFalse(plausible)
        self.assertIn("Sunday", reason)

    def test_weekday_midday_is_plausible(self):
        # Wednesday 16 Sep, 19:00 UTC = 14:00 local.
        plausible, _ = workcal.crew_plausible(utc(2026, 9, 16, 19))
        self.assertTrue(plausible)

    def test_middle_of_the_night_is_not(self):
        # 08:00 UTC = 03:00 local.
        plausible, reason = workcal.crew_plausible(utc(2026, 9, 16, 8))
        self.assertFalse(plausible)
        self.assertIn("before typical working hours", reason)

    def test_a_permit_declaring_24_7_always_counts(self):
        plausible, reason = workcal.crew_plausible(
            utc(2026, 9, 13, 19), description="Work proceeds 24/7 for the duration."
        )
        self.assertTrue(plausible)
        self.assertIn("continuously", reason)

    def test_declared_weekend_work_counts_on_a_weekend(self):
        plausible, _ = workcal.crew_plausible(
            utc(2026, 9, 13, 19),
            description="Deliveries on Saturday and Sunday when UT does not play.",
        )
        self.assertTrue(plausible)

    def test_night_work_flips_the_window(self):
        night = "Night work only, lanes reopen by 6am."
        plausible, _ = workcal.crew_plausible(utc(2026, 9, 16, 8), description=night)
        self.assertTrue(plausible, "03:00 local should suit a night permit")
        plausible, _ = workcal.crew_plausible(utc(2026, 9, 16, 19), description=night)
        self.assertFalse(plausible, "14:00 local should not")


class ConfidenceInteractionTests(unittest.TestCase):
    """A checked-in crew at 3am is a stale checkbox, not a sighting."""

    def zone(self, **kw):
        base = {
            "vehicle_impact": "all-lanes-closed",
            "description": "Replace wiring.",
            "are_workers_present": True,
            "_start": utc(2026, 9, 1, 12),
            "_end": utc(2026, 9, 30, 12),
        }
        base.update(kw)
        return base

    def test_workers_present_confirms_during_working_hours(self):
        verdict = conf.score_work_zone(self.zone(), now=utc(2026, 9, 16, 19))
        self.assertIs(verdict.level, conf.Confidence.CONFIRMED)
        self.assertTrue(verdict.pushable)

    def test_workers_present_does_not_confirm_on_a_sunday(self):
        verdict = conf.score_work_zone(self.zone(), now=utc(2026, 9, 13, 19))
        self.assertIsNot(verdict.level, conf.Confidence.CONFIRMED)
        self.assertFalse(verdict.pushable)
        self.assertTrue(any("checked in, but" in r for r in verdict.reasons))

    def test_a_24_7_permit_still_confirms_on_a_sunday(self):
        verdict = conf.score_work_zone(
            self.zone(description="Work proceeds 24/7."), now=utc(2026, 9, 13, 19)
        )
        self.assertIs(verdict.level, conf.Confidence.CONFIRMED)


class ActivityNoteTests(unittest.TestCase):
    def test_note_says_the_closure_is_probably_still_there(self):
        """Cones do not go home on Sunday.

        A reader who drives around a closure that is genuinely there trusts
        us less next time, so the note must never read as "ignore this".
        """
        note = workcal.activity_note({"description": "Replace wiring."}, utc(2026, 9, 13, 19))
        self.assertIn("no crew expected", note)
        self.assertIn("still in place", note)

    def test_no_note_during_working_hours(self):
        self.assertEqual(
            workcal.activity_note({"description": "x"}, utc(2026, 9, 16, 19)), ""
        )


class AccessClauseTests(unittest.TestCase):
    """"24/7" in these permits is usually an obligation, not a schedule.

    Observed live: "ACCESS TO RESTAURANT MUST BE MAINTAINED 24/7" is a
    promise to keep a doorway reachable. Reading it as a work schedule made
    a Sunday lane closure look actively staffed.
    """

    def test_access_obligation_is_not_a_work_schedule(self):
        text = "**ACCESS TO RESTAURANT MUST BE MAINTAINED 24/7** Portions of the Right of Way will be affected."
        self.assertNotIn("always_on", workcal.schedule_hints(text))
        plausible, reason = workcal.crew_plausible(utc(2026, 9, 13, 19), description=text)
        self.assertFalse(plausible)
        self.assertIn("Sunday", reason)

    def test_a_real_continuous_schedule_still_counts(self):
        text = "Excavation proceeds 24/7 until the main is restored."
        self.assertIn("always_on", workcal.schedule_hints(text))

    def test_both_clauses_in_one_description(self):
        text = (
            "Pedestrian access must be maintained 24/7. "
            "Work will run continuously until the tie-in is complete."
        )
        self.assertIn("always_on", workcal.schedule_hints(text))


class RewriteTruncationTests(unittest.TestCase):
    """A fluent fragment is worse than no rewrite at all.

    Observed live with a 150-token cap: the model spent the budget on
    reasoning and emitted "Crews are installing a bike" — authoritative
    phrasing that says nothing. The deterministic cleaner's rougher but
    complete sentence is the better product.
    """

    def test_fragments_are_rejected(self):
        from detour import describe

        for fragment in [
            "Crews are",
            "Crews are installing a bike",
            "Austin Energy is replacing wiring on the",
            "Work is happening near",
        ]:
            with self.subTest(fragment=fragment):
                self.assertTrue(describe._looks_truncated(fragment))

    def test_complete_sentences_pass(self):
        from detour import describe

        for good in [
            "Austin Energy is replacing overhead wiring; one eastbound lane is closed.",
            "Crews are installing a new bike lane north of the intersection.",
        ]:
            with self.subTest(good=good):
                self.assertFalse(describe._looks_truncated(good))
