"""CapMetro rider impact.

A closure on a bus corridor does not just cost drivers a detour. It costs
riders a stop, and they get told even less than drivers do — a diverted bus
route is announced, if at all, on a sign at a stop the bus is no longer
serving.

CapMetro publishes GTFS-Realtime on the state portal. The JSON mirrors return
403; only the protobuf endpoints serve, which is why `protobuf.py` exists.

Field numbers below come from the GTFS-Realtime spec and are verified against
the live feed rather than trusted from documentation:

    FeedMessage      1 header, 2 entity
    FeedEntity       1 id, 3 trip_update, 4 vehicle, 5 alert
    VehiclePosition  1 trip, 2 position, 5 timestamp, 7 stop_id, 8 vehicle
    TripDescriptor   1 trip_id, 3 start_date, 5 route_id, 6 direction_id
    Position         1 latitude, 2 longitude, 3 bearing, 5 speed
"""

from __future__ import annotations

import datetime as dt
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field

from . import geo, protobuf as pb

VEHICLE_POSITIONS = "https://data.texas.gov/download/eiei-9rpf/application/octet-stream"
TRIP_UPDATES = "https://data.texas.gov/download/rmk2-acnw/application/octet-stream"

# How close a bus has to pass to a closure for that route to count as
# affected. Generous: a vehicle is sampled every few seconds, so it may not
# have a fix exactly at the closure.
CORRIDOR_TOLERANCE_M = 120.0

_cache: dict[str, tuple[float, bytes]] = {}
CACHE_TTL_S = 60


@dataclass
class Vehicle:
    vehicle_id: str
    route_id: str
    trip_id: str
    point: geo.Point | None
    stop_id: str = ""
    timestamp: int = 0
    speed: float | None = None

    @property
    def seen_at(self) -> dt.datetime | None:
        if not self.timestamp:
            return None
        return dt.datetime.fromtimestamp(self.timestamp, dt.timezone.utc)


@dataclass
class Snapshot:
    vehicles: list[Vehicle] = field(default_factory=list)
    feed_timestamp: int = 0
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error

    @property
    def routes(self) -> set[str]:
        return {v.route_id for v in self.vehicles if v.route_id}

    @property
    def age_seconds(self) -> float:
        if not self.feed_timestamp:
            return float("inf")
        return max(0.0, time.time() - self.feed_timestamp)


def _fetch(url: str, *, use_cache: bool = True, timeout: int = 45) -> bytes:
    if use_cache:
        hit = _cache.get(url)
        if hit and time.time() - hit[0] < CACHE_TTL_S:
            return hit[1]
    with urllib.request.urlopen(url, timeout=timeout) as response:
        raw = response.read()
    _cache[url] = (time.time(), raw)
    return raw


def vehicles(*, use_cache: bool = True) -> Snapshot:
    """Every CapMetro vehicle currently reporting a position."""
    try:
        raw = _fetch(VEHICLE_POSITIONS, use_cache=use_cache)
    except (urllib.error.URLError, TimeoutError) as exc:
        return Snapshot(error=f"feed unreachable: {exc}")

    try:
        message = pb.parse(raw)
    except pb.ProtobufError as exc:
        return Snapshot(error=f"feed did not decode: {exc}")

    snapshot = Snapshot(feed_timestamp=int(pb.first(message, 1, 3) or 0))

    for entity in pb.every(message, 2):
        position_msg = pb.first(entity, 4)
        if not isinstance(position_msg, dict):
            continue

        lat = pb.first(position_msg, 2, 1)
        lon = pb.first(position_msg, 2, 2)
        point = (float(lon), float(lat)) if lat is not None and lon is not None else None

        snapshot.vehicles.append(
            Vehicle(
                vehicle_id=pb.as_text(pb.first(position_msg, 8, 1))
                or pb.as_text(pb.first(entity, 1)),
                route_id=pb.as_text(pb.first(position_msg, 1, 5)),
                trip_id=pb.as_text(pb.first(position_msg, 1, 1)),
                point=point,
                stop_id=pb.as_text(pb.first(position_msg, 7)),
                timestamp=int(pb.first(position_msg, 5) or 0),
                speed=pb.first(position_msg, 2, 5),
            )
        )
    return snapshot


@dataclass
class RouteImpact:
    route_id: str
    vehicles_nearby: int
    nearest_m: float

    def describe(self) -> str:
        return (
            f"Route {self.route_id} runs here "
            f"({self.vehicles_nearby} bus(es) tracked within "
            f"{self.nearest_m:.0f} m of the closure)"
        )


def routes_near(
    line: list[geo.Point], snapshot: Snapshot, *, tolerance_m: float = CORRIDOR_TOLERANCE_M
) -> list[RouteImpact]:
    """Which bus routes currently have vehicles on this stretch of road?

    Live positions rather than the static GTFS shapes, deliberately. A route
    that *should* run down a street may already have been diverted around the
    closure, in which case its buses are not there and it does not appear —
    which is the honest answer.
    """
    if not line or not snapshot.ok:
        return []

    box = geo.pad_bbox(geo.bbox(line), tolerance_m + 60)
    nearest: dict[str, tuple[int, float]] = {}

    for vehicle in snapshot.vehicles:
        if not vehicle.point or not vehicle.route_id:
            continue
        if not geo.bbox_overlaps(box, (vehicle.point[0], vehicle.point[1],
                                       vehicle.point[0], vehicle.point[1])):
            continue
        distance = geo.point_to_path_m(vehicle.point, line)
        if distance > tolerance_m:
            continue
        count, best = nearest.get(vehicle.route_id, (0, float("inf")))
        nearest[vehicle.route_id] = (count + 1, min(best, distance))

    impacts = [
        RouteImpact(route_id=route, vehicles_nearby=count, nearest_m=best)
        for route, (count, best) in nearest.items()
    ]
    impacts.sort(key=lambda i: (-i.vehicles_nearby, i.nearest_m))
    return impacts


def impact_note(impacts: list[RouteImpact]) -> str:
    """One line for the brief, or empty."""
    if not impacts:
        return ""
    routes = ", ".join(i.route_id for i in impacts[:4])
    more = f" and {len(impacts) - 4} more" if len(impacts) > 4 else ""
    many = len(impacts) > 1
    noun = "routes" if many else "route"
    verb = "run" if many else "runs"
    return (
        f"Bus {noun} {routes}{more} currently {verb} through this closure; "
        f"riders may find stops moved or skipped."
    )
