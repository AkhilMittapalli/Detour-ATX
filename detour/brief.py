"""Assemble the daily brief for one saved route."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

from . import confidence as conf
from . import bike, cluster, describe, graph as graph_mod, ledger, reroute, workcal
from . import route as rt, severity, sources, transit

# Separator between the clauses a detail line accretes: the rewritten
# description, the confidence reasons, the footway note, the work-calendar
# note and the rider note. Without it they run together into one sentence.
GAP = "—"


@dataclass
class Item:
    tier: severity.Tier
    verdict: conf.Verdict
    kind: str
    headline: str
    detail: str
    action: str = ""
    distance_m: float = 0.0
    record_id: str = ""

    @property
    def pushable(self) -> bool:
        return self.tier is severity.Tier.BLOCKING and self.verdict.pushable


@dataclass
class Brief:
    route: rt.Route
    generated_at: dt.datetime
    items: list[Item] = field(default_factory=list)
    streets: list[str] = field(default_factory=list)
    delta: reroute.Delta | None = None
    bus_routes: list = field(default_factory=list)

    @property
    def blocking(self) -> list[Item]:
        return [i for i in self.items if i.tier is severity.Tier.BLOCKING]

    @property
    def pushable(self) -> list[Item]:
        return [i for i in self.items if i.pushable]


def _impact_phrase(zone: dict) -> str:
    impact = (zone.get("vehicle_impact") or "").strip().lower()
    return {
        "all-lanes-closed": "fully closed",
        "some-lanes-closed": "down to reduced lanes",
    }.get(impact, "affected by work")


def _group_key(zone: dict) -> str:
    """Work zones arrive one row per direction.

    A single closure on Colorado St shows up twice — northbound and
    southbound — under the same permit `name`. Grouping on that keeps the
    brief from reporting one closure as two.
    """
    return (zone.get("name") or zone.get("id") or "").strip().upper()


def _directions(zones: list[dict]) -> str:
    """Readable direction, skipping the feed's literal "unknown" value."""
    seen = []
    for zone in zones:
        value = (zone.get("direction") or "").strip().lower()
        if value in ("", "unknown", "undefined"):
            continue
        if value not in seen:
            seen.append(value)
    if len(seen) > 1:
        return "both directions"
    return seen[0] if seen else ""


def _duration(since: dt.datetime | None, now: dt.datetime) -> str:
    if not since:
        return "an unknown length of time"
    days = (now - since).days
    if days >= 730:
        return f"{days // 365} years"
    if days >= 365:
        return "over a year"
    if days >= 2:
        return f"{days} days"
    hours = max(1, int((now - since).total_seconds() // 3600))
    return f"{hours} hours"


def _work_zone_items(
    matches: list[rt.Match],
    now: dt.datetime,
    *,
    rewrite: bool,
    observations: dict | None = None,
    facilities: list | None = None,
) -> list[Item]:
    grouped: dict[str, list[rt.Match]] = {}
    for match in matches:
        grouped.setdefault(_group_key(match.record), []).append(match)

    items: list[Item] = []
    for group in grouped.values():
        zones = [m.record for m in group]
        lead = zones[0]
        record_id = str(lead.get("id") or "")

        # A closure arrives as several rows, one per direction, and an
        # observation may be filed against any of them. Check every id in
        # the group, not just the lead, or a verdict recorded against the
        # southbound row is invisible on the northbound one.
        index = observations or {}
        group_ids = [str(z.get("id") or "") for z in zones if z.get("id")]

        def newest(source: str):
            found = [
                claim
                for gid in group_ids
                if (claim := ledger.latest_observation(
                    gid, now=now, index=index, source=source
                ))
            ]
            return found[0] if found else None

        seen = newest("resident")
        agent = newest("verifier")
        verdict = conf.score_work_zone(
            lead, now=now, observation=seen, agent_verdict=agent
        )
        # On a bike the feed's own impact field is the wrong question, so
        # the cyclist reading can raise the tier and never lower it.
        impact = bike.zone_impact(lead, facilities) if facilities else None
        base = severity.work_zone_tier(lead)
        if impact is not None:
            base = max(base, impact.tier)
        tier = severity.combine(base, verdict)

        road = (lead.get("road_names") or "this street").strip()
        heading = _directions(zones)
        if impact is not None and impact.separated and (
            lead.get("vehicle_impact") or ""
        ).strip().lower() == "some-lanes-closed":
            headline = f"{road}: the {impact.label} is closed"
        else:
            headline = f"{road} {_impact_phrase(lead)}"
        if heading:
            headline += f", {heading}"
        if lead.get("_end") and verdict.level is not conf.Confidence.PROBABLY_OVER:
            headline += f", through {conf.fmt_date(lead['_end'])}"

        detail = describe.rewrite(
            lead.get("description"), road=road, enabled=rewrite
        )
        if verdict.reasons:
            suffix = "; ".join(verdict.reasons)
            # Not str.capitalize() — it lowercases the rest of the string and
            # turns "23 Sep 2026" into "23 sep 2026".
            detail = f"{detail} ({suffix})" if detail else suffix[0].upper() + suffix[1:]

        if severity.affects_pedestrians(lead):
            detail += " Footway affected: this one also closes a sidewalk, crossing or ramp."

        if impact is not None:
            detail += f" {impact.note}."
            if not impact.said_so:
                detail += " The permit does not say what this means for bikes."

        note = workcal.activity_note(lead, now)
        if note:
            detail = f"{detail} — {note}" if detail else note.capitalize()

        action = ""
        if verdict.level is conf.Confidence.PROBABLY_OVER:
            action = "Passed it today? Tell us whether it is still there."
        elif tier is severity.Tier.BLOCKING:
            action = "Plan to divert around this one."

        items.append(
            Item(
                tier=tier,
                verdict=verdict,
                kind="work_zone",
                headline=headline,
                detail=detail,
                action=action,
                distance_m=min(m.distance_m for m in group),
                record_id=record_id,
            )
        )
    return items


def _signal_items(
    matches: list[rt.Match],
    now: dt.datetime,
    *,
    swept: set[dt.datetime] | None = None,
) -> list[Item]:
    """Signal items for the brief.

    `swept` holds timestamps written by the city's nightly sweep rather than
    by a real state change. A duration measured from one of those is wrong —
    a signal unreachable for years reads as unreachable for hours — so those
    items say the onset is unknown instead of quoting a false figure.
    """
    swept = swept or set()
    items: list[Item] = []
    for match in matches:
        signal = match.record
        verdict = conf.score_signal(signal, now=now)
        tier = severity.combine(severity.signal_tier(signal), verdict)

        where = (signal.get("location_name") or "An intersection on your route").strip()
        state = (signal.get("operation_text") or "degraded").strip()
        onset_known = signal.get("_since") not in swept
        elapsed = _duration(signal.get("_since"), now)

        if "flash" in state.lower():
            headline = (
                f"{where} — signal flashing for {elapsed}"
                if onset_known
                else f"{where} — signal flashing"
            )
            detail = (
                "The controller tripped its conflict monitor and fell back to flash."
            )
            action = "In Texas a flashing red is a stop. Treat it as a four-way stop."
        elif onset_known:
            headline = f"{where} — no city telemetry for {elapsed}"
            detail = (
                "The signal still runs on its local timer, so it is not dangerous, "
                "but engineers cannot see or retime it when there is a crash upstream."
            )
            action = ""
        else:
            headline = f"{where} — no city telemetry"
            detail = (
                "The signal still runs on its local timer, so it is not dangerous, "
                "but engineers cannot see or retime it when there is a crash upstream. "
                "How long it has been unreachable is unknown: the city's overnight "
                "sweep restamps these records, so the feed's timestamp is when it was "
                "last counted, not when it failed."
            )
            action = ""

        items.append(
            Item(
                tier=tier,
                verdict=verdict,
                kind="signal",
                headline=headline,
                detail=detail,
                action=action,
                distance_m=match.distance_m,
            )
        )
    return items


def _incident_items(matches: list[rt.Match], now: dt.datetime) -> list[Item]:
    items: list[Item] = []
    for match in matches:
        incident = match.record
        issue = (incident.get("issue_reported") or "Incident").strip().title()
        address = (incident.get("address") or "on your route").strip().title()
        published = incident.get("_published")

        verdict = conf.Verdict(
            conf.Confidence.CONFIRMED, ["live dispatch feed still marks this active"]
        )
        detail = "Reported by Austin dispatch"
        if published:
            detail += f" {_duration(published, now)} ago"

        items.append(
            Item(
                tier=severity.incident_tier(incident),
                verdict=verdict,
                kind="incident",
                headline=f"{issue} — {address}",
                detail=detail + ".",
                distance_m=match.distance_m,
            )
        )
    return items


def _compute_delta(
    route_obj: rt.Route, centerline: list[dict], zones: list[dict]
) -> reroute.Delta | None:
    """Route the trip with and without today's full closures.

    Closures are taken from the whole corridor, not only those already
    matched to the route: a closure two blocks away is exactly what makes
    the diversion longer, and excluding it would understate the delta.
    """
    street_graph = graph_mod.build(centerline)
    if not street_graph.edges:
        return None

    corridor = geo_padded_bbox(route_obj)
    full_closures = [
        zone
        for zone in zones
        if (zone.get("vehicle_impact") or "").strip().lower() == "all-lanes-closed"
        and _within(zone, corridor)
    ]

    # Pass the saved waypoints, not just the endpoints: the baseline has to
    # follow the user's own streets or closures on them never register.
    return reroute.delta(street_graph, route_obj.waypoints, full_closures)


def geo_padded_bbox(route_obj: rt.Route) -> tuple[float, float, float, float]:
    from . import geo

    return geo.pad_bbox(route_obj.bbox, 900)


def _within(zone: dict, box: tuple[float, float, float, float]) -> bool:
    from . import geo

    points = geo.coords_of(zone.get("geometry"))
    return bool(points) and geo.bbox_overlaps(box, geo.bbox(points))


def build(
    route_obj: rt.Route,
    *,
    now: dt.datetime | None = None,
    rewrite: bool = True,
    use_cache: bool = True,
    with_detour: bool = True,
    with_transit: bool = True,
    mode: str = "drive",
) -> Brief:
    """Fetch every feed, join to the route, and assemble the brief.

    `mode="bike"` reads the same closures as a cyclist. It does not change
    which records are fetched, only what they are taken to mean: a permit
    the feed files as a partial lane closure has taken the whole of a
    rider's lane when the lane it took was theirs.
    """
    now = now or sources.now_utc()

    zones = sources.fetch_work_zones(use_cache=use_cache)
    signals = sources.fetch_signals(use_cache=use_cache)
    incidents = sources.fetch_active_incidents(use_cache=use_cache)
    centerline = sources.fetch_centerline_near(
        geo_padded_bbox(route_obj), use_cache=use_cache
    )
    observations = ledger.by_record()

    facilities = []
    if mode == "bike":
        facilities = bike.index_facilities(
            sources.fetch_bike_facilities_near(
                geo_padded_bbox(route_obj), use_cache=use_cache
            )
        )

    items: list[Item] = []
    items += _work_zone_items(
        rt.match_work_zones(route_obj, zones),
        now,
        rewrite=rewrite,
        observations=observations,
        facilities=facilities,
    )
    items += _signal_items(
        rt.match_signals(route_obj, signals),
        now,
        swept=cluster.batch_timestamps(signals),
    )
    items += _incident_items(rt.match_incidents(route_obj, incidents), now)

    # Most decision-forcing first; within a tier, most trustworthy first.
    order = {
        conf.Confidence.CONFIRMED: 0,
        conf.Confidence.REPORTED: 1,
        conf.Confidence.PROBABLY_OVER: 2,
    }
    items.sort(key=lambda i: (-int(i.tier), order[i.verdict.level], i.distance_m))

    delta = _compute_delta(route_obj, centerline, zones) if with_detour else None

    # Give the blocking items the routing consequence, which is the answer
    # the city never published for any of its 377 open full closures.
    if delta is not None and delta.affected and not delta.implausible:
        for item in items:
            if item.tier is severity.Tier.BLOCKING and item.kind == "work_zone":
                item.action = f"Detour costs {delta.summary()}"
                if delta.via:
                    item.action += f" — via {' → '.join(delta.via[:4])}"

    # Rider impact. A closure on a bus corridor costs riders a stop, and they
    # are told even less than drivers are. Never allowed to fail a brief: a
    # transit feed being down is not a reason for someone not to hear about a
    # closed street.
    bus_routes = []
    if with_transit and any(i.tier is severity.Tier.BLOCKING for i in items):
        snapshot = transit.vehicles()
        if snapshot.ok:
            bus_routes = transit.routes_near(route_obj.path, snapshot)
            note = transit.impact_note(bus_routes)
            if note:
                for item in items:
                    if item.tier is severity.Tier.BLOCKING:
                        item.detail = f"{item.detail} {GAP} {note}".strip()

    return Brief(
        route=route_obj,
        generated_at=now,
        items=items,
        streets=rt.streets_on_route(route_obj, centerline),
        delta=delta,
        bus_routes=bus_routes,
    )
