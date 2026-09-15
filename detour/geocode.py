"""Addresses to coordinates, using the city's own locator.

Austin runs a public ArcGIS geocoder at
`maps.austintexas.gov/arcgis/.../Geocode/COA_Locator`. Using it rather than a
commercial one keeps the project dependency-free and key-free, and it is
authoritative for Austin addresses in a way a global geocoder is not — it is
built from the same address points the city uses for 911 dispatch.

It only covers Austin, which for this product is the whole world.
"""

from __future__ import annotations

import json
import urllib.parse
import urllib.request
from dataclasses import dataclass

from . import geo

LOCATOR = (
    "https://maps.austintexas.gov/arcgis/rest/services/Geocode/"
    "COA_Locator/GeocodeServer/findAddressCandidates"
)

# Below this the locator is guessing. 80 still tolerates a missing unit
# number or a misspelt street type.
MIN_SCORE = 80.0


class GeocodeError(RuntimeError):
    pass


@dataclass
class Place:
    address: str
    point: geo.Point
    score: float

    def __str__(self) -> str:
        return f"{self.address} ({self.point[0]:.5f}, {self.point[1]:.5f})"


def lookup(address: str, *, limit: int = 3, timeout: int = 40) -> list[Place]:
    """Candidate matches for a free-text address, best first."""
    query = (address or "").strip()
    if not query:
        raise GeocodeError("empty address")

    url = LOCATOR + "?" + urllib.parse.urlencode(
        {
            "SingleLine": query,
            "f": "json",
            "outSR": "4326",          # WGS84, matching every other feed here
            "maxLocations": str(limit),
        }
    )

    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            body = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise GeocodeError(f"locator unreachable: {exc}") from exc

    if "error" in body:
        raise GeocodeError(str(body["error"].get("message", "locator error"))[:160])

    places: list[Place] = []
    for candidate in body.get("candidates", []):
        location = candidate.get("location") or {}
        x, y = location.get("x"), location.get("y")
        if x is None or y is None:
            continue
        places.append(
            Place(
                address=str(candidate.get("address", query)),
                point=(float(x), float(y)),
                score=float(candidate.get("score", 0)),
            )
        )
    return places


def resolve(address: str) -> Place:
    """The single best match, or an error explaining what to try instead."""
    places = lookup(address)
    if not places:
        raise GeocodeError(
            f"no Austin address matches {address!r}. "
            "Try including the street number and type, e.g. '301 W 2nd St'."
        )

    best = places[0]
    if best.score < MIN_SCORE:
        raise GeocodeError(
            f"best match for {address!r} was {best.address!r} at score "
            f"{best.score:.0f}, which is too low to trust. Add more detail."
        )
    return best
