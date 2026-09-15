"""Is a crew plausibly on site right now?

This exists because the Verifier reasoned its way to the distinction on its
own. Asked about a 30-day duct bank permit that opened two days earlier, it
answered "unclear" and said: the window is open, but today is a Sunday. That
is a real signal, it is fully deterministic, and having a model rediscover it
on every record is waste.

The important half is what this does *not* do. There are two different
questions hiding in "is this work zone real":

1. **Is the restriction in place?** Barricades, cones and a closed lane stay
   up overnight and through the weekend. For a driver this is the question
   that matters.
2. **Is a crew actively working?** That is day-and-hour dependent.

So this never suppresses a closure. A Sunday full closure is still a full
closure. What it does is add context to the brief, and stop us treating a
`are_workers_present` flag at 3am on a Sunday as strong evidence.
"""

from __future__ import annotations

import datetime as dt
import re

from .sources import _central_offset_hours, _nth_weekday

# Typical right-of-way working hours. Austin restricts arterial lane closures
# to off-peak windows, and the exact hours vary per permit, so this is a
# general-purpose prior rather than a claim about any specific permit.
WORK_START_HOUR = 7
WORK_END_HOUR = 18

# Permits that explicitly say they run outside normal hours.
ALWAYS_ON = re.compile(r"\b(24/7|24-7|around the clock|continuous(?:ly)?)\b", re.I)
NIGHT_WORK = re.compile(r"\b(night(?:\s*time|\s*work)?|overnight|after hours)\b", re.I)
WEEKEND_WORK = re.compile(r"\b(weekend|saturday|sunday)\b", re.I)

# "24/7" usually appears in these permits as an *obligation on the
# contractor*, not a work schedule: "ACCESS TO RESTAURANT MUST BE MAINTAINED
# 24/7" is a real description, and it means the opposite of continuous work.
# Requiring the phrase not to sit in an access clause avoids reading a
# driveway guarantee as a night shift.
ACCESS_CLAUSE = re.compile(
    r"\b(access|egress|entry|entrance|ingress|open|passable|maintain(?:ed|ing)?|"
    r"pedestrian|sidewalk|driveway)\b",
    re.I,
)
CLAUSE_SPLIT = re.compile(r"[.;\n]")


def to_central(moment: dt.datetime) -> dt.datetime:
    """UTC instant as naive Austin local time."""
    if moment.tzinfo is not None:
        moment = moment.astimezone(dt.timezone.utc).replace(tzinfo=None)
    # Convert by trying the offset that applies at the resulting local time.
    approximate = moment - dt.timedelta(hours=6)
    offset = _central_offset_hours(approximate)
    return moment + dt.timedelta(hours=offset)


def federal_holiday(day: dt.date) -> str | None:
    """Named US federal holiday, for the ones crews actually take off."""
    fixed = {
        (1, 1): "New Year's Day",
        (6, 19): "Juneteenth",
        (7, 4): "Independence Day",
        (11, 11): "Veterans Day",
        (12, 25): "Christmas Day",
    }
    if (day.month, day.day) in fixed:
        return fixed[(day.month, day.day)]

    year = day.year
    if day == _nth_weekday(year, 1, 0, 3):
        return "Martin Luther King Jr. Day"
    if day == _nth_weekday(year, 9, 0, 1):
        return "Labor Day"
    if day == _nth_weekday(year, 11, 3, 4):
        return "Thanksgiving"
    # Memorial Day is the LAST Monday in May.
    last_may_monday = _nth_weekday(year, 5, 0, 4)
    if last_may_monday.month != 5:
        last_may_monday = _nth_weekday(year, 5, 0, 3)
    if day == last_may_monday:
        return "Memorial Day"
    return None


def _declares_continuous_work(text: str) -> bool:
    """True only when "24/7" describes the work, not an access obligation.

    Checked clause by clause, because one description can carry both: a
    promise to keep a driveway open at all hours *and* a separate statement
    about the work schedule.
    """
    for clause in CLAUSE_SPLIT.split(text):
        if ALWAYS_ON.search(clause) and not ACCESS_CLAUSE.search(clause):
            return True
    return False


def schedule_hints(description: str | None) -> set[str]:
    """Explicit scheduling the permit narrative declares."""
    text = description or ""
    hints: set[str] = set()
    if _declares_continuous_work(text):
        hints.add("always_on")
    if NIGHT_WORK.search(text):
        hints.add("night")
    if WEEKEND_WORK.search(text):
        hints.add("weekend")
    return hints


def crew_plausible(
    now: dt.datetime, *, description: str | None = None
) -> tuple[bool, str]:
    """Would a crew plausibly be on site at this moment?

    Returns (plausible, reason). The reason is written to be shown to a
    reader, not just logged.
    """
    hints = schedule_hints(description)
    local = to_central(now)

    if "always_on" in hints:
        return True, "permit says the work runs continuously"

    holiday = federal_holiday(local.date())
    if holiday:
        return False, f"today is {holiday}"

    weekday = local.weekday()  # Monday is 0
    if weekday == 6 and "weekend" not in hints:
        return False, "it is Sunday and the permit does not mention weekend work"
    if weekday == 5 and "weekend" not in hints:
        return False, "it is Saturday and the permit does not mention weekend work"

    hour = local.hour
    if "night" in hints:
        if hour >= 20 or hour < 6:
            return True, "permit describes night work and it is night"
        return False, "permit describes night work and it is daytime"

    if hour < WORK_START_HOUR:
        return False, f"it is {local:%H:%M} local, before typical working hours"
    if hour >= WORK_END_HOUR:
        return False, f"it is {local:%H:%M} local, after typical working hours"

    return True, f"it is {local:%A} at {local:%H:%M} local, within working hours"


def activity_note(zone: dict, now: dt.datetime) -> str:
    """One phrase for the brief, or empty when a crew is plausible.

    Deliberately says the restriction is probably still in place. Cones do
    not go home on Sunday, and a reader who drives around a closure that is
    genuinely there trusts us less next time.
    """
    plausible, reason = crew_plausible(now, description=zone.get("description"))
    if plausible:
        return ""
    return f"no crew expected right now — {reason}, though any closure is likely still in place"


def discount_workers_present(zone: dict, now: dt.datetime) -> bool:
    """Should an `are_workers_present` flag be believed at this moment?

    The flag is self-reported by contractors through a check-in app and is
    not necessarily cleared when they leave. At 3am on a Sunday it is stale,
    not evidence.
    """
    plausible, _ = crew_plausible(now, description=zone.get("description"))
    return not plausible
