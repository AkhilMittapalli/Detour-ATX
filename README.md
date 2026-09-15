# Detour ATX

A daily briefing of what is broken on your route, built from the City of
Austin's open feeds.

Austin publishes every road closure in the city and never publishes a single
detour. This is the first slice of a product that fills that gap.

**Phase 1** is deliberately rules-only — no agents, no model in the critical
path — so there is a deterministic baseline any later agent has to beat to
justify its latency and cost. **Phase 2** adds the routable street graph, the
derived detour delta, and the evidence ledger behind the ground truth loop.
**Phase 3** adds the Verifier and the Desk Editor — the two roles where an
agent earns its keep. All of it is built and running against live data.

---

## Using it

No dependencies. Python 3.11+.

```bash
python cli.py --add-route "Commute" --from "4550 Mueller Blvd" --to "1100 Congress Ave"
python cli.py --run-all
```

That is the whole product loop: two addresses in, a routed path saved, and a
brief per route written to `out/` every morning. See
[SCHEDULE.md](SCHEDULE.md) for running it on a timer.

Routes are geocoded and routed with the city's own services &mdash; the
`COA_Locator` geocoder and the Street Centerline layer &mdash; so there is no
third-party mapping key anywhere in the project.

To inspect a single route:

```bash
python cli.py routes/guadalupe.json
```

```bash
python cli.py --audit
```

```bash
python cli.py --confirm <record_id>          # a resident saw it; --absent if not
python cli.py --ledger                       # ground truth tally
```

```bash
python cli.py --verify-route routes/guadalupe.json --limit 3
python cli.py --clusters                     # clustering only, no model calls
python cli.py --events                       # cluster, then interpret
python cli.py --snapshot                     # record state so recurrence works later
python cli.py --models                       # model ids this key can use
```

Export a snapshot for the dashboard:

```bash
python -c "from detour import export; export.write('dist/data.json')"
```

```bash
python -m unittest discover tests
```

Useful flags: `--json` for machine-readable output, `--fresh` to bypass the
five-minute response cache, `--no-rewrite` to skip the model call,
`--no-detour` to skip building the street graph.

### Optional environment

| Variable | Effect |
|---|---|
| `SOCRATA_APP_TOKEN` | Lifts Socrata's shared-pool rate limit. Free, and worth having before any polling loop. |
| `GEMINI_API_KEY` | Enables the Verifier agent and the plain-language rewrite. Set it in your environment — never paste a key into a file or a chat window. |
| `ANTHROPIC_API_KEY` | Alternative provider for the rewrite. Gemini wins if both are set. |
| `GEMINI_MODEL` | Override the model id. `--models` lists what your key can actually use. |

Without any key the deterministic cleaner runs instead, the Verifier reports a
clear error, and every other command works unchanged.

---

## What it does

1. **Loads a saved route** as an ordered list of waypoints and densifies it
   to a vertex roughly every 60 m.
2. **Joins it to four live feeds** — work zones, degraded traffic signals,
   active dispatch incidents, and the street centreline.
3. **Scores confidence** on every disruption.
4. **Assigns a severity tier** based on what the reader has to decide.
5. **Subtracts full closures** from a routable graph built out of the
   centreline layer, re-routes, and reports the delta.
6. **Renders a brief**, most decision-forcing first.

### The detour delta

Austin publishes closures and no detours — `event_type` is `work-zone` on all
3,853 records, though the WZDx standard it publishes to also defines `detour`.
The official diversion exists on an orange sign bolted to a barricade and was
never digitised.

We cannot invent that signed route, so we compute the *consequence* instead.
The centreline layer is genuinely routable: in a downtown sample, 91% of
segment endpoints are shared by more than one segment, with three- and
four-way intersections dominating. Subtract the closed edges, re-route, report
the difference:

```
  ROUTE IMPACT

    +0.3 mi, no extra time, +3 turns
    via TRINITY ST -> E CESAR CHAVEZ ST -> BRAZOS ST -> E 11TH ST
```

Reporting the delta rather than turn-by-turn is deliberate. It is what a
commuter wants before deciding whether to leave early, it keeps us out of a
routing fight with Google that we would lose, and it degrades gracefully — if
routing fails, the closure still shows.

Three guards, because no work zone record carries a verified position:

| Guard | Catches |
|---|---|
| **Name agreement** | A permit naming COLORADO ST whose geometry sits on LAVACA ST. Refused, not silently applied to the wrong road. |
| **Plausibility** | A detour wildly out of proportion to the trip — usually bad geometry rather than a real diversion. |
| **Baseline inflation** | A saved route that map-matches into a zigzag, which means it runs against a one-way street. |

### The ground truth loop

A resident standing at a closure is the only verified observation anyone
holds — the city verifies a date on 126 of 3,853 records and a position on
none. So a fresh sighting outranks the permit:

```
before        Slowing | Reported      | permit window is 30 days, critical corridor
--confirm     Slowing | Confirmed     | a resident confirmed this is there
--absent      Slowing | Probably over | a resident reported this is not there
```

Observations land in an append-only ledger and expire after 48 hours, so a
stale confirmation stops overriding the feed instead of pinning a verdict
forever. `--ledger` tallies them, which over time answers the question the
city cannot: what share of active right-of-way permits describe work that is
really happening.

### The confidence model

This is the point of the whole thing. Austin flags its own uncertainty and
the numbers are stark — of 3,853 work zone records, 126 carry a verified end
date and **none** carry a verified position. Dates are permit windows, not
work windows: one observed record covers replacing wiring between two utility
poles, runs for a full year, and its own notes say the crew already left.

Running `--audit` against live data on 13 Sep 2026:

```
  Confirmed         109   2.8%
  Reported        2,422  62.9%
  Probably over   1,322  34.3%

  Eligible to push: 109 (2.8%)
```

So nothing is presented as fact. Every item carries a label and the reasons
that produced it, and **only `Confirmed` items may ever generate a push
notification** — a rule enforced in `Verdict.pushable` and independent of
how dramatic the disruption would be if it were real.

### The severity tiers

| Tier | Asks the reader to |
|---|---|
| Blocking | Change route or leave earlier |
| Slowing | Add a few minutes |
| Background | Nothing — context only |

Severity and confidence are orthogonal and combined at the end. A record we
believe is probably finished is capped at Slowing — never Blocking, because
one false alarm costs more trust than ten missed lane restrictions; but never
silently dropped to Background either, because a visible low-confidence item
is exactly what prompts someone to tap "still there?" and hand us ground
truth.

---

## The Verifier

The one role here that justifies an agent. The question — *does this permit
describe something that is actually on the ground today* — cannot be answered
by a rule, because **the evidence path branches**. Sometimes the description
settles it. Sometimes it cites another permit by number and you have to go
read that one. Sometimes the signal is an absence.

It runs only over the **Reported tier** — the records the deterministic model
could not resolve either way. Confirmed and Probably-over already have an
answer, and spending a model call to re-derive one is waste.

### Tools

| Tool | Answers |
|---|---|
| `permit_narrative` | The record's own fields, plus any permit numbers its description references |
| `search_311` | Nearby 311 activity — and says so explicitly when there is none |
| `overlapping_permits` | Other permits on the same stretch; several suggests real staged work |
| `find_permit` | Follows a cross-reference to another permit |
| `check_geometry` | Whether the coordinates sit on the street the permit names |
| `recent_incidents` | Live dispatch activity at the site |

`check_geometry` is deterministic and exposed as a tool anyway — the agent
decides *when* location correctness matters, which is the judgement; the
check itself should never be a model's guess.

### The 311 vocabulary is not what you would expect

The obvious request types are dead. `Lane/Road Closure Notification` (11,613
all-time) and `Obstruction in ROW` (12,797) have taken **zero** records since
before June 2026. The live corroboration signals are `TPW - Activate/Deactivate
Work Zone` and `TPW - Construction Concerns in Right of Way`, so those are what
the tool looks for. Anyone building on the all-time counts would be querying a
retired vocabulary and concluding, wrongly, that nobody reports closures.

### An agent may suppress a notification, never authorise one

A resident sighting is an observation. An agent verdict is inference over
permit text. They are not interchangeable evidence, and the ledger keeps them
apart by source:

| Source | "absent" | "present" |
|---|---|---|
| Resident | Probably over | **Confirmed** — pushable |
| Verifier | Probably over | Reported — recorded as a reason, **not** pushable |

The asymmetry is the safety property. Wrongly suppressing costs a missed lane
restriction. Wrongly authorising puts a model in the loop of deciding whose
phone buzzes, on data where no record has a verified position. Enforced in
`confidence.score_work_zone` and tested in `tests/test_verifier.py`.

### Budget and failure

`MAX_STEPS` caps tool rounds and `MAX_TOOLS_PER_RUN` caps total calls, so one
pathological record cannot burn a night's spend. An unparseable reply is an
error, never a guessed verdict. The system prompt tells the model to use
"unclear" freely, because a confident wrong answer is worse than an honest
one when the output gates a notification.

---

## The Desk Editor

Clustering is arithmetic; interpretation is not. `cluster.py` decides *that*
seven signals on Riverside Drive are one event, using distance and time.
`editor.py` decides *why*, and that is an agent because each hypothesis is
tested by a different lookup:

| Hypothesis | Tool that tests it |
|---|---|
| A crew cut the fibre | `excavation_nearby` — digging permits beside the signals |
| Deliberate event flash | `venue_nearby`, `prior_occurrences` |
| A crash took out a pole | `incidents_nearby` |
| Upstream comms or power | a tight cluster with none of the above |

Two live readings, both correct:

```
3 signals unscheduled (conflict) flash within 4 min
  cause: event_flash  0.70   driver impact: hazard
  - Moody Center is 202 m away, DKR Stadium 253 m
  - entered flash sequentially over four minutes
  - no dispatch incidents nearby

7 signals communication issue within 0 min
  cause: construction  0.70   driver impact: none
  - permitted wastewater excavation active within 400 m on E Riverside
  - all seven reported at 13:01 UTC within the same minute
```

The second is the one that shows why this layer exists. Seven signals are
"down" and the correct driver impact is **none** — a communication issue
means the city lost telemetry, not that the signal stopped working. A naive
product alerts seven times about nothing.

### Recurrence needs history the city does not publish

The signals feed is a current-state table: 131 rows saying what is broken
now, nothing about yesterday. Recurrence is the strongest discriminator
between a fault and a scheduled flash, so `--snapshot` accumulates it the
same way the ground truth loop accumulates observations.

---

## The nightly sweep

The clustering found this, and it was a live correctness bug, not just noise.

An early run produced an 87-signal "cluster" spanning FM 2222 to Mueller to
Southwest Parkway. Checking the timestamps:

```
2026-09-13 09:00:59 UTC  x90      <- 04:00 local, every one second == 59
2026-09-13 09:00:44 UTC  x19
2026-09-13 13:01:52 UTC  x7       <- the real Riverside event
```

**109 of 131 degraded signals are restamped by an overnight sweep.** Their
`operation_state_datetime` is when the batch last counted them, not when the
fault began.

Two consequences. Clustering had to exclude them or it chained half the city
into one event. And every duration derived from them was wrong — the brief
was reporting a signal unreachable for years as unreachable for hours. Items
built on a swept timestamp now say the onset is unknown instead of quoting a
false figure.

Detection is about shape rather than a hardcoded hour: many signals, at one
instant, spread wider than any single cause reaches. A genuine seven-signal
corridor event at the same minute survives it.

---

## The work calendar

This module exists because **the agent found it, not me**. Asked about a
30-day duct bank permit whose window had opened two days earlier, the Verifier
answered `unclear` and explained: the window is open, but today is a Sunday.

That is fully deterministic, so having a model rediscover it on every record
is waste. `workcal.py` encodes it, which both improves the rules baseline and
gives the agent a tool so it can stop reasoning it out from scratch.

The important half is what it does **not** do. Two different questions hide
inside "is this work zone real":

1. **Is the restriction in place?** Cones and barricades stay up overnight and
   through the weekend. For a driver this is the question that matters.
2. **Is a crew actively working?** Day and hour dependent.

So it never suppresses a closure. A Sunday full closure is still a full
closure, and the brief says so explicitly — *"no crew expected right now, it is
Sunday ... though any closure is likely still in place"*. A reader who drives
around a closure that is genuinely there trusts us less next time.

What it does change: a contractor's self-reported `are_workers_present`
check-in no longer reaches `Confirmed` at 3am on a Sunday. That flag is set
through a check-in app and is not reliably cleared when crews leave, so out of
hours it is a stale checkbox rather than a sighting — and `Confirmed` is what
gates push.

### The "24/7" trap

Permits declaring continuous work are exempt from all of the above. But the
first version of that check was wrong in a way worth recording: it matched

> `**ACCESS TO RESTAURANT MUST BE MAINTAINED 24/7**`

which is a promise to keep a doorway reachable, not a work schedule — the
*opposite* of continuous work. It made a Sunday lane closure look actively
staffed. The check now runs clause by clause and ignores `24/7` inside an
access obligation, while still catching a genuine one in a different clause of
the same description.

---

## Things the data does, that cost real time to find

Written down because they are not in any documentation and each one produced
a bug before it produced a fix.

- **Timestamps are not consistently zoned.** Work zones and incidents are UTC
  with a trailing `Z`. Signal status is naive local Central with no offset.
  Mixing them is a five-hour error, which here means claiming a signal has
  been flashing since before it was.
- **Default ordering is not newest-first.** Every query that cares about
  recency says so explicitly, or it reads 2013 rows and concludes the feed is
  dead.
- **Work zones arrive one row per direction.** A single closure on Colorado
  St appears twice, northbound and southbound, under one permit `name`.
  Without grouping, the brief reports one closure as two.
- **The feed publishes mangled characters.** `U+00BF` shows up mid-sentence
  where a dash or an inch mark was destroyed by a cp1252/UTF-8 round trip
  inside the city's permit system — "a 9x5<?> duct bank". The corruption is in
  the published data, not in our decoding.
- **`direction` can be the literal string `"unknown"`.**
- **Descriptions carry permit administration**, including extension ledgers
  four entries deep, contractor cross-references, work order numbers, and the
  names of city staff who signed off.
- **Proximity is the wrong test for "on my route".** A cross-town route
  passes within metres of every street it crosses. A street has to stay near
  the route for a sustained stretch to count as one you actually drive on.

---

## Known limitations

- **Routes must respect one-way streets.** `routes/trinity.json` originally
  ran north down a street that is `one_way=FT` northbound; every leg then
  looped around the block, the baseline inflated 2.6x, and the detour came out
  *shorter* than the "normal" route. The inflation guard now catches this, but
  the route still has to be authored correctly.
- **Routes are traced, not hand-placed.** Both saved routes come from the
  city's own centreline layer. A hand-drawn route of a few straight legs cuts
  diagonally across the grid and map-matches poorly.
- **Turns are counted as street-name changes**, not bearings. Cheaper, robust,
  and close to what a driver feels — but it misses a turn onto a street of the
  same name and counts a name change that is not a turn.
- **Incidents have no closure semantics.** The dispatch feed has no
  road-closure category, so a crash that shuts a lane is indistinguishable
  from a fender bender. Everything active is Slowing — honest about what we
  know.
- **Close-out phrasing is partly hypothesis.** Only `CLEARED THE ROW` is
  confirmed in live data. The other patterns in `CLOSEOUT_PATTERNS` are
  plausible variants and should be scored individually once the ground truth
  loop has confirmations.

---

## Layout

```
detour/
  geo.py          pure-stdlib geometry — distance, densify, bbox
  sources.py      Socrata clients, and the timezone normalisation
  confidence.py   the confidence model, and why each verdict was reached
  severity.py     three tiers, and how confidence caps them
  route.py        saved routes and the spatial join
  describe.py     permit narrative to one readable sentence
  graph.py        routable street graph, Dijkstra, route map-matching
  reroute.py      closure subtraction and the detour delta
  ledger.py       append-only evidence store behind the ground truth loop
  llm.py          provider-agnostic model access, Gemini and Anthropic
  evidence.py     the lookups the Verifier can call
  verifier.py     the agent: tool declarations, loop, verdict parsing
  workcal.py      whether a crew is plausibly on site right now
  cluster.py      grouping failures into events, and sweep detection
  editor.py       the Desk Editor agent: why a cluster happened
  geocode.py      addresses to coordinates, via the city's own locator
  plan.py         two addresses to a routed, saved commute
  correspondent.py  what is worth telling a reader who was told yesterday
  deliver.py      the brief as standalone, email-safe HTML
  daily.py        the morning run across every saved route
  transport.py    file, email and webhook delivery, dry-run by default
  baseline.py     311 close-time distributions per request type
  advocate.py     drafts a 311 report. No send path, by design.
  evaluate.py     does the agent actually change any decisions?
  protobuf.py     a minimal protobuf wire-format reader
  transit.py      CapMetro rider impact from GTFS-Realtime
  export.py       JSON snapshot for a frontend, basemap included
dist/             the published dashboard (index.html + data.js)
  brief.py        assembly and ordering
cli.py            terminal renderer, --audit, --confirm, --ledger,
                  --verify, --verify-route, --models, --json
routes/           saved routes, traced from the centreline layer
tests/            202 offline tests, no network, no API key
```

## The dashboard

`dist/` is a published page showing a live snapshot: every full closure in
central Austin shaded by confidence, the clustered signal events with the Desk
Editor's readings, and the sweep finding.

Two constraints shaped it. Map tiles are a cross-origin image request and are
blocked, so the basemap is drawn as vectors from the city's own centreline
layer — 4,335 segments that travel with the data. And 4,335 polylines would
choke SVG, so the map is canvas.

The export is pure ASCII (`json.dumps` escapes non-ASCII by default) and the
markup uses HTML entities, so neither depends on a `Content-Type` charset
being set correctly — which `python -m http.server` does not do.

## Next

The Correspondent — per-reader memory, so a closure told for ninety straight
days stops being told, while that same closure slipping its end date by three
months becomes news again.

Then the evaluation that matters. Austin's 311 archive holds 2,542,175
requests, 2,529,210 of them with a close date — 99.5% complete over twelve
years. That gives a per-type median close time as a free baseline to measure
agent-drafted requests against.
