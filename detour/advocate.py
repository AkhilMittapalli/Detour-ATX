"""The Advocate — drafting a well-evidenced civic report.

Residents file roughly 99,000 traffic-signal requests and thousands of
right-of-way complaints, mostly badly formed: no permit number, no duration,
no citation, no specific ask. A department that receives "the light is broken"
has to go and find everything this project already knows.

So this drafts. It assembles the evidence already in the ledger and the feeds
into a report someone can read, check and send.

**It has no send path, and that is deliberate.** There is no transport here,
no flag that enables filing, nothing to misconfigure. `transport.py` exists
and is wired to briefs; this module cannot reach it. Automatically filing
government service requests at scale is abuse regardless of intent, and it
would poison both the 311 system and the dataset this product depends on. The
value is a *better-formed* request, not more requests.

The draft is written to a file. A person reads it, decides, and files it
themselves.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from pathlib import Path as FsPath

from . import baseline, cluster, confidence as conf, geo, llm, workcal

DRAFTS_DIR = FsPath(__file__).resolve().parent.parent / "drafts"

# One person should not be drafting dozens of reports in a sitting. This is a
# courtesy limit on the human, not a technical one.
MAX_DRAFTS_PER_RUN = 5

SYSTEM = """You draft short, factual reports for Austin's 311 system on behalf \
of a resident.

Rules:
- Open with the specific problem and where it is. No preamble, no pleasantries.
- State only facts you are given. Never invent a date, a permit number or an \
observation.
- Cite the evidence: permit numbers, dataset names, durations.
- Close with one concrete ask.
- Under 130 words. Plain, civil, unemotional. No outrage, no rhetorical \
questions, no demands.

You are writing something a city employee will read in fifteen seconds and be \
able to act on."""


@dataclass
class Draft:
    kind: str                       # stale_permit | dark_signal | bad_geometry
    subject: str
    body: str
    evidence: list[str] = field(default_factory=list)
    suggested_type: str = ""
    baseline_note: str = ""
    generated: bool = False         # True when a model wrote the prose
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error

    def render(self) -> str:
        lines = [
            "DETOUR ATX - DRAFT 311 REPORT",
            "This is a draft. Nothing has been filed. Read it, edit it, and",
            "submit it yourself at austintexas.gov/311 or by calling 3-1-1.",
            "",
            f"Suggested request type: {self.suggested_type or 'unsure'}",
            f"Subject: {self.subject}",
            "",
            self.body,
            "",
            "Evidence",
        ]
        lines += [f"  - {e}" for e in self.evidence]
        if self.baseline_note:
            lines += ["", f"Typical handling time: {self.baseline_note}"]
        lines += [
            "",
            "Sources: City of Austin open data portal - Roadway Work Zones",
            "(qyfh-gwei), Traffic Signals Status (5zpr-dehc), 311 Unified",
            "Service Requests (xwdj-i9he).",
        ]
        return "\n".join(lines)


def _compose(facts: str, fallback: str, *, enabled: bool = True) -> tuple[str, bool]:
    """Turn assembled facts into prose, or fall back to a plain template.

    The fallback is not a degraded mode. A blunt, correct paragraph is a
    perfectly good 311 report, and a report that never gets written because a
    model was unreachable is worth nothing.
    """
    if not enabled or llm.provider() is None:
        return fallback, False

    reply = llm.complete(f"{SYSTEM}\n\nFacts:\n{facts}\n\nWrite the report body.",
                         max_tokens=2048)
    if not reply or len(reply.strip()) < 60:
        return fallback, False
    return reply.strip(), True


def for_stale_permit(zone: dict, *, now: dt.datetime, enabled: bool = True) -> Draft:
    """A permit that is still listed as active when the work looks finished."""
    verdict = conf.score_work_zone(zone, now=now)
    road = (zone.get("road_names") or "an Austin street").strip()
    name = (zone.get("name") or "").strip()
    ends = zone["_end"].date().isoformat() if zone.get("_end") else "no end date"
    window = (
        (zone["_end"] - zone["_start"]).days
        if zone.get("_start") and zone.get("_end") else None
    )

    evidence = [
        f"Permit: {name or 'unnamed'}",
        f"Road as recorded: {road}",
        f"Listed impact: {zone.get('vehicle_impact') or 'unknown'}",
        f"Permit window ends {ends}" + (f" ({window} day window)" if window else ""),
    ]
    evidence += [f"Automated assessment: {r}" for r in verdict.reasons[:3]]

    facts = (
        f"A right-of-way permit on {road} is still published as active in the "
        f"city's Roadway Work Zones dataset. Its window runs to {ends}"
        + (f", a {window}-day window. " if window else ". ")
        + "Automated review suggests the work is finished or was never started: "
        + "; ".join(verdict.reasons[:3])
        + ". The record still appears to drivers and to any service built on this feed."
    )
    fallback = (
        f"The Roadway Work Zones dataset still lists an active permit on {road} "
        f"(window ending {ends}). The record's own details suggest the work is "
        f"complete or not yet started: {'; '.join(verdict.reasons[:2])}. "
        f"Please confirm whether this permit should be closed out so the public "
        f"feed reflects conditions on the ground."
    )
    body, generated = _compose(facts, fallback, enabled=enabled)

    stat = baseline.for_type("TPW - Construction Concerns in Right of Way")
    return Draft(
        kind="stale_permit",
        subject=f"Possibly stale right-of-way permit on {road}",
        body=body,
        evidence=evidence,
        suggested_type="TPW - Construction Concerns in Right of Way",
        baseline_note=stat.describe() if stat else "",
        generated=generated,
    )


def for_dark_signal(
    signal: dict, *, now: dt.datetime, swept: set | None = None, enabled: bool = True
) -> Draft:
    """A signal the city has had no telemetry on for a very long time."""
    where = (signal.get("location_name") or "an intersection").strip()
    state = (signal.get("operation_text") or "degraded").strip()
    onset_known = cluster.onset_is_real(signal, swept or set())
    since = signal.get("_since")

    if onset_known and since:
        days = (now - since).days
        duration = f"since {since.date().isoformat()} ({days} days)"
    else:
        # Saying "11 hours" about a signal down for years would be worse than
        # saying nothing, and the nightly sweep makes that easy to do.
        duration = (
            "for an unknown period — the published timestamp was rewritten by "
            "the overnight batch, so the true onset is older than it appears"
        )

    evidence = [
        f"Intersection: {where}",
        f"Published state: {state}",
        f"Unreachable {duration}",
        "Source: Traffic Signals Status (5zpr-dehc), an exception table listing "
        "only signals in a non-normal state",
    ]

    facts = (
        f"The intersection at {where} appears in the city's Traffic Signals "
        f"Status feed with the state '{state}', {duration}. A communication "
        f"issue means the central system has no telemetry to the cabinet: the "
        f"signal is likely still cycling on its local timer, but it cannot be "
        f"monitored or retimed remotely during an incident."
    )
    fallback = (
        f"{where} is listed in the Traffic Signals Status dataset as "
        f"'{state}', {duration}. The signal itself may be operating on its "
        f"local timer, but the city has no remote visibility of it. Please "
        f"confirm whether communications to this cabinet can be restored."
    )
    body, generated = _compose(facts, fallback, enabled=enabled)

    stat = baseline.for_type("TPW - Traffic Signal - Maintenance")
    return Draft(
        kind="dark_signal",
        subject=f"Traffic signal communications: {where}",
        body=body,
        evidence=evidence,
        suggested_type="TPW - Traffic Signal - Maintenance",
        baseline_note=stat.describe() if stat else "",
        generated=generated,
    )


def for_bad_geometry(zone: dict, found_on: list[str], *, enabled: bool = True) -> Draft:
    """A permit whose coordinates sit on a different street than it names."""
    road = (zone.get("road_names") or "an unnamed street").strip()
    actual = ", ".join(found_on[:3]) or "unnamed roads"
    name = (zone.get("name") or "").strip()

    evidence = [
        f"Permit: {name or 'unnamed'}",
        f"Road named on the permit: {road}",
        f"Streets under the published geometry: {actual}",
        "No record in this dataset carries a verified position "
        "(is_start_position_verified is false on all 3,853)",
    ]
    facts = (
        f"A work zone record names {road} but its published geometry sits on "
        f"{actual}. Anything routing around this closure would remove the "
        f"wrong street from the network."
    )
    fallback = (
        f"A record in the Roadway Work Zones dataset names {road}, but its "
        f"coordinates fall on {actual}. Please check the geometry on this "
        f"permit; routing tools built on this feed would divert traffic around "
        f"the wrong street."
    )
    body, generated = _compose(facts, fallback, enabled=enabled)

    return Draft(
        kind="bad_geometry",
        subject=f"Work zone geometry does not match named street ({road})",
        body=body,
        evidence=evidence,
        suggested_type="TPW - Construction Concerns in Right of Way",
        generated=generated,
    )


def save(draft: Draft, *, directory: FsPath | None = None, stamp: dt.datetime | None = None) -> FsPath:
    """Write a draft to disk for a human to read. Nothing is filed."""
    folder = directory or DRAFTS_DIR
    folder.mkdir(parents=True, exist_ok=True)
    when = workcal.to_central(stamp or dt.datetime.now(dt.timezone.utc))
    slug = "".join(c if c.isalnum() else "-" for c in draft.subject.lower())[:52].strip("-")
    target = folder / f"{when:%Y-%m-%d}-{draft.kind}-{slug}.txt"
    target.write_text(draft.render(), encoding="utf-8")
    return target


def candidates(zones: list[dict], signals: list[dict], *, now: dt.datetime) -> dict:
    """Things a resident might reasonably report, worst first."""
    stale = [
        z for z in zones
        if conf.score_work_zone(z, now=now).level is conf.Confidence.PROBABLY_OVER
        and (z.get("vehicle_impact") or "").strip().lower() == "all-lanes-closed"
    ]

    swept = cluster.batch_timestamps(signals)
    dark = [
        s for s in signals
        if "communication" in (s.get("operation_text") or "").lower()
        and cluster.onset_is_real(s, swept)
        and s.get("_since")
        and (now - s["_since"]).days > 365
    ]
    dark.sort(key=lambda s: s["_since"])

    return {"stale_permits": stale, "dark_signals": dark, "swept": swept}
