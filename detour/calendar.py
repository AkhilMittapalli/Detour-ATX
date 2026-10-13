"""The brief as a calendar event.

The third surface, and the one that asks least of anyone. A notification
needs you to read it; an app needs you to open it. A calendar entry
arrives inside a habit that already exists — you are going to look at
tomorrow morning anyway, and the alert fires before you leave rather than
while you are already sitting in the closure.

So: one event per route that has something worth knowing, timed to the
departure the route recorded, carrying the Google Maps handoff in its URL
field. Calendar alert, tap, directions. The brief never has to be opened.

A note on the format, because iCalendar punishes carelessness quietly.
RFC 5545 wants CRLF line endings, content lines folded at 75 octets, and
commas, semicolons, backslashes and newlines escaped inside TEXT values.
Get any of those wrong and Google Calendar does not complain — it just
declines to import the file, which is a far worse failure than an error.
Everything here is hand-written against the spec; there is no dependency.
"""

from __future__ import annotations

import datetime as dt
import hashlib
from dataclasses import dataclass

from . import handoff, severity

PRODID = "-//Detour ATX//Route briefing//EN"

# How long before departure the alert fires. Long enough to change your
# mind about the route, short enough that you have not forgotten by the
# time you leave.
ALARM_LEAD_MIN = 20

# When a route records no departure time there is nothing to attach to,
# so the event lands here and reads as a heads-up for the day rather than
# pretending to know when you travel.
DEFAULT_HOUR = 8


def _escape(text: str) -> str:
    """RFC 5545 TEXT escaping. Backslash first, or it eats the others."""
    return (
        str(text or "")
        .replace("\\", "\\\\")
        .replace("\n", "\\n")
        .replace(",", "\\,")
        .replace(";", "\\;")
    )


def _fold(line: str) -> str:
    """Fold a content line at 75 octets, continuing with a leading space.

    Folding counts octets, not characters. A brief that happens to carry a
    non-ASCII character — and the rewritten descriptions do, em dashes
    among them — would otherwise be folded mid-codepoint and arrive as
    mojibake.
    """
    raw = line.encode("utf-8")
    if len(raw) <= 75:
        return line

    out = []
    chunk = bytearray()
    limit = 75
    for char in line:
        encoded = char.encode("utf-8")
        if len(chunk) + len(encoded) > limit:
            out.append(chunk.decode("utf-8"))
            chunk = bytearray()
            limit = 74  # the continuation space costs one
        chunk += encoded
    out.append(chunk.decode("utf-8"))
    return "\r\n ".join(out)


def _stamp(moment: dt.datetime) -> str:
    """UTC, basic format. Naive values are taken as already UTC."""
    if moment.tzinfo is not None:
        moment = moment.astimezone(dt.timezone.utc)
    return moment.strftime("%Y%m%dT%H%M%SZ")


def _local(moment: dt.datetime) -> str:
    """Floating local time — no zone, no Z.

    Deliberate. The brief is about Austin and the reader is in Austin, so
    an event at 08:30 should read 08:30. Pinning it to a zone would be
    more correct and would show the wrong time to anyone whose laptop
    calendar is set elsewhere, which is the common case for a demo.
    """
    return moment.strftime("%Y%m%dT%H%M%S")


@dataclass
class Event:
    uid: str
    start: dt.datetime
    minutes: int
    summary: str
    description: str
    url: str
    alarm: bool = True


def _uid(route_name: str, day: dt.date) -> str:
    """Stable per route per day, so re-importing updates rather than
    duplicating. A reader who subscribes on Monday and again on Tuesday
    should end up with two events, not four."""
    digest = hashlib.sha1(f"{route_name}|{day.isoformat()}".encode("utf-8")).hexdigest()
    return f"{digest[:20]}@detour-atx"


def _lines(event: Event, now: dt.datetime) -> list[str]:
    end = event.start + dt.timedelta(minutes=max(5, event.minutes))
    out = [
        "BEGIN:VEVENT",
        f"UID:{event.uid}",
        f"DTSTAMP:{_stamp(now)}",
        f"DTSTART:{_local(event.start)}",
        f"DTEND:{_local(end)}",
        f"SUMMARY:{_escape(event.summary)}",
        f"DESCRIPTION:{_escape(event.description)}",
    ]
    if event.url:
        # URL is not a TEXT value, so it is not escaped — escaping it
        # would turn every comma in a waypoint list into a backslash and
        # break the link.
        out.append(f"URL:{event.url}")
    if event.alarm:
        out += [
            "BEGIN:VALARM",
            "ACTION:DISPLAY",
            f"TRIGGER:-PT{ALARM_LEAD_MIN}M",
            f"DESCRIPTION:{_escape(event.summary)}",
            "END:VALARM",
        ]
    out.append("END:VEVENT")
    return out


def render(events: list[Event], *, now: dt.datetime | None = None) -> str:
    """A complete .ics document. Empty in, empty calendar out."""
    now = now or dt.datetime.now(dt.timezone.utc)
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        f"PRODID:{PRODID}",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        "X-WR-CALNAME:Detour ATX",
    ]
    for event in events:
        lines += _lines(event, now)
    lines.append("END:VCALENDAR")
    return "\r\n".join(_fold(line) for line in lines) + "\r\n"


def _headline(result, tell) -> tuple[str, str]:
    """What the event is called, and what it says inside."""
    blocking = [i for i, _ in tell if i.tier is severity.Tier.BLOCKING]
    route = getattr(getattr(result, "route", None), "name", "your route")

    if blocking:
        summary = (
            f"{route}: {len(blocking)} thing{'s' if len(blocking) > 1 else ''} "
            "to plan around"
        )
    else:
        summary = f"{route}: {len(tell)} update{'s' if len(tell) != 1 else ''}"

    body = [f"{item.headline}." for item, _ in tell[:6]]
    if len(tell) > 6:
        body.append(f"and {len(tell) - 6} more.")

    delta = getattr(result, "delta", None)
    if delta is not None and getattr(delta, "affected", False) and not getattr(
        delta, "implausible", False
    ):
        body.append(f"Route impact: {delta.summary()}.")

    body.append("Derived from City of Austin open data. Nothing here is "
                "confirmed unless it says so.")
    return summary, "\n".join(body)


def event_for(result, tell, *, day: dt.date, mode: str = "drive") -> Event | None:
    """One day's event for one route, or None when there is nothing to say.

    Returning None is the point. A calendar that fills up with "nothing is
    wrong" is a calendar someone turns off, and then the one morning it
    matters they do not see it either.
    """
    if not tell:
        return None

    route = getattr(result, "route", None)
    if route is None:
        return None

    if not getattr(route, "runs_on", lambda _d: True)(day):
        return None

    leaves = getattr(route, "leaves_at", None)
    start = dt.datetime.combine(day, leaves or dt.time(DEFAULT_HOUR, 0))

    # Point the event at the moment they would now have to leave, not the
    # moment they used to. That is the whole advice, expressed as a time
    # rather than as "five minutes earlier than something".
    delta = getattr(result, "delta", None)
    extra = 0
    if delta is not None and getattr(delta, "affected", False) and not getattr(
        delta, "implausible", False
    ):
        extra = max(0, round(getattr(delta, "extra_s", 0) / 60))
    if leaves is not None and extra:
        start -= dt.timedelta(minutes=extra)

    summary, description = _headline(result, tell)
    if leaves is not None and extra:
        summary = f"Leave by {start.strftime('%H:%M')} — {summary}"

    link = handoff.for_brief(result, mode=mode)
    if link is not None:
        description = f"{description}\n\nOpen in Google Maps: {link.url}"

    # Block out the trip itself, so the event occupies the time it costs.
    seconds = 0.0
    if delta is not None:
        seconds = getattr(delta, "baseline_s", 0) + getattr(delta, "extra_s", 0)
    minutes = max(5, round(seconds / 60))

    return Event(
        uid=_uid(getattr(route, "name", "route"), day),
        start=start,
        minutes=minutes,
        summary=summary,
        description=description,
        url=link.url if link is not None else "",
    )
