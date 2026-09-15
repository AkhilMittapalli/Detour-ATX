"""The Desk Editor — deciding what a cluster of failures actually means.

Clustering is arithmetic and lives in `cluster.py`. This is the other half:
given that seven signals dropped at the same minute along Riverside Drive, or
that three near the UT campus fell into flash inside four minutes, *why*.

The reason this is an agent and not a rule is that each hypothesis is tested
by a different lookup:

  a crew cut the fibre     -> look for excavation permits beside the signals
  deliberate event flash   -> look for a venue, and for the same signals
                              having flashed before at a different hour
  a crash took out a pole  -> look at live dispatch incidents
  a comms hub failed       -> a tight cluster with none of the above

You cannot write that as a fixed pipeline, because which lookup matters
depends on what the last one returned. That is the whole justification.

Like the Verifier, this writes to the evidence ledger rather than returning a
conclusion directly, and it holds no authority to raise anything to a
pushable tier.
"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass, field
from typing import Any

from . import cluster as cluster_mod
from . import geo, ledger, llm, sources
from .verifier import _parse_verdict

MAX_STEPS = 5
MAX_TOOLS_PER_RUN = 10

# Signals near a large venue are sometimes put into flash deliberately for
# event traffic. There is no open feed of this — the city's event listings
# dataset covers 73 rows of Palmer and the Convention Center, and carries
# nothing for the stadiums — so the venue itself is the available signal.
VENUES = [
    ("Darrell K Royal Memorial Stadium", (-97.7325, 30.2837), 700),
    ("Moody Center", (-97.7320, 30.2840), 600),
    ("Q2 Stadium", (-97.7195, 30.3880), 700),
    ("Austin Convention Center", (-97.7395, 30.2635), 500),
    ("Palmer Events Center", (-97.7550, 30.2600), 500),
    ("Zilker Park", (-97.7729, 30.2669), 900),
    ("Circuit of the Americas", (-97.6411, 30.1328), 1500),
]

# Work that plausibly severs signal communications.
DIGGING = (
    "excavat", "bore", "trench", "directional drill", "conduit", "duct bank",
    "fiber", "fibre", "cable", "utility cut", "street cut",
)

SYSTEM = """You are a traffic desk editor for Austin. You are given a cluster \
of traffic signals that changed state together, and you decide what it means.

Candidate explanations, each tested differently:
- A construction crew severed signal communications. Look for excavation, \
boring, trenching or conduit work beside the affected intersections.
- Deliberate flash for event traffic. Signals near stadiums and arenas are \
sometimes put into flash on purpose. Look for a venue, and for the same \
signals having done this before at a different time of day.
- A crash took out a pole or cabinet. Look at live dispatch incidents.
- An upstream communications or power failure. A tight cluster with none of \
the above.

Two facts you must weigh:
- A cluster spread of 0 minutes means every signal reported in the same \
minute. That points upstream, not at independent faults.
- "Communication issue" means the city lost telemetry to the cabinet. The \
signal itself is almost certainly still cycling normally on its local timer. \
It is NOT a driver-facing hazard. Only flash is.

Reply with ONLY a JSON object:
{"headline": "one line a commuter would understand",
 "cause": "construction" | "event_flash" | "crash" | "upstream_failure" | "unknown",
 "confidence": 0.0-1.0,
 "driver_impact": "none" | "caution" | "hazard",
 "rationale": "one or two sentences",
 "evidence": ["facts you actually established"]}

Use "unknown" freely. Inventing a cause you did not establish is worse than \
saying you could not tell."""


TOOLS = [
    {
        "name": "event_detail",
        "description": "The intersections in this cluster, their state, and how tightly grouped they are in time and space.",
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "name": "excavation_nearby",
        "description": "Permitted digging, boring, trenching or conduit work near the affected intersections. The construction-cut hypothesis.",
        "parameters": {
            "type": "object",
            "properties": {"radius_m": {"type": "integer", "description": "default 400"}},
        },
    },
    {
        "name": "venue_nearby",
        "description": "Whether the cluster sits beside a stadium, arena or event venue. The deliberate-flash hypothesis.",
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "name": "incidents_nearby",
        "description": "Live dispatch incidents near the cluster. The crash hypothesis.",
        "parameters": {
            "type": "object",
            "properties": {"radius_m": {"type": "integer", "description": "default 500"}},
        },
    },
    {
        "name": "prior_occurrences",
        "description": "Whether these intersections have been in this state before, from our own recorded snapshots. Recurrence at a different hour suggests something scheduled rather than a fault.",
        "parameters": {"type": "object", "properties": {}},
    },
]


@dataclass
class Reading:
    headline: str = ""
    cause: str = "unknown"
    confidence: float = 0.0
    driver_impact: str = "none"
    rationale: str = ""
    evidence: list[str] = field(default_factory=list)
    tools_used: list[str] = field(default_factory=list)
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error


# --------------------------------------------------------------------------
# Snapshots, so recurrence is answerable at all
# --------------------------------------------------------------------------

def snapshot_signals(signals: list[dict], *, path=None) -> int:
    """Record today's degraded signals so tomorrow can compare.

    The signals feed is a current-state table with no history — 131 rows
    saying what is broken now and nothing about what was broken yesterday.
    Recurrence is the single strongest discriminator between a fault and a
    scheduled flash, so we accumulate the history the city does not publish,
    the same way the ground truth loop accumulates observations.
    """
    written = 0
    for signal in signals:
        signal_id = str(signal.get("signal_id") or "")
        since = signal.get("_since")
        if not signal_id or not since:
            continue
        ledger.append(
            ledger.Claim(
                record_id=f"signal:{signal_id}",
                kind="signal",
                claim="observed_state",
                source="feed",
                observed_at=since.isoformat(),
                detail=f"{(signal.get('operation_text') or '').strip()} | "
                       f"{(signal.get('location_name') or '').strip()}",
            ),
            path=path,
        )
        written += 1
    return written


class EventTools:
    """Evidence lookups bound to one clustered event."""

    def __init__(
        self,
        event: cluster_mod.Event,
        zones: list[dict],
        *,
        now: dt.datetime | None = None,
    ):
        self.event = event
        self.zones = zones
        self.now = now or sources.now_utc()

    def event_detail(self) -> dict[str, Any]:
        members = [
            {
                "intersection": (m.get("location_name") or "").strip(),
                "state": (m.get("operation_text") or "").strip(),
                "since_utc": m["_since"].isoformat() if m.get("_since") else None,
            }
            for m in self.event.members
        ]
        return {
            "count": self.event.size,
            "spread_minutes": round(self.event.spread_minutes, 1),
            "local_time_now": self.now.isoformat(),
            "members": members,
            "note": (
                "A spread of 0 means every signal reported in the same minute."
                if self.event.spread_minutes == 0
                else ""
            ),
        }

    def excavation_nearby(self, radius_m: int = 400) -> dict[str, Any]:
        centre = self.event.centroid
        if not centre:
            return {"error": "event has no location"}

        found = []
        for zone in self.zones:
            points = geo.coords_of(zone.get("geometry"))
            if not points:
                continue
            if geo.point_to_path_m(centre, points) > radius_m:
                continue
            text = f"{zone.get('name') or ''} {zone.get('description') or ''}".lower()
            hits = sorted({word for word in DIGGING if word in text})
            if hits:
                found.append(
                    {
                        "road_names": zone.get("road_names"),
                        "name": (zone.get("name") or "")[:70],
                        "matched_terms": hits,
                        "ends": zone["_end"].date().isoformat() if zone.get("_end") else None,
                    }
                )

        return {
            "radius_m": radius_m,
            "count": len(found),
            "permits": found[:6],
            "note": "No digging permits near this cluster." if not found else "",
        }

    def venue_nearby(self) -> dict[str, Any]:
        centre = self.event.centroid
        if not centre:
            return {"error": "event has no location"}

        near = []
        for name, point, radius in VENUES:
            distance = geo.haversine_m(centre, point)
            if distance <= radius * 3:
                near.append(
                    {"venue": name, "metres_away": round(distance), "typical_radius_m": radius}
                )
        near.sort(key=lambda v: v["metres_away"])
        return {
            "count": len(near),
            "venues": near,
            "note": (
                "No major venue nearby."
                if not near
                else "Austin publishes no feed of deliberate event flash, so "
                     "proximity is suggestive, not proof."
            ),
        }

    def incidents_nearby(self, radius_m: int = 500) -> dict[str, Any]:
        centre = self.event.centroid
        if not centre:
            return {"error": "event has no location"}
        try:
            incidents = sources.fetch_active_incidents()
        except RuntimeError as exc:
            return {"error": str(exc)[:160]}

        found = [
            {
                "issue": i.get("issue_reported"),
                "address": i.get("address"),
                "published": i.get("published_date"),
            }
            for i in incidents
            if (p := geo.coords_of(i.get("location")))
            and geo.haversine_m(centre, p[0]) <= radius_m
        ]
        return {"count": len(found), "incidents": found[:5],
                "note": "No active dispatch incidents nearby." if not found else ""}

    def prior_occurrences(self) -> dict[str, Any]:
        ids = {f"signal:{m.get('signal_id')}" for m in self.event.members if m.get("signal_id")}
        index = ledger.by_record()

        history = []
        for record_id in sorted(ids):
            for claim in index.get(record_id, []):
                if claim.claim != "observed_state":
                    continue
                history.append({"signal": record_id, "when": claim.observed_at[:16],
                                "state": claim.detail.split("|")[0].strip()})

        current = {m["_since"].isoformat()[:16] for m in self.event.members if m.get("_since")}
        earlier = [h for h in history if h["when"] not in current]

        return {
            "snapshots_held": len(history),
            "earlier_occurrences": earlier[:8],
            "note": (
                "No snapshot history yet — this builds up over successive runs, "
                "so recurrence cannot be judged today."
                if not history
                else "Occurrences at a different hour suggest something scheduled."
            ),
        }


def read_event(
    event: cluster_mod.Event,
    zones: list[dict],
    *,
    now: dt.datetime | None = None,
    model: str | None = None,
    write_ledger: bool = True,
) -> Reading:
    """Interpret one clustered event."""
    tools = EventTools(event, zones, now=now)
    reading = Reading()

    dispatch = {
        "event_detail": lambda a: tools.event_detail(),
        "excavation_nearby": lambda a: tools.excavation_nearby(int(a.get("radius_m", 400))),
        "venue_nearby": lambda a: tools.venue_nearby(),
        "incidents_nearby": lambda a: tools.incidents_nearby(int(a.get("radius_m", 500))),
        "prior_occurrences": lambda a: tools.prior_occurrences(),
    }

    try:
        session = llm.GeminiSession(TOOLS, model=model, system=SYSTEM)
    except llm.LLMError as exc:
        return Reading(error=str(exc))

    opening = (
        f"Interpret this cluster: {event.label}.\n"
        f"It has {event.size} signal(s) with a {event.spread_minutes:.0f} minute spread.\n"
        "Start with event_detail."
    )

    try:
        turn = session.ask(opening)
        for _ in range(MAX_STEPS):
            if not turn.wants_tools:
                break
            if session.calls_made > MAX_TOOLS_PER_RUN:
                turn = session.ask("Tool budget reached. Give your JSON reading now.")
                break
            outputs = []
            for call in turn.calls:
                handler = dispatch.get(call.name)
                reading.tools_used.append(call.name)
                outputs.append((call.name, handler(call.args) if handler else {"error": "no such tool"}))
            turn = session.give_results(outputs)
        else:
            turn = session.ask("Give your JSON reading now.")

        parsed = _parse_verdict_like(turn.text)
        if not parsed and getattr(turn, "truncated", False):
            turn = session.ask("Your reply was cut off. Reply with ONLY the JSON object.")
            parsed = _parse_verdict_like(turn.text)
    except llm.LLMError as exc:
        return Reading(error=str(exc), tools_used=reading.tools_used)

    if not parsed:
        reading.error = f"unparseable reading: {turn.text[:140]}"
        return reading

    reading.headline = str(parsed.get("headline", ""))[:160]
    cause = str(parsed.get("cause", "unknown")).lower().strip()
    reading.cause = cause if cause in (
        "construction", "event_flash", "crash", "upstream_failure", "unknown"
    ) else "unknown"
    try:
        reading.confidence = max(0.0, min(1.0, float(parsed.get("confidence", 0.0))))
    except (TypeError, ValueError):
        reading.confidence = 0.0
    impact = str(parsed.get("driver_impact", "none")).lower().strip()
    reading.driver_impact = impact if impact in ("none", "caution", "hazard") else "none"
    reading.rationale = str(parsed.get("rationale", ""))[:400]
    raw = parsed.get("evidence")
    if isinstance(raw, list):
        reading.evidence = [str(e)[:200] for e in raw[:6]]

    if write_ledger and reading.cause != "unknown":
        ledger.append(
            ledger.Claim(
                record_id=f"event:{event.started.isoformat()}" if event.started else "event:unknown",
                kind="signal_event",
                claim="interpreted",
                source="editor",
                observed_at=dt.datetime.now(dt.timezone.utc).isoformat(),
                detail=f"{reading.cause} | {reading.confidence:.2f} | {reading.headline}",
            )
        )
    return reading


def _parse_verdict_like(text: str) -> dict | None:
    """Same lenient extraction as the Verifier, minus the verdict key check."""
    parsed = _parse_verdict(text)
    if parsed:
        return parsed
    if not text:
        return None
    brace = text.find("{")
    if brace == -1:
        return None
    try:
        candidate = json.loads(text[brace : text.rfind("}") + 1])
    except json.JSONDecodeError:
        return None
    return candidate if isinstance(candidate, dict) else None
