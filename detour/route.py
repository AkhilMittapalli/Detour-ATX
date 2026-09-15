"""Saved routes, and the spatial join that decides what is "on" one.

A route is stored once as an ordered list of waypoints and reduced to a
densified polyline. Everything downstream is a proximity test against that
polyline, which is what keeps the daily run cheap: no routing engine, no
map matching service, no per-request geocoding.

Match tolerances are deliberately generous. Not one of the 3,853 work zone
records has a verified position, so tightening these would trade a real
false-negative rate for false precision we have not earned.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path as FsPath

from . import geo

WORK_ZONE_TOLERANCE_M = 45.0
SIGNAL_TOLERANCE_M = 55.0
INCIDENT_TOLERANCE_M = 70.0
CENTERLINE_TOLERANCE_M = 25.0


@dataclass
class Route:
    name: str
    waypoints: list[geo.Point]
    path: list[geo.Point]

    @property
    def length_m(self) -> float:
        return geo.path_length_m(self.path)

    @property
    def length_mi(self) -> float:
        return self.length_m / 1609.344

    @property
    def bbox(self) -> tuple[float, float, float, float]:
        return geo.bbox(self.path)


def load_route(path: str | FsPath) -> Route:
    """Load a saved route from JSON.

    Expected shape:

        {"name": "Mueller to Downtown",
         "waypoints": [[-97.7005, 30.2985], [-97.7431, 30.2711]]}

    Waypoints are [longitude, latitude] — GeoJSON order, matching what the
    city's own feeds return, so a route can be pasted straight out of a map
    export without re-ordering the pairs.
    """
    data = json.loads(FsPath(path).read_text(encoding="utf-8"))

    waypoints = geo.as_points(data["waypoints"])
    if len(waypoints) < 2:
        raise ValueError(f"{path}: a route needs at least two waypoints")

    return Route(
        name=data.get("name") or FsPath(path).stem,
        waypoints=waypoints,
        path=geo.densify(waypoints),
    )


@dataclass
class Match:
    """One disruption that falls near the route."""

    record: dict
    distance_m: float


def _match_by_geometry(
    route: Route,
    rows: list[dict],
    geometry_key: str,
    tolerance_m: float,
) -> list[Match]:
    route_box = geo.pad_bbox(route.bbox, tolerance_m + 50)
    matches: list[Match] = []

    for row in rows:
        points = geo.coords_of(row.get(geometry_key))
        if not points:
            continue
        if not geo.bbox_overlaps(route_box, geo.bbox(points)):
            continue

        distance = geo.path_to_path_m(route.path, points, give_up_at=tolerance_m)
        if distance <= tolerance_m:
            matches.append(Match(row, distance))

    matches.sort(key=lambda m: m.distance_m)
    return matches


def match_work_zones(route: Route, zones: list[dict]) -> list[Match]:
    return _match_by_geometry(route, zones, "geometry", WORK_ZONE_TOLERANCE_M)


def match_signals(route: Route, signals: list[dict]) -> list[Match]:
    return _match_by_geometry(route, signals, "location", SIGNAL_TOLERANCE_M)


def match_incidents(route: Route, incidents: list[dict]) -> list[Match]:
    return _match_by_geometry(route, incidents, "location", INCIDENT_TOLERANCE_M)


# A route vertex every ~60 m means three consecutive close vertices is
# roughly 120 m of shared travel — enough to tell "we drive along this
# street" from "we cross it at a light".
MIN_SHARED_VERTICES = 3


def streets_on_route(route: Route, centerline: list[dict]) -> list[str]:
    """Street names the route actually runs *along*, in travel order.

    Proximity alone is the wrong test. A cross-town route passes within a
    few metres of every street it crosses, which on Austin's downtown grid
    means dozens of names that the driver never drives on. So a street has
    to stay near the route for a sustained stretch to count, measured as the
    number of densified route vertices within tolerance of it.

    Also used to sanity check a work zone's `road_names` against the street
    its geometry actually sits on — the cheapest available guard against the
    unverified-position problem.
    """
    route_box = geo.pad_bbox(route.bbox, CENTERLINE_TOLERANCE_M + 50)
    best_at: dict[str, tuple[int, int]] = {}  # name -> (first vertex, shared count)

    for row in centerline:
        name = (row.get("full_street_name") or row.get("street_name") or "").strip()
        if not name:
            continue

        points = geo.coords_of(row.get("the_geom"))
        if not points or not geo.bbox_overlaps(route_box, geo.bbox(points)):
            continue

        near = [
            i
            for i, vertex in enumerate(route.path)
            if geo.point_to_path_m(vertex, points) <= CENTERLINE_TOLERANCE_M
        ]
        if len(near) < MIN_SHARED_VERTICES:
            continue

        previous = best_at.get(name)
        shared = len(near)
        if previous is None:
            best_at[name] = (near[0], shared)
        else:
            best_at[name] = (min(previous[0], near[0]), previous[1] + shared)

    return [name for name, _ in sorted(best_at.items(), key=lambda kv: kv[1][0])]
