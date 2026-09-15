"""A routable street graph built from the city's centreline layer.

Austin publishes 68,717 centreline segments with the three fields routing
actually needs — `one_way`, `speed_limit` and `road_class` — and the
segments node cleanly: in a downtown sample, 91% of endpoints are shared by
more than one segment, with three- and four-way intersections dominating.
That is a real street network, not a pile of disconnected lines.

Nodes are rounded coordinates. Rounding to six decimal places is about
0.1 m at this latitude, which is far tighter than any positional error in
the data and still coarse enough that segments meeting at an intersection
land on the same node.
"""

from __future__ import annotations

import heapq
import math
from dataclasses import dataclass, field

from . import geo

NODE_PRECISION = 6
DEFAULT_SPEED_MPH = 30.0
MPH_TO_MS = 0.44704

# road_class -> assumed mph, for the segments with no speed_limit set.
# Lower numbers are bigger roads in this layer.
ROAD_CLASS_SPEED = {
    "1": 60.0, "2": 55.0, "4": 45.0, "5": 40.0,
    "6": 30.0, "8": 30.0, "10": 25.0, "15": 20.0, "16": 20.0,
}

Node = tuple[float, float]


def node_of(point: geo.Point) -> Node:
    return (round(point[0], NODE_PRECISION), round(point[1], NODE_PRECISION))


@dataclass
class Edge:
    segment_id: str
    name: str
    tail: Node
    head: Node
    length_m: float
    seconds: float
    points: list[geo.Point]
    blocked: bool = False


@dataclass
class Graph:
    edges: list[Edge] = field(default_factory=list)
    out: dict[Node, list[int]] = field(default_factory=dict)
    nodes: list[Node] = field(default_factory=list)

    def add(self, edge: Edge) -> None:
        index = len(self.edges)
        self.edges.append(edge)
        self.out.setdefault(edge.tail, []).append(index)
        self.out.setdefault(edge.head, [])

    def finalise(self) -> None:
        self.nodes = list(self.out.keys())

    def nearest_node(self, point: geo.Point) -> Node | None:
        """Closest graph node to an arbitrary coordinate."""
        best: Node | None = None
        best_d = math.inf
        for node in self.nodes:
            d = geo.haversine_m(point, node)
            if d < best_d:
                best, best_d = node, d
        return best


def _speed_ms(row: dict) -> float:
    raw = (row.get("speed_limit") or "").strip()
    try:
        mph = float(raw)
    except ValueError:
        mph = 0.0
    if mph <= 0:
        mph = ROAD_CLASS_SPEED.get((row.get("road_class") or "").strip(), DEFAULT_SPEED_MPH)
    return max(5.0, mph) * MPH_TO_MS


def _directions(row: dict) -> tuple[bool, bool]:
    """(forward, backward) travel permitted along the digitised direction.

    Values are B (both), FT (from-to) and TF (to-from). One row in the layer
    carries a lowercase 'b' and two carry nothing at all, so this normalises
    rather than trusting the field.
    """
    value = (row.get("one_way") or "B").strip().upper()
    if value == "FT":
        return True, False
    if value == "TF":
        return False, True
    return True, True


def build(centerline: list[dict]) -> Graph:
    """Turn centreline rows into a directed graph."""
    graph = Graph()

    for row in centerline:
        points = geo.coords_of(row.get("the_geom"))
        if len(points) < 2:
            continue

        length = geo.path_length_m(points)
        if length <= 0:
            continue

        seconds = length / _speed_ms(row)
        name = (row.get("full_street_name") or row.get("street_name") or "").strip().upper()
        segment_id = str(row.get("segment_id") or row.get("objectid") or "")

        tail, head = node_of(points[0]), node_of(points[-1])
        if tail == head:
            continue

        forward, backward = _directions(row)
        if forward:
            graph.add(Edge(segment_id, name, tail, head, length, seconds, points))
        if backward:
            graph.add(
                Edge(segment_id, name, head, tail, length, seconds, list(reversed(points)))
            )

    graph.finalise()
    return graph


def shortest_path(
    graph: Graph, start: Node, goal: Node, *, avoid_blocked: bool = True
) -> tuple[float, float, list[int]] | None:
    """Dijkstra by travel time.

    Returns (seconds, metres, edge indices), or None when the goal is
    unreachable — which, once closures are subtracted, is itself a result
    worth reporting.
    """
    if start not in graph.out or goal not in graph.out:
        return None

    best: dict[Node, float] = {start: 0.0}
    came: dict[Node, tuple[Node, int]] = {}
    queue: list[tuple[float, Node]] = [(0.0, start)]
    seen: set[Node] = set()

    while queue:
        cost, node = heapq.heappop(queue)
        if node in seen:
            continue
        seen.add(node)

        if node == goal:
            break

        for index in graph.out.get(node, ()):
            edge = graph.edges[index]
            if avoid_blocked and edge.blocked:
                continue
            candidate = cost + edge.seconds
            if candidate < best.get(edge.head, math.inf):
                best[edge.head] = candidate
                came[edge.head] = (node, index)
                heapq.heappush(queue, (candidate, edge.head))

    if goal not in best:
        return None

    indices: list[int] = []
    metres = 0.0
    cursor = goal
    while cursor != start:
        previous, index = came[cursor]
        indices.append(index)
        metres += graph.edges[index].length_m
        cursor = previous
    indices.reverse()

    return best[goal], metres, indices


def follow_route(
    graph: Graph, waypoints: list[geo.Point]
) -> tuple[float, float, list[int]] | None:
    """Map-match a saved route onto the graph, leg by leg.

    This exists because the obvious approach is wrong. Routing the user's
    origin to their destination by travel time returns the *globally
    fastest* path, which downtown is almost always the interstate — so a
    closure on the surface street the user actually drives never appears in
    the baseline, and the detour delta silently reports no impact.

    Chaining shortest paths between consecutive waypoints keeps the baseline
    on the user's own roads. With waypoints roughly every 100 m, each leg is
    a single block and the reconstruction is faithful.
    """
    if len(waypoints) < 2:
        return None

    nodes: list[Node] = []
    for point in waypoints:
        node = graph.nearest_node(point)
        if node is not None and (not nodes or node != nodes[-1]):
            nodes.append(node)

    if len(nodes) < 2:
        return None

    total_s = total_m = 0.0
    indices: list[int] = []
    for tail, head in zip(nodes, nodes[1:]):
        leg = shortest_path(graph, tail, head, avoid_blocked=False)
        if leg is None:
            continue
        seconds, metres, path = leg
        total_s += seconds
        total_m += metres
        indices.extend(path)

    if not indices:
        return None
    return total_s, total_m, indices


def street_sequence(graph: Graph, indices: list[int]) -> list[str]:
    """Street names along a path, collapsing consecutive repeats."""
    names: list[str] = []
    for index in indices:
        name = graph.edges[index].name
        if name and (not names or names[-1] != name):
            names.append(name)
    return names


def count_turns(graph: Graph, indices: list[int]) -> int:
    """Changes of street name along a path — a proxy for turns.

    Cheaper and more robust than computing bearings, and it is the number a
    driver actually feels: how many times they have to do something.
    """
    return max(0, len(street_sequence(graph, indices)) - 1)
