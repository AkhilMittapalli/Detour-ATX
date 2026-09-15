"""How long does Austin actually take to close a 311 request?

The archive holds 2,542,175 requests and 2,529,210 of them carry a close
date — 99.5% complete over twelve years. That gives a per-type median close
time for free, which is the only honest yardstick this project has for two
different questions:

* **For the Advocate.** Telling someone "this usually takes 6 days, yours is
  on day 14" is worth more than any amount of sympathetic phrasing.
* **For evaluation.** If agent-drafted requests close faster than the
  historical median for their type, that is a real outcome measured against
  real data rather than a screenshot of agents talking.

Percentiles are computed server-side where Socrata allows it and locally
otherwise, because SoQL has no median aggregate.
"""

from __future__ import annotations

import datetime as dt
import json
import statistics
from dataclasses import dataclass
from pathlib import Path as FsPath

from . import sources

REQUESTS = "xwdj-i9he"
CACHE = FsPath(__file__).resolve().parent.parent / ".cache" / "baseline.json"

# Enough to be stable without pulling the whole archive for one type.
SAMPLE_SIZE = 5000

# Types this product plausibly drafts a request about.
RELEVANT_TYPES = [
    "TPW - Activate/Deactivate Work Zone",
    "TPW - Construction Concerns in Right of Way",
    "TPW - Traffic Signal - Maintenance",
    "Traffic Signal - Maintenance",
    "Obstruction in ROW",
    "Lane/Road Closure Notification",
    "Sidewalk Repair",
    "Pothole Repair",
    "Sign - Traffic Sign Maintenance",
]


@dataclass
class Stat:
    request_type: str
    sampled: int
    median_days: float
    p90_days: float
    still_open_share: float

    @property
    def auto_closed(self) -> bool:
        """Is this a notification that is stamped closed, not a serviced queue?

        Several 311 "types" are internal notifications rather than requests
        for work. `TPW - Activate/Deactivate Work Zone` closes at a 0.0 day
        median *and* a 0.0 day p90 across 1,898 records: nothing is being
        waited on, the record is opened and closed in the same motion.

        This matters because judging a real request against such a baseline
        produces nonsense — an early version reported a one-day-old signal
        request as "slower than 90% of comparable requests", which would be a
        humiliating thing to put in front of a city department.
        """
        return self.p90_days < 1.0

    def describe(self) -> str:
        if self.auto_closed:
            return (
                f"{self.request_type}: closed on creation "
                f"(median and p90 both under a day, n={self.sampled:,}) — a "
                f"notification rather than a serviced queue"
            )
        return (
            f"{self.request_type}: median {self.median_days:.1f} days, "
            f"p90 {self.p90_days:.0f} days (n={self.sampled:,})"
        )

    def standing(self, age_days: float) -> str:
        """Where one request sits against the historical distribution."""
        if self.auto_closed:
            return (
                "no meaningful wait time to compare against — requests of this "
                "type are closed on creation"
            )
        if age_days <= self.median_days:
            return f"within the usual {self.median_days:.1f}-day median"
        if age_days <= self.p90_days:
            return (
                f"past the {self.median_days:.1f}-day median, "
                f"but inside the {self.p90_days:.0f}-day p90"
            )
        return (
            f"beyond the {self.p90_days:.0f}-day p90 — slower than 90% of "
            f"comparable requests"
        )


def _days(created: str | None, closed: str | None) -> float | None:
    start = sources.parse_ts(created, naive_is_central=True)
    end = sources.parse_ts(closed, naive_is_central=True)
    if not start or not end:
        return None
    delta = (end - start).total_seconds() / 86400
    # Negative or absurd spans are data errors, not fast service.
    return delta if 0 <= delta <= 3650 else None


def measure(request_type: str, *, sample: int = SAMPLE_SIZE, use_cache=True) -> Stat | None:
    """Close-time distribution for one request type."""
    rows = sources.soda(
        REQUESTS,
        {
            "$select": "sr_created_date,sr_closed_date,sr_status_desc",
            "$where": f"sr_type_desc='{request_type}'",
            "$order": "sr_created_date DESC",
            "$limit": sample,
        },
        use_cache=use_cache,
    )
    if not rows:
        return None

    spans = [d for r in rows if (d := _days(r.get("sr_created_date"), r.get("sr_closed_date"))) is not None]
    if len(spans) < 20:
        return None

    open_share = sum(
        1 for r in rows if (r.get("sr_status_desc") or "").strip().lower() in ("open", "new", "in progress")
    ) / len(rows)

    spans.sort()
    return Stat(
        request_type=request_type,
        sampled=len(spans),
        median_days=statistics.median(spans),
        p90_days=spans[min(len(spans) - 1, int(0.9 * len(spans)))],
        still_open_share=open_share,
    )


def table(types: list[str] | None = None, *, refresh: bool = False) -> dict[str, Stat]:
    """Baselines for every relevant type, cached on disk.

    The archive does not move quickly, so this is cached separately from the
    five-minute feed cache and only recomputed on request.
    """
    wanted = types or RELEVANT_TYPES

    if not refresh and CACHE.exists():
        try:
            raw = json.loads(CACHE.read_text(encoding="utf-8"))
            cached = {k: Stat(**v) for k, v in raw.items()}
            if all(t in cached for t in wanted):
                return {t: cached[t] for t in wanted}
        except (json.JSONDecodeError, TypeError):
            pass

    out: dict[str, Stat] = {}
    for request_type in wanted:
        stat = measure(request_type)
        if stat:
            out[request_type] = stat

    CACHE.parent.mkdir(parents=True, exist_ok=True)
    CACHE.write_text(
        json.dumps({k: v.__dict__ for k, v in out.items()}, indent=1), encoding="utf-8"
    )
    return out


def for_type(request_type: str) -> Stat | None:
    return table().get(request_type)
