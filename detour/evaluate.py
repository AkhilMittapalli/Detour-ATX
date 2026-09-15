"""Does the agent earn its cost?

Every claim in this project has been that the deterministic model is the
baseline and the agent has to beat it. That claim has been asserted for a long
time without being measured, which is exactly the sort of thing that should
embarrass an engineer.

This measures three things, none of which need ground truth to accumulate
first:

1. **Does the agent change decisions at all?** If the Verifier agrees with the
   rules on every record, it is an expensive no-op and should be cut.
2. **Which way do the disagreements go?** A Verifier that only ever says
   "present" is not verifying, it is agreeing with the permit.
3. **What does a changed decision cost?** Tool calls per record tells you
   whether this runs nightly over 2,422 records or only over the 155 that can
   actually reach a blocking tier.

What it explicitly does *not* measure is correctness. That needs the resident
loop, which has no volume yet. Reporting agreement as though it were accuracy
would be the same species of dishonesty this whole project is built against.
"""

from __future__ import annotations

import datetime as dt
import json
import random
from dataclasses import dataclass, field
from pathlib import Path as FsPath

from . import confidence as conf
from . import evidence as ev
from . import sources, verifier

RESULTS = FsPath(__file__).resolve().parent.parent / "out" / "evaluation.json"

# The Verifier's verdict maps onto the rules tier like this: "absent" means
# the agent would demote the record, "present" means it corroborates it, and
# "unclear" means it declined.
AGREES = {"present": conf.Confidence.REPORTED, "absent": conf.Confidence.PROBABLY_OVER}


@dataclass
class Case:
    record_id: str
    road: str
    impact: str
    rules: str
    agent: str
    agent_confidence: float
    tools: int
    changed: bool
    rationale: str = ""
    error: str = ""


@dataclass
class Report:
    sampled: int = 0
    errors: int = 0
    cases: list[Case] = field(default_factory=list)
    population: int = 0
    slice_name: str = ""

    @property
    def usable(self) -> list[Case]:
        return [c for c in self.cases if not c.error]

    @property
    def changed(self) -> list[Case]:
        return [c for c in self.usable if c.changed]

    @property
    def by_verdict(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for case in self.usable:
            out[case.agent] = out.get(case.agent, 0) + 1
        return out

    @property
    def agreement(self) -> float:
        if not self.usable:
            return 0.0
        return 1 - len(self.changed) / len(self.usable)

    @property
    def tools_per_record(self) -> float:
        if not self.usable:
            return 0.0
        return sum(c.tools for c in self.usable) / len(self.usable)

    def verdict(self) -> str:
        """The sentence a reader actually wants."""
        if not self.usable:
            return "No usable results."
        changed = len(self.changed)
        if changed == 0:
            return (
                "The agent changed no decisions on this sample. On this slice it "
                "is an expensive no-op and should not run."
            )
        share = changed / len(self.usable)
        return (
            f"The agent changed {changed} of {len(self.usable)} decisions "
            f"({share:.0%}) at {self.tools_per_record:.1f} tool calls per record. "
            f"Those {changed} are the set worth reading by hand — agreement is "
            f"not accuracy, and only a resident sighting settles correctness."
        )


SLICES = {
    "reported-full": "Reported-tier full closures",
    "suppressed-critical": "Probably-over full closures on critical corridors",
}


def sample_population(zones: list[dict], *, now: dt.datetime, slice_name: str) -> list[dict]:
    """The records worth spending a model call on.

    Two slices, and the difference between them turned out to matter more than
    anything else this module measures.

    `reported-full` was the obvious choice: Reported-tier full closures, the
    records whose verdict decides whether someone reroutes a morning. It
    produced 100% agreement on the first run, for a structural reason. The
    rules have already moved every stale-looking record to Probably-over, so
    what remains in Reported is enriched for genuinely active work — and
    because an agent may only suppress and never authorise, a verdict of
    "present" cannot change anything. Only "absent" can, and there was
    nothing there to find.

    `suppressed-critical` inverts it: records the *rules* suppressed, on
    corridors where wrongly suppressing one is expensive. Here an agent
    finding the work is genuinely present is a rescue, and the asymmetry rule
    points the right way.
    """
    full = lambda z: (z.get("vehicle_impact") or "").strip().lower() == "all-lanes-closed"

    if slice_name == "suppressed-critical":
        return [
            z for z in zones
            if full(z)
            and _as_bool(z.get("critical_corridor"))
            and conf.score_work_zone(z, now=now).level is conf.Confidence.PROBABLY_OVER
        ]

    return [z for z in verifier.reported_records(zones, now=now) if full(z)]


def _as_bool(value) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() == "true"
    return False


def run(
    *,
    size: int = 20,
    seed: int = 11,
    slice_name: str = "reported-full",
    model: str | None = None,
    now: dt.datetime | None = None,
    progress=None,
) -> Report:
    """Verify a random sample and compare against the rules baseline."""
    now = now or sources.now_utc()
    zones = sources.fetch_work_zones()
    pool = sample_population(zones, now=now, slice_name=slice_name)
    report = Report(population=len(pool), slice_name=SLICES.get(slice_name, slice_name))
    if not pool:
        return report

    # Seeded, so a re-run measures the same records and the numbers are
    # comparable across prompt or model changes.
    chosen = random.Random(seed).sample(pool, min(size, len(pool)))
    report.sampled = len(chosen)

    toolbox = ev.Toolbox(zones, now=now)
    for index, zone in enumerate(chosen, 1):
        record_id = str(zone.get("id") or "")
        rules = conf.score_work_zone(zone, now=now)

        result = verifier.verify(record_id, toolbox, model=model, write_ledger=False)
        if progress:
            progress(index, len(chosen), zone, result)

        if not result.ok:
            report.errors += 1
            report.cases.append(
                Case(record_id, (zone.get("road_names") or "?").strip(),
                     zone.get("vehicle_impact") or "", rules.level.value,
                     "", 0.0, len(result.tools_used), False, error=result.error[:120])
            )
            continue

        implied = AGREES.get(result.verdict)
        changed = implied is not None and implied is not rules.level

        report.cases.append(
            Case(
                record_id=record_id,
                road=(zone.get("road_names") or "?").strip(),
                impact=zone.get("vehicle_impact") or "",
                rules=rules.level.value,
                agent=result.verdict,
                agent_confidence=result.confidence,
                tools=len(result.tools_used),
                changed=changed,
                rationale=result.rationale[:160],
            )
        )

    return report


def save(report: Report, *, path: FsPath | None = None) -> FsPath:
    target = path or RESULTS
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(
            {
                "slice": report.slice_name,
                "population": report.population,
                "sampled": report.sampled,
                "errors": report.errors,
                "agreement": round(report.agreement, 3),
                "changed": len(report.changed),
                "tools_per_record": round(report.tools_per_record, 2),
                "by_verdict": report.by_verdict,
                "verdict": report.verdict(),
                "cases": [c.__dict__ for c in report.cases],
            },
            indent=1,
        ),
        encoding="utf-8",
    )
    return target
