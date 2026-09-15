"""Evidence lookups the Verifier can call.

Each function answers one narrow question about whether a work zone record
describes something real, and returns a small JSON-safe dict. They are
ordinary functions with no model involvement: the agent decides *which* to
call and in what order, which is the part that genuinely branches.

A note on the 311 vocabulary. The obvious types are dead — `Lane/Road
Closure Notification` (11,613 all-time) and `Obstruction in ROW` (12,797)
have not taken a record since before June 2026. The live corroboration
signals are `TPW - Activate/Deactivate Work Zone` and `TPW - Construction
Concerns in Right of Way`, so those are what we look for.
"""

from __future__ import annotations

import datetime as dt
import re
from typing import Any

from . import geo, sources, workcal
from .reroute import _normalise as normalise_street

# 311 types that say something about whether work is happening here.
CORROBORATING_TYPES = [
    "TPW - Activate/Deactivate Work Zone",
    "TPW - Construction Concerns in Right of Way",
    "TPW - Traffic Signal - Maintenance",
    "TPW - Parking Sign Maintenance",
]

# Permit cross-references that appear inside description text, e.g.
# "PER COORDINATION WITH 2025-032743 RW".
PERMIT_REFERENCE = re.compile(r"\b(\d{4}-\d{5,7})\s*(RW|EX|DS)?\b", re.I)


def _centroid(points: list[geo.Point]) -> geo.Point | None:
    if not points:
        return None
    return (
        sum(p[0] for p in points) / len(points),
        sum(p[1] for p in points) / len(points),
    )


class Toolbox:
    """Evidence lookups bound to one corpus of work zones."""

    def __init__(self, zones: list[dict], *, now: dt.datetime | None = None):
        self.zones = zones
        self.by_id = {str(z.get("id") or ""): z for z in zones}
        self.now = now or sources.now_utc()

    # -- helpers ---------------------------------------------------------
    def zone(self, record_id: str) -> dict | None:
        return self.by_id.get(str(record_id))

    def _where(self, record_id: str) -> geo.Point | None:
        zone = self.zone(record_id)
        if not zone:
            return None
        return _centroid(geo.coords_of(zone.get("geometry")))

    # -- tools -----------------------------------------------------------
    def search_311(
        self, record_id: str, radius_m: int = 300, days: int = 180
    ) -> dict[str, Any]:
        """Nearby 311 activity, and whether any of it speaks to this work.

        Absence is informative here. Months of silence around a supposed
        full closure on a busy corridor is evidence against it, not the
        absence of evidence.
        """
        where = self._where(record_id)
        if not where:
            return {"error": "no geometry for that record"}

        since = (self.now - dt.timedelta(days=days)).strftime("%Y-%m-%d")
        lon, lat = where
        clause = (
            f"within_circle(sr_location_lat_long, {lat}, {lon}, {int(radius_m)}) "
            f"AND sr_created_date > '{since}'"
        )
        try:
            rows = sources.soda(
                "xwdj-i9he",
                {
                    "$select": "sr_type_desc,sr_created_date,sr_status_desc,sr_location",
                    "$where": clause,
                    "$order": "sr_created_date DESC",
                    "$limit": 40,
                },
            )
        except RuntimeError as exc:
            return {"error": str(exc)[:160]}

        corroborating = [
            {
                "type": r.get("sr_type_desc"),
                "created": (r.get("sr_created_date") or "")[:10],
                "status": r.get("sr_status_desc"),
            }
            for r in rows
            if r.get("sr_type_desc") in CORROBORATING_TYPES
        ]

        return {
            "radius_m": radius_m,
            "window_days": days,
            "total_nearby": len(rows),
            "corroborating_count": len(corroborating),
            "corroborating": corroborating[:10],
            "note": (
                "No work-zone-related 311 activity nearby in this window."
                if not corroborating
                else "Requests below relate to work zones or construction here."
            ),
        }

    def overlapping_permits(self, record_id: str, radius_m: int = 60) -> dict[str, Any]:
        """Other work zone records whose geometry sits on the same stretch.

        Several permits covering one site usually means real, staged work.
        A lone permit with none around it is weaker.
        """
        zone = self.zone(record_id)
        if not zone:
            return {"error": "unknown record"}

        points = geo.coords_of(zone.get("geometry"))
        if not points:
            return {"error": "no geometry for that record"}

        box = geo.pad_bbox(geo.bbox(points), radius_m + 40)
        found = []
        for other in self.zones:
            if str(other.get("id")) == str(record_id):
                continue
            other_points = geo.coords_of(other.get("geometry"))
            if not other_points or not geo.bbox_overlaps(box, geo.bbox(other_points)):
                continue
            if geo.path_to_path_m(points, other_points, give_up_at=radius_m) <= radius_m:
                found.append(
                    {
                        "road_names": other.get("road_names"),
                        "name": (other.get("name") or "")[:80],
                        "impact": other.get("vehicle_impact"),
                        "ends": other["_end"].date().isoformat() if other.get("_end") else None,
                        "workers_present": bool(other.get("are_workers_present")),
                    }
                )

        return {"count": len(found), "permits": found[:8]}

    def find_permit(self, reference: str) -> dict[str, Any]:
        """Look up a permit referenced inside another permit's description."""
        cleaned = reference.strip().upper()
        hits = []
        for zone in self.zones:
            haystack = f"{zone.get('name') or ''} {zone.get('description') or ''}".upper()
            if cleaned and cleaned in haystack:
                hits.append(
                    {
                        "id": zone.get("id"),
                        "road_names": zone.get("road_names"),
                        "impact": zone.get("vehicle_impact"),
                        "starts": zone["_start"].date().isoformat() if zone.get("_start") else None,
                        "ends": zone["_end"].date().isoformat() if zone.get("_end") else None,
                        "description": (zone.get("description") or "")[:300],
                    }
                )
        return {"reference": reference, "count": len(hits), "matches": hits[:4]}

    def check_geometry(self, record_id: str) -> dict[str, Any]:
        """Does the geometry sit on the street the permit names?

        Deterministic, and exposed as a tool precisely because it is: not one
        of the 3,853 records carries a verified position, so this is the
        cheapest available check on whether the coordinates can be trusted.
        """
        zone = self.zone(record_id)
        if not zone:
            return {"error": "unknown record"}

        points = geo.coords_of(zone.get("geometry"))
        road_names = (zone.get("road_names") or "").strip()
        if not points or not road_names:
            return {"error": "record lacks geometry or a road name"}

        box = geo.pad_bbox(geo.bbox(points), 60)
        try:
            centerline = sources.fetch_centerline_near(box)
        except RuntimeError as exc:
            return {"error": str(exc)[:160]}

        wanted = normalise_street(road_names)
        nearby: dict[str, float] = {}
        for row in centerline:
            name = (row.get("full_street_name") or "").strip().upper()
            seg = geo.coords_of(row.get("the_geom"))
            if not name or not seg:
                continue
            distance = geo.path_to_path_m(points, seg, give_up_at=25.0)
            if distance <= 40.0:
                nearby[name] = min(nearby.get(name, 1e9), distance)

        matches = [n for n in nearby if normalise_street(n) & wanted]
        return {
            "permit_says": road_names,
            "streets_under_geometry": sorted(nearby, key=lambda n: nearby[n])[:5],
            "name_agrees": bool(matches),
            "note": (
                "Geometry sits on the street the permit names."
                if matches
                else "Geometry does NOT sit on the street the permit names — "
                "treat the coordinates as unreliable."
            ),
        }

    def recent_incidents(self, record_id: str, radius_m: int = 250) -> dict[str, Any]:
        """Live dispatch incidents near the site."""
        where = self._where(record_id)
        if not where:
            return {"error": "no geometry for that record"}

        try:
            incidents = sources.fetch_active_incidents()
        except RuntimeError as exc:
            return {"error": str(exc)[:160]}

        found = []
        for incident in incidents:
            point = geo.coords_of(incident.get("location"))
            if point and geo.haversine_m(where, point[0]) <= radius_m:
                found.append(
                    {
                        "issue": incident.get("issue_reported"),
                        "address": incident.get("address"),
                        "published": incident.get("published_date"),
                    }
                )
        return {"count": len(found), "incidents": found[:5]}

    def work_calendar(self, record_id: str) -> dict[str, Any]:
        """Would a crew plausibly be working here right now?"""
        zone = self.zone(record_id)
        if not zone:
            return {"error": "unknown record"}

        plausible, reason = workcal.crew_plausible(
            self.now, description=zone.get("description")
        )
        local = workcal.to_central(self.now)
        return {
            "local_time": local.strftime("%A %Y-%m-%d %H:%M"),
            "crew_plausible_now": plausible,
            "reason": reason,
            "declared_schedule": sorted(workcal.schedule_hints(zone.get("description"))),
            "note": (
                "A closure's barricades usually stay in place even when no crew "
                "is working, so this speaks to active work, not to whether the "
                "restriction exists."
            ),
        }

    def permit_narrative(self, record_id: str) -> dict[str, Any]:
        """The record's own fields, including references worth chasing."""
        zone = self.zone(record_id)
        if not zone:
            return {"error": "unknown record"}

        description = zone.get("description") or ""
        references = sorted(
            {f"{m.group(1)} {m.group(2) or ''}".strip() for m in PERMIT_REFERENCE.finditer(description)}
        )
        return {
            "road_names": zone.get("road_names"),
            "direction": zone.get("direction"),
            "impact": zone.get("vehicle_impact"),
            "critical_corridor": bool(zone.get("critical_corridor")),
            "workers_present": bool(zone.get("are_workers_present")),
            "end_date_verified": bool(zone.get("is_end_date_verified")),
            "starts": zone["_start"].date().isoformat() if zone.get("_start") else None,
            "ends": zone["_end"].date().isoformat() if zone.get("_end") else None,
            "window_days": (
                (zone["_end"] - zone["_start"]).days
                if zone.get("_start") and zone.get("_end")
                else None
            ),
            "description": description[:1200],
            "referenced_permits": references,
        }
