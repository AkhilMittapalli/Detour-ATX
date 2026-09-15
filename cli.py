"""Run a Detour ATX brief from the terminal.

    python cli.py routes/demo.json
    python cli.py routes/demo.json --json
    python cli.py --audit
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import sys

from detour import brief as brief_mod
from detour import confidence as conf
from detour import cluster, config, ledger, llm, plan, route as rt
from detour import transport as tp
from detour import severity, sources

def _enable_utf8() -> bool:
    """Windows consoles default to cp1252, which cannot encode the glyphs.

    Returns whether we can safely print non-ASCII; callers fall back to
    plain characters when we cannot.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, OSError):
            pass
    encoding = (getattr(sys.stdout, "encoding", "") or "").lower()
    return "utf" in encoding


UNICODE = _enable_utf8()

COLOR = sys.stdout.isatty() and not os.environ.get("NO_COLOR")

GLYPH = {
    "bar": "█" if UNICODE else "#",
    "mark": "▌" if UNICODE else "|",
    "arrow": "→" if UNICODE else "->",
    "dot": "·" if UNICODE else "-",
}

ANSI = {
    "reset": "\033[0m",
    "dim": "\033[2m",
    "bold": "\033[1m",
    severity.Tier.BLOCKING: "\033[38;5;202m",
    severity.Tier.SLOWING: "\033[38;5;179m",
    severity.Tier.BACKGROUND: "\033[38;5;109m",
}


def paint(text: str, key) -> str:
    if not COLOR or key not in ANSI:
        return text
    return f"{ANSI[key]}{text}{ANSI['reset']}"


def render(result: brief_mod.Brief) -> str:
    lines: list[str] = []
    local_hint = result.generated_at.strftime("%a %d %b %H:%M UTC")

    lines.append("")
    lines.append(paint(f"  {result.route.name.upper()}", "bold"))
    lines.append(
        paint(
            f"  {result.route.length_mi:.1f} mi {GLYPH['dot']} {local_hint}",
            "dim",
        )
    )
    if result.streets:
        shown = f" {GLYPH['arrow']} ".join(result.streets[:6])
        if len(result.streets) > 6:
            shown += f" (+{len(result.streets) - 6} more)"
        lines.append(paint(f"  via {shown}", "dim"))
    lines.append("")

    if not result.items:
        lines.append("  Nothing reported on your route today.")
        lines.append("")
        return "\n".join(lines)

    current: severity.Tier | None = None
    for item in result.items:
        if item.tier != current:
            current = item.tier
            lines.append(
                paint(f"  {severity.LABELS[item.tier].upper()}", item.tier)
                + paint(f"   {severity.ASKS[item.tier]}", "dim")
            )
            lines.append("")

        marker = paint(GLYPH["mark"], item.tier)
        lines.append(f"  {marker} {item.headline}")
        lines.append(f"    {paint('[' + item.verdict.label + ']', 'dim')} {item.detail}")
        if item.action:
            lines.append(f"    {paint(GLYPH['arrow'] + ' ' + item.action, item.tier)}")
        lines.append("")

    delta = result.delta
    if delta is not None and (delta.affected or delta.implausible):
        lines.append(paint("  ROUTE IMPACT", severity.Tier.BLOCKING))
        lines.append("")
        lines.append(f"    {delta.summary()}")
        if delta.via and delta.affected and not delta.implausible:
            lines.append(
                paint(
                    f"    via {f' {GLYPH['arrow']} '.join(delta.via[:6])}",
                    "dim",
                )
            )
        lines.append("")

    if delta is not None:
        rejected = [b for b in delta.blockages if b.note and not b.applied]
        if rejected:
            lines.append(
                paint(
                    f"  {len(rejected)} closure(s) not applied — geometry did not "
                    "match the street named on the permit",
                    "dim",
                )
            )

    pushable = len(result.pushable)
    lines.append(
        paint(
            f"  {len(result.items)} items {GLYPH['dot']} {len(result.blocking)} blocking {GLYPH['dot']} "
            f"{pushable} would push",
            "dim",
        )
    )
    lines.append("")
    return "\n".join(lines)


def as_dict(result: brief_mod.Brief) -> dict:
    return {
        "route": result.route.name,
        "length_mi": round(result.route.length_mi, 2),
        "generated_at": result.generated_at.isoformat(),
        "streets": result.streets,
        "items": [
            {
                **{k: v for k, v in dataclasses.asdict(item).items() if k != "verdict"},
                "tier": severity.LABELS[item.tier],
                "confidence": item.verdict.label,
                "reasons": item.verdict.reasons,
                "pushable": item.pushable,
                "distance_m": round(item.distance_m, 1),
            }
            for item in result.items
        ],
    }


def audit(use_cache: bool = True) -> str:
    """Score every work zone in the feed and report the distribution.

    This is the claim the whole product rests on, so it is worth being able
    to re-run it against live data on any given day rather than trusting a
    number written down once.
    """
    now = sources.now_utc()
    zones = sources.fetch_work_zones(use_cache=use_cache)
    verdicts = [conf.score_work_zone(z, now=now) for z in zones]
    counts = conf.distribution(verdicts)

    total = len(zones) or 1
    lines = ["", f"  WORK ZONE CONFIDENCE AUDIT  ({len(zones):,} records)", ""]
    for label, count in counts.items():
        bar = GLYPH["bar"] * round(40 * count / total)
        lines.append(f"  {label:<14} {count:>6,}  {count / total:>5.1%}  {bar}")

    pushable = sum(1 for v in verdicts if v.pushable)
    lines += [
        "",
        f"  Eligible to push: {pushable:,} ({pushable / total:.1%})",
        "",
    ]
    return "\n".join(lines)


def run_verifier(
    record_ids: list[str], *, model: str | None, use_cache: bool = True
) -> str:
    """Investigate records with the Verifier agent and report what it found."""
    from detour import evidence, verifier

    now = sources.now_utc()
    zones = sources.fetch_work_zones(use_cache=use_cache)
    toolbox = evidence.Toolbox(zones, now=now)

    lines = ["", f"  VERIFIER  ({len(record_ids)} record(s), model {model or llm.DEFAULT_GEMINI_MODEL})", ""]
    tally: dict[str, int] = {}

    for record_id in record_ids:
        result = verifier.verify(record_id, toolbox, model=model)
        zone = toolbox.zone(record_id) or {}
        road = (zone.get("road_names") or "?").strip()[:28]

        if not result.ok:
            lines.append(f"  {road:<28} ERROR  {result.error[:90]}")
            continue

        tally[result.verdict] = tally.get(result.verdict, 0) + 1
        tint = {
            "present": severity.Tier.BLOCKING,
            "absent": severity.Tier.BACKGROUND,
            "unclear": severity.Tier.SLOWING,
        }[result.verdict]

        lines.append(
            f"  {road:<28} "
            + paint(f"{result.verdict.upper():<8}", tint)
            + f" {result.confidence:.2f}"
        )
        lines.append(f"      {result.rationale[:110]}")
        for item in result.evidence[:3]:
            lines.append(paint(f"        - {item[:104]}", "dim"))
        lines.append(
            paint(f"      tools: {' '.join(result.tools_used) or 'none'}", "dim")
        )
        lines.append("")

    if tally:
        summary = "  ".join(f"{k}: {v}" for k, v in sorted(tally.items()))
        lines += ["", paint(f"  {summary}", "dim"), ""]
    return "\n".join(lines)


def run_editor(limit: int, *, model: str | None, use_cache: bool = True) -> str:
    """Cluster today's signal failures and have the Desk Editor read them."""
    from detour import editor

    now = sources.now_utc()
    signals = sources.fetch_signals(use_cache=use_cache)
    zones = sources.fetch_work_zones(use_cache=use_cache)

    swept = cluster.batch_timestamps(signals)
    events = cluster.notable(cluster.cluster_signals(signals), now=now)
    clusters = [e for e in events if e.is_cluster][:limit]

    lines = [
        "",
        f"  DESK EDITOR  ({len(signals)} degraded signals)",
        paint(
            f"  {sum(1 for s in signals if s.get('_since') in swept)} restamped by the "
            f"city's overnight sweep and excluded",
            "dim",
        ),
        "",
    ]
    if not clusters:
        lines += ["  No multi-signal clusters right now.", ""]
        return "\n".join(lines)

    for event in clusters:
        reading = editor.read_event(event, zones, now=now, model=model)
        tint = {
            "hazard": severity.Tier.BLOCKING,
            "caution": severity.Tier.SLOWING,
            "none": severity.Tier.BACKGROUND,
        }.get(reading.driver_impact, severity.Tier.BACKGROUND)

        lines.append(paint(f"  {GLYPH['mark']} {event.label}", tint))
        for member in event.members[:6]:
            lines.append(paint(f"      {(member.get('location_name') or '').strip()[:52]}", "dim"))

        if not reading.ok:
            lines += [f"      ERROR  {reading.error[:100]}", ""]
            continue

        lines.append("")
        lines.append(f"      {reading.headline}")
        lines.append(
            paint(f"      cause: {reading.cause}  {reading.confidence:.2f}  "
                  f"driver impact: {reading.driver_impact}", tint)
        )
        lines.append(f"      {reading.rationale[:150]}")
        for item in reading.evidence[:3]:
            lines.append(paint(f"        - {item[:100]}", "dim"))
        lines.append(paint(f"      tools: {' '.join(reading.tools_used)}", "dim"))
        lines.append("")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Detour ATX — Phase 1 brief")
    parser.add_argument("route", nargs="?", help="path to a saved route JSON file")
    parser.add_argument("--json", action="store_true", help="emit JSON instead of text")
    parser.add_argument(
        "--audit",
        action="store_true",
        help="score every work zone in the feed and print the distribution",
    )
    parser.add_argument(
        "--no-rewrite",
        action="store_true",
        help="skip the model call and use the deterministic cleaner only",
    )
    parser.add_argument(
        "--fresh", action="store_true", help="bypass the local response cache"
    )
    parser.add_argument(
        "--no-transit",
        action="store_true",
        help="skip the CapMetro rider-impact lookup",
    )
    parser.add_argument(
        "--buses",
        metavar="ROUTE",
        help="which CapMetro routes currently run along a saved route",
    )
    parser.add_argument(
        "--no-detour",
        action="store_true",
        help="skip building the street graph and computing the detour delta",
    )
    parser.add_argument(
        "--confirm",
        metavar="RECORD_ID",
        help="record a resident observation against a work zone id",
    )
    parser.add_argument(
        "--absent",
        action="store_true",
        help="with --confirm, report the disruption is NOT there (default: present)",
    )
    parser.add_argument(
        "--ledger", action="store_true", help="show the ground truth tally"
    )
    parser.add_argument(
        "--verify",
        metavar="RECORD_ID",
        help="run the Verifier agent over one work zone record",
    )
    parser.add_argument(
        "--verify-route",
        metavar="ROUTE",
        help="run the Verifier over the Reported-tier work zones on a route",
    )
    parser.add_argument(
        "--limit", type=int, default=5, help="max records for --verify-route"
    )
    parser.add_argument("--model", help="override the model id")
    parser.add_argument(
        "--config", action="store_true",
        help="show which keys are configured, without printing them"
    )
    parser.add_argument(
        "--models", action="store_true", help="list model ids this API key can use"
    )
    parser.add_argument(
        "--events",
        action="store_true",
        help="cluster today's signal failures and interpret them with the Desk Editor",
    )
    parser.add_argument(
        "--clusters",
        action="store_true",
        help="show the clustering only, with no model calls",
    )
    parser.add_argument(
        "--add-route",
        metavar="NAME",
        help="create a saved route from two addresses (use with --from and --to)",
    )
    parser.add_argument("--from", dest="origin", help="origin address")
    parser.add_argument("--to", dest="dest", help="destination address")
    parser.add_argument(
        "--routes", action="store_true", help="list saved routes"
    )
    parser.add_argument(
        "--run-all",
        action="store_true",
        help="the daily run: a brief for every saved route, written to out/",
    )
    parser.add_argument(
        "--reader", default="you", help="who the daily run is for (default: you)"
    )
    parser.add_argument(
        "--transport",
        choices=["file", "email", "webhook"],
        default="file",
        help="where the brief goes (default: file, written to out/)",
    )
    parser.add_argument(
        "--send",
        action="store_true",
        help="actually deliver. Without this every transport is a dry run.",
    )
    parser.add_argument(
        "--draft",
        choices=["stale", "dark-signal", "geometry"],
        help="draft a 311 report for a human to read, check and file themselves",
    )
    parser.add_argument(
        "--evaluate",
        action="store_true",
        help="measure the Verifier against the rules baseline on a random sample",
    )
    parser.add_argument(
        "--slice",
        dest="eval_slice",
        choices=["reported-full", "suppressed-critical"],
        default="reported-full",
        help="which population to evaluate on",
    )
    parser.add_argument(
        "--baselines",
        action="store_true",
        help="311 close-time baselines per request type",
    )
    parser.add_argument(
        "--snapshot",
        action="store_true",
        help="record today's degraded signals so recurrence can be judged later",
    )
    args = parser.parse_args()

    try:
        if args.buses:
            from detour import transit

            snap = transit.vehicles()
            if not snap.ok:
                print(f"detour: {snap.error}", file=sys.stderr)
                return 1
            route_obj = rt.load_route(args.buses)
            impacts = transit.routes_near(route_obj.path, snap)
            print("")
            print(f"  {route_obj.name}")
            print(paint(f"  {len(snap.vehicles)} CapMetro vehicles reporting, "
                        f"feed {snap.age_seconds:.0f}s old", "dim"))
            print("")
            if not impacts:
                print("  No CapMetro vehicles currently on this route.")
            for i in impacts:
                print(f"  {i.describe()}")
            print("")
            return 0

        if args.baselines:
            from detour import baseline

            table = baseline.table(refresh=args.fresh)
            print("")
            print("  311 CLOSE-TIME BASELINES")
            print("")
            for stat in sorted(table.values(), key=lambda x: x.median_days):
                print(f"  {stat.describe()}")
            print("")
            return 0

        if args.draft:
            from detour import advocate

            now = sources.now_utc()
            zones = sources.fetch_work_zones(use_cache=not args.fresh)
            signals = sources.fetch_signals(use_cache=not args.fresh)
            pool = advocate.candidates(zones, signals, now=now)

            drafts = []
            if args.draft == "stale":
                for z in pool["stale_permits"][: args.limit]:
                    drafts.append(advocate.for_stale_permit(
                        z, now=now, enabled=not args.no_rewrite))
            elif args.draft == "dark-signal":
                for sig in pool["dark_signals"][: args.limit]:
                    drafts.append(advocate.for_dark_signal(
                        sig, now=now, swept=pool["swept"], enabled=not args.no_rewrite))
            else:
                print("Geometry drafts need a specific record; use --verify first.")
                return 0

            if not drafts:
                print("Nothing worth reporting right now.")
                return 0

            print("")
            print(paint("  DRAFTS ONLY - nothing is filed. Read, edit, and submit",
                        severity.Tier.SLOWING))
            print(paint("  these yourself at austintexas.gov/311.", severity.Tier.SLOWING))
            print("")
            for d in drafts[: advocate.MAX_DRAFTS_PER_RUN]:
                path = advocate.save(d, stamp=now)
                tag = "model-written" if d.generated else "template"
                print(f"  {d.subject[:66]}")
                print(paint(f"      {tag}, {len(d.evidence)} evidence line(s)", "dim"))
                print(paint(f"      {path}", "dim"))
                print()
            return 0

        if args.evaluate:
            from detour import evaluate

            def tick(i, total, zone, result):
                road = (zone.get("road_names") or "?").strip()[:26]
                mark = result.verdict if result.ok else "ERROR"
                print(paint(f"    [{i}/{total}] {road:<26} {mark}", "dim"))

            print("")
            print(f"  EVALUATION  (sample {args.limit}, model "
                  f"{args.model or llm.DEFAULT_GEMINI_MODEL})")
            print("")
            report = evaluate.run(size=args.limit, model=args.model,
                                  slice_name=args.eval_slice, progress=tick)
            saved = evaluate.save(report)

            print("")
            print(f"  slice        {report.slice_name}, population {report.population}")
            print(f"  sampled      {report.sampled}  ({report.errors} error(s))")
            print(f"  agreement    {report.agreement:.0%} with the rules baseline")
            print(f"  changed      {len(report.changed)} decision(s)")
            print(f"  cost         {report.tools_per_record:.1f} tool calls per record")
            print(f"  verdicts     {report.by_verdict}")
            print("")
            for c in report.changed:
                print(paint(f"    {c.road:<26} {c.rules} -> {c.agent} "
                            f"({c.agent_confidence:.2f})", severity.Tier.BLOCKING))
                print(paint(f"      {c.rationale[:96]}", "dim"))
            print("")
            print(f"  {report.verdict()}")
            print(paint(f"  saved to {saved}", "dim"))
            print("")
            return 0

        if args.add_route:
            if not args.origin or not args.dest:
                parser.error("--add-route needs --from and --to addresses")
            built = plan.build(args.add_route, args.origin, args.dest)
            saved = plan.save(built)
            print(f"\n  {built.name}")
            print(f"  {built.origin.address}")
            print(f"    to {built.destination.address}")
            print(f"  {built.miles:.1f} mi, about {built.minutes:.0f} min, "
                  f"{len(built.waypoints)} waypoints")
            print(f"  via {' -> '.join(built.streets[:7])}")
            print(f"  saved to {saved}\n")
            return 0

        if args.routes:
            files = plan.saved_routes()
            if not files:
                print("No saved routes. Add one with --add-route.")
                return 0
            print()
            for f in files:
                info = json.loads(f.read_text(encoding="utf-8"))
                print(f"  {info.get('name', f.stem)}")
                if info.get("origin"):
                    print(f"      {info['origin']} -> {info.get('destination','')}")
                print(f"      {f.name}, {len(info.get('waypoints', []))} waypoints")
            print()
            return 0

        if args.run_all:
            from detour import daily

            results = daily.run_all(
                reader=args.reader,
                rewrite=not args.no_rewrite,
                with_detour=not args.no_detour,
            )
            if not results:
                print("No saved routes. Add one with --add-route.")
                return 0

            try:
                carrier = tp.build(args.transport, out_dir=daily.OUT_DIR)
            except tp.TransportError as exc:
                print(f"detour: {exc}", file=sys.stderr)
                return 1

            mode = "SENDING" if args.send else "DRY RUN"
            header = (f"  DAILY RUN  ({len(results)} route(s), reader: {args.reader}, "
                      f"via {args.transport})")
            print("")
            print(header)
            print(paint(f"  {mode}" + ("" if args.send else
                        " - nothing will be delivered. Add --send to deliver."),
                        severity.Tier.BLOCKING if args.send else severity.Tier.SLOWING))
            print("")

            now = sources.now_utc()
            for r in results:
                if not r.ok:
                    print(f"  {r.route:<28} ERROR {r.error[:70]}")
                    continue
                tint = (severity.Tier.BLOCKING if r.blocking
                        else severity.Tier.BACKGROUND)
                print(f"  {paint(r.route, tint)}")
                print(paint(f"      told {r.told}, held {r.held}, "
                            f"blocking {r.blocking}, would push {r.pushable}", "dim"))
                for line in r.news:
                    print(paint(f"        {line}", "dim"))

                worth, why = tp.worth_sending(r, reader=args.reader, now=now)
                if not worth:
                    print(paint(f"      skipped: {why}", "dim"))
                    print()
                    continue

                d = carrier.send(subject=tp.subject_for(r), html=r.html,
                                 text=r.push_text, dry_run=not args.send)
                if d.error:
                    print(paint(f"      DELIVERY FAILED: {d.error}",
                                severity.Tier.BLOCKING))
                else:
                    print(paint(f"      {d.detail}  ({why})",
                                severity.Tier.SLOWING if d.dry_run else severity.Tier.BACKGROUND))
                    if d.sent:
                        tp.record_sent(r.route, reader=args.reader, now=now,
                                       detail=f"{carrier.name}:{d.target}")
                print(paint(f"      {r.path}", "dim"))
                print()
            return 0

        if args.snapshot:
            from detour import editor

            written = editor.snapshot_signals(sources.fetch_signals(use_cache=False))
            print(f"Recorded {written} signal states.")
            return 0

        if args.clusters:
            now = sources.now_utc()
            signals = sources.fetch_signals(use_cache=not args.fresh)
            swept = cluster.batch_timestamps(signals)
            events = cluster.cluster_signals(signals)
            swept_count = sum(1 for s in signals if s.get("_since") in swept)
            multi = sum(1 for e in events if e.is_cluster)
            print()
            print(f"  {len(signals)} degraded signals")
            print(f"  {swept_count} restamped by the overnight sweep, excluded")
            print(f"  {len(events)} events, {multi} of them multi-signal")
            print()
            for event in cluster.notable(events, now=now):
                if event.is_cluster:
                    print(f"  [{event.size}] {event.label}")
                    for m in event.members[:8]:
                        print(f"        {(m.get('location_name') or '').strip()[:52]}")
                    print()
            return 0

        if args.events:
            print(run_editor(args.limit, model=args.model, use_cache=not args.fresh))
            return 0

        if args.config:
            print(config.describe())
            return 0

        if args.models:
            for name in llm.list_gemini_models():
                print(" ", name)
            return 0

        if args.verify:
            print(run_verifier([args.verify], model=args.model,
                               use_cache=not args.fresh))
            return 0

        if args.verify_route:
            from detour import verifier as verifier_mod

            now = sources.now_utc()
            route_obj = rt.load_route(args.verify_route)
            zones = sources.fetch_work_zones(use_cache=not args.fresh)
            on_route = [m.record for m in rt.match_work_zones(route_obj, zones)]
            targets = verifier_mod.reported_records(on_route, now=now)

            seen: list[str] = []
            for zone in targets:
                rid = str(zone.get("id") or "")
                if rid and rid not in seen:
                    seen.append(rid)
            if not seen:
                print("Nothing on this route is in the Reported tier.")
                return 0
            print(run_verifier(seen[: args.limit], model=args.model,
                               use_cache=not args.fresh))
            return 0

        if args.confirm:
            claim = ledger.confirm(args.confirm, present=not args.absent)
            state = "present" if claim.claim == "present" else "absent"
            print(f"Recorded: {args.confirm} observed {state} at {claim.observed_at}")
            return 0

        if args.ledger:
            tally = ledger.accuracy()
            if not tally["total"]:
                print("No resident observations recorded yet.")
                return 0
            share = tally["absent"] / tally["total"]
            print(
                f"\n  {tally['total']} resident observation(s)\n"
                f"    present : {tally['present']}\n"
                f"    absent  : {tally['absent']}\n\n"
                f"  {share:.0%} of checked permits described work that was not there.\n"
            )
            return 0

        if args.audit:
            print(audit(use_cache=not args.fresh))
            return 0

        if not args.route:
            parser.error("give a route file, or pass --audit")

        result = brief_mod.build(
            rt.load_route(args.route),
            rewrite=not args.no_rewrite,
            use_cache=not args.fresh,
            with_detour=not args.no_detour,
            with_transit=not args.no_transit,
        )
    except (RuntimeError, ValueError, OSError) as exc:
        print(f"detour: {exc}", file=sys.stderr)
        return 1

    print(json.dumps(as_dict(result), indent=2) if args.json else render(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
