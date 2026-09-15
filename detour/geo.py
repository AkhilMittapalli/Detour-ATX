"""Pure-stdlib geometry for route/disruption proximity.

Everything here works in WGS84 degrees and returns metres. We deliberately
avoid shapely: the only operations this project needs are point-to-polyline
distance and polyline-to-polyline proximity, both of which are short enough
to own outright and keep the project install-free.
"""

from __future__ import annotations

import math
from typing import Iterable, Sequence

Point = tuple[float, float]  # (lon, lat) — GeoJSON order, not lat/lon
Path = Sequence[Point]

EARTH_RADIUS_M = 6_371_008.8


def haversine_m(a: Point, b: Point) -> float:
    """Great-circle distance between two (lon, lat) points, in metres."""
    lon1, lat1 = a
    lon2, lat2 = b
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(h))


def _local_xy(p: Point, lat0: float) -> tuple[float, float]:
    """Project to a local equirectangular plane in metres.

    Accurate to well under a metre over the few kilometres we ever measure,
    which is far below the positional error already present in the city's
    own work zone geometry.
    """
    lon, lat = p
    x = math.radians(lon) * EARTH_RADIUS_M * math.cos(math.radians(lat0))
    y = math.radians(lat) * EARTH_RADIUS_M
    return x, y


def point_to_segment_m(p: Point, a: Point, b: Point) -> float:
    """Shortest distance from point `p` to the line segment `a`-`b`."""
    lat0 = (a[1] + b[1] + p[1]) / 3
    px, py = _local_xy(p, lat0)
    ax, ay = _local_xy(a, lat0)
    bx, by = _local_xy(b, lat0)

    dx, dy = bx - ax, by - ay
    if dx == 0 and dy == 0:
        return math.hypot(px - ax, py - ay)

    t = ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)
    t = max(0.0, min(1.0, t))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def point_to_path_m(p: Point, path: Path) -> float:
    """Shortest distance from a point to a polyline."""
    if not path:
        return math.inf
    if len(path) == 1:
        return haversine_m(p, path[0])
    return min(
        point_to_segment_m(p, path[i], path[i + 1]) for i in range(len(path) - 1)
    )


def path_to_path_m(a: Path, b: Path, *, give_up_at: float | None = None) -> float:
    """Approximate minimum distance between two polylines.

    Samples vertices of each against the other, which is exact when the
    closest approach happens at a vertex and slightly over-estimates when it
    happens mid-segment on both. That error is bounded by segment length and
    is immaterial at the tolerances this project uses.

    `give_up_at` lets callers bail out early once a pair is obviously close
    enough to count as a match.
    """
    if not a or not b:
        return math.inf

    best = math.inf
    for p in a:
        best = min(best, point_to_path_m(p, b))
        if give_up_at is not None and best <= give_up_at:
            return best
    for p in b:
        best = min(best, point_to_path_m(p, a))
        if give_up_at is not None and best <= give_up_at:
            return best
    return best


def bbox(path: Path) -> tuple[float, float, float, float]:
    """(min_lon, min_lat, max_lon, max_lat) for a polyline."""
    lons = [p[0] for p in path]
    lats = [p[1] for p in path]
    return min(lons), min(lats), max(lons), max(lats)


def pad_bbox(
    box: tuple[float, float, float, float], metres: float
) -> tuple[float, float, float, float]:
    """Grow a bbox by roughly `metres` on every side."""
    min_lon, min_lat, max_lon, max_lat = box
    dlat = metres / 111_320.0
    mid_lat = (min_lat + max_lat) / 2
    dlon = metres / (111_320.0 * max(0.1, math.cos(math.radians(mid_lat))))
    return min_lon - dlon, min_lat - dlat, max_lon + dlon, max_lat + dlat


def bbox_overlaps(
    a: tuple[float, float, float, float], b: tuple[float, float, float, float]
) -> bool:
    """Cheap prefilter so we only run real distance maths on plausible pairs."""
    return not (a[2] < b[0] or b[2] < a[0] or a[3] < b[1] or b[3] < a[1])


def coords_of(geometry: dict | None) -> list[Point]:
    """Flatten a GeoJSON geometry into a list of (lon, lat) vertices.

    Handles the three shapes Austin's portal actually serves: Point for
    signals and incidents, LineString for work zones, MultiLineString for
    street centreline segments.
    """
    if not geometry:
        return []

    kind = geometry.get("type")
    raw = geometry.get("coordinates")
    if raw is None:
        return []

    if kind == "Point":
        return [(float(raw[0]), float(raw[1]))]
    if kind == "LineString":
        return [(float(x), float(y)) for x, y in raw]
    if kind == "MultiLineString":
        out: list[Point] = []
        for line in raw:
            out.extend((float(x), float(y)) for x, y in line)
        return out
    if kind == "Polygon":
        return [(float(x), float(y)) for x, y in raw[0]]
    return []


def densify(path: Path, max_gap_m: float = 60.0) -> list[Point]:
    """Insert intermediate vertices so long straight legs sample evenly.

    A saved route is often just a handful of waypoints. Without this, a 2 km
    leg has only two vertices and `path_to_path_m` can miss a work zone
    sitting halfway along it.
    """
    if len(path) < 2:
        return list(path)

    out: list[Point] = [path[0]]
    for a, b in zip(path, path[1:]):
        span = haversine_m(a, b)
        steps = max(1, math.ceil(span / max_gap_m))
        for i in range(1, steps + 1):
            t = i / steps
            out.append((a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t))
    return out


def path_length_m(path: Path) -> float:
    return sum(haversine_m(a, b) for a, b in zip(path, path[1:]))


def as_points(pairs: Iterable[Sequence[float]]) -> list[Point]:
    """Coerce [[lon, lat], ...] from JSON into typed points."""
    return [(float(p[0]), float(p[1])) for p in pairs]
