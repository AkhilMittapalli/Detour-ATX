"""Tests for the calendar surface.

Most of these are about the iCalendar format, because it fails quietly.
Google Calendar does not report a bad .ics — it simply declines to import
it, which is a worse failure than an error and invisible without tests.

The rest are about restraint: no event when there is nothing to say.
"""

from __future__ import annotations

import datetime as dt
import unittest

from detour import calendar, severity


class FakeVerdict:
    pushable = False


class FakeItem:
    def __init__(self, headline, tier=severity.Tier.BLOCKING):
        self.headline = headline
        self.tier = tier
        self.verdict = FakeVerdict()


class FakeDelta:
    def __init__(self, affected=True, implausible=False, extra_s=300, baseline_s=600):
        self.affected = affected
        self.implausible = implausible
        self.extra_s = extra_s
        self.baseline_s = baseline_s

    def summary(self):
        return "+0.8 mi, +5 min"


class FakeRoute:
    def __init__(self, leaves_at=None, days=(), name="South Congress commute"):
        self.name = name
        self._leaves = leaves_at
        self.days = days
        self.path = [(-97.75, 30.27), (-97.745, 30.27), (-97.74, 30.27)]
        self.origin = "1100 S Congress Ave"
        self.destination = "701 Brazos St"

    @property
    def leaves_at(self):
        return self._leaves

    def runs_on(self, day):
        return not self.days or day.weekday() in self.days


class FakeBrief:
    def __init__(self, route, delta=None):
        self.route = route
        self.delta = delta


MONDAY = dt.date(2026, 10, 12)
SATURDAY = dt.date(2026, 10, 17)


def tell(*headlines):
    return [(FakeItem(h), None) for h in headlines]


class Escaping(unittest.TestCase):
    def test_backslash_is_escaped_first(self):
        """Escape it last and it doubles the escapes you just added."""
        self.assertEqual(calendar._escape("a\\b"), "a\\\\b")

    def test_commas_semicolons_and_newlines(self):
        self.assertEqual(calendar._escape("a,b"), "a\\,b")
        self.assertEqual(calendar._escape("a;b"), "a\\;b")
        self.assertEqual(calendar._escape("a\nb"), "a\\nb")

    def test_a_real_headline_survives(self):
        out = calendar._escape("CONGRESS AVE fully closed, both directions")
        self.assertIn("closed\\, both", out)


class Folding(unittest.TestCase):
    def test_short_lines_are_untouched(self):
        self.assertEqual(calendar._fold("SUMMARY:short"), "SUMMARY:short")

    def test_long_lines_fold_with_a_leading_space(self):
        folded = calendar._fold("DESCRIPTION:" + "x" * 200)
        self.assertIn("\r\n ", folded)
        for piece in folded.split("\r\n"):
            self.assertLessEqual(len(piece.encode("utf-8")), 75)

    def test_folding_counts_octets_not_characters(self):
        """The rewritten descriptions carry em dashes — three bytes each."""
        folded = calendar._fold("DESCRIPTION:" + "—" * 60)
        for piece in folded.split("\r\n"):
            self.assertLessEqual(len(piece.encode("utf-8")), 75)

    def test_folding_never_splits_a_codepoint(self):
        folded = calendar._fold("DESCRIPTION:" + "—" * 60)
        # Would raise if a multi-byte character had been cut in half.
        folded.encode("utf-8").decode("utf-8")
        self.assertEqual(folded.replace("\r\n ", "").count("—"), 60)


class Document(unittest.TestCase):
    def ics(self, events=None):
        return calendar.render(
            events if events is not None else [],
            now=dt.datetime(2026, 10, 12, 6, 0, tzinfo=dt.timezone.utc),
        )

    def test_every_line_ends_crlf(self):
        text = self.ics()
        self.assertTrue(text.endswith("\r\n"))
        self.assertNotIn("\n\n", text)
        for line in text.split("\r\n")[:-1]:
            self.assertFalse(line.endswith("\r"))

    def test_envelope_is_well_formed(self):
        text = self.ics()
        self.assertTrue(text.startswith("BEGIN:VCALENDAR\r\n"))
        self.assertIn("VERSION:2.0", text)
        self.assertIn("PRODID:", text)
        self.assertTrue(text.rstrip().endswith("END:VCALENDAR"))

    def test_an_empty_calendar_is_still_valid(self):
        text = self.ics()
        self.assertEqual(text.count("BEGIN:VEVENT"), 0)

    def test_events_balance_their_tags(self):
        event = calendar.event_for(
            FakeBrief(FakeRoute()), tell("a"), day=MONDAY
        )
        text = self.ics([event])
        for tag in ("VEVENT", "VALARM"):
            self.assertEqual(text.count(f"BEGIN:{tag}"), text.count(f"END:{tag}"))

    def test_the_url_is_not_text_escaped(self):
        """Escaping it turns every waypoint comma into a backslash."""
        event = calendar.event_for(FakeBrief(FakeRoute()), tell("a"), day=MONDAY)
        line = [l for l in self.ics([event]).split("\r\n") if l.startswith("URL:")]
        self.assertTrue(line)
        self.assertNotIn("\\,", line[0])


class WhenToFire(unittest.TestCase):
    def test_nothing_to_say_means_no_event(self):
        """A calendar full of 'nothing is wrong' gets switched off."""
        self.assertIsNone(calendar.event_for(FakeBrief(FakeRoute()), [], day=MONDAY))

    def test_a_route_that_does_not_run_today_is_skipped(self):
        weekdays = FakeRoute(days=(0, 1, 2, 3, 4))
        self.assertIsNotNone(calendar.event_for(FakeBrief(weekdays), tell("a"), day=MONDAY))
        self.assertIsNone(calendar.event_for(FakeBrief(weekdays), tell("a"), day=SATURDAY))

    def test_a_missing_route_does_not_raise(self):
        self.assertIsNone(calendar.event_for(FakeBrief(None), tell("a"), day=MONDAY))


class TheTimeItNames(unittest.TestCase):
    def test_the_event_moves_earlier_by_the_detour_cost(self):
        """The advice is a time, not 'five minutes earlier than something'."""
        route = FakeRoute(leaves_at=dt.time(8, 30))
        event = calendar.event_for(
            FakeBrief(route, FakeDelta(extra_s=300)), tell("a"), day=MONDAY
        )
        self.assertEqual(event.start.strftime("%H:%M"), "08:25")
        self.assertIn("Leave by 08:25", event.summary)

    def test_no_detour_means_the_usual_time(self):
        route = FakeRoute(leaves_at=dt.time(8, 30))
        event = calendar.event_for(FakeBrief(route), tell("a"), day=MONDAY)
        self.assertEqual(event.start.strftime("%H:%M"), "08:30")
        self.assertNotIn("Leave by", event.summary)

    def test_an_implausible_detour_does_not_move_the_time(self):
        route = FakeRoute(leaves_at=dt.time(8, 30))
        delta = FakeDelta(implausible=True, extra_s=3000)
        event = calendar.event_for(FakeBrief(route, delta), tell("a"), day=MONDAY)
        self.assertEqual(event.start.strftime("%H:%M"), "08:30")

    def test_a_route_with_no_departure_time_still_gets_a_heads_up(self):
        event = calendar.event_for(FakeBrief(FakeRoute()), tell("a"), day=MONDAY)
        self.assertEqual(event.start.hour, calendar.DEFAULT_HOUR)
        self.assertNotIn("Leave by", event.summary)

    def test_the_alarm_fires_before_departure(self):
        event = calendar.event_for(FakeBrief(FakeRoute()), tell("a"), day=MONDAY)
        text = calendar.render([event])
        self.assertIn(f"TRIGGER:-PT{calendar.ALARM_LEAD_MIN}M", text)


class PushText(unittest.TestCase):
    """Feature 1's missing half: "earlier" is not an instruction."""

    def brief(self, leaves_at=None, delta=None):
        return FakeBrief(FakeRoute(leaves_at=leaves_at), delta)

    def test_a_route_with_a_departure_time_gets_a_leave_by(self):
        from detour import deliver

        brief = self.brief(dt.time(8, 30), FakeDelta(extra_s=300))
        self.assertEqual(deliver.leave_by(brief), "08:25")

    def test_no_departure_time_means_no_claim_about_one(self):
        from detour import deliver

        self.assertEqual(deliver.leave_by(self.brief(None, FakeDelta())), "")

    def test_no_detour_means_no_leave_by(self):
        from detour import deliver

        self.assertEqual(deliver.leave_by(self.brief(dt.time(8, 30))), "")

    def test_an_implausible_detour_does_not_move_the_time(self):
        from detour import deliver

        delta = FakeDelta(implausible=True, extra_s=3000)
        self.assertEqual(deliver.leave_by(self.brief(dt.time(8, 30), delta)), "")

    def test_a_sub_minute_detour_does_not_fake_precision(self):
        from detour import deliver

        self.assertEqual(deliver.leave_by(self.brief(dt.time(8, 30), FakeDelta(extra_s=20))), "")

    def test_the_push_payload_leads_with_the_time(self):
        from detour import deliver

        brief = self.brief(dt.time(8, 30), FakeDelta(extra_s=300))
        text = deliver.plain_text(brief, tell=tell("CONGRESS AVE fully closed"))
        self.assertTrue(text.startswith("Leave by 08:25"), text)


class Content(unittest.TestCase):
    def test_the_maps_link_rides_along(self):
        event = calendar.event_for(FakeBrief(FakeRoute()), tell("a"), day=MONDAY)
        self.assertIn("google.com/maps", event.url)
        self.assertIn("Open in Google Maps", event.description)

    def test_blocking_items_are_counted_in_the_summary(self):
        event = calendar.event_for(
            FakeBrief(FakeRoute()), tell("a", "b", "c"), day=MONDAY
        )
        self.assertIn("3 things to plan around", event.summary)

    def test_long_lists_are_truncated_rather_than_dumped(self):
        event = calendar.event_for(
            FakeBrief(FakeRoute()), tell(*[f"item {i}" for i in range(12)]), day=MONDAY
        )
        self.assertIn("and 6 more", event.description)

    def test_the_uid_is_stable_for_a_route_and_day(self):
        """Re-importing should update the event, not duplicate it."""
        a = calendar.event_for(FakeBrief(FakeRoute()), tell("a"), day=MONDAY)
        b = calendar.event_for(FakeBrief(FakeRoute()), tell("b"), day=MONDAY)
        c = calendar.event_for(FakeBrief(FakeRoute()), tell("a"), day=SATURDAY)
        self.assertEqual(a.uid, b.uid)
        self.assertNotEqual(a.uid, c.uid)

    def test_it_says_where_the_data_came_from(self):
        event = calendar.event_for(FakeBrief(FakeRoute()), tell("a"), day=MONDAY)
        self.assertIn("City of Austin", event.description)
        self.assertIn("confirmed unless it says so", event.description)


if __name__ == "__main__":
    unittest.main()
