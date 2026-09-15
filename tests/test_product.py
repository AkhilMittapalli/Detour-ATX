"""Tests for the layer that turns the engine into a product.

The Correspondent is the one with real logic: deciding what is worth telling
a reader who was already told yesterday. Everything about it is deterministic,
so all of it is testable offline.
"""

from __future__ import annotations

import datetime as dt
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from detour import confidence as conf
from detour import correspondent, deliver, geocode, plan, severity
from detour.brief import Item

UTC = dt.timezone.utc
NOW = dt.datetime(2026, 9, 13, 19, 0, tzinfo=UTC)


def item(headline="W 16TH ST down to reduced lanes", level=conf.Confidence.REPORTED,
         tier=severity.Tier.SLOWING, record_id="abc", detail="Crews are digging."):
    return Item(
        tier=tier,
        verdict=conf.Verdict(level, ["permit window is 30 days"]),
        kind="work_zone",
        headline=headline,
        detail=detail,
        record_id=record_id,
    )


class CorrespondentTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name) / "ledger.jsonl"

    def tearDown(self):
        self.dir.cleanup()

    def _assess(self, it, *, now=NOW):
        from detour import ledger
        return correspondent.assess(
            it, reader="you", now=now, index=ledger.by_record(path=self.path)
        )

    def test_never_told_is_new(self):
        self.assertIs(self._assess(item()).news, correspondent.News.NEW)

    def test_told_today_is_a_repeat_and_is_held_back(self):
        """The anti-wallpaper rule.

        A closure reported for ninety straight mornings stops being
        information.
        """
        correspondent.record_told([item()], reader="you", now=NOW, path=self.path)
        decision = self._assess(item())
        self.assertIs(decision.news, correspondent.News.REPEAT)
        self.assertFalse(decision.tell)

    def test_a_confidence_change_is_news_again(self):
        correspondent.record_told([item()], reader="you", now=NOW, path=self.path)
        changed = item(level=conf.Confidence.PROBABLY_OVER)
        decision = self._assess(changed)
        self.assertIs(decision.news, correspondent.News.CHANGED)
        self.assertTrue(decision.tell)
        self.assertIn("Probably over", decision.reason)

    def test_a_severity_change_is_news_again(self):
        correspondent.record_told([item()], reader="you", now=NOW, path=self.path)
        decision = self._assess(item(tier=severity.Tier.BLOCKING))
        self.assertIs(decision.news, correspondent.News.CHANGED)
        self.assertIn("severity", decision.reason)

    def test_an_end_date_slipping_is_news_again(self):
        """The case that motivates the whole module.

        The same closure told for ninety days is noise; that closure
        quietly moving its end date by three months is not.
        """
        told = item(headline="COLORADO ST fully closed, through 25 Sep 2026")
        correspondent.record_told([told], reader="you", now=NOW, path=self.path)
        slipped = item(headline="COLORADO ST fully closed, through 25 Dec 2026")
        decision = self._assess(slipped)
        self.assertIs(decision.news, correspondent.News.CHANGED)
        self.assertIn("25 Dec 2026", decision.reason)

    def test_an_old_mention_resurfaces(self):
        correspondent.record_told([item()], reader="you", now=NOW, path=self.path)
        later = NOW + dt.timedelta(days=30)
        decision = self._assess(item(), now=later)
        self.assertIs(decision.news, correspondent.News.RESURFACED)
        self.assertTrue(decision.tell)

    def test_readers_do_not_share_memory(self):
        correspondent.record_told([item()], reader="alice", now=NOW, path=self.path)
        from detour import ledger
        decision = correspondent.assess(
            item(), reader="bob", now=NOW, index=ledger.by_record(path=self.path)
        )
        self.assertIs(decision.news, correspondent.News.NEW)

    def test_curate_splits_told_from_held(self):
        first = item(record_id="a", headline="A ST closed")
        second = item(record_id="b", headline="B ST closed")
        correspondent.record_told([first], reader="you", now=NOW, path=self.path)
        tell, held = correspondent.curate(
            [first, second], reader="you", now=NOW, path=self.path
        )
        self.assertEqual([i.record_id for i, _ in tell], ["b"])
        self.assertEqual([i.record_id for i, _ in held], ["a"])

    def test_items_without_a_record_id_still_get_a_stable_key(self):
        signal = Item(
            tier=severity.Tier.BACKGROUND,
            verdict=conf.Verdict(conf.Confidence.REPORTED, []),
            kind="signal",
            headline="RED RIVER ST / CLYDE LITTLEFIELD DR - flashing",
            detail="",
        )
        self.assertEqual(correspondent.item_key(signal), correspondent.item_key(signal))
        self.assertTrue(correspondent.item_key(signal).startswith("signal:"))


class SlugTests(unittest.TestCase):
    def test_names_become_safe_filenames(self):
        self.assertEqual(plan.slugify("South Congress commute"), "south-congress-commute")
        self.assertEqual(plan.slugify("Mueller -> Downtown!"), "mueller-downtown")
        self.assertEqual(plan.slugify("   "), "route")


class GeocodeTests(unittest.TestCase):
    def test_empty_address_is_refused_before_any_request(self):
        with self.assertRaises(geocode.GeocodeError):
            geocode.lookup("   ")

    def test_a_low_score_match_is_refused_with_advice(self):
        weak = [geocode.Place("SOMEWHERE, AUSTIN, TX", (-97.74, 30.26), 42.0)]
        with mock.patch.object(geocode, "lookup", return_value=weak):
            with self.assertRaises(geocode.GeocodeError) as ctx:
                geocode.resolve("xyz")
        self.assertIn("too low to trust", str(ctx.exception))

    def test_no_candidates_suggests_what_to_try(self):
        with mock.patch.object(geocode, "lookup", return_value=[]):
            with self.assertRaises(geocode.GeocodeError) as ctx:
                geocode.resolve("nowhere at all")
        self.assertIn("street number", str(ctx.exception))


class DeliverTests(unittest.TestCase):
    class FakeRoute:
        name = "South Congress commute"
        length_mi = 2.2

    class FakeBrief:
        route = None
        generated_at = NOW
        delta = None

    def _brief(self):
        b = self.FakeBrief()
        b.route = self.FakeRoute()
        return b

    def test_headline_counts_only_what_is_being_told(self):
        tell = [(item(tier=severity.Tier.BLOCKING), correspondent.Decision(correspondent.News.NEW))]
        out = deliver.render(self._brief(), tell=tell, held=[])
        self.assertIn("1 thing to plan around", out)

    def test_a_quiet_day_says_so_rather_than_looking_broken(self):
        out = deliver.render(self._brief(), tell=[], held=[])
        self.assertIn("Nothing new on your route", out)
        self.assertIn("Quiet is the normal state", out)

    def test_held_items_appear_as_a_standing_footer(self):
        held = [(item(headline="OLD ST closed"), correspondent.Decision(correspondent.News.REPEAT))]
        out = deliver.render(self._brief(), tell=[], held=held)
        self.assertIn("Still ongoing, told before", out)
        self.assertIn("OLD ST closed", out)

    def test_content_is_escaped(self):
        nasty = item(headline='<script>alert("x")</script>')
        tell = [(nasty, correspondent.Decision(correspondent.News.NEW))]
        out = deliver.render(self._brief(), tell=tell, held=[])
        self.assertNotIn("<script>", out)
        self.assertIn("&lt;script&gt;", out)

    def test_push_text_names_the_blocking_item(self):
        tell = [(item(headline="E 2ND ST fully closed", tier=severity.Tier.BLOCKING),
                 correspondent.Decision(correspondent.News.NEW))]
        text = deliver.plain_text(self._brief(), tell=tell)
        self.assertIn("E 2ND ST fully closed", text)

    def test_push_text_is_quiet_when_nothing_blocks(self):
        tell = [(item(), correspondent.Decision(correspondent.News.NEW))]
        self.assertIn("nothing blocking", deliver.plain_text(self._brief(), tell=tell))


if __name__ == "__main__":
    unittest.main()
