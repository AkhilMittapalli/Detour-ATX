"""Deriving the detour the city never published.

Austin publishes closures and no detours: `event_type` is `work-zone` on all
3,853 records, though the WZDx standard it publishes to also defines
`detour`. The official diversion exists on an orange sign bolted to a
barricade and was never digitised.

We cannot invent that signed route and should not pretend to. What we can
compute is the routing *consequence* — subtract the closed edges from the
graph, re-route, and report the delta. That is what a commuter actually
wants before deciding whether to leave early, and it keeps us out of a
turn-by-turn fight with Google that we would lose.

Two guards matter here, because not one work zone record in the feed carries
a verified position:

* **Name agreement.** If a closure says COLORADO ST and the only edges near
  its geometry are LAVACA ST, the geometry is suspect and we say so rather
  than silently deleting the wrong street.
* **Plausibility.** A detour that balloons beyond all proportion to the trip
  is far more likely to be bad geometry than a real diversion, so it is
  reported as unusable rather than shown as advice.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from . import geo, graph as graph_mod

EDGE_MATCH_TOLERANCE_M = 28.0

# A detour is rejected as implausible past either bound.
#
# The ratio test only applies above RATIO_FLOOR_M. Below it the ratio is
# meaningless: routing around a single closed 104 m block genuinely costs
# 542 m — a 5x ratio that is entirely correct, and an earlier version of
# this guard rejected exactly that case as a bad closure.
MAX_PLAUSIBLE_RATIO = 4.0
RATIO_FLOOR_M = 800.0
MAX_PLAUSIBLE_EXTRA_M = 8000.0

# Map-matching a legal route should stay close to the drawn polyline.
# Well past this, the waypoints are almost certainly running the wrong way
# down a one-way street.
BASELINE_INFLATION_LIMIT = 1.6


@dataclass
class Blockage:
    """The result of subtracting one closure from the graph."""

    closure_name: str
    road_names: str
    edge_indices: list[int] = field(default_factory=list)
    name_matched: bool = True
    note: str = ""

    @property
    def applied(self) -> bool:
        return bool(self.edge_indices)


def _normalise(name: str) -> set[str]:
    """Comparable tokens from a street name, minus type suffixes.

    "W OLTORF ST" and "OLTORF STREET" should agree; "COLORADO ST" and
    "LAVACA ST" should not.
    """
    drop = {
        "ST", "STREET", "RD", "ROAD", "DR", "DRIVE", "AVE", "AVENUE", "BLVD",
        "BOULEVARD", "LN", "LANE", "PL", "PLACE", "CT", "COURT", "HWY",
        "HIGHWAY", "PKWY", "PARKWAY", "TRL", "TRAIL", "CV", "COVE", "SVRD",
        "N", "S", "E", "W", "NB", "SB", "EB", "WB",
    }
    tokens = {t for t in name.upper().replace("/", " ").split() if t}
    return {t for t in tokens if t not in drop} or tokens


def block_closure(graph: graph_mod.Graph, closure: dict) -> Blockage:
    """Mark the edges a full closure removes from the network.

    Only `all-lanes-closed` subtracts anything. A lane restriction slows
    traffic but does not change the topology, and treating it as a removal
    would invent diversions nobody needs to take.
    """
    road_names = (closure.get("road_names") or "").strip()
    label = (closure.get("name") or road_names or "closure").strip()

    impact = (closure.get("vehicle_impact") or "").strip().lower()
    if impact != "all-lanes-closed":
        return Blockage(label, road_names, note="not a full closure")

    points = geo.coords_of(closure.get("geometry"))
    if len(points) < 2:
        return Blockage(label, road_names, note="closure has no usable geometry")

    closure_box = geo.pad_bbox(geo.bbox(points), EDGE_MATCH_TOLERANCE_M + 40)

    near: list[int] = []
    for index, edge in enumerate(graph.edges):
        if not geo.bbox_overlaps(closure_box, geo.bbox(edge.points)):
            continue
        distance = geo.path_to_path_m(
            points, edge.points, give_up_at=EDGE_MATCH_TOLERANCE_M
        )
        if distance <= EDGE_MATCH_TOLERANCE_M:
            near.append(index)

    if not near:
        return Blockage(label, road_names, note="no graph edges near this closure")

    # Prefer edges whose street name agrees with the permit.
    matched = near
    name_matched = True
    if road_names:
        wanted = _normalise(road_names)
        agreeing = [i for i in near if _normalise(graph.edges[i].name) & wanted]
        if agreeing:
            matched = agreeing
        else:
            name_matched = False

    if not name_matched:
        nearby = sorted({graph.edges[i].name for i in near if graph.edges[i].name})[:3]
        return Blockage(
            label,
            road_names,
            note=(
                f"geometry sits on {', '.join(nearby) or 'unnamed roads'}, "
                f"not {road_names} — not applied"
            ),
            name_matched=False,
        )

    for index in matched:
        graph.edges[index].blocked = True

    return Blockage(label, road_names, edge_indices=matched)


@dataclass
class Delta:
    """The routing consequence of the closures that were applied."""

    baseline_m: float
    baseline_s: float
    baseline_turns: int
    detour_m: float | None
    detour_s: float | None
    detour_turns: int | None
    via: list[str] = field(default_factory=list)
    blockages: list[Blockage] = field(default_factory=list)
    unreachable: bool = False
    implausible: bool = False
    baseline_suspect: bool = False

    @property
    def extra_m(self) -> float:
        return (self.detour_m - self.baseline_m) if self.detour_m is not None else 0.0

    @property
    def extra_s(self) -> float:
        return (self.detour_s - self.baseline_s) if self.detour_s is not None else 0.0

    @property
    def extra_turns(self) -> int:
        if self.detour_turns is None:
            return 0
        return max(0, self.detour_turns - self.baseline_turns)

    @property
    def affected(self) -> bool:
        """Did any applied closure actually change the route?"""
        if self.baseline_suspect:
            return False
        return self.unreachable or self.extra_m > 5.0

    def summary(self) -> str:
        if self.baseline_suspect:
            return (
                "Saved route does not map-match cleanly — it may run against a "
                "one-way street. Detour not computed."
            )
        if self.unreachable:
            return "No way through — every route around this closure is also cut."
        if self.implausible:
            return (
                "Detour looks implausible, which usually means the closure geometry "
                "is wrong. Reported but not used."
            )
        if not self.affected:
            return "No change to your route."

        miles = self.extra_m / 1609.344
        minutes = self.extra_s / 60

        parts = [f"+{miles:.1f} mi" if miles >= 0.05 else "about the same distance"]
        # Extra distance on faster streets can cost no measurable time, which
        # is a real and useful answer — just not one to print as "+0 min".
        parts.append(f"+{minutes:.0f} min" if minutes >= 0.5 else "no extra time")
        if self.extra_turns:
            parts.append(f"+{self.extra_turns} turn{'s' if self.extra_turns > 1 else ''}")
        return ", ".join(parts)


def delta(
    graph: graph_mod.Graph,
    waypoints: list[geo.Point],
    closures: list[dict],
) -> Delta | None:
    """Compare the user's own route against the best way around its closures.

    The baseline is the saved route map-matched onto the graph, *not* a
    fresh origin-to-destination optimisation — see `graph.follow_route` for
    why that distinction decides whether this works at all.
    """
    if len(waypoints) < 2:
        return None

    start = graph.nearest_node(waypoints[0])
    goal = graph.nearest_node(waypoints[-1])
    if start is None or goal is None:
        return None

    for edge in graph.edges:
        edge.blocked = False

    baseline = graph_mod.follow_route(graph, waypoints)
    if baseline is None:
        return None
    base_s, base_m, base_path = baseline

    # If map-matching inflates the drawn route badly, the saved waypoints
    # probably run against a one-way street: each leg then loops around the
    # block, the baseline balloons, and every subsequent comparison is
    # meaningless. Trinity St downtown is one_way=FT and catches this
    # exactly.
    drawn_m = geo.path_length_m(waypoints)
    baseline_suspect = drawn_m > 0 and base_m / drawn_m > BASELINE_INFLATION_LIMIT

    blockages = [block_closure(graph, closure) for closure in closures]
    if not any(b.applied for b in blockages):
        return Delta(
            baseline_m=base_m,
            baseline_s=base_s,
            baseline_turns=graph_mod.count_turns(graph, base_path),
            detour_m=base_m,
            detour_s=base_s,
            detour_turns=graph_mod.count_turns(graph, base_path),
            via=graph_mod.street_sequence(graph, base_path),
            blockages=blockages,
            baseline_suspect=baseline_suspect,
        )

    # If the baseline never used a blocked edge, the closure is beside the
    # route rather than on it and nothing needs to change.
    blocked = {i for b in blockages for i in b.edge_indices}
    if not blocked & set(base_path):
        return Delta(
            baseline_m=base_m,
            baseline_s=base_s,
            baseline_turns=graph_mod.count_turns(graph, base_path),
            detour_m=base_m,
            detour_s=base_s,
            detour_turns=graph_mod.count_turns(graph, base_path),
            via=graph_mod.street_sequence(graph, base_path),
            blockages=blockages,
            baseline_suspect=baseline_suspect,
        )

    diverted = graph_mod.shortest_path(graph, start, goal, avoid_blocked=True)
    base_turns = graph_mod.count_turns(graph, base_path)

    if diverted is None:
        return Delta(
            baseline_m=base_m,
            baseline_s=base_s,
            baseline_turns=base_turns,
            detour_m=None,
            detour_s=None,
            detour_turns=None,
            blockages=blockages,
            unreachable=True,
            baseline_suspect=baseline_suspect,
        )

    new_s, new_m, new_path = diverted
    extra = new_m - base_m
    implausible = extra > MAX_PLAUSIBLE_EXTRA_M or (
        base_m > RATIO_FLOOR_M and new_m / base_m > MAX_PLAUSIBLE_RATIO
    )

    return Delta(
        baseline_m=base_m,
        baseline_s=base_s,
        baseline_turns=base_turns,
        detour_m=new_m,
        detour_s=new_s,
        detour_turns=graph_mod.count_turns(graph, new_path),
        via=graph_mod.street_sequence(graph, new_path),
        blockages=blockages,
        implausible=implausible,
        baseline_suspect=baseline_suspect,
    )
