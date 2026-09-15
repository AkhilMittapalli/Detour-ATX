"""An append-only evidence ledger.

Every claim about a record — where it came from, when it was observed, and
who said it — lands here and is never mutated in place. Three reasons that
matters:

* **Ground truth.** When a resident standing at a closure says whether it is
  actually there, that is the only verified observation anyone holds. Not one
  of the 3,853 work zone records carries a city-verified position, so these
  confirmations are genuinely new information, not a correction to something
  better.
* **Audit.** "Why did you tell four thousand people this street was closed"
  needs an answer, and a superseded claim has to stay visible rather than be
  overwritten.
* **It is the substrate for the agent layer.** The Phase 3 Verifier writes
  into this file rather than returning a verdict directly, so the agents
  communicate through the ledger and the queue and never by calling each
  other.

Deliberately a JSONL file. A database is the right answer at scale, and the
wrong answer for proving the loop works.
"""

from __future__ import annotations

import datetime as dt
import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path as FsPath

LEDGER_PATH = FsPath(
    os.environ.get("DETOUR_LEDGER", FsPath(__file__).resolve().parent.parent / "ledger.jsonl")
)

# How long a resident's observation stays authoritative. Work zones change
# on a scale of days, so a two-day-old "still there" is good evidence and a
# two-week-old one is not.
GROUND_TRUTH_TTL_H = 48


@dataclass
class Claim:
    record_id: str
    kind: str          # work_zone | signal | incident
    claim: str         # present | absent | observed_state | note
    source: str        # feed | resident | verifier
    observed_at: str   # ISO 8601, UTC
    detail: str = ""

    def to_json(self) -> str:
        return json.dumps(asdict(self), separators=(",", ":"))


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def append(claim: Claim, *, path: FsPath | None = None) -> Claim:
    target = path or LEDGER_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a", encoding="utf-8") as handle:
        handle.write(claim.to_json() + "\n")
    return claim


def confirm(
    record_id: str,
    *,
    present: bool,
    kind: str = "work_zone",
    source: str = "resident",
    detail: str = "",
    path: FsPath | None = None,
) -> Claim:
    """Record that someone observed a disruption to be there, or not.

    This is the whole ground truth loop: one tap, two options, appended.
    """
    return append(
        Claim(
            record_id=record_id,
            kind=kind,
            claim="present" if present else "absent",
            source=source,
            observed_at=_now(),
            detail=detail,
        ),
        path=path,
    )


def read_all(*, path: FsPath | None = None) -> list[Claim]:
    target = path or LEDGER_PATH
    if not target.exists():
        return []

    claims: list[Claim] = []
    for line in target.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            claims.append(Claim(**json.loads(line)))
        except (json.JSONDecodeError, TypeError):
            # A malformed line must not take down the morning run.
            continue
    return claims


def by_record(*, path: FsPath | None = None) -> dict[str, list[Claim]]:
    grouped: dict[str, list[Claim]] = {}
    for claim in read_all(path=path):
        grouped.setdefault(claim.record_id, []).append(claim)
    return grouped


def latest_observation(
    record_id: str,
    *,
    now: dt.datetime,
    index: dict[str, list[Claim]] | None = None,
    path: FsPath | None = None,
    source: str = "resident",
) -> Claim | None:
    """The most recent still-fresh observation for a record, from one source.

    `source` defaults to "resident" deliberately. A person standing at the
    site and an agent reasoning about permit text are not interchangeable
    evidence, and the caller has to say which it wants rather than getting
    whichever happened to be written last.

    Returns None once the observation ages past `GROUND_TRUTH_TTL_H`, so a
    stale confirmation quietly stops overriding the feed instead of pinning
    a verdict forever.
    """
    claims = (index or by_record(path=path)).get(record_id, [])
    observations = [
        c for c in claims if c.claim in ("present", "absent") and c.source == source
    ]
    if not observations:
        return None

    def when(claim: Claim) -> dt.datetime:
        try:
            parsed = dt.datetime.fromisoformat(claim.observed_at)
        except ValueError:
            return dt.datetime.min.replace(tzinfo=dt.timezone.utc)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.timezone.utc)

    newest = max(observations, key=when)
    age_h = (now - when(newest)).total_seconds() / 3600
    return newest if age_h <= GROUND_TRUTH_TTL_H else None


def accuracy(*, path: FsPath | None = None) -> dict[str, int]:
    """Tally resident observations — the numerator of the eventual metric.

    Once this has volume it answers the question the city cannot: what share
    of active right-of-way permits describe work that is really happening.
    """
    present = absent = 0
    for claim in read_all(path=path):
        if claim.source != "resident":
            continue
        if claim.claim == "present":
            present += 1
        elif claim.claim == "absent":
            absent += 1
    return {"present": present, "absent": absent, "total": present + absent}
