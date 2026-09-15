"""Three tiers, sorted by what the reader has to decide.

The tiers are not about how bad something is. They are about whether it
changes what you do in the next ten minutes:

    Blocking    change route or leave earlier
    Slowing     add a few minutes
    Background  nothing — context only

Severity and confidence are orthogonal. Severity asks "what would this mean
if it is true"; confidence asks "is it true". They are combined at the end,
and the combination is where the anti-crying-wolf rule lives: something we
believe is probably finished never reaches the top tier, however dramatic it
would be if it were real.
"""

from __future__ import annotations

from enum import IntEnum

import re

from .confidence import Confidence, Verdict

# Sidewalk and crossing closures are reported far less than lane closures and
# hurt more: a driver loses a minute, someone using a wheelchair loses the
# route. The feed has no field for it, so it comes out of the description —
# about 40 records citywide mention a sidewalk closure and 56 mention curb
# ramps or ADA work.
PEDESTRIAN = re.compile(
    r"(sidewalk[^.]{0,40}clos|clos[^.]{0,40}sidewalk|"
    r"pedestrian[^.]{0,30}(detour|clos|reroute)|"
    r"crosswalk[^.]{0,30}clos|curb ramp|\bADA\b)",
    re.I,
)


class Tier(IntEnum):
    BACKGROUND = 0
    SLOWING = 1
    BLOCKING = 2


LABELS = {
    Tier.BLOCKING: "Blocking",
    Tier.SLOWING: "Slowing",
    Tier.BACKGROUND: "Background",
}

ASKS = {
    Tier.BLOCKING: "Change route or leave earlier",
    Tier.SLOWING: "Add a few minutes",
    Tier.BACKGROUND: "Nothing — context only",
}


def _as_bool(value) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() == "true"
    return False


def affects_pedestrians(zone: dict) -> bool:
    """Does this work zone take a footway, not just a traffic lane?"""
    text = f"{zone.get('name') or ''} {zone.get('description') or ''}"
    return bool(PEDESTRIAN.search(text))


def work_zone_tier(zone: dict) -> Tier:
    """Intrinsic severity of a work zone, before confidence is applied."""
    impact = (zone.get("vehicle_impact") or "").strip().lower()

    if impact == "all-lanes-closed":
        return Tier.BLOCKING
    if impact == "some-lanes-closed":
        if (
            _as_bool(zone.get("critical_corridor"))
            or _as_bool(zone.get("are_workers_present"))
            or affects_pedestrians(zone)
        ):
            return Tier.SLOWING
        return Tier.BACKGROUND

    # A permit with no vehicle impact can still close a footway.
    return Tier.SLOWING if affects_pedestrians(zone) else Tier.BACKGROUND


def signal_tier(signal: dict) -> Tier:
    """Intrinsic severity of a degraded signal.

    A flashing signal is a driver-facing hazard: under Texas law a flashing
    red is a stop, and a dark signal is a four-way stop, which most drivers
    do not know. A communication issue is invisible from the car — the
    signal keeps cycling on its local timer — so it is context, not an
    instruction.
    """
    state = (signal.get("operation_text") or "").lower()
    return Tier.BLOCKING if "flash" in state else Tier.BACKGROUND


def incident_tier(incident: dict) -> Tier:
    """Live dispatch incidents.

    The feed has no road-closure category, so we cannot tell a fender bender
    from a shut arterial. Everything active on your route is Slowing: honest
    about what we know, and it keeps the top tier for things we can actually
    stand behind.
    """
    return Tier.SLOWING


def combine(tier: Tier, verdict: Verdict) -> Tier:
    """Apply confidence to intrinsic severity.

    A record we believe is probably finished is capped at Slowing — never
    Blocking, because one alarm about a closure that is not there costs more
    trust than ten missed lane restrictions; but never silently dropped to
    Background either, because a visible low-confidence item is exactly what
    prompts a resident to tap "still there?" and hand us ground truth.

    Reported items keep their intrinsic tier. Only 126 records in the whole
    feed carry a verified date, so demoting everything unverified would
    leave the top tier permanently empty and the product useless. The
    calibration happens through the confidence chip shown beside the item,
    and through `Verdict.pushable`, which independently gates notifications
    to Confirmed alone.
    """
    if verdict.level is Confidence.PROBABLY_OVER:
        return min(tier, Tier.SLOWING)
    return tier
