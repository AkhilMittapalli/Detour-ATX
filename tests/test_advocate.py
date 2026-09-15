"""Tests for the Advocate, the 311 baselines, and the evaluation harness.

The Advocate's most important property is what it cannot do: there is no send
path, so the tests assert the absence of one rather than the correctness of a
flag that could be flipped.
"""

from __future__ import annotations

import datetime as dt
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from detour import advocate, baseline, confidence as conf, evaluate, ledger

UTC = dt.timezone.utc
NOW = dt.datetime(2026, 9, 16, 19, 0, tzinfo=UTC)   # a Wednesday


def zone(**kw):
    base = {
        "id": "z1",
        "name": "CIP - Example St reconstruction",
        "road_names": "EXAMPLE ST",
        "vehicle_impact": "all-lanes-closed",
        "critical_corridor": True,
        "are_workers_present": False,
        "is_end_date_verified": False,
        "description": "Replace the water main.",
        "_start": NOW - dt.timedelta(days=200),
        "_end": NOW + dt.timedelta(days=170),
    }
    base.update(kw)
    return base


def signal(**kw):
    base = {
        "signal_id": "889",
        "location_name": "LAMAR BLVD SVRD / RESEARCH BLVD SVRD",
        "operation_text": "Communication issue",
        "location": {"type": "Point", "coordinates": [-97.74, 30.26]},
        "_since": NOW - dt.timedelta(days=1878),
    }
    base.update(kw)
    return base


class NoSendPathTests(unittest.TestCase):
    """The guardrail is architectural, not a flag.

    Automatically filing government service requests at scale is abuse
    regardless of intent. There is deliberately nothing here to misconfigure.
    """

    def test_the_module_cannot_reach_a_transport(self):
        import detour.advocate as module

        source = Path(module.__file__).read_text(encoding="utf-8")
        self.assertNotIn("import transport", source)
        self.assertNotIn("smtplib", source)
        self.assertNotIn("urlopen", source)

    def test_no_function_is_named_send_or_file(self):
        names = [n for n in dir(advocate) if not n.startswith("_")]
        for banned in ("send", "submit", "file_request", "post"):
            self.assertNotIn(banned, names)

    def test_every_draft_says_it_is_a_draft(self):
        draft = advocate.for_dark_signal(signal(), now=NOW, enabled=False)
        rendered = draft.render()
        self.assertIn("DRAFT", rendered)
        self.assertIn("Nothing has been filed", rendered)
        self.assertIn("submit it yourself", rendered)


class DraftContentTests(unittest.TestCase):
    def test_a_dark_signal_draft_states_the_duration(self):
        draft = advocate.for_dark_signal(signal(), now=NOW, enabled=False)
        self.assertIn("1878 days", " ".join(draft.evidence))
        self.assertTrue(draft.ok)

    def test_a_swept_timestamp_is_not_quoted_as_a_duration(self):
        """Saying "11 hours" about a signal down for years is worse than
        saying nothing, and the nightly sweep makes that easy to do."""
        sig = signal(_since=NOW - dt.timedelta(hours=11))
        draft = advocate.for_dark_signal(
            sig, now=NOW, swept={sig["_since"]}, enabled=False
        )
        joined = " ".join(draft.evidence) + draft.body
        self.assertIn("unknown period", joined)
        self.assertNotIn("11 hours", joined)

    def test_a_stale_permit_draft_cites_the_permit(self):
        draft = advocate.for_stale_permit(zone(), now=NOW, enabled=False)
        self.assertIn("CIP - Example St reconstruction", " ".join(draft.evidence))
        self.assertIn("EXAMPLE ST", draft.subject)

    def test_a_geometry_draft_names_both_streets(self):
        draft = advocate.for_bad_geometry(
            zone(road_names="COLORADO ST"), ["LAVACA ST", "BRAZOS ST"], enabled=False
        )
        joined = draft.body + " ".join(draft.evidence)
        self.assertIn("COLORADO ST", joined)
        self.assertIn("LAVACA ST", joined)

    def test_the_template_fallback_is_used_without_a_model(self):
        with mock.patch("detour.llm.provider", return_value=None):
            draft = advocate.for_dark_signal(signal(), now=NOW)
        self.assertFalse(draft.generated)
        self.assertGreater(len(draft.body), 60)

    def test_a_short_model_reply_falls_back_rather_than_shipping_a_fragment(self):
        with mock.patch("detour.llm.provider", return_value="gemini"), \
             mock.patch("detour.llm.complete", return_value="Too short."):
            draft = advocate.for_dark_signal(signal(), now=NOW)
        self.assertFalse(draft.generated)
        self.assertIn("Traffic Signals Status", draft.body)

    def test_saving_writes_a_file_and_files_nothing(self):
        with tempfile.TemporaryDirectory() as d:
            draft = advocate.for_dark_signal(signal(), now=NOW, enabled=False)
            path = advocate.save(draft, directory=Path(d), stamp=NOW)
            self.assertTrue(path.exists())
            self.assertIn("DRAFT", path.read_text(encoding="utf-8"))


class CandidateTests(unittest.TestCase):
    def test_only_long_dead_signals_are_candidates(self):
        old = signal(signal_id="a", _since=NOW - dt.timedelta(days=900))
        recent = signal(signal_id="b", _since=NOW - dt.timedelta(days=10))
        found = advocate.candidates([], [old, recent], now=NOW)
        ids = [s["signal_id"] for s in found["dark_signals"]]
        self.assertEqual(ids, ["a"])

    def test_flashing_signals_are_not_advocacy_candidates(self):
        """A flash is an operational emergency, not a paperwork complaint."""
        flash = signal(operation_text="Unscheduled (Conflict) flash")
        found = advocate.candidates([], [flash], now=NOW)
        self.assertEqual(found["dark_signals"], [])


class BaselineTests(unittest.TestCase):
    def test_an_auto_closed_type_refuses_to_judge_a_request(self):
        """The bug this caught: a one-day-old request reported as slower
        than 90% of comparable requests, because the "comparable" type is
        closed on creation."""
        stat = baseline.Stat("TPW - Activate/Deactivate Work Zone", 1898, 0.0, 0.0, 0.0)
        self.assertTrue(stat.auto_closed)
        self.assertIn("no meaningful wait time", stat.standing(1))
        self.assertIn("notification rather than", stat.describe())

    def test_a_real_queue_places_a_request_in_the_distribution(self):
        stat = baseline.Stat("Obstruction in ROW", 4986, 112.1, 258.6, 0.2)
        self.assertFalse(stat.auto_closed)
        self.assertIn("within", stat.standing(30))
        self.assertIn("p90", stat.standing(150))
        self.assertIn("slower than 90%", stat.standing(400))

    def test_impossible_spans_are_discarded(self):
        self.assertIsNone(baseline._days("2026-09-10T00:00:00", "2026-09-01T00:00:00"))
        self.assertIsNone(baseline._days("2026-09-01T00:00:00", None))
        self.assertIsNotNone(baseline._days("2026-09-01T00:00:00", "2026-09-05T00:00:00"))


class AgentLiftTests(unittest.TestCase):
    """The change the evaluation forced.

    An earlier version let the agent only suppress. Measuring it on records
    the rules had suppressed showed the agent finding genuine active work in
    5 of 12 cases, every one of which was discarded before reaching a verdict.
    """

    def _claim(self, verdict, detail="0.9 | six overlapping permits"):
        return ledger.Claim("z1", "work_zone", verdict, "verifier", NOW.isoformat(), detail)

    def test_the_rules_alone_suppress_a_year_long_window(self):
        self.assertIs(
            conf.score_work_zone(zone(), now=NOW).level, conf.Confidence.PROBABLY_OVER
        )

    def test_an_agent_finding_work_lifts_it_back_to_reported(self):
        verdict = conf.score_work_zone(zone(), now=NOW, agent_verdict=self._claim("present"))
        self.assertIs(verdict.level, conf.Confidence.REPORTED)
        self.assertTrue(any("verifier found active work" in r for r in verdict.reasons))

    def test_the_lift_still_cannot_reach_confirmed(self):
        """The ceiling that makes the lift safe: Reported cannot push."""
        verdict = conf.score_work_zone(zone(), now=NOW, agent_verdict=self._claim("present"))
        self.assertFalse(verdict.pushable)

    def test_the_lift_explains_what_the_rules_had_said(self):
        verdict = conf.score_work_zone(zone(), now=NOW, agent_verdict=self._claim("present"))
        self.assertTrue(any("rules suppressed this" in r for r in verdict.reasons))

    def test_suppression_still_works_in_the_other_direction(self):
        active = zone(_start=NOW - dt.timedelta(days=5), _end=NOW + dt.timedelta(days=5))
        verdict = conf.score_work_zone(active, now=NOW, agent_verdict=self._claim("absent"))
        self.assertIs(verdict.level, conf.Confidence.PROBABLY_OVER)

    def test_a_resident_still_outranks_the_agent(self):
        resident = ledger.Claim("z1", "work_zone", "absent", "resident", NOW.isoformat())
        verdict = conf.score_work_zone(
            zone(), now=NOW, observation=resident, agent_verdict=self._claim("present")
        )
        self.assertIs(verdict.level, conf.Confidence.PROBABLY_OVER)


class EvaluationTests(unittest.TestCase):
    def test_the_two_slices_select_opposite_populations(self):
        suppressed = zone()                                     # year-long window
        active = zone(id="z2", _start=NOW - dt.timedelta(days=5),
                      _end=NOW + dt.timedelta(days=5))
        pool = [suppressed, active]

        reported = evaluate.sample_population(pool, now=NOW, slice_name="reported-full")
        rescued = evaluate.sample_population(pool, now=NOW, slice_name="suppressed-critical")
        self.assertEqual([z["id"] for z in reported], ["z2"])
        self.assertEqual([z["id"] for z in rescued], ["z1"])

    def test_a_no_op_result_says_so_plainly(self):
        report = evaluate.Report(sampled=12, population=155, slice_name="x")
        report.cases = [
            evaluate.Case(f"r{i}", "RD", "all-lanes-closed", "reported",
                          "present", 0.8, 6, False)
            for i in range(12)
        ]
        self.assertEqual(report.agreement, 1.0)
        self.assertIn("expensive no-op", report.verdict())

    def test_changes_are_reported_with_their_cost(self):
        report = evaluate.Report(sampled=2, population=81, slice_name="x")
        report.cases = [
            evaluate.Case("a", "RD", "all-lanes-closed", "probably_over", "present", 0.9, 6, True),
            evaluate.Case("b", "RD", "all-lanes-closed", "probably_over", "unclear", 0.3, 6, False),
        ]
        self.assertEqual(len(report.changed), 1)
        self.assertIn("tool calls per record", report.verdict())

    def test_agreement_is_never_reported_as_accuracy(self):
        report = evaluate.Report(sampled=1, population=1, slice_name="x")
        report.cases = [
            evaluate.Case("a", "RD", "all-lanes-closed", "probably_over", "present", 0.9, 6, True)
        ]
        self.assertIn("agreement is not accuracy", report.verdict())

    def test_errored_cases_do_not_count_towards_agreement(self):
        report = evaluate.Report(sampled=2, population=2, slice_name="x")
        report.cases = [
            evaluate.Case("a", "RD", "", "reported", "", 0.0, 0, False, error="boom"),
            evaluate.Case("b", "RD", "", "probably_over", "present", 0.9, 6, True),
        ]
        self.assertEqual(len(report.usable), 1)
        self.assertEqual(report.agreement, 0.0)


if __name__ == "__main__":
    unittest.main()
