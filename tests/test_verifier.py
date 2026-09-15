"""Offline tests for the model layer, the evidence tools and the Verifier.

No network and no API key. The tools that would query Socrata are exercised
through the ones that work purely on the in-memory corpus; the agent loop is
driven by a fake session.

    python -m unittest discover tests
"""

from __future__ import annotations

import datetime as dt
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from detour import confidence as conf
from detour import config, evidence, ledger, llm, verifier

UTC = dt.timezone.utc
NOW = dt.datetime(2026, 9, 13, 19, 0, tzinfo=UTC)


def zone(record_id="z1", **overrides) -> dict:
    base = {
        "id": record_id,
        "name": f"permit {record_id}",
        "road_names": "W OLTORF ST",
        "vehicle_impact": "some-lanes-closed",
        "direction": "eastbound",
        "critical_corridor": False,
        "are_workers_present": False,
        "is_end_date_verified": False,
        "description": "Replace wiring from P7 to P8.",
        "geometry": {
            "type": "LineString",
            "coordinates": [[-97.7450, 30.2700], [-97.7440, 30.2700]],
        },
        "_start": NOW - dt.timedelta(days=10),
        "_end": NOW + dt.timedelta(days=10),
    }
    base.update(overrides)
    return base


class VerdictParsingTests(unittest.TestCase):
    """Models wrap JSON in fences and prose. The parser has to cope."""

    def test_bare_json(self):
        parsed = verifier._parse_verdict('{"verdict":"absent","confidence":0.8}')
        self.assertEqual(parsed["verdict"], "absent")

    def test_fenced_json(self):
        text = 'Here is my answer:\n```json\n{"verdict": "present", "confidence": 0.9}\n```'
        self.assertEqual(verifier._parse_verdict(text)["verdict"], "present")

    def test_json_with_trailing_prose(self):
        text = '{"verdict": "unclear", "confidence": 0.3}\n\nLet me know if you need more.'
        self.assertEqual(verifier._parse_verdict(text)["verdict"], "unclear")

    def test_no_json_returns_none(self):
        self.assertIsNone(verifier._parse_verdict("I could not determine this."))
        self.assertIsNone(verifier._parse_verdict(""))

    def test_json_without_a_verdict_key_is_rejected(self):
        self.assertIsNone(verifier._parse_verdict('{"confidence": 0.9}'))


class ScopeTests(unittest.TestCase):
    def test_only_reported_records_are_worth_a_model_call(self):
        """Confirmed and Probably-over already have an answer."""
        corpus = [
            zone("reported"),
            zone("confirmed", is_end_date_verified=True),
            zone("stale", description="THIS PROJECT HAS CLEARED THE ROW."),
        ]
        picked = [z["id"] for z in verifier.reported_records(corpus, now=NOW)]
        self.assertEqual(picked, ["reported"])


class ToolboxTests(unittest.TestCase):
    def setUp(self):
        self.box = evidence.Toolbox([zone("a"), zone("b")], now=NOW)

    def test_permit_narrative_extracts_referenced_permits(self):
        """Descriptions cross-reference other permits by number.

        Following that reference is exactly the branch a fixed pipeline
        cannot express, and why this role is an agent at all.
        """
        box = evidence.Toolbox(
            [zone("a", description="PER COORDINATION WITH 2025-032743 RW, cleared.")],
            now=NOW,
        )
        out = box.permit_narrative("a")
        self.assertIn("2025-032743 RW", out["referenced_permits"])

    def test_permit_narrative_reports_the_window_length(self):
        self.assertEqual(self.box.permit_narrative("a")["window_days"], 20)

    def test_find_permit_locates_a_cross_reference(self):
        box = evidence.Toolbox(
            [zone("a", description="Work under 2025-032743 RW."), zone("b")], now=NOW
        )
        out = box.find_permit("2025-032743")
        self.assertEqual(out["count"], 1)
        self.assertEqual(out["matches"][0]["id"], "a")

    def test_overlapping_permits_finds_neighbours_on_the_same_stretch(self):
        out = self.box.overlapping_permits("a")
        self.assertEqual(out["count"], 1)

    def test_overlapping_permits_ignores_distant_work(self):
        far = zone("far", geometry={
            "type": "LineString", "coordinates": [[-97.60, 30.40], [-97.599, 30.401]]
        })
        box = evidence.Toolbox([zone("a"), far], now=NOW)
        self.assertEqual(box.overlapping_permits("a")["count"], 0)

    def test_unknown_record_is_an_error_not_a_crash(self):
        self.assertIn("error", self.box.permit_narrative("nope"))
        self.assertIn("error", self.box.overlapping_permits("nope"))


class ProviderTests(unittest.TestCase):
    """A real .env on the developer's machine must not leak into these."""

    def setUp(self):
        patcher = mock.patch.object(config, "ensure_loaded", lambda: None)
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher2 = mock.patch.object(llm, "ensure_loaded", lambda: None)
        patcher2.start()
        self.addCleanup(patcher2.stop)

    def test_gemini_key_wins_when_both_are_set(self):
        with mock.patch.dict(os.environ, {"GEMINI_API_KEY": "x", "ANTHROPIC_API_KEY": "y"}):
            self.assertEqual(llm.provider(), "gemini")

    def test_no_key_means_no_provider(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(llm.provider())

    def test_session_without_a_key_fails_clearly(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(llm.LLMError):
                llm.GeminiSession([])

    def test_complete_returns_none_with_no_provider(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(llm.complete("hello"))


class FakeSession:
    """Replays a scripted sequence of model turns."""

    def __init__(self, turns):
        self._turns = list(turns)
        self.calls_made = 0
        self.prompts: list[str] = []

    def _next(self):
        turn = self._turns.pop(0) if self._turns else llm.Turn(text="{}")
        self.calls_made += len(turn.calls)
        return turn

    def ask(self, text):
        self.prompts.append(text)
        return self._next()

    def give_results(self, results):
        self.prompts.append(f"results:{[name for name, _ in results]}")
        return self._next()


class AgentLoopTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.ledger_path = Path(self.dir.name) / "ledger.jsonl"
        self.box = evidence.Toolbox([zone("a")], now=NOW)

    def tearDown(self):
        self.dir.cleanup()

    def _run(self, turns, **kw):
        with mock.patch.object(llm, "GeminiSession", lambda *a, **k: FakeSession(turns)):
            with mock.patch.object(verifier.llm, "GeminiSession", lambda *a, **k: FakeSession(turns)):
                return verifier.verify("a", self.box, write_ledger=False, **kw)

    def test_straight_answer_without_tools(self):
        result = self._run([llm.Turn(text='{"verdict":"absent","confidence":0.7,"rationale":"done"}')])
        self.assertTrue(result.ok)
        self.assertEqual(result.verdict, "absent")
        self.assertEqual(result.confidence, 0.7)

    def test_tool_call_then_verdict(self):
        turns = [
            llm.Turn(calls=[llm.ToolCall("permit_narrative", {"record_id": "a"})]),
            llm.Turn(text='{"verdict":"present","confidence":0.6,"evidence":["window is 20 days"]}'),
        ]
        result = self._run(turns)
        self.assertEqual(result.tools_used, ["permit_narrative"])
        self.assertEqual(result.verdict, "present")
        self.assertEqual(result.evidence, ["window is 20 days"])

    def test_unknown_tool_does_not_crash_the_run(self):
        turns = [
            llm.Turn(calls=[llm.ToolCall("not_a_tool", {})]),
            llm.Turn(text='{"verdict":"unclear","confidence":0.1}'),
        ]
        self.assertEqual(self._run(turns).verdict, "unclear")

    def test_unparseable_reply_is_an_error_not_a_guess(self):
        result = self._run([llm.Turn(text="I think it is probably gone.")])
        self.assertFalse(result.ok)
        self.assertIn("unparseable reply", result.error)

    def test_truncated_verdict_is_retried_once(self):
        """Observed live: a reply cut off mid-JSON at the token ceiling.

        That is a budget problem, not a bad answer, so it earns one retry
        asking for the object alone rather than being reported as garbage.
        """
        turns = [
            llm.Turn(
                text='```json\n{\n "verdict": "unclear",\n "confidence": 0.65,',
                finish_reason="MAX_TOKENS",
            ),
            llm.Turn(text='{"verdict":"unclear","confidence":0.65,"rationale":"retried"}'),
        ]
        result = self._run(turns)
        self.assertTrue(result.ok)
        self.assertEqual(result.verdict, "unclear")
        self.assertEqual(result.rationale, "retried")

    def test_truncation_that_survives_the_retry_says_so(self):
        turns = [
            llm.Turn(text='{"verdict": "present",', finish_reason="MAX_TOKENS"),
            llm.Turn(text='{"verdict": "present",', finish_reason="MAX_TOKENS"),
        ]
        result = self._run(turns)
        self.assertFalse(result.ok)
        self.assertIn("truncated", result.error)

    def test_out_of_range_confidence_is_clamped(self):
        result = self._run([llm.Turn(text='{"verdict":"present","confidence":9.5}')])
        self.assertEqual(result.confidence, 1.0)

    def test_unknown_verdict_word_becomes_unclear(self):
        result = self._run([llm.Turn(text='{"verdict":"definitely","confidence":0.9}')])
        self.assertEqual(result.verdict, "unclear")

    def test_unknown_record_never_reaches_the_model(self):
        result = verifier.verify("missing", self.box, write_ledger=False)
        self.assertFalse(result.ok)
        self.assertIn("unknown record", result.error)


class AgentAuthorityTests(unittest.TestCase):
    """An agent may suppress a notification but never authorise one.

    A resident sighting is an observation; an agent verdict is inference
    over permit text. Letting inference mint a Confirmed — the tier that
    gates push — would put a model in the loop of deciding whose phone
    buzzes, on data where no record has a verified position.
    """

    def setUp(self):
        self.zone = zone(vehicle_impact="all-lanes-closed")

    def test_agent_absent_suppresses(self):
        claim = ledger.Claim("z1", "work_zone", "absent", "verifier", NOW.isoformat(), "0.8 | gone")
        verdict = conf.score_work_zone(self.zone, now=NOW, agent_verdict=claim)
        self.assertIs(verdict.level, conf.Confidence.PROBABLY_OVER)
        self.assertFalse(verdict.pushable)

    def test_agent_present_cannot_reach_confirmed(self):
        claim = ledger.Claim("z1", "work_zone", "present", "verifier", NOW.isoformat(), "0.9 | real")
        verdict = conf.score_work_zone(self.zone, now=NOW, agent_verdict=claim)
        self.assertIs(verdict.level, conf.Confidence.REPORTED)
        self.assertFalse(verdict.pushable)

    def test_a_resident_still_can(self):
        claim = ledger.Claim("z1", "work_zone", "present", "resident", NOW.isoformat())
        verdict = conf.score_work_zone(self.zone, now=NOW, observation=claim)
        self.assertIs(verdict.level, conf.Confidence.CONFIRMED)
        self.assertTrue(verdict.pushable)

    def test_a_resident_outranks_the_agent(self):
        resident = ledger.Claim("z1", "work_zone", "present", "resident", NOW.isoformat())
        agent = ledger.Claim("z1", "work_zone", "absent", "verifier", NOW.isoformat(), "0.9")
        verdict = conf.score_work_zone(
            self.zone, now=NOW, observation=resident, agent_verdict=agent
        )
        self.assertIs(verdict.level, conf.Confidence.CONFIRMED)


class LedgerSourceTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name) / "ledger.jsonl"

    def tearDown(self):
        self.dir.cleanup()

    def test_sources_do_not_bleed_into_each_other(self):
        ledger.append(
            ledger.Claim("a", "work_zone", "absent", "verifier", NOW.isoformat()),
            path=self.path,
        )
        self.assertIsNone(
            ledger.latest_observation("a", now=NOW, path=self.path, source="resident")
        )
        found = ledger.latest_observation(
            "a", now=NOW, path=self.path, source="verifier"
        )
        self.assertEqual(found.claim, "absent")

    def test_accuracy_counts_only_residents(self):
        ledger.append(
            ledger.Claim("a", "work_zone", "absent", "verifier", NOW.isoformat()),
            path=self.path,
        )
        ledger.confirm("b", present=True, path=self.path)
        self.assertEqual(ledger.accuracy(path=self.path)["total"], 1)


if __name__ == "__main__":
    unittest.main()


class GroupedObservationTests(unittest.TestCase):
    """A closure arrives as one row per direction.

    An observation filed against the southbound row must still show on the
    item, which is grouped under a single permit name. Missing this made
    verifier verdicts silently invisible in the brief even though the ledger
    held them.
    """

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name) / "ledger.jsonl"

    def tearDown(self):
        self.dir.cleanup()

    def test_verdict_on_a_non_lead_row_still_applies(self):
        from detour import brief as brief_mod
        from detour import route as rt

        north = zone("nb", direction="northbound", name="CIP - Example St")
        south = zone("sb", direction="southbound", name="CIP - Example St")

        ledger.append(
            ledger.Claim("sb", "work_zone", "absent", "verifier", NOW.isoformat(), "0.9 | gone"),
            path=self.path,
        )
        index = ledger.by_record(path=self.path)

        matches = [rt.Match(north, 1.0), rt.Match(south, 2.0)]
        items = brief_mod._work_zone_items(
            matches, NOW, rewrite=False, observations=index
        )

        self.assertEqual(len(items), 1, "per-direction rows should collapse to one item")
        self.assertIs(items[0].verdict.level, conf.Confidence.PROBABLY_OVER)
