"""What is worth telling this reader today?

A closure reported for ninety straight mornings stops being information and
becomes wallpaper. That same closure quietly slipping its end date by three
months is news again. Deciding between those is what this does.

It is rules, not an agent, and that is a deliberate call against my own
earlier sketch. The test I have applied to every other role is whether the
evidence path branches — whether which question you ask next depends on the
last answer. Here it does not. "Has this reader been told, and has anything
material changed since" is a lookup and a comparison. An agent would add
latency, cost and non-determinism to a fingerprint diff.

The memory lives in the same evidence ledger as everything else, so a reader's
history is inspectable and a brief can be explained after the fact.
"""

from __future__ import annotations

import datetime as dt
import hashlib
from dataclasses import dataclass
from enum import Enum

from . import ledger

# Below this, a repeat is suppressed unless something changed.
QUIET_DAYS = 6

# Past this, a long-running closure is surfaced again briefly, because a
# reader who has been away needs the context back.
RESURFACE_DAYS = 21

# An end date moving by less than this is schedule noise, not news.
MATERIAL_DATE_SHIFT_DAYS = 7


class News(str, Enum):
    NEW = "new"                 # never told
    CHANGED = "changed"         # told, but something material moved
    RESURFACED = "resurfaced"   # told long ago, still going
    REPEAT = "repeat"           # told recently, nothing changed


@dataclass
class Decision:
    news: News
    reason: str = ""

    @property
    def tell(self) -> bool:
        """Repeats stay out of the body and go in the standing footer."""
        return self.news is not News.REPEAT


def item_key(item) -> str:
    """A stable identity for a disruption across days.

    Record ids are stable for work zones. Signals and incidents have none, so
    the headline is hashed — imperfect if wording changes, which is why the
    rewrite is cached rather than regenerated per run.
    """
    if getattr(item, "record_id", ""):
        return f"wz:{item.record_id}"
    digest = hashlib.sha1(item.headline.encode("utf-8")).hexdigest()[:16]
    return f"{item.kind}:{digest}"


def fingerprint(item) -> str:
    """The parts of an item whose change is worth a reader's attention."""
    return "|".join(
        [
            item.verdict.label,
            str(int(item.tier)),
            item.headline.split(", through ")[-1] if ", through " in item.headline else "",
        ]
    )


def _parse(value: str) -> dt.datetime | None:
    try:
        parsed = dt.datetime.fromisoformat(value)
    except (ValueError, TypeError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.timezone.utc)


def _history(reader: str, key: str, index: dict) -> list[ledger.Claim]:
    return [c for c in index.get(f"{reader}#{key}", []) if c.claim == "told"]


def assess(item, *, reader: str, now: dt.datetime, index: dict) -> Decision:
    """Decide how newsworthy one item is for one reader."""
    key = item_key(item)
    told = _history(reader, key, index)

    if not told:
        return Decision(News.NEW)

    latest = max(told, key=lambda c: c.observed_at)
    when = _parse(latest.observed_at)
    age_days = (now - when).days if when else 999

    if latest.detail != fingerprint(item):
        before, after = latest.detail.split("|"), fingerprint(item).split("|")
        if before[0] != after[0]:
            return Decision(News.CHANGED, f"confidence moved from {before[0]} to {after[0]}")
        if before[1] != after[1]:
            return Decision(News.CHANGED, "severity changed")
        if before[2] != after[2] and after[2]:
            return Decision(News.CHANGED, f"end date moved to {after[2]}")
        return Decision(News.CHANGED, "details changed")

    if age_days >= RESURFACE_DAYS:
        return Decision(News.RESURFACED, f"still going, last mentioned {age_days} days ago")
    if age_days < QUIET_DAYS:
        return Decision(News.REPEAT, f"told {age_days} day(s) ago, nothing changed")
    return Decision(News.RESURFACED, f"last mentioned {age_days} days ago")


def curate(items, *, reader: str, now: dt.datetime, path=None):
    """Split a brief's items into what to tell and what to hold back.

    Returns (tell, held) where each entry is (item, Decision).
    """
    index = ledger.by_record(path=path)
    tell, held = [], []
    for item in items:
        decision = assess(item, reader=reader, now=now, index=index)
        (tell if decision.tell else held).append((item, decision))
    return tell, held


def record_told(items, *, reader: str, now: dt.datetime, path=None) -> int:
    """Remember what this reader was shown, so tomorrow can compare."""
    written = 0
    for item in items:
        ledger.append(
            ledger.Claim(
                record_id=f"{reader}#{item_key(item)}",
                kind="brief",
                claim="told",
                source="correspondent",
                observed_at=now.isoformat(),
                detail=fingerprint(item),
            ),
            path=path,
        )
        written += 1
    return written
