"""Socrata clients for the four City of Austin feeds Phase 1 reads.

Two things here are less boring than they look:

1. **Timestamps are not consistently zoned.** Work zones and incidents come
   back as UTC with a trailing `Z`. Signal status comes back as a naive local
   Central time with no offset at all. Mixing them silently produces a
   five-hour error, which in this product means telling someone a signal has
   been flashing since before it actually was.

2. **Default ordering is not newest-first.** Every query that cares about
   recency says so explicitly.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path as FsPath
from typing import Any

DOMAIN = "https://data.austintexas.gov"

WORK_ZONES = "qyfh-gwei"
SIGNALS = "5zpr-dehc"
INCIDENTS = "dx9v-zd7x"
CENTERLINE = "8hf2-pdmb"
BIKE_FACILITIES = "23hw-a95n"

CACHE_DIR = FsPath(__file__).resolve().parent.parent / ".cache"
CACHE_TTL_S = 300

UTC = dt.timezone.utc


# --------------------------------------------------------------------------
# Central time without a tzdata dependency
# --------------------------------------------------------------------------

def _nth_weekday(year: int, month: int, weekday: int, n: int) -> dt.date:
    """The nth (1-based) `weekday` of a month. Monday is 0."""
    first = dt.date(year, month, 1)
    offset = (weekday - first.weekday()) % 7
    return first + dt.timedelta(days=offset + 7 * (n - 1))


def _central_offset_hours(naive: dt.datetime) -> int:
    """-5 during CDT, -6 during CST.

    US rule since 2007: DST runs from the second Sunday in March to the first
    Sunday in November, switching at 02:00 local. Windows ships no IANA
    database, so we compute this rather than depend on `tzdata`.
    """
    year = naive.year
    start = dt.datetime.combine(_nth_weekday(year, 3, 6, 2), dt.time(2, 0))
    end = dt.datetime.combine(_nth_weekday(year, 11, 6, 1), dt.time(2, 0))
    return -5 if start <= naive < end else -6


def parse_ts(value: str | None, *, naive_is_central: bool) -> dt.datetime | None:
    """Parse a Socrata timestamp into an aware UTC datetime.

    `naive_is_central` says how to read a value that carries no offset. Get
    this wrong and every duration in the brief is off by five or six hours.
    """
    if not value:
        return None

    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = dt.datetime.fromisoformat(text)
    except ValueError:
        return None

    if parsed.tzinfo is not None:
        return parsed.astimezone(UTC)

    if naive_is_central:
        offset = _central_offset_hours(parsed)
        return parsed.replace(tzinfo=dt.timezone(dt.timedelta(hours=offset))).astimezone(UTC)
    return parsed.replace(tzinfo=UTC)


def now_utc() -> dt.datetime:
    return dt.datetime.now(UTC)


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------

def _cache_path(dataset: str, params: dict[str, Any]) -> FsPath:
    key = urllib.parse.urlencode(sorted(params.items()))
    digest = str(abs(hash((dataset, key))))
    return CACHE_DIR / f"{dataset}-{digest}.json"


def soda(
    dataset: str,
    params: dict[str, Any] | None = None,
    *,
    use_cache: bool = True,
    timeout: int = 60,
) -> list[dict]:
    """GET one SODA resource. Returns the decoded rows."""
    params = dict(params or {})
    params.setdefault("$limit", 50000)

    cached = _cache_path(dataset, params)
    if use_cache and cached.exists() and time.time() - cached.stat().st_mtime < CACHE_TTL_S:
        return json.loads(cached.read_text(encoding="utf-8"))

    url = f"{DOMAIN}/resource/{dataset}.json?" + urllib.parse.urlencode(params)
    request = urllib.request.Request(url, headers={"Accept": "application/json"})

    token = os.environ.get("SOCRATA_APP_TOKEN")
    if token:
        request.add_header("X-App-Token", token)

    last_error: Exception | None = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                rows = json.loads(response.read().decode("utf-8"))
            break
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            last_error = exc
            time.sleep(1.5 * (attempt + 1))
    else:
        raise RuntimeError(f"{dataset}: request failed after 3 attempts") from last_error

    if not isinstance(rows, list):
        raise RuntimeError(f"{dataset}: unexpected response {str(rows)[:200]}")

    if use_cache:
        CACHE_DIR.mkdir(exist_ok=True)
        cached.write_text(json.dumps(rows), encoding="utf-8")
    return rows


# --------------------------------------------------------------------------
# Feeds
# --------------------------------------------------------------------------

def fetch_work_zones(**kw) -> list[dict]:
    """All roadway work zones, with dates normalised to UTC.

    We fetch the whole set rather than filtering server-side because the
    confidence model needs to see permit windows that have already closed in
    order to recognise the stale ones.
    """
    rows = soda(WORK_ZONES, **kw)
    for row in rows:
        row["_start"] = parse_ts(row.get("start_date"), naive_is_central=False)
        row["_end"] = parse_ts(row.get("end_date"), naive_is_central=False)
    return rows


def fetch_signals(**kw) -> list[dict]:
    """Signals currently in a non-normal state.

    This feed is an exception table, not a roster: every row is a signal the
    city considers degraded. Timestamps are naive Central.
    """
    rows = soda(SIGNALS, **kw)
    for row in rows:
        row["_since"] = parse_ts(row.get("operation_state_datetime"), naive_is_central=True)
        row["_processed"] = parse_ts(row.get("processed_datetime"), naive_is_central=True)
    return rows


def fetch_active_incidents(**kw) -> list[dict]:
    """Only incidents the dispatch feed still marks ACTIVE."""
    params = {
        "traffic_report_status": "ACTIVE",
        "$order": "published_date DESC",
        "$limit": 500,
    }
    rows = soda(INCIDENTS, params, **kw)
    for row in rows:
        row["_published"] = parse_ts(row.get("published_date"), naive_is_central=False)
    return rows


BIKE_FIELDS = (
    "the_geom,bicycle_facility,bike_level_of_comfort,line_type,"
    "full_street_name,rec_bicycle_aaanetwork"
)


def fetch_bike_facilities_near(
    box: tuple[float, float, float, float], **kw
) -> list[dict]:
    """Bike infrastructure inside a bounding box.

    17,753 rows citywide, and the whole layer is an export of the
    Comprehensive Transportation Network — two thirds of it is ordinary
    street with a comfort rating attached. Filtering to real facilities is
    `bike.index_facilities`'s job, not the server's, because the comfort
    rating on an unrated street is still worth reading when we route.
    """
    min_lon, min_lat, max_lon, max_lat = box
    params = {
        "$select": BIKE_FIELDS,
        "$where": f"within_box(the_geom, {max_lat}, {min_lon}, {min_lat}, {max_lon})",
        "$limit": 20000,
    }
    return soda(BIKE_FACILITIES, params, **kw)


def fetch_centerline_near(
    box: tuple[float, float, float, float], **kw
) -> list[dict]:
    """Street centreline segments inside a bounding box.

    The full layer is 68,717 segments. A route only ever needs the handful
    around it, so we push the filter to the server with `within_box`, whose
    argument order is (north lat, west lon, south lat, east lon).
    """
    min_lon, min_lat, max_lon, max_lat = box
    params = {
        "$where": f"within_box(the_geom, {max_lat}, {min_lon}, {min_lat}, {max_lon})",
        "$limit": 5000,
    }
    return soda(CENTERLINE, params, **kw)
