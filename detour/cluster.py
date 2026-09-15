"""Group today's disruptions into events.

This is arithmetic, deliberately. Deciding that four signals which dropped at
the same minute on adjacent corners are one event is a distance-and-time
test, and a model adding latency and non-determinism to it would buy nothing.
Interpreting *why* they dropped is the part that branches, and that lives in
`editor.py`.

The split matters for the product, not just the architecture. A reader whose
route crosses all four should get one line about a corridor, not four alerts
that look like four separate problems.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

from . import geo

# Signals on one corridor sit a few hundred metres apart; a comms hub or a
# cut can take out a run of them. Fifteen minutes is generous for a single
# cause propagating through a network.
SIGNAL_RADIUS_M = 1800.0
SIGNAL_WINDOW_MIN = 15.0

# A timestamp shared to the minute is a much stronger signal than mere
# proximity, so it widens the radius.
EXACT_TIME_RADIUS_M = 4000.0

# --- the nightly sweep -----------------------------------------------------
#
# Most "communication issue" rows do not carry the moment the fault began.
# They carry the moment a nightly batch last recorded it. On one observed
# day, 90 of 131 degraded signals shared the timestamp 09:00:59 UTC — 04:00
# local, every one with `second == 59`, spread across a 26 km bounding box
# from FM 2222 to Mueller to Southwest Parkway.
#
# Two consequences, and the second is the one that matters for the product:
#
# 1. Clustering on such a timestamp chains most of the city into one
#    meaningless "event".
# 2. Any duration derived from it is wrong. A signal unreachable for years
#    reports as unreachable for hours, because the sweep restamped it this
#    morning.
#
# Detection is deliberately about shape rather than a hardcoded hour: many
# signals, at one instant, spread far wider than any single cause reaches.
BATCH_MIN_MEMBERS = 8
BATCH_MIN_DIAMETER_M = 10_000.0


@dataclass
class Event:
    """One or more disruptions that plausibly share a cause."""

    kind: str                       # signal | work_zone
    members: list[dict] = field(default_factory=list)
    label: str = ""
    onset_known: bool = True        # False when a sweep restamped the members

    @property
    def size(self) -> int:
        return len(self.members)

    @property
    def is_cluster(self) -> bool:
        return self.size > 1

    @property
    def points(self) -> list[geo.Point]:
        out: list[geo.Point] = []
        for member in self.members:
            out.extend(geo.coords_of(member.get("location") or member.get("geometry")))
        return out

    @property
    def centroid(self) -> geo.Point | None:
        points = self.points
        if not points:
            return None
        return (
            sum(p[0] for p in points) / len(points),
            sum(p[1] for p in points) / len(points),
        )

    @property
    def started(self) -> dt.datetime | None:
        times = [m["_since"] for m in self.members if m.get("_since")]
        return min(times) if times else None

    @property
    def spread_minutes(self) -> float:
        """Wall-clock spread between the first and last member.

        Zero means every member changed state in the same minute, which is
        the strongest available hint at a single upstream cause.
        """
        times = sorted(m["_since"] for m in self.members if m.get("_since"))
        if len(times) < 2:
            return 0.0
        return (times[-1] - times[0]).total_seconds() / 60.0

    @property
    def states(self) -> set[str]:
        return {(m.get("operation_text") or "").strip() for m in self.members}

    def describe(self) -> str:
        where = sorted(
            (m.get("location_name") or "").strip() for m in self.members if m.get("location_name")
        )
        state = "/".join(sorted(s for s in self.states if s)) or "degraded"
        if not self.is_cluster:
            return f"{where[0] if where else 'A signal'} — {state}"
        return f"{self.size} signals {state.lower()} within {self.spread_minutes:.0f} min"


class _Union:
    """Minimal union-find for single-linkage grouping."""

    def __init__(self, size: int):
        self.parent = list(range(size))

    def find(self, index: int) -> int:
        while self.parent[index] != index:
            self.parent[index] = self.parent[self.parent[index]]
            index = self.parent[index]
        return index

    def join(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[rb] = ra


def _point_of(record: dict) -> geo.Point | None:
    points = geo.coords_of(record.get("location") or record.get("geometry"))
    return points[0] if points else None


def _diameter_m(records: list[dict]) -> float:
    points = [p for r in records if (p := _point_of(r))]
    if len(points) < 2:
        return 0.0
    lons = [p[0] for p in points]
    lats = [p[1] for p in points]
    return geo.haversine_m((min(lons), min(lats)), (max(lons), max(lats)))


def batch_timestamps(signals: list[dict]) -> set[dt.datetime]:
    """Timestamps that are a system sweep rather than a real event.

    See the note above BATCH_MIN_MEMBERS. Returning the offending instants
    lets callers both skip them when clustering and refuse to quote a
    duration derived from them.
    """
    by_instant: dict[dt.datetime, list[dict]] = {}
    for signal in signals:
        when = signal.get("_since")
        if when:
            by_instant.setdefault(when, []).append(signal)

    return {
        when
        for when, members in by_instant.items()
        if len(members) >= BATCH_MIN_MEMBERS
        and _diameter_m(members) >= BATCH_MIN_DIAMETER_M
    }


def onset_is_real(signal: dict, batch: set[dt.datetime]) -> bool:
    """Is this signal's timestamp the moment the fault began?

    False when it was restamped by a sweep, in which case the true onset is
    unknown and older than it appears.
    """
    return signal.get("_since") not in batch


def _linked(a: dict, b: dict) -> bool:
    """Do two signal failures plausibly share a cause?

    Same state, close in space, close in time. An exact timestamp match
    widens the radius: four cabinets reporting at the same minute is not a
    coincidence even when they are a kilometre apart.
    """
    if (a.get("operation_text") or "") != (b.get("operation_text") or ""):
        return False

    pa, pb = _point_of(a), _point_of(b)
    if not pa or not pb:
        return False

    ta, tb = a.get("_since"), b.get("_since")
    if not ta or not tb:
        return False

    gap_min = abs((ta - tb).total_seconds()) / 60.0
    distance = geo.haversine_m(pa, pb)

    if gap_min < 1.0:
        return distance <= EXACT_TIME_RADIUS_M
    return gap_min <= SIGNAL_WINDOW_MIN and distance <= SIGNAL_RADIUS_M


def cluster_signals(signals: list[dict]) -> list[Event]:
    """Group degraded signals into events, newest first.

    Single-linkage: A joins B, B joins C, so A B and C are one event even if
    A and C are far apart. That is the right shape for a fault propagating
    along a corridor — and exactly why the nightly sweep has to be excluded
    first, or it chains most of the city into one meaningless event.
    """
    batch = batch_timestamps(signals)
    usable = [
        s
        for s in signals
        if s.get("_since") and _point_of(s) and s["_since"] not in batch
    ]
    if not usable:
        return []

    union = _Union(len(usable))
    for i in range(len(usable)):
        for j in range(i + 1, len(usable)):
            if _linked(usable[i], usable[j]):
                union.join(i, j)

    grouped: dict[int, list[dict]] = {}
    for index, record in enumerate(usable):
        grouped.setdefault(union.find(index), []).append(record)

    events = [Event(kind="signal", members=members) for members in grouped.values()]
    for event in events:
        event.label = event.describe()

    events.sort(key=lambda e: (e.started or dt.datetime.min), reverse=True)
    return events


def notable(events: list[Event], *, now: dt.datetime, within_hours: float = 36.0) -> list[Event]:
    """Events worth a human looking at.

    A multi-signal cluster is always notable. A lone failure is only notable
    while it is fresh — the 128 communication issues sitting in this feed
    include one unreachable since July 2021, and re-reporting it every
    morning is how a brief becomes wallpaper.
    """
    out: list[Event] = []
    for event in events:
        started = event.started
        fresh = started is not None and (now - started).total_seconds() / 3600 <= within_hours
        if event.is_cluster or fresh:
            out.append(event)
    return out
