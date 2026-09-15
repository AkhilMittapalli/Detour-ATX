"""Turn raw permit narrative into one sentence a resident can read.

A real `description` from the work zone feed looks like this:

    Temporary use of Right of Way Permit has been issued for this location.
    Details: ***PER COORDINATION WITH 2025-032743 RW, THIS PROJECT HAS
    CLEARED THE ROW. CONFLICT CHECK REQUIRED FOR EXTENSION*** EXTENDED PER
    <staff name> WR 197389 - 1571-1547 W Oltorf (S Lamar) -
    AEU/Mastec/Primoris - Replace wiring from P7 to P8, Work area 12x70 ft,
    Vehicles required: Underground truck 12x24, crew truck 12x22 and wire
    trailer 12x28. Work will be behind the curb.

Somewhere in there is "Austin Energy is replacing overhead wiring; one
eastbound lane is taken and the work sits behind the curb". Everything else
is permit administration — cross-references, approval initials, work order
numbers, and the names of city staff who signed off.

The deterministic cleaner runs always. The model call is optional and only
ever runs on the handful of records that reach a brief, never on all 3,853.
"""

from __future__ import annotations

import re

from . import llm

# Permit administration that carries nothing for a driver.
NOISE = [
    # "Temporary use of Right of Way Permit...", "Excavation Permit...", etc.
    re.compile(r"\b[\w ]{0,40}Permit has been issued for this location\.?", re.I),
    re.compile(r"\*\*\*.*?\*\*\*", re.S),                 # inline coordination notes
    re.compile(r"\bEXTENDED\s+PER\s+[A-Z][A-Za-z.'-]*(?:\s+[A-Z][A-Za-z.'-]*)*", re.I),
    re.compile(r"\bper request on \d{1,2}/\d{1,2}/\d{2,4}\.?", re.I),
    re.compile(r"\bWR\s*\d{4,}\s*-?\s*", re.I),           # work order numbers
    re.compile(r"\b\d{4}-\d{6}\s*RW\b", re.I),            # permit cross-references
    re.compile(r"\bDetails\s*:\s*", re.I),
    # Permit extension ledgers: "27 extension from Aug 26, 2026 to Sep 21,
    # 2026 46 extension from Jul 11, 2026 to Aug 25, 2026 ..." — these often
    # run to four or five entries and crowd out the actual work description.
    re.compile(
        r"\b\d+\s+extension\s+from\s+\w+\s+\d{1,2},?\s+\d{4}\s+to\s+\w+\s+\d{1,2},?\s+\d{4}",
        re.I,
    ),
    re.compile(r"\bhas been added as a sub ?contractor\b.*?(?:\.|$)", re.I | re.S),
]

WHITESPACE = re.compile(r"\s+")

# U+00BF and U+FFFD appear mid-sentence throughout this feed where a dash or
# an inch mark was mangled by a cp1252/UTF-8 round trip somewhere inside the
# city's permit system — "a 9x5<?> duct bank", "two sections <?> a bore
# section". The corruption is in the published data, not in our decoding, so
# there is nothing to recover; dropping the character reads better than
# printing a glyph that looks like our bug.
MOJIBAKE = re.compile(r"[¿�]")


def clean(description: str | None) -> str:
    """Strip permit administration and collapse whitespace.

    Always safe to call, never hits the network. This is what ships when
    there is no API key, and what the model rewrites when there is.
    """
    if not description:
        return ""

    text = MOJIBAKE.sub(" ", description)
    for pattern in NOISE:
        text = pattern.sub(" ", text)

    text = WHITESPACE.sub(" ", text).strip(" -–—.,;:")

    # Prefer the clause that actually describes work over the leftovers.
    sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", text) if s.strip()]
    if not sentences:
        return ""

    verbs = re.compile(
        r"\b(replac|instal|repair|construct|excavat|close|remov|resurfac|"
        r"pav|bore|trench|lane|sidewalk|traffic)\w*",
        re.I,
    )
    ranked = sorted(sentences, key=lambda s: (len(verbs.findall(s)), len(s)), reverse=True)
    best = ranked[0]

    return best if len(best) <= 240 else best[:237].rstrip() + "..."


def rewrite(description: str | None, *, road: str = "", enabled: bool = True) -> str:
    """One plain sentence describing the work, or the cleaned text.

    Falls back to `clean` on a missing key, a network failure, or an empty
    response. A brief that renders slightly rougher prose is a far better
    outcome than a brief that does not send.
    """
    cleaned = clean(description)
    if not enabled or llm.provider() is None or not cleaned:
        return cleaned

    prompt = (
        "Rewrite this Austin right-of-way permit note as ONE plain sentence "
        "for a driver, saying who is doing what and how the road is affected. "
        "Under 20 words. No permit numbers, no contractor names, no staff names. "
        "If the note does not say what the work is, reply with the single word NONE.\n\n"
        f"Street: {road or 'unknown'}\n"
        f"Note: {cleaned}"
    )

    # Generous budget: reasoning models spend output tokens before emitting
    # any visible text, so a tight cap does not produce a shorter sentence,
    # it produces half a sentence. Observed live at 150: "Crews are".
    result = llm.complete(prompt, max_tokens=2048)
    if not result or result.strip().upper().startswith("NONE"):
        return cleaned
    if _looks_truncated(result):
        return cleaned
    return result


def _looks_truncated(text: str) -> bool:
    """Reject a rewrite that stops mid-thought.

    A rougher but complete sentence from the deterministic cleaner beats a
    fluent fragment. "Crews are installing a bike" is worse than no rewrite
    at all, because it reads as authoritative while saying nothing.
    """
    stripped = text.strip()
    if len(stripped) < 25:
        return True
    if stripped[-1] not in ".!?":
        return True
    # A trailing article or preposition means the thought was cut off even if
    # a full stop happened to land.
    tail = stripped.rstrip(".!?").split()
    return bool(tail) and tail[-1].lower() in {
        "a", "an", "the", "and", "or", "of", "to", "for", "with", "on", "at",
        "in", "by", "from", "near", "along", "between",
    }
