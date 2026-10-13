"""Handing off to the app people actually drive with.

There is no way to put anything inside Google Maps, and there never will
be — no API overlays third-party data onto the consumer app. But the two
products answer different questions at different moments. Maps answers
"how do I get there now", from the driver's seat. This answers "what
should I know before I go", from the kitchen. They never have to compete
for the same tap.

So this decides *whether* and *which way*; Maps does turn-by-turn.

That matters most here rather than in the browser app. The morning brief
is the surface this product actually wins on — nobody opens a second map
while driving, but they do read one message at 07:10 — and a brief that
ends in a tappable route is the difference between information and a
decision someone can act on.

The Maps URLs scheme is universal, needs no key and costs nothing.
Origin and destination go in as text, because a person may later read,
share or save the link. Waypoints go in as coordinates, because nobody
reads those and a street name like "S 1ST ST" geocodes anywhere along
three miles of it.
"""

from __future__ import annotations

import re
import urllib.parse
from dataclasses import dataclass

from . import geo

MAPS_DIR = "https://www.google.com/maps/dir/"

# Few on purpose. Each waypoint is a constraint, and over-constraining
# makes Maps draw a rigid zigzag through points that were only ever
# samples of a line. Three is enough to force the shape of a diversion.
MAX_WAYPOINTS = 3


@dataclass
class Handoff:
    url: str
    waypoints: int
    why: str
    caveat: str


def sample_waypoints(
    line: list[geo.Point], count: int = MAX_WAYPOINTS
) -> list[geo.Point]:
    """Evenly spaced along the path by distance, endpoints excluded.

    The endpoints are already the origin and destination, and repeating
    them as waypoints makes Maps insert a stop at the kerb you started
    from.
    """
    if not line or len(line) < 3:
        return []
    total = geo.path_length_m(line)
    if total <= 0:
        return []

    wanted = [total * i / (count + 1) for i in range(1, count + 1)]

    out: list[geo.Point] = []
    walked = 0.0
    nxt = 0
    for i in range(1, len(line)):
        if nxt >= len(wanted):
            break
        walked += geo.haversine_m(line[i - 1], line[i])
        while nxt < len(wanted) and walked >= wanted[nxt]:
            out.append(line[i])
            nxt += 1
    return out


# A bare "lat,lng" pair. Maps parses that as a point, and only as long as
# nothing else is bolted onto it — appending the city turns a valid
# coordinate into a string that geocodes to nowhere.
COORD_PAIR = re.compile(r"^-?\d+(\.\d+)?\s*,\s*-?\d+(\.\d+)?$")


def _place(text: str | None) -> str:
    value = (text or "").strip()
    if not value or COORD_PAIR.match(value):
        return value
    return value if "austin" in value.lower() else f"{value}, Austin, TX"


def _fixed(point: geo.Point) -> str:
    return f"{point[1]:.6f},{point[0]:.6f}"


def build(
    origin: str,
    destination: str,
    *,
    line: list[geo.Point] | None = None,
    mode: str = "drive",
    why: str = "",
) -> Handoff:
    """A Google Maps directions link, optionally shaped by a route line."""
    riding = mode == "bike"

    params = {
        "api": "1",
        "origin": _place(origin),
        "destination": _place(destination),
        "travelmode": "bicycling" if riding else "driving",
    }

    points = sample_waypoints(line) if line else []
    if points:
        params["waypoints"] = "|".join(_fixed(p) for p in points)

    query = urllib.parse.urlencode(params, safe="|,")
    return Handoff(
        url=f"{MAPS_DIR}?{query}",
        waypoints=len(points),
        why=why,
        # Maps may well route around these if it disagrees. That is fine,
        # and it is what this project already says about every derived
        # detour, so it is stated rather than hidden.
        caveat=(
            "Maps may reroute around these if it disagrees — follow posted "
            "signs where they differ."
            if points
            else ""
        ),
    )


def for_brief(result, *, mode: str = "drive") -> Handoff | None:
    """The handoff for an assembled brief, or None when there is nothing
    worth handing over.

    Returning None matters. A button that does nothing the reader could
    not already do from their home screen is noise, and this brief has
    spent a lot of effort not being noise.
    """
    route = getattr(result, "route", None)
    if route is None:
        return None

    # Read defensively rather than by attribute. A brief that fails to
    # render because a convenience link could not be built is the wrong
    # trade — the same rule the transit layer already follows, where a
    # dead feed is not a reason for someone not to hear about a closed
    # street. The link is the least important thing on the page.
    path = list(getattr(route, "path", None) or [])
    origin = str(getattr(route, "origin", "") or "").strip()
    destination = str(getattr(route, "destination", "") or "").strip()

    # Older saved routes predate the address fields. Coordinates are an
    # uglier link but a working one, which beats no link at all.
    if len(path) >= 2:
        origin = origin or _fixed(path[0])
        destination = destination or _fixed(path[-1])
    if not origin or not destination:
        return None

    delta = getattr(result, "delta", None)
    line = None
    why = ""

    if (
        delta is not None
        and getattr(delta, "affected", False)
        and not getattr(delta, "implausible", False)
    ):
        line = list(getattr(delta, "detour_line", None) or [])
        why = "with the derived detour as waypoints"

    if not line and mode == "bike" and len(path) > 2:
        # Maps has a cycling mode and it does not know which lanes are
        # shut or which roads are hostile. Our route is weighted for
        # comfort and excludes freeways outright, so it is worth carrying
        # over even when nothing is closed.
        line = path
        why = "following the low-stress route"

    return build(origin, destination, line=line, mode=mode, why=why)
