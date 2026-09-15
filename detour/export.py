"""Export a snapshot of today's state for a frontend.

Everything a viewer needs in one JSON document: the confidence distribution,
the full closures with their geometry and verdicts, the clustered signal
events, and enough street centreline to draw a basemap.

The basemap matters more than it sounds. An artifact cannot load map tiles —
they are a cross-origin image request — so the streets have to travel with
the data and be drawn as vectors. Since the city publishes the centreline
layer we already use for routing, that is free.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

from . import cluster, confidence as conf, geo, sources, workcal

# Central Austin: the area where almost all of the closures and every
# clustered signal event sit.
DOWNTOWN = (-97.7800, 30.2350, -97.7150, 30.2980)

ROUND = 5  # ~1 m at this latitude, and roughly halves the payload


def _simplify(points: list[geo.Point], tolerance_m: float = 12.0) -> list[list[float]]:
    """Drop vertices that add no visible shape at map scale."""
    if len(points) < 3:
        return [[round(x, ROUND), round(y, ROUND)] for x, y in points]

    kept = [points[0]]
    for point in points[1:-1]:
        if geo.haversine_m(kept[-1], point) >= tolerance_m:
            kept.append(point)
    kept.append(points[-1])
    return [[round(x, ROUND), round(y, ROUND)] for x, y in kept]


def _in_box(points: list[geo.Point], box) -> bool:
    return bool(points) and geo.bbox_overlaps(box, geo.bbox(points))


def snapshot(
    *, box=DOWNTOWN, now: dt.datetime | None = None, use_cache: bool = True
) -> dict:
    now = now or sources.now_utc()

    zones = sources.fetch_work_zones(use_cache=use_cache)
    signals = sources.fetch_signals(use_cache=use_cache)
    centerline = sources.fetch_centerline_near(box, use_cache=use_cache)

    verdicts = [conf.score_work_zone(z, now=now) for z in zones]
    counts = conf.distribution(verdicts)

    # --- closures, with the verdict that decides how they are drawn -------
    closures = []
    for zone, verdict in zip(zones, verdicts):
        if (zone.get("vehicle_impact") or "").strip().lower() != "all-lanes-closed":
            continue
        points = geo.coords_of(zone.get("geometry"))
        if not _in_box(points, box):
            continue

        active = zone.get("_start") and zone["_start"] <= now and (
            not zone.get("_end") or zone["_end"] >= now
        )
        closures.append(
            {
                "road": (zone.get("road_names") or "").strip(),
                "name": (zone.get("name") or "").strip()[:90],
                "confidence": verdict.level.value,
                "reasons": verdict.reasons[:3],
                "ends": zone["_end"].date().isoformat() if zone.get("_end") else None,
                "critical": bool(zone.get("critical_corridor")),
                "active": bool(active),
                "line": _simplify(points),
            }
        )

    # --- signal events, with the sweep separated out ----------------------
    swept = cluster.batch_timestamps(signals)
    events = cluster.notable(cluster.cluster_signals(signals), now=now)

    event_rows = []
    for event in events:
        if not event.is_cluster:
            continue
        event_rows.append(
            {
                "label": event.label,
                "size": event.size,
                "spread_minutes": round(event.spread_minutes, 1),
                "state": sorted(s for s in event.states if s),
                "started": event.started.isoformat() if event.started else None,
                "members": [
                    {
                        "name": (m.get("location_name") or "").strip(),
                        "point": [
                            round(p[0], ROUND),
                            round(p[1], ROUND),
                        ]
                        if (p := (geo.coords_of(m.get("location")) or [None])[0])
                        else None,
                    }
                    for m in event.members
                ],
            }
        )

    # --- basemap ----------------------------------------------------------
    streets = []
    for row in centerline:
        points = geo.coords_of(row.get("the_geom"))
        if len(points) < 2 or not _in_box(points, box):
            continue
        streets.append(
            {
                "cls": (row.get("road_class") or "6").strip(),
                "line": _simplify(points, tolerance_m=20.0),
            }
        )

    crew_plausible, crew_reason = workcal.crew_plausible(now)

    return {
        "generated_at": now.isoformat(),
        "local_time": workcal.to_central(now).strftime("%A %d %B %Y, %H:%M"),
        "bbox": list(box),
        "totals": {
            "work_zones": len(zones),
            "confidence": counts,
            "pushable": sum(1 for v in verdicts if v.pushable),
            "full_closures_citywide": sum(
                1
                for z in zones
                if (z.get("vehicle_impact") or "").strip().lower() == "all-lanes-closed"
            ),
            "signals_degraded": len(signals),
            "signals_swept": sum(1 for s in signals if s.get("_since") in swept),
            "sweep_stamps": sorted(t.isoformat() for t in swept),
        },
        "crew": {"plausible": crew_plausible, "reason": crew_reason},
        "closures": closures,
        "events": event_rows,
        "streets": streets,
    }


def write(path: str | Path, **kw) -> Path:
    target = Path(path)
    data = snapshot(**kw)
    target.write_text(json.dumps(data, separators=(",", ":")), encoding="utf-8")
    return target
