"""The Verifier — the one role in this product where an agent earns its keep.

The question is "does this permit describe something that is actually on the
ground today", and it cannot be answered by a rule because the evidence path
branches. Sometimes the description settles it. Sometimes it cites another
permit by number and you have to go read that one. Sometimes the signal is an
*absence*: months of silence in 311 around a supposed full closure on a busy
corridor.

Scope is deliberately bounded. It runs over the Reported tier — the records
the deterministic model could not resolve either way — never the whole feed.
Confirmed and Probably-over records already have an answer, and spending a
model call to re-derive one is waste.

Its output is written to the evidence ledger rather than returned directly,
so the agent layer communicates through the ledger and the work queue and
never by calling other agents.
"""

from __future__ import annotations

import datetime as dt
import json
import re
from dataclasses import dataclass, field

from . import confidence as conf
from . import evidence as ev
from . import ledger, llm, workcal

MAX_STEPS = 6          # tool rounds before we force a verdict
MAX_TOOLS_PER_RUN = 12  # hard budget, so one pathological record cannot run away

SYSTEM = """You verify whether City of Austin road work permits describe work \
that is really happening right now.

Context you must assume:
- Permit dates are permit WINDOWS, not work windows. A year-long window on a \
two-day job is normal and tells you almost nothing.
- Of 3,853 records, 126 have a city-verified end date and NONE have a verified \
position. Coordinates can be wrong.
- Absence of evidence is evidence here. Months of silence in 311 around a \
supposed full closure on a busy corridor is a real signal.

Investigate with the tools before deciding. Follow references: if a description \
cites another permit number, look it up. Check the geometry when the location \
matters.

When you are done, reply with ONLY a JSON object:
{"verdict": "present" | "absent" | "unclear",
 "confidence": 0.0-1.0,
 "rationale": "one or two sentences",
 "evidence": ["short factual statements you actually established"]}

Use "unclear" freely. A wrong confident answer is worse than an honest \
"unclear" — this drives whether a resident gets a push notification."""


TOOLS = [
    {
        "name": "permit_narrative",
        "description": "The permit's own fields and description text, plus any other permit numbers it references.",
        "parameters": {
            "type": "object",
            "properties": {"record_id": {"type": "string"}},
            "required": ["record_id"],
        },
    },
    {
        "name": "search_311",
        "description": "311 requests near this work zone. Reports work-zone-related requests specifically, and says so when there are none.",
        "parameters": {
            "type": "object",
            "properties": {
                "record_id": {"type": "string"},
                "radius_m": {"type": "integer", "description": "default 300"},
                "days": {"type": "integer", "description": "lookback window, default 180"},
            },
            "required": ["record_id"],
        },
    },
    {
        "name": "overlapping_permits",
        "description": "Other work zone permits on the same stretch of road. Several permits at one site suggests real staged work.",
        "parameters": {
            "type": "object",
            "properties": {"record_id": {"type": "string"}},
            "required": ["record_id"],
        },
    },
    {
        "name": "find_permit",
        "description": "Look up a permit referenced inside another permit's description, e.g. '2025-032743 RW'.",
        "parameters": {
            "type": "object",
            "properties": {"reference": {"type": "string"}},
            "required": ["reference"],
        },
    },
    {
        "name": "check_geometry",
        "description": "Whether the permit's coordinates actually sit on the street it names. Use when location correctness matters.",
        "parameters": {
            "type": "object",
            "properties": {"record_id": {"type": "string"}},
            "required": ["record_id"],
        },
    },
    {
        "name": "work_calendar",
        "description": (
            "Whether a crew would plausibly be on site at this moment, given the "
            "local day and hour and any night or weekend work the permit declares. "
            "Note a closure's barricades usually stay in place even when no crew is working."
        ),
        "parameters": {
            "type": "object",
            "properties": {"record_id": {"type": "string"}},
            "required": ["record_id"],
        },
    },
    {
        "name": "recent_incidents",
        "description": "Live dispatch incidents near the site.",
        "parameters": {
            "type": "object",
            "properties": {"record_id": {"type": "string"}},
            "required": ["record_id"],
        },
    },
]


@dataclass
class Result:
    record_id: str
    verdict: str = "unclear"
    confidence: float = 0.0
    rationale: str = ""
    evidence: list[str] = field(default_factory=list)
    tools_used: list[str] = field(default_factory=list)
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error


def _parse_verdict(text: str) -> dict | None:
    """Pull the JSON object out of a reply, fences and prose included."""
    if not text:
        return None

    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    candidates = [fenced.group(1)] if fenced else []

    brace = text.find("{")
    if brace != -1:
        candidates.append(text[brace : text.rfind("}") + 1])

    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict) and "verdict" in parsed:
            return parsed
    return None


def verify(
    record_id: str,
    toolbox: ev.Toolbox,
    *,
    model: str | None = None,
    write_ledger: bool = True,
) -> Result:
    """Investigate one record and record the verdict."""
    zone = toolbox.zone(record_id)
    if not zone:
        return Result(record_id, error="unknown record id")

    result = Result(record_id)
    dispatch = {
        "permit_narrative": lambda a: toolbox.permit_narrative(a.get("record_id", record_id)),
        "search_311": lambda a: toolbox.search_311(
            a.get("record_id", record_id),
            radius_m=int(a.get("radius_m", 300)),
            days=int(a.get("days", 180)),
        ),
        "overlapping_permits": lambda a: toolbox.overlapping_permits(
            a.get("record_id", record_id)
        ),
        "find_permit": lambda a: toolbox.find_permit(a.get("reference", "")),
        "check_geometry": lambda a: toolbox.check_geometry(a.get("record_id", record_id)),
        "recent_incidents": lambda a: toolbox.recent_incidents(a.get("record_id", record_id)),
        "work_calendar": lambda a: toolbox.work_calendar(a.get("record_id", record_id)),
    }

    try:
        session = llm.GeminiSession(TOOLS, model=model, system=SYSTEM)
    except llm.LLMError as exc:
        return Result(record_id, error=str(exc))

    opening = (
        f"Verify work zone record {record_id}.\n"
        f"Road: {zone.get('road_names')}\n"
        f"Impact: {zone.get('vehicle_impact')}\n"
        f"Today: {toolbox.now.date().isoformat()}\n\n"
        "Start by reading the permit narrative."
    )

    try:
        turn = session.ask(opening)
        for _ in range(MAX_STEPS):
            if not turn.wants_tools:
                break
            if session.calls_made > MAX_TOOLS_PER_RUN:
                turn = session.ask(
                    "Tool budget reached. Give your JSON verdict now using what you have."
                )
                break

            outputs = []
            for call in turn.calls:
                handler = dispatch.get(call.name)
                result.tools_used.append(call.name)
                outputs.append(
                    (call.name, handler(call.args) if handler else {"error": "no such tool"})
                )
            turn = session.give_results(outputs)
        else:
            turn = session.ask("Give your JSON verdict now using what you have.")
    except llm.LLMError as exc:
        return Result(record_id, error=str(exc), tools_used=result.tools_used)

    parsed = _parse_verdict(turn.text)

    # A verdict cut off mid-JSON is a budget problem, not a bad answer. Ask
    # once more for the object alone, with no room for preamble.
    if not parsed and getattr(turn, "truncated", False):
        try:
            turn = session.ask(
                "Your reply was cut off. Reply with ONLY the JSON object, "
                "no preamble and no explanation."
            )
            parsed = _parse_verdict(turn.text)
        except llm.LLMError as exc:
            result.error = f"retry after truncation failed: {exc}"
            return result

    if not parsed:
        why = "reply was truncated" if getattr(turn, "truncated", False) else "unparseable reply"
        result.error = f"{why}: {turn.text[:140]}"
        return result

    verdict = str(parsed.get("verdict", "unclear")).lower().strip()
    result.verdict = verdict if verdict in ("present", "absent", "unclear") else "unclear"
    try:
        result.confidence = max(0.0, min(1.0, float(parsed.get("confidence", 0.0))))
    except (TypeError, ValueError):
        result.confidence = 0.0
    result.rationale = str(parsed.get("rationale", ""))[:400]
    raw_evidence = parsed.get("evidence")
    if isinstance(raw_evidence, list):
        result.evidence = [str(e)[:200] for e in raw_evidence[:6]]

    if write_ledger and result.verdict != "unclear":
        ledger.append(
            ledger.Claim(
                record_id=record_id,
                kind="work_zone",
                claim=result.verdict,
                source="verifier",
                observed_at=dt.datetime.now(dt.timezone.utc).isoformat(),
                detail=f"{result.confidence:.2f} | {result.rationale}",
            )
        )

    return result


def reported_records(zones: list[dict], *, now: dt.datetime) -> list[dict]:
    """The records worth spending a model call on.

    Only the tier the rules could not resolve. Confirmed and Probably-over
    already have an answer.
    """
    return [
        zone
        for zone in zones
        if conf.score_work_zone(zone, now=now).level is conf.Confidence.REPORTED
    ]
