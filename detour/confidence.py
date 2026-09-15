"""Is this work zone describing something that is actually on the ground?

Austin flags its own uncertainty and the numbers are stark: of 3,853 work
zone records, 126 have a verified end date and *none* have a verified
position. Dates in this feed are permit windows, not work windows — one
observed record covers replacing wiring between two utility poles and runs
for a full year, with its own description noting the crew already left.

So nothing here is presented as fact. Every record gets a label and a list
of the reasons that produced it. The reasons matter as much as the label:
they are what a user sees when they ask "why are you telling me this", and
they are the evidence trail the Phase 2 Verifier agent extends rather than
replaces.

This module is deliberately rules-only. It is the deterministic baseline
that any later agent has to beat to justify its cost.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field
from enum import Enum

from . import workcal

LONG_PERMIT_DAYS = 180
NEAR_TERM_DAYS = 30


class Confidence(str, Enum):
    CONFIRMED = "confirmed"
    REPORTED = "reported"
    PROBABLY_OVER = "probably_over"


LABELS = {
    Confidence.CONFIRMED: "Confirmed",
    Confidence.REPORTED: "Reported",
    Confidence.PROBABLY_OVER: "Probably over",
}

# Phrasing that indicates a permit was closed out, extended past its real
# work, or voided while the record stayed "active".
#
# "CLEARED THE ROW" is observed in live data. The rest are plausible
# variants and should be treated as hypotheses until the ground truth loop
# has enough confirmations to score them individually.
CLOSEOUT_PATTERNS = [
    (re.compile(r"\bCLEARED\s+THE\s+ROW\b", re.I), "permit notes say the crew cleared the right of way"),
    (re.compile(r"\bWORK\s+(?:IS\s+)?COMPLETE[D]?\b", re.I), "permit notes say the work is complete"),
    (re.compile(r"\b(?:CANCELL?ED|VOIDED|WITHDRAWN)\b", re.I), "permit appears cancelled or voided"),
    (re.compile(r"\bNO\s+LONGER\s+(?:ACTIVE|NEEDED)\b", re.I), "permit notes say it is no longer active"),
]


def fmt_date(value: dt.datetime) -> str:
    """Format as "4 Mar 2026".

    strftime("%-d") is a glibc extension that raises on Windows, so the day
    is interpolated rather than formatted.
    """
    return f"{value.day} {value:%b %Y}"


def _as_bool(value) -> bool:
    """Socrata serves booleans as real JSON bools, but not always."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() == "true"
    return False


@dataclass
class Verdict:
    level: Confidence
    reasons: list[str] = field(default_factory=list)

    @property
    def label(self) -> str:
        return LABELS[self.level]

    @property
    def pushable(self) -> bool:
        """Only Confirmed items may generate a push notification.

        This single rule is what keeps the product trustworthy. The fastest
        way to lose a user is one alert about a closure that is not there.
        """
        return self.level is Confidence.CONFIRMED


def score_work_zone(
    zone: dict, *, now: dt.datetime, observation=None, agent_verdict=None
) -> Verdict:
    """Classify one work zone record.

    `observation` is a fresh resident sighting from the evidence ledger and
    `agent_verdict` is the Verifier's conclusion. They are not equivalent:
    a sighting can raise a record to Confirmed, an agent verdict can only
    lower it. See the comment at the branch below.

    A resident sighting outranks everything else here: the city verifies a
    date on 126 records out of 3,853 and a position on none of them, so
    someone who just drove past knows more than the permit does.
    """
    reasons: list[str] = []

    if observation is not None:
        if observation.claim == "present":
            return Verdict(Confidence.CONFIRMED, ["a resident confirmed this is there"])
        if observation.claim == "absent":
            return Verdict(
                Confidence.PROBABLY_OVER, ["a resident reported this is not there"]
            )

    # An agent verdict is inference, not observation. It may never reach
    # Confirmed, because Confirmed is what gates push and no amount of
    # reasoning about permit text is a sighting. Within that ceiling it moves
    # in both directions:
    #
    #   "absent"  -> Probably over. Cheap if wrong: a missed lane restriction.
    #   "present" -> lifts a rules-suppressed record back to Reported, capped
    #                there. Also cheap if wrong, because Reported cannot push.
    #
    # The lift was added after evaluation. An earlier version only let the
    # agent suppress, and measuring it showed why that was wrong: on records
    # the rules had suppressed on critical corridors, the agent found genuine
    # active work in 5 of 12 cases, citing overlapping CIP permits and recent
    # construction 311 reports — and every one of those rescues was being
    # discarded before it reached the verdict.
    agent_note: str | None = None
    if agent_verdict is not None:
        if agent_verdict.claim == "absent":
            return Verdict(
                Confidence.PROBABLY_OVER,
                [f"the verifier found this is probably not there — {agent_verdict.detail}"[:200]],
            )
        if agent_verdict.claim == "present":
            agent_note = "the verifier found supporting evidence"
            rules_only = score_work_zone(zone, now=now)
            if rules_only.level is Confidence.PROBABLY_OVER:
                return Verdict(
                    Confidence.REPORTED,
                    [
                        f"the rules suppressed this ({rules_only.reasons[0]})",
                        f"but the verifier found active work — {agent_verdict.detail}"[:200],
                    ],
                )

    start = zone.get("_start")
    end = zone.get("_end")
    description = zone.get("description") or ""

    # --- disqualifying signals: the permit window does not cover today -----
    if start and start > now:
        return Verdict(
            Confidence.PROBABLY_OVER,
            [f"permit does not start until {fmt_date(start)}"],
        )
    if end and end < now:
        return Verdict(
            Confidence.PROBABLY_OVER,
            [f"permit window closed {fmt_date(end)}"],
        )

    # --- close-out phrasing in the permit narrative -----------------------
    for pattern, reason in CLOSEOUT_PATTERNS:
        if pattern.search(description):
            return Verdict(Confidence.PROBABLY_OVER, [reason])

    # --- positive verification -------------------------------------------
    if _as_bool(zone.get("are_workers_present")):
        # The flag is self-reported by contractors through a check-in app and
        # is not reliably cleared when they leave. Outside plausible working
        # hours it is a stale checkbox, not a sighting, so it stops being
        # grounds for Confirmed — which is what gates push.
        if workcal.discount_workers_present(zone, now):
            _, why = workcal.crew_plausible(now, description=description)
            reasons.append(f"crew is checked in, but {why}")
        else:
            reasons.append("crew checked in on site")
            return Verdict(Confidence.CONFIRMED, reasons)

    if _as_bool(zone.get("is_end_date_verified")):
        reasons.append("end date verified by the city")
        if _as_bool(zone.get("critical_corridor")):
            reasons.append("flagged as a critical corridor")
        return Verdict(Confidence.CONFIRMED, reasons)

    # --- everything else is a permit window we cannot corroborate ---------
    window_days = (end - start).days if start and end else None

    if window_days is not None and window_days > LONG_PERMIT_DAYS:
        return Verdict(
            Confidence.PROBABLY_OVER,
            [f"permit window runs {window_days} days, so the dates say little about today"],
        )

    if window_days is not None:
        reasons.append(f"permit window is {window_days} days")
    if _as_bool(zone.get("critical_corridor")):
        reasons.append("flagged as a critical corridor")
    if end and (end - now).days <= NEAR_TERM_DAYS:
        reasons.append(f"scheduled to end {fmt_date(end)}")
    if agent_note:
        reasons.append(agent_note)
    if not reasons:
        reasons.append("permit is inside its window but unverified")

    return Verdict(Confidence.REPORTED, reasons)


def score_signal(signal: dict, *, now: dt.datetime) -> Verdict:
    """Classify one degraded traffic signal.

    A conflict flash is a driver-facing fact: the controller tripped its
    conflict monitor and fell back to flashing, which is directly
    observable. A communication issue is not — it means the city lost
    telemetry to the cabinet, while the signal almost certainly keeps
    cycling on its local timer.
    """
    state = (signal.get("operation_text") or "").strip()
    since = signal.get("_since")

    if "flash" in state.lower():
        reasons = ["controller is reporting flash, which is observable at the intersection"]
        if since:
            hours = (now - since).total_seconds() / 3600
            if hours < 24:
                reasons.append(f"started {hours:.0f} h ago")
        return Verdict(Confidence.CONFIRMED, reasons)

    reasons = ["the city has lost telemetry, not necessarily the signal"]
    if since:
        days = (now - since).days
        if days > 365:
            reasons.append(f"unreachable for {days // 365} year(s)")
        elif days > 0:
            reasons.append(f"unreachable for {days} day(s)")
    return Verdict(Confidence.REPORTED, reasons)


def distribution(verdicts: list[Verdict]) -> dict[str, int]:
    """Count by label, for the summary line and for regression tests."""
    counts = {label: 0 for label in LABELS.values()}
    for verdict in verdicts:
        counts[verdict.label] += 1
    return counts
