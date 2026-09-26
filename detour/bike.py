"""Cyclists are the readers this feed serves worst.

The measurement that justifies this module, run against the live feeds:

* 1,819 of 4,056 work zones sit within 20 m of *dedicated* bike
  infrastructure — a lane, a trail, a bikeway, not merely a street that
  happens to carry a comfort rating.
* 614 of those are on protected or high-comfort infrastructure: the case
  where a closure pushes someone out of separated space and into moving
  traffic.
* **87 of the 1,819 mention bikes anywhere in the description. 4.8%.**

That last number is the product. The city closes a protected bike lane and
says nothing about it nineteen times out of twenty.

There is a second, quieter failure. `vehicle_impact` is written from a car:
1,593 of those 1,819 zones are tagged `some-lanes-closed`, which for a
driver means losing a lane and waiting. If the closed lane *is* the bike
lane, the rider has lost 100% of their lanes. The feed's own severity field
systematically understates what happened to them, so we re-tier it here
rather than inheriting the mistake.

`bike_level_of_comfort` is undocumented in the portal — no column
descriptions are published — so the codes below were decoded by
cross-tabulating them against `bicycle_facility` across all 17,753 rows.
The mapping is an inference from that correlation, stated as such, and the
four codes it does not explain are left unranked rather than guessed at.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from . import geo
from .severity import Tier

# --------------------------------------------------------------------------
# Decoding bike_level_of_comfort
# --------------------------------------------------------------------------

# Evidence for each ranked code, from the cross-tab against bicycle_facility:
#
#   H   819 rows, 453 protected one-way + 224 protected two-way + 48 buffered
#   HP  1,166 rows, 1,066 paved trail             -> high, paved
#   HU  439 rows, 426 unpaved trail               -> high, unpaved
#   M   6,421 rows, bike lanes and wide curb lanes: painted, not separated
#   L   3,506 rows, shoulders and unrated street
#
# EL (436), SS (844), RT (99) and TC (74) do not separate cleanly against
# facility type, so they get no rank. 3,949 rows carry no code at all.
# Together that is 30% of the layer we decline to rate — the honest outcome,
# not a gap to paper over.
COMFORT_RANK: dict[str, int] = {
    "H": 3,
    "HP": 3,
    "HU": 2,
    "M": 2,
    "L": 1,
}

UNRANKED_CODES = frozenset({"EL", "SS", "RT", "TC"})

DEDICATED = frozenset({
    "Bike Lane",
    "Bike Lane - Buffered",
    "Bike Lane - Protected One-Way",
    "Bike Lane - Protected Two-Way",
    "Bike Lane - wParking",
    "Bike Lane - Climbing",
    "Trail - Paved",
    "Trail - Unpaved",
    "Neighborhood Bikeway",
    "Sharrows",
    "Shared Lane",
})

# Infrastructure that puts something physical, or a whole quiet street,
# between the rider and traffic. Losing one of these is a categorically
# different event from losing a painted stripe, because there is nowhere
# comfortable to fall back to.
SEPARATED = frozenset({
    "Bike Lane - Protected One-Way",
    "Bike Lane - Protected Two-Way",
    "Bike Lane - Buffered",
    "Trail - Paved",
    "Neighborhood Bikeway",
})

# Controlled-access highway classes in the centreline layer: 1 is the
# interstates and tollways (IH 35, SH 130), 2 the divided highways (SH 71,
# US 290), 10 the freeway ramps. Bicycles are prohibited on all of them in
# Texas, so no amount of nearby infrastructure makes them routable.
#
# This is not a theoretical guard. Routing downtown to south Austin, the
# I-35 mainlane matched a "Shared Lane" at 0.0 m and a paved trail at
# 11.9 m, because the frontage roads and the shared-use path run within
# metres of the mainlane centreline. Without this the router can be handed
# a freeway and told it is comfortable.
BIKES_PROHIBITED_CLASSES = frozenset({"1", "2", "10"})


def bikes_prohibited(road_class: str | None) -> bool:
    return (road_class or "").strip() in BIKES_PROHIBITED_CLASSES


CYCLIST = re.compile(
    r"(bike\s*lane|bicycle\s*lane|bikeway|sharrow|shared\s*lane|"
    r"(bike|bicycle|cycl\w*)[^.]{0,30}(clos|detour|reroute|restrict)|"
    r"(clos|detour|reroute)[^.]{0,30}(bike|bicycle)|"
    r"\bshoulder[^.]{0,25}clos)",
    re.I,
)


def comfort_rank(code: str | None) -> int | None:
    """3 comfortable, 2 tolerable, 1 stressful, None unrated.

    None is not a score of zero. Nearly a third of the layer is unrated, and
    treating that as "bad" would invent a hazard the city never reported.
    """
    if not code:
        return None
    return COMFORT_RANK.get(code.strip().upper())


def is_dedicated(facility: str | None) -> bool:
    return (facility or "").strip() in DEDICATED


def is_separated(facility: str | None) -> bool:
    return (facility or "").strip() in SEPARATED


# The layer's own labels are database values, not English. "Bike Lane -
# Protected One-Way" is precise and reads terribly in the middle of a
# sentence, so every facility gets a phrase a person would actually say.
FACILITY_LABEL = {
    "Bike Lane": "bike lane",
    "Bike Lane - Buffered": "buffered bike lane",
    "Bike Lane - Protected One-Way": "protected bike lane",
    "Bike Lane - Protected Two-Way": "two-way protected bike lane",
    "Bike Lane - wParking": "bike lane beside parking",
    "Bike Lane - Climbing": "uphill bike lane",
    "Trail - Paved": "paved trail",
    "Trail - Unpaved": "unpaved trail",
    "Neighborhood Bikeway": "neighbourhood bikeway",
    "Sharrows": "shared-lane markings",
    "Shared Lane": "shared lane",
}


def facility_label(facility: str | None) -> str:
    name = (facility or "").strip()
    return FACILITY_LABEL.get(name) or name.lower() or "bike route"


# --------------------------------------------------------------------------
# The facility index
# --------------------------------------------------------------------------

@dataclass
class Facility:
    points: list[geo.Point]
    box: tuple[float, float, float, float]
    facility: str
    comfort: str | None
    line_type: str
    street: str

    @property
    def rank(self) -> int | None:
        return comfort_rank(self.comfort)

    @property
    def separated(self) -> bool:
        return is_separated(self.facility)


def index_facilities(rows: list[dict], *, dedicated_only: bool = True) -> list[Facility]:
    """Turn raw rows into something a spatial join can walk.

    `dedicated_only` matters more than it looks. The layer is an export of
    the whole Comprehensive Transportation Network, so 11,617 of its 17,753
    rows are ordinary streets carrying a comfort rating and no bike facility
    at all. Join against all of them and 74% of work zones appear to "affect
    cyclists" — a number that is true of nothing and useful to nobody.
    """
    out: list[Facility] = []
    for row in rows:
        facility = (row.get("bicycle_facility") or "").strip()
        if dedicated_only and facility not in DEDICATED:
            continue
        points = geo.coords_of(row.get("the_geom"))
        if len(points) < 2:
            continue
        out.append(
            Facility(
                points=points,
                box=geo.bbox(points),
                facility=facility,
                comfort=(row.get("bike_level_of_comfort") or "").strip() or None,
                line_type=(row.get("line_type") or "").strip(),
                street=(row.get("full_street_name") or "").strip().upper(),
            )
        )
    return out


# --------------------------------------------------------------------------
# What a closure actually does to a rider
# --------------------------------------------------------------------------

@dataclass
class BikeImpact:
    facility: Facility
    label: str
    distance_m: float
    separated: bool
    said_so: bool
    tier: Tier
    note: str


def affects_cyclists(zone: dict) -> bool:
    """Does the permit text say this affects people on bikes?

    Mirrors `severity.affects_pedestrians`. Almost always False — that is
    the finding, not a weakness in the pattern.

    Note the question is narrower than "mentions bikes". One live record
    reads "will electrify the existing bike station on E 2nd St": it names
    a bike, and it tells a rider nothing about the lane being shut. So the
    pattern wants bikes near a closure word, and the sentence we print says
    the permit does not say *what this means* for bikes rather than that it
    never mentions them.
    """
    text = " ".join(
        str(zone.get(key) or "") for key in ("name", "description", "road_names")
    )
    return bool(CYCLIST.search(text))


def nearest_facility(
    path: list[geo.Point], facilities: list[Facility], *, tolerance_m: float = 20.0
) -> tuple[Facility, float] | None:
    """Closest dedicated bike facility to a geometry, within tolerance."""
    if not path:
        return None
    search = geo.pad_bbox(geo.bbox(path), tolerance_m + 25)

    best: tuple[Facility, float] | None = None
    for candidate in facilities:
        if not geo.bbox_overlaps(search, candidate.box):
            continue
        distance = geo.path_to_path_m(path, candidate.points, give_up_at=tolerance_m)
        if distance <= tolerance_m and (best is None or distance < best[1]):
            best = (candidate, distance)
    return best


def zone_impact(
    zone: dict, facilities: list[Facility], *, tolerance_m: float = 20.0
) -> BikeImpact | None:
    """Re-read a work zone as a cyclist rather than as a driver.

    The re-tiering rule is the point of this function. `some-lanes-closed`
    is the feed's most common impact value and it is written from a car.
    When the closed lane is a bike lane, the rider has not lost *some* of
    their options — they have lost all of them, and the fallback is a live
    traffic lane.

    We never do the reverse. A zone the feed calls `all-lanes-closed` is not
    softened here, because being wrong in that direction puts someone in
    front of a truck.
    """
    path = geo.coords_of(zone.get("geometry"))
    hit = nearest_facility(path, facilities, tolerance_m=tolerance_m)
    if hit is None:
        return None

    facility, distance = hit
    impact = (zone.get("vehicle_impact") or "").strip().lower()
    said_so = affects_cyclists(zone)
    separated = facility.separated

    label = facility_label(facility.facility)

    if impact == "all-lanes-closed":
        tier = Tier.BLOCKING
        note = f"Full closure across the {label}"
    elif separated:
        tier = Tier.BLOCKING
        note = (
            f"The {label} is closed. The feed calls this a partial closure, "
            "which is true for a car and not for a bike"
        )
    else:
        tier = Tier.SLOWING
        note = f"Work in the {label} — expect to merge out"

    return BikeImpact(
        facility=facility,
        label=label,
        distance_m=distance,
        separated=separated,
        said_so=said_so,
        tier=tier,
        note=note,
    )


# --------------------------------------------------------------------------
# Routing cost
# --------------------------------------------------------------------------

# Metres per second on the flat. Austin is not flat, but the centreline layer
# carries no elevation, so pretending to model gradient would be invention.
CYCLING_SPEED_MS = 4.4
UNPAVED_SPEED_MS = 3.3

# How much worse a metre feels than it measures.
#
# These are calibrated judgement, not measurement: they follow the shape of
# the Level of Traffic Stress literature, where stress rises sharply with
# motor traffic speed once no separation exists. They are deliberately steep
# at the top end — a 50 mph arterial with no bike facility scores 9, which
# makes Dijkstra treat 1 km of it as worse than 8 km of neighbourhood street,
# and that is the intended behaviour rather than an artefact.
STRESS_SEPARATED = 1.0
STRESS_PAINTED = 1.35
STRESS_SHARED = 1.8

MIXED_TRAFFIC_STRESS = (
    (25.0, 1.6),
    (35.0, 2.6),
    (45.0, 4.5),
    (999.0, 9.0),
)


def stress_multiplier(
    speed_mph: float, facility: str | None, comfort: str | None = None
) -> float:
    """Perceived cost per real metre for someone on a bike.

    Note that this returns a *perception* multiplier, not a time. The caller
    must keep the real duration separate, or the app will quote a rider
    forty minutes for a twenty-five minute ride.
    """
    name = (facility or "").strip()

    if name in SEPARATED:
        return STRESS_SEPARATED
    if name in ("Sharrows", "Shared Lane"):
        return STRESS_SHARED
    if name in DEDICATED:
        return STRESS_PAINTED

    for ceiling, penalty in MIXED_TRAFFIC_STRESS:
        if speed_mph <= ceiling:
            base = penalty
            break
    else:  # pragma: no cover - the table ends at 999
        base = MIXED_TRAFFIC_STRESS[-1][1]

    # An unrated street is not evidence of a bad street. Where the city has
    # rated it comfortable, take the rating; where it has said nothing, fall
    # back to the traffic-speed estimate rather than assuming the worst.
    rank = comfort_rank(comfort)
    if rank == 3:
        return min(base, 1.2)
    if rank == 2:
        return min(base, 1.9)
    return base


def cycling_speed_ms(facility: str | None) -> float:
    return UNPAVED_SPEED_MS if (facility or "").strip() == "Trail - Unpaved" else CYCLING_SPEED_MS
