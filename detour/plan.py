"""Turn two addresses into a saved route.

Until now a route was hand-traced out of the centreline layer, which made the
whole thing unusable by anyone who was not willing to write SoQL. This closes
that gap: two addresses in, a real routed path out, saved for the daily run.

Routing the trip optimally is correct *here*, unlike in `reroute.delta`. There
we reconstruct a route the user already drives, so we follow their waypoints.
Here we are choosing the route on their behalf, and the fastest path is the
one they would take.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path as FsPath

from . import geo, geocode, graph as graph_mod, sources

ROUTES_DIR = FsPath(__file__).resolve().parent.parent / "routes"

# Padding around the origin-destination box, so the router can leave the
# straight line between them when the road network requires it.
CORRIDOR_PAD_M = 2200.0

# Vertices are thinned to roughly this spacing. Matching the densify step in
# geo keeps the "runs along" test working on the saved path.
WAYPOINT_SPACING_M = 110.0


class PlanError(RuntimeError):
    pass


def slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")
    return slug or "route"


@dataclass
class Plan:
    name: str
    origin: geocode.Place
    destination: geocode.Place
    waypoints: list[geo.Point] = field(default_factory=list)
    streets: list[str] = field(default_factory=list)
    minutes: float = 0.0
    metres: float = 0.0

    @property
    def miles(self) -> float:
        return self.metres / 1609.344

    def to_json(self) -> dict:
        return {
            "name": self.name,
            "note": (
                f"Routed from {self.origin.address} to {self.destination.address} "
                f"using the city's Street Centerline layer. "
                f"{self.miles:.1f} mi, about {self.minutes:.0f} min."
            ),
            "origin": self.origin.address,
            "destination": self.destination.address,
            "via": self.streets[:12],
            "waypoints": [[round(x, 6), round(y, 6)] for x, y in self.waypoints],
        }


def _thin(points: list[geo.Point], spacing_m: float) -> list[geo.Point]:
    if len(points) < 3:
        return list(points)
    kept = [points[0]]
    for point in points[1:-1]:
        if geo.haversine_m(kept[-1], point) >= spacing_m:
            kept.append(point)
    kept.append(points[-1])
    return kept


def build(
    name: str,
    origin_address: str,
    destination_address: str,
    *,
    use_cache: bool = True,
) -> Plan:
    """Geocode both ends, route between them, and return a saved-route plan."""
    origin = geocode.resolve(origin_address)
    destination = geocode.resolve(destination_address)

    if geo.haversine_m(origin.point, destination.point) < 200:
        raise PlanError("origin and destination are the same place")

    box = geo.pad_bbox(geo.bbox([origin.point, destination.point]), CORRIDOR_PAD_M)
    centerline = sources.fetch_centerline_near(box, use_cache=use_cache)
    if not centerline:
        raise PlanError(
            "no street data for that corridor — both addresses must be in Austin"
        )

    street_graph = graph_mod.build(centerline)
    start = street_graph.nearest_node(origin.point)
    goal = street_graph.nearest_node(destination.point)
    if start is None or goal is None:
        raise PlanError("could not snap those addresses to the street network")

    found = graph_mod.shortest_path(street_graph, start, goal, avoid_blocked=False)
    if found is None:
        raise PlanError(
            "no route found between those addresses. If they are far apart, the "
            "corridor window may be too narrow to contain a legal path."
        )

    seconds, metres, indices = found

    points: list[geo.Point] = []
    for index in indices:
        for point in street_graph.edges[index].points:
            if not points or geo.haversine_m(points[-1], point) > 1.0:
                points.append(point)

    return Plan(
        name=name,
        origin=origin,
        destination=destination,
        waypoints=_thin(points, WAYPOINT_SPACING_M),
        streets=graph_mod.street_sequence(street_graph, indices),
        minutes=seconds / 60,
        metres=metres,
    )


def save(plan: Plan, *, directory: FsPath | None = None) -> FsPath:
    target = (directory or ROUTES_DIR) / f"{slugify(plan.name)}.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(plan.to_json(), indent=2) + "\n", encoding="utf-8")
    return target


def saved_routes(directory: FsPath | None = None) -> list[FsPath]:
    """Every saved route, for the daily run."""
    folder = directory or ROUTES_DIR
    if not folder.exists():
        return []
    return sorted(p for p in folder.glob("*.json") if p.is_file())
