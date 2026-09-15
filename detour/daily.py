"""The daily run.

One entry point that does what a product does every morning: for each saved
route, build the brief, decide what is actually worth telling this reader,
write it out, and remember what was said so tomorrow can compare.

Feeds are fetched once and reused across routes. With a handful of routes
that matters little; with a few hundred it is the difference between a job
that finishes before breakfast and one that gets rate-limited.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from pathlib import Path as FsPath

from . import brief as brief_mod
from . import correspondent, deliver, plan, route as rt, severity, sources

OUT_DIR = FsPath(__file__).resolve().parent.parent / "out"


@dataclass
class Outcome:
    route: str
    path: FsPath | None = None
    told: int = 0
    held: int = 0
    blocking: int = 0
    pushable: int = 0
    push_text: str = ""
    html: str = ""
    error: str = ""
    news: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.error


def run_route(
    route_file: FsPath,
    *,
    reader: str,
    now: dt.datetime,
    out_dir: FsPath,
    rewrite: bool = True,
    with_detour: bool = True,
    remember: bool = True,
) -> Outcome:
    try:
        route_obj = rt.load_route(route_file)
    except (ValueError, OSError) as exc:
        return Outcome(route=route_file.stem, error=str(exc)[:140])

    try:
        result = brief_mod.build(
            route_obj, now=now, rewrite=rewrite, with_detour=with_detour
        )
    except (RuntimeError, OSError) as exc:
        return Outcome(route=route_obj.name, error=str(exc)[:140])

    tell, held = correspondent.curate(result.items, reader=reader, now=now)

    outcome = Outcome(
        route=route_obj.name,
        told=len(tell),
        held=len(held),
        blocking=sum(1 for i, _ in tell if i.tier is severity.Tier.BLOCKING),
        pushable=sum(1 for i, _ in tell if i.pushable),
        news=[f"{d.news.value}: {i.headline[:54]}" for i, d in tell[:6]],
    )
    outcome.html = deliver.render(result, tell=tell, held=held, reader=reader)
    outcome.path = deliver.write(
        result, tell=tell, held=held, directory=out_dir, reader=reader
    )
    outcome.push_text = deliver.plain_text(result, tell=tell)

    # Only remember what was actually shown. Holding an item back must not
    # reset its clock, or a suppressed closure would look new again in a week.
    if remember and tell:
        correspondent.record_told([i for i, _ in tell], reader=reader, now=now)

    return outcome


def run_all(
    *,
    reader: str = "you",
    now: dt.datetime | None = None,
    out_dir: FsPath | None = None,
    routes_dir: FsPath | None = None,
    rewrite: bool = True,
    with_detour: bool = True,
    remember: bool = True,
) -> list[Outcome]:
    now = now or sources.now_utc()
    out_dir = out_dir or OUT_DIR
    files = plan.saved_routes(routes_dir)

    # Warm the shared feeds once rather than per route.
    sources.fetch_work_zones()
    sources.fetch_signals()
    sources.fetch_active_incidents()

    return [
        run_route(
            f,
            reader=reader,
            now=now,
            out_dir=out_dir,
            rewrite=rewrite,
            with_detour=with_detour,
            remember=remember,
        )
        for f in files
    ]
