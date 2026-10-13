"""Tests for the Google Maps handoff.

The point of this module is that the two products do not compete for the
same tap, so the tests that matter are the ones about restraint: no link
when there is nothing to hand over, and no broken brief when the link
cannot be built.
"""

from __future__ import annotations

import unittest
import urllib.parse

from detour import handoff


def query(url: str) -> dict[str, str]:
    return dict(urllib.parse.parse_qsl(urllib.parse.urlparse(url).query))


def straight_line(n: int = 21):
    return [(-97.75 + i * 0.0005, 30.27) for i in range(n)]


class Waypoints(unittest.TestCase):
    def test_three_evenly_spaced_points(self):
        points = handoff.sample_waypoints(straight_line())
        self.assertEqual(len(points), 3)
        lons = [p[0] for p in points]
        self.assertAlmostEqual(lons[0], -97.747, places=4)
        self.assertAlmostEqual(lons[1], -97.7445, places=4)
        self.assertAlmostEqual(lons[2], -97.742, places=4)

    def test_endpoints_are_excluded(self):
        """Repeating them makes Maps add a stop at the kerb you left from."""
        line = straight_line()
        points = handoff.sample_waypoints(line)
        self.assertNotIn(line[0], points)
        self.assertNotIn(line[-1], points)

    def test_short_or_empty_lines_yield_nothing(self):
        self.assertEqual(handoff.sample_waypoints([]), [])
        self.assertEqual(handoff.sample_waypoints([(-97.75, 30.27)]), [])
        self.assertEqual(
            handoff.sample_waypoints([(-97.75, 30.27), (-97.74, 30.27)]), []
        )

    def test_a_zero_length_line_does_not_divide_by_zero(self):
        same = [(-97.75, 30.27)] * 5
        self.assertEqual(handoff.sample_waypoints(same), [])


class UrlShape(unittest.TestCase):
    def test_addresses_get_the_city_appended(self):
        url = handoff.build("701 Brazos St", "1100 Congress Ave").url
        self.assertEqual(query(url)["origin"], "701 Brazos St, Austin, TX")

    def test_an_address_already_naming_austin_is_left_alone(self):
        url = handoff.build("701 BRAZOS ST, AUSTIN, TX, 78701", "x").url
        self.assertEqual(query(url)["origin"], "701 BRAZOS ST, AUSTIN, TX, 78701")

    def test_coordinates_are_not_turned_into_prose(self):
        """The bug this guards: "30.29,-97.74, Austin, TX" geocodes nowhere."""
        url = handoff.build("30.294668,-97.742490", "30.274859,-97.744134").url
        self.assertEqual(query(url)["origin"], "30.294668,-97.742490")
        self.assertNotIn("Austin", query(url)["origin"])

    def test_cycling_asks_maps_for_cycling(self):
        url = handoff.build("a", "b", mode="bike").url
        self.assertEqual(query(url)["travelmode"], "bicycling")
        self.assertEqual(query(handoff.build("a", "b").url)["travelmode"], "driving")

    def test_api_version_appears_exactly_once(self):
        url = handoff.build("a", "b").url
        self.assertEqual(url.count("api=1"), 1)

    def test_waypoints_are_lat_lng_in_that_order(self):
        """GeoJSON is (lon, lat); Maps wants (lat, lon). Easy to invert."""
        link = handoff.build("a", "b", line=straight_line())
        first = query(link.url)["waypoints"].split("|")[0]
        lat, lon = (float(v) for v in first.split(","))
        self.assertAlmostEqual(lat, 30.27, places=2)
        self.assertAlmostEqual(lon, -97.747, places=2)

    def test_no_waypoints_means_no_waypoint_parameter(self):
        self.assertNotIn("waypoints", query(handoff.build("a", "b").url))

    def test_a_derived_route_carries_its_caveat(self):
        self.assertTrue(handoff.build("a", "b", line=straight_line()).caveat)
        self.assertFalse(handoff.build("a", "b").caveat)


class FakeRoute:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class FakeDelta:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class FakeBrief:
    def __init__(self, route=None, delta=None):
        self.route = route
        self.delta = delta


class ForBrief(unittest.TestCase):
    def route(self, **kw):
        base = dict(path=straight_line(), origin="701 Brazos St", destination="1100 Congress Ave")
        base.update(kw)
        return FakeRoute(**base)

    def test_plain_route_gets_a_link_with_no_waypoints(self):
        link = handoff.for_brief(FakeBrief(self.route()))
        self.assertIsNotNone(link)
        self.assertEqual(link.waypoints, 0)

    def test_a_real_detour_is_handed_over_as_waypoints(self):
        delta = FakeDelta(affected=True, implausible=False, detour_line=straight_line())
        link = handoff.for_brief(FakeBrief(self.route(), delta))
        self.assertEqual(link.waypoints, 3)
        self.assertIn("derived detour", link.why)

    def test_an_implausible_detour_is_not_handed_over(self):
        """We already refuse to show it; handing it to Maps is worse."""
        delta = FakeDelta(affected=True, implausible=True, detour_line=straight_line())
        self.assertEqual(handoff.for_brief(FakeBrief(self.route(), delta)).waypoints, 0)

    def test_cycling_carries_the_route_even_with_nothing_closed(self):
        """Maps has a cycling mode; it does not know which roads are hostile."""
        link = handoff.for_brief(FakeBrief(self.route()), mode="bike")
        self.assertEqual(link.waypoints, 3)
        self.assertIn("low-stress", link.why)

    def test_a_route_with_no_addresses_falls_back_to_coordinates(self):
        link = handoff.for_brief(FakeBrief(self.route(origin="", destination="")))
        origin = query(link.url)["origin"]
        self.assertTrue(origin.startswith("30."), origin)
        self.assertNotIn("Austin", origin)

    def test_a_missing_route_yields_no_link_rather_than_an_error(self):
        self.assertIsNone(handoff.for_brief(FakeBrief(None)))

    def test_a_route_missing_everything_does_not_raise(self):
        """A brief must never fail to render over a convenience link."""
        self.assertIsNone(handoff.for_brief(FakeBrief(FakeRoute())))

    def test_an_object_that_is_not_a_brief_does_not_raise(self):
        self.assertIsNone(handoff.for_brief(object()))


if __name__ == "__main__":
    unittest.main()
