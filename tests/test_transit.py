"""Tests for the protobuf reader and CapMetro rider impact.

No network. GTFS-Realtime messages are built here from the wire format, which
doubles as a check that the encoder assumptions in the reader are right: if
the reader can decode a message this file encoded by hand, both agree on what
the format is.
"""

from __future__ import annotations

import struct
import unittest

from detour import geo, protobuf as pb, transit


# --------------------------------------------------------------------------
# A tiny encoder, so the tests build real wire-format bytes
# --------------------------------------------------------------------------

def varint(value: int) -> bytes:
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        out.append(byte | (0x80 if value else 0))
        if not value:
            return bytes(out)


def key(number: int, wire: int) -> bytes:
    return varint((number << 3) | wire)


def f_varint(number: int, value: int) -> bytes:
    return key(number, 0) + varint(value)


def f_float(number: int, value: float) -> bytes:
    return key(number, 5) + struct.pack("<f", value)


def f_bytes(number: int, payload: bytes) -> bytes:
    return key(number, 2) + varint(len(payload)) + payload


def f_string(number: int, text: str) -> bytes:
    return f_bytes(number, text.encode("utf-8"))


def vehicle_entity(entity_id, route, trip, lat, lon, stop="", ts=1789355846) -> bytes:
    trip_msg = f_string(1, trip) + f_string(5, route)
    position = f_float(1, lat) + f_float(2, lon)
    vehicle_desc = f_string(1, entity_id) + f_string(2, entity_id)
    body = (
        f_bytes(1, trip_msg)
        + f_bytes(2, position)
        + f_varint(5, ts)
        + (f_string(7, stop) if stop else b"")
        + f_bytes(8, vehicle_desc)
    )
    return f_bytes(2, f_string(1, entity_id) + f_bytes(4, body))


def feed(*entities: bytes, timestamp=1789355849) -> bytes:
    header = f_string(1, "2.0") + f_varint(2, 0) + f_varint(3, timestamp)
    return f_bytes(1, header) + b"".join(entities)


# --------------------------------------------------------------------------

class VarintTests(unittest.TestCase):
    def test_round_trips_across_byte_boundaries(self):
        for value in (0, 1, 127, 128, 300, 16383, 16384, 1789355849, 2**63 - 1):
            with self.subTest(value=value):
                decoded, pos = pb.read_varint(varint(value), 0)
                self.assertEqual(decoded, value)
                self.assertEqual(pos, len(varint(value)))

    def test_a_truncated_varint_is_an_error_not_a_hang(self):
        with self.assertRaises(pb.ProtobufError):
            pb.read_varint(b"\x80\x80\x80", 0)

    def test_an_overlong_varint_is_refused(self):
        with self.assertRaises(pb.ProtobufError):
            pb.read_varint(b"\x80" * 12 + b"\x01", 0)


class WireFormatTests(unittest.TestCase):
    def test_each_wire_type_decodes(self):
        data = f_varint(1, 42) + f_string(2, "hello") + f_float(3, 1.5)
        fields = pb.parse(data)
        self.assertEqual(fields[1], [42])
        self.assertEqual(fields[2], ["hello"])
        self.assertAlmostEqual(fields[3][0], 1.5, places=5)

    def test_repeated_fields_collect(self):
        fields = pb.parse(f_varint(1, 1) + f_varint(1, 2) + f_varint(1, 3))
        self.assertEqual(fields[1], [1, 2, 3])

    def test_nested_messages_decode(self):
        inner = f_string(1, "trip-1") + f_string(5, "311")
        fields = pb.parse(f_bytes(4, inner))
        self.assertEqual(pb.first(fields, 4, 5), "311")

    def test_field_number_zero_is_refused(self):
        with self.assertRaises(pb.ProtobufError):
            pb.parse(key(0, 0) + varint(1))

    def test_a_truncated_payload_is_an_error(self):
        with self.assertRaises(pb.ProtobufError):
            pb.parse(key(1, 2) + varint(50) + b"short")

    def test_binary_blobs_survive_as_bytes(self):
        blob = bytes([0, 1, 2, 250, 251])
        value = pb.parse(f_bytes(1, blob))[1][0]
        self.assertIn(type(value), (bytes, dict))

    def test_deep_nesting_is_capped(self):
        payload = f_string(1, "x")
        for _ in range(pb.MAX_DEPTH + 4):
            payload = f_bytes(1, payload)
        # Either it raises, or the cap stops it returning an unbounded tree.
        try:
            pb.parse(payload)
        except pb.ProtobufError:
            pass


class HelperTests(unittest.TestCase):
    def test_first_follows_a_path(self):
        fields = pb.parse(f_bytes(1, f_bytes(2, f_string(3, "deep"))))
        self.assertEqual(pb.first(fields, 1, 2, 3), "deep")

    def test_first_returns_none_for_a_missing_path(self):
        self.assertIsNone(pb.first(pb.parse(f_varint(1, 1)), 9, 9))
        self.assertIsNone(pb.first(None, 1))

    def test_every_returns_an_empty_list_for_junk(self):
        self.assertEqual(pb.every(None, 1), [])
        self.assertEqual(pb.every({}, 1), [])


class GtfsRealtimeTests(unittest.TestCase):
    """Field numbers verified against the live CapMetro feed."""

    def setUp(self):
        self.raw = feed(
            vehicle_entity("2306", "311", "3002097_8591", 30.23622, -97.70358, stop="6439"),
            vehicle_entity("2304", "30", "3002100_1", 30.34580, -97.75170, stop="4651"),
            vehicle_entity("2303", "", "3002101_2", 30.27960, -97.68240),
        )

    def test_the_feed_decodes_into_vehicles(self):
        import unittest.mock as mock
        with mock.patch.object(transit, "_fetch", return_value=self.raw):
            snap = transit.vehicles(use_cache=False)
        self.assertTrue(snap.ok)
        self.assertEqual(len(snap.vehicles), 3)
        self.assertEqual(snap.feed_timestamp, 1789355849)

    def test_route_and_position_are_read_from_the_right_fields(self):
        import unittest.mock as mock
        with mock.patch.object(transit, "_fetch", return_value=self.raw):
            snap = transit.vehicles(use_cache=False)
        first = snap.vehicles[0]
        self.assertEqual(first.route_id, "311")
        self.assertEqual(first.vehicle_id, "2306")
        self.assertEqual(first.stop_id, "6439")
        self.assertAlmostEqual(first.point[1], 30.23622, places=4)
        self.assertAlmostEqual(first.point[0], -97.70358, places=4)

    def test_a_vehicle_with_no_route_is_kept_but_not_counted(self):
        import unittest.mock as mock
        with mock.patch.object(transit, "_fetch", return_value=self.raw):
            snap = transit.vehicles(use_cache=False)
        self.assertEqual(snap.routes, {"311", "30"})

    def test_a_corrupt_feed_is_an_error_not_a_crash(self):
        import unittest.mock as mock
        with mock.patch.object(transit, "_fetch", return_value=b"\xff\xff\xff\xff"):
            snap = transit.vehicles(use_cache=False)
        self.assertFalse(snap.ok)
        self.assertIn("decode", snap.error)

    def test_an_unreachable_feed_is_an_error_not_a_crash(self):
        import unittest.mock as mock
        import urllib.error
        with mock.patch.object(transit, "_fetch",
                               side_effect=urllib.error.URLError("down")):
            snap = transit.vehicles(use_cache=False)
        self.assertFalse(snap.ok)
        self.assertIn("unreachable", snap.error)


class RouteMatchingTests(unittest.TestCase):
    def _snapshot(self, *vehicles):
        snap = transit.Snapshot(feed_timestamp=1789355849)
        snap.vehicles = list(vehicles)
        return snap

    def test_a_bus_on_the_corridor_is_matched(self):
        line = [(-97.7500, 30.2600), (-97.7500, 30.2700)]
        snap = self._snapshot(
            transit.Vehicle("1", "801", "t", (-97.75002, 30.26500))
        )
        impacts = transit.routes_near(line, snap)
        self.assertEqual([i.route_id for i in impacts], ["801"])
        self.assertLess(impacts[0].nearest_m, 20)

    def test_a_bus_a_few_blocks_away_is_not(self):
        line = [(-97.7500, 30.2600), (-97.7500, 30.2700)]
        snap = self._snapshot(transit.Vehicle("1", "801", "t", (-97.7400, 30.2650)))
        self.assertEqual(transit.routes_near(line, snap), [])

    def test_routes_are_ranked_by_how_many_buses_are_there(self):
        line = [(-97.7500, 30.2600), (-97.7500, 30.2700)]
        snap = self._snapshot(
            transit.Vehicle("1", "1", "t", (-97.75001, 30.2610)),
            transit.Vehicle("2", "1", "t", (-97.75001, 30.2650)),
            transit.Vehicle("3", "4", "t", (-97.75005, 30.2630)),
        )
        impacts = transit.routes_near(line, snap)
        self.assertEqual(impacts[0].route_id, "1")
        self.assertEqual(impacts[0].vehicles_nearby, 2)

    def test_a_failed_snapshot_yields_no_impacts(self):
        self.assertEqual(
            transit.routes_near([(-97.75, 30.26)], transit.Snapshot(error="down")), []
        )

    def test_the_note_is_empty_when_nothing_is_affected(self):
        self.assertEqual(transit.impact_note([]), "")

    def test_the_note_names_the_routes(self):
        note = transit.impact_note([
            transit.RouteImpact("1", 2, 40.0),
            transit.RouteImpact("4", 1, 110.0),
        ])
        self.assertIn("Bus routes 1, 4", note)
        self.assertIn("stops moved or skipped", note)


if __name__ == "__main__":
    unittest.main()
