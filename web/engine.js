/* Detour ATX — the engine, ported from the Python package.
 *
 * Everything here runs in the browser. Austin's open data endpoints all send
 * CORS headers and Socrata will filter server-side by bounding box, so a
 * route's worth of data is about a megabyte and there is no backend to run.
 *
 * The logic is a faithful port of detour/{geo,graph,reroute,confidence,
 * severity,workcal}.py, which carry the reasoning behind each rule. Where a
 * constant looks arbitrary it is not, and the Python source says why.
 */

/* ------------------------------------------------------------------ geo */

const EARTH_R = 6371008.8;
const rad = (d) => (d * Math.PI) / 180;

export function haversine(a, b) {
  const p1 = rad(a[1]), p2 = rad(b[1]);
  const dp = p2 - p1, dl = rad(b[0] - a[0]);
  const h = Math.sin(dp / 2) ** 2 + Math.cos(p1) * Math.cos(p2) * Math.sin(dl / 2) ** 2;
  return 2 * EARTH_R * Math.asin(Math.sqrt(h));
}

function localXY(p, lat0) {
  return [rad(p[0]) * EARTH_R * Math.cos(rad(lat0)), rad(p[1]) * EARTH_R];
}

export function pointToSegment(p, a, b) {
  const lat0 = (a[1] + b[1] + p[1]) / 3;
  const [px, py] = localXY(p, lat0);
  const [ax, ay] = localXY(a, lat0);
  const [bx, by] = localXY(b, lat0);
  const dx = bx - ax, dy = by - ay;
  if (dx === 0 && dy === 0) return Math.hypot(px - ax, py - ay);
  let t = ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy);
  t = Math.max(0, Math.min(1, t));
  return Math.hypot(px - (ax + t * dx), py - (ay + t * dy));
}

export function pointToPath(p, path) {
  if (!path || !path.length) return Infinity;
  if (path.length === 1) return haversine(p, path[0]);
  let best = Infinity;
  for (let i = 0; i < path.length - 1; i++) {
    const d = pointToSegment(p, path[i], path[i + 1]);
    if (d < best) best = d;
  }
  return best;
}

export function pathToPath(a, b, giveUpAt) {
  if (!a.length || !b.length) return Infinity;
  let best = Infinity;
  for (const p of a) {
    best = Math.min(best, pointToPath(p, b));
    if (giveUpAt != null && best <= giveUpAt) return best;
  }
  for (const p of b) {
    best = Math.min(best, pointToPath(p, a));
    if (giveUpAt != null && best <= giveUpAt) return best;
  }
  return best;
}

export function bbox(path) {
  let minx = Infinity, miny = Infinity, maxx = -Infinity, maxy = -Infinity;
  for (const [x, y] of path) {
    if (x < minx) minx = x; if (x > maxx) maxx = x;
    if (y < miny) miny = y; if (y > maxy) maxy = y;
  }
  return [minx, miny, maxx, maxy];
}

export function padBbox(box, metres) {
  const [x0, y0, x1, y1] = box;
  const dlat = metres / 111320;
  const mid = (y0 + y1) / 2;
  const dlon = metres / (111320 * Math.max(0.1, Math.cos(rad(mid))));
  return [x0 - dlon, y0 - dlat, x1 + dlon, y1 + dlat];
}

export const boxesOverlap = (a, b) =>
  !(a[2] < b[0] || b[2] < a[0] || a[3] < b[1] || b[3] < a[1]);

export function coordsOf(geometry) {
  if (!geometry || !geometry.coordinates) return [];
  const { type, coordinates: c } = geometry;
  if (type === 'Point') return [[+c[0], +c[1]]];
  if (type === 'LineString') return c.map(([x, y]) => [+x, +y]);
  if (type === 'MultiLineString') return c.flat().map(([x, y]) => [+x, +y]);
  if (type === 'Polygon') return c[0].map(([x, y]) => [+x, +y]);
  return [];
}

export function pathLength(path) {
  let total = 0;
  for (let i = 0; i < path.length - 1; i++) total += haversine(path[i], path[i + 1]);
  return total;
}

export function densify(path, maxGap = 60) {
  if (path.length < 2) return path.slice();
  const out = [path[0]];
  for (let i = 0; i < path.length - 1; i++) {
    const a = path[i], b = path[i + 1];
    const steps = Math.max(1, Math.ceil(haversine(a, b) / maxGap));
    for (let s = 1; s <= steps; s++) {
      const t = s / steps;
      out.push([a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t]);
    }
  }
  return out;
}

/* ---------------------------------------------------------------- graph */

const NODE_PRECISION = 6;
const MPH_TO_MS = 0.44704;
const ROAD_CLASS_SPEED = {
  '1': 60, '2': 55, '4': 45, '5': 40, '6': 30, '8': 30, '10': 25, '15': 20, '16': 20,
};

const nodeKey = (p) => `${p[0].toFixed(NODE_PRECISION)},${p[1].toFixed(NODE_PRECISION)}`;

function speedMs(row) {
  let mph = parseFloat(row.speed_limit);
  if (!Number.isFinite(mph) || mph <= 0) {
    mph = ROAD_CLASS_SPEED[(row.road_class || '').trim()] ?? 30;
  }
  return Math.max(5, mph) * MPH_TO_MS;
}

// B both, FT from-to, TF to-from. One row in the layer carries a lowercase
// 'b' and two carry nothing, so normalise rather than trust the field.
function directions(row) {
  const v = (row.one_way || 'B').trim().toUpperCase();
  if (v === 'FT') return [true, false];
  if (v === 'TF') return [false, true];
  return [true, true];
}

export function buildGraph(centerline, { mode = 'drive', facilities = null } = {}) {
  const edges = [];
  const out = new Map();
  const nodePoints = new Map();

  const add = (tail, head, length, seconds, points, name, segId, stress, facility) => {
    const i = edges.length;
    edges.push({ tail, head, length, seconds, points, name, segId,
                 blocked: false, stress, facility });
    if (!out.has(tail)) out.set(tail, []);
    out.get(tail).push(i);
    if (!out.has(head)) out.set(head, []);
  };

  const cycling = mode === 'bike';
  const index = cycling && facilities && facilities.length ? new FacilityIndex(facilities) : null;

  for (const row of centerline) {
    /* Bikes are not legal on a freeway, so a freeway is not an edge of the
     * bike network. Dropping it rather than penalising it means a corridor
     * with no legal crossing reports "no route" — true, and better than
     * quietly routing someone onto IH 35. */
    if (cycling && bikesProhibited(row.road_class)) continue;

    const pts = coordsOf(row.the_geom);
    if (pts.length < 2) continue;
    const length = pathLength(pts);
    if (length <= 0) continue;

    const name = (row.full_street_name || row.street_name || '').trim().toUpperCase();
    const segId = String(row.segment_id || row.objectid || '');

    let seconds, stress = 1, facility = null;
    if (cycling) {
      const match = index ? index.lookup(pts, name) : null;
      facility = match ? match.facility : null;
      seconds = length / cyclingSpeedMs(facility);
      stress = stressMultiplier(mphOf(row), facility, match ? match.comfort : null);
    } else {
      seconds = length / speedMs(row);
    }

    const tail = nodeKey(pts[0]), head = nodeKey(pts[pts.length - 1]);
    if (tail === head) continue;

    nodePoints.set(tail, pts[0]);
    nodePoints.set(head, pts[pts.length - 1]);

    const [fwd, back] = directions(row);
    if (fwd) add(tail, head, length, seconds, pts, name, segId, stress, facility);
    if (back) add(head, tail, length, seconds, pts.slice().reverse(), name, segId, stress, facility);
  }
  return { edges, out, nodePoints };
}

export function nearestNode(graph, point) {
  let best = null, bestD = Infinity;
  for (const [key, p] of graph.nodePoints) {
    const d = haversine(point, p);
    if (d < bestD) { bestD = d; best = key; }
  }
  return best;
}

class Heap {
  constructor() { this.a = []; }
  get size() { return this.a.length; }
  push(item) {
    const a = this.a; a.push(item);
    let i = a.length - 1;
    while (i > 0) {
      const p = (i - 1) >> 1;
      if (a[p][0] <= a[i][0]) break;
      [a[p], a[i]] = [a[i], a[p]]; i = p;
    }
  }
  pop() {
    const a = this.a, top = a[0], last = a.pop();
    if (a.length) {
      a[0] = last;
      let i = 0;
      for (;;) {
        const l = 2 * i + 1, r = l + 1;
        let m = i;
        if (l < a.length && a[l][0] < a[m][0]) m = l;
        if (r < a.length && a[r][0] < a[m][0]) m = r;
        if (m === i) break;
        [a[m], a[i]] = [a[i], a[m]]; i = m;
      }
    }
    return top;
  }
}

export function shortestPath(graph, start, goal, { avoidBlocked = true } = {}) {
  if (!graph.out.has(start) || !graph.out.has(goal)) return null;

  const best = new Map([[start, 0]]);
  const came = new Map();
  const seen = new Set();
  const queue = new Heap();
  queue.push([0, start]);

  while (queue.size) {
    const [cost, node] = queue.pop();
    if (seen.has(node)) continue;
    seen.add(node);
    if (node === goal) break;

    for (const i of graph.out.get(node) || []) {
      const edge = graph.edges[i];
      if (avoidBlocked && edge.blocked) continue;
      const next = cost + edge.seconds * (edge.stress || 1);
      if (next < (best.get(edge.head) ?? Infinity)) {
        best.set(edge.head, next);
        came.set(edge.head, [node, i]);
        queue.push([next, edge.head]);
      }
    }
  }

  if (!best.has(goal)) return null;

  /* best.get(goal) is perceived cost, which is what Dijkstra minimised and
   * what nobody should ever be shown. Re-walk the path for the real
   * duration. In driving mode every stress is 1 and the two agree. */
  const indices = [];
  let metres = 0, seconds = 0, cursor = goal;
  while (cursor !== start) {
    const step = came.get(cursor);
    if (!step) return null;
    indices.push(step[1]);
    metres += graph.edges[step[1]].length;
    seconds += graph.edges[step[1]].seconds;
    cursor = step[0];
  }
  indices.reverse();
  return { seconds, metres, indices };
}

/* Map-match a saved route leg by leg.
 *
 * Routing endpoint-to-endpoint by travel time returns the globally fastest
 * path, which downtown is almost always the interstate — so a closure on the
 * surface street the user actually drives never enters the baseline and the
 * detour delta silently reports no impact. */
export function followRoute(graph, waypoints) {
  if (waypoints.length < 2) return null;
  const nodes = [];
  for (const p of waypoints) {
    const n = nearestNode(graph, p);
    if (n && (!nodes.length || n !== nodes[nodes.length - 1])) nodes.push(n);
  }
  if (nodes.length < 2) return null;

  let seconds = 0, metres = 0;
  const indices = [];
  for (let i = 0; i < nodes.length - 1; i++) {
    const leg = shortestPath(graph, nodes[i], nodes[i + 1], { avoidBlocked: false });
    if (!leg) continue;
    seconds += leg.seconds; metres += leg.metres;
    indices.push(...leg.indices);
  }
  return indices.length ? { seconds, metres, indices } : null;
}

export function streetSequence(graph, indices) {
  const names = [];
  for (const i of indices) {
    const n = graph.edges[i].name;
    if (n && names[names.length - 1] !== n) names.push(n);
  }
  return names;
}

export const countTurns = (graph, indices) =>
  Math.max(0, streetSequence(graph, indices).length - 1);

/* Edge indices to one continuous polyline, for drawing. */
export function pathGeometry(graph, indices) {
  const out = [];
  for (const i of indices) {
    for (const p of graph.edges[i].points) {
      if (!out.length || haversine(out[out.length - 1], p) > 1) out.push(p);
    }
  }
  return out;
}


/* ----------------------------------------------------------------- tolls */

/* Austin's centreline layer has no toll column, so this reads the road name.
 * That is a real limitation and the UI says so, but it is far from guesswork:
 * the fully tolled facilities carry their designation in the name, and the
 * tolled lanes of otherwise free highways are labelled TOLL explicitly.
 *
 * Two things this deliberately does NOT do:
 *
 *   It does not match US 183, US 290, SH 71 or MoPac wholesale. Those are
 *   mostly free roads with a tolled section, and blanket-avoiding them would
 *   send people the long way round a highway they could use for nothing.
 *
 *   It does not match TOLL as a substring. Austin has a TOLLINGTON ST, a
 *   TOLLESBORO CV and a TOLLARD LN, and routing a resident around their own
 *   street would be a memorable way to lose their trust.
 */
const TOLL_FACILITY = /(^|[^A-Z0-9])183A([^A-Z0-9]|$)|(^|[^A-Z0-9])SH ?45([^0-9]|$)|(^|[^A-Z0-9])SH ?130([^0-9]|$)|(^|[^A-Z0-9])45 ?[NESW]{1,2}([^A-Z0-9]|$)|(^|[^A-Z0-9])130 ?[NESW]{1,2}([^A-Z0-9]|$)/i;
const TOLL_WORD = /\bTOLL\b/i;

export const isTollRoad = (name) => {
  const n = (name || '').toUpperCase();
  return TOLL_WORD.test(n) || TOLL_FACILITY.test(n);
};

/* Mark every tolled edge blocked, reusing the same mechanism closures use.
 * Returns how many edges were taken out, so the caller can tell the reader
 * whether avoiding tolls actually changed anything. */
export function blockTolls(graph) {
  let blocked = 0;
  for (const edge of graph.edges) {
    if (isTollRoad(edge.name)) { edge.blocked = true; blocked++; }
  }
  return blocked;
}

/* -------------------------------------------------------------- reroute */

const EDGE_MATCH_M = 28;
const MAX_RATIO = 4, RATIO_FLOOR_M = 800, MAX_EXTRA_M = 8000;
const BASELINE_INFLATION_LIMIT = 1.6;

const TYPE_WORDS = new Set(['ST','STREET','RD','ROAD','DR','DRIVE','AVE','AVENUE','BLVD',
  'BOULEVARD','LN','LANE','PL','PLACE','CT','COURT','HWY','HIGHWAY','PKWY','PARKWAY',
  'TRL','TRAIL','CV','COVE','SVRD','N','S','E','W','NB','SB','EB','WB']);

function nameTokens(name) {
  const all = (name || '').toUpperCase().replace(/\//g, ' ').split(/\s+/).filter(Boolean);
  const kept = all.filter((t) => !TYPE_WORDS.has(t));
  return new Set(kept.length ? kept : all);
}

const shareToken = (a, b) => [...a].some((t) => b.has(t));

/* Only a full closure subtracts anything. A lane restriction slows traffic
 * but does not change the topology, and treating it as a removal would
 * invent diversions nobody needs to take. */
export function blockClosure(graph, zone) {
  const road = (zone.road_names || '').trim();
  const label = (zone.name || road || 'closure').trim();
  const impact = (zone.vehicle_impact || '').trim().toLowerCase();
  if (impact !== 'all-lanes-closed') return { label, road, edges: [], note: 'not a full closure' };

  const pts = coordsOf(zone.geometry);
  if (pts.length < 2) return { label, road, edges: [], note: 'no usable geometry' };

  const box = padBbox(bbox(pts), EDGE_MATCH_M + 40);
  const near = [];
  graph.edges.forEach((edge, i) => {
    if (!boxesOverlap(box, bbox(edge.points))) return;
    if (pathToPath(pts, edge.points, EDGE_MATCH_M) <= EDGE_MATCH_M) near.push(i);
  });
  if (!near.length) return { label, road, edges: [], note: 'no graph edges near this closure' };

  // Not one record in the feed carries a verified position, so if the permit
  // names one street and the geometry sits on another, refuse rather than
  // silently delete the wrong road.
  let matched = near;
  if (road) {
    const want = nameTokens(road);
    const agreeing = near.filter((i) => shareToken(nameTokens(graph.edges[i].name), want));
    if (agreeing.length) matched = agreeing;
    else {
      const found = [...new Set(near.map((i) => graph.edges[i].name).filter(Boolean))].slice(0, 3);
      return { label, road, edges: [], nameMatched: false,
               note: `geometry sits on ${found.join(', ') || 'unnamed roads'}, not ${road}` };
    }
  }
  matched.forEach((i) => { graph.edges[i].blocked = true; });
  return { label, road, edges: matched };
}

export function detourDelta(graph, waypoints, closures) {
  if (waypoints.length < 2) return null;
  const start = nearestNode(graph, waypoints[0]);
  const goal = nearestNode(graph, waypoints[waypoints.length - 1]);
  if (!start || !goal) return null;

  graph.edges.forEach((e) => { e.blocked = false; });

  const baseline = followRoute(graph, waypoints);
  if (!baseline) return null;

  // A saved route that map-matches into a zigzag is running against a
  // one-way street; every comparison after that is meaningless.
  const drawn = pathLength(waypoints);
  const baselineSuspect = drawn > 0 && baseline.metres / drawn > BASELINE_INFLATION_LIMIT;

  const blockages = closures.map((c) => blockClosure(graph, c));
  const blocked = new Set(blockages.flatMap((b) => b.edges));
  const basePath = new Set(baseline.indices);
  const hits = [...blocked].some((i) => basePath.has(i));

  const base = {
    baselineM: baseline.metres, baselineS: baseline.seconds,
    baselineTurns: countTurns(graph, baseline.indices),
    via: streetSequence(graph, baseline.indices),
    blockages, baselineSuspect,
  };

  if (!blocked.size || !hits) return { ...base, detourM: baseline.metres, detourS: baseline.seconds, extraM: 0, extraS: 0, extraTurns: 0, affected: false };

  const diverted = shortestPath(graph, start, goal, { avoidBlocked: true });
  if (!diverted) return { ...base, unreachable: true, affected: true, extraM: 0, extraS: 0, extraTurns: 0 };

  const extraM = diverted.metres - baseline.metres;
  const implausible = extraM > MAX_EXTRA_M ||
    (baseline.metres > RATIO_FLOOR_M && diverted.metres / baseline.metres > MAX_RATIO);

  return {
    ...base,
    detourM: diverted.metres, detourS: diverted.seconds,
    detourTurns: countTurns(graph, diverted.indices),
    detourLine: pathGeometry(graph, diverted.indices),
    via: streetSequence(graph, diverted.indices),
    extraM, extraS: diverted.seconds - baseline.seconds,
    extraTurns: Math.max(0, countTurns(graph, diverted.indices) - base.baselineTurns),
    implausible,
    affected: !baselineSuspect && !implausible && extraM > 5,
  };
}

export function deltaSummary(d) {
  if (!d) return '';
  if (d.baselineSuspect) return 'Saved route does not map-match cleanly — it may run against a one-way street.';
  if (d.unreachable) return 'No way through — every route around this closure is also cut.';
  if (d.implausible) return 'Detour looks implausible, which usually means the closure geometry is wrong.';
  if (!d.affected) return 'No change to your route.';
  const miles = d.extraM / 1609.344, minutes = d.extraS / 60;
  const parts = [miles >= 0.05 ? `+${miles.toFixed(1)} mi` : 'about the same distance'];
  parts.push(minutes >= 0.5 ? `+${Math.round(minutes)} min` : 'no extra time');
  if (d.extraTurns) parts.push(`+${d.extraTurns} turn${d.extraTurns > 1 ? 's' : ''}`);
  return parts.join(', ');
}

/* -------------------------------------------------------------- workcal */

const ALWAYS_ON = /\b(24\/7|24-7|around the clock|continuous(ly)?)\b/i;
const NIGHT_WORK = /\b(night(\s*time|\s*work)?|overnight|after hours)\b/i;
const WEEKEND_WORK = /\b(weekend|saturday|sunday)\b/i;
// "24/7" in these permits is usually an obligation on the contractor —
// "ACCESS TO RESTAURANT MUST BE MAINTAINED 24/7" — not a work schedule.
const ACCESS_CLAUSE = /\b(access|egress|entry|entrance|ingress|open|passable|maintain(ed|ing)?|pedestrian|sidewalk|driveway)\b/i;

const WORK_START = 7, WORK_END = 18;

export function austinNow(now = new Date()) {
  // Central time without a tz database: US DST runs second Sunday in March
  // to first Sunday in November.
  const y = now.getUTCFullYear();
  const nth = (month, weekday, n) => {
    const first = new Date(Date.UTC(y, month, 1));
    const offset = (weekday - first.getUTCDay() + 7) % 7;
    return new Date(Date.UTC(y, month, 1 + offset + 7 * (n - 1)));
  };
  const start = nth(2, 0, 2), end = nth(10, 0, 1);
  const utcNoonish = new Date(now.getTime() - 6 * 3600e3);
  const offset = utcNoonish >= start && utcNoonish < end ? -5 : -6;
  return new Date(now.getTime() + offset * 3600e3);
}

function scheduleHints(description) {
  const text = description || '';
  const hints = new Set();
  for (const clause of text.split(/[.;\n]/)) {
    if (ALWAYS_ON.test(clause) && !ACCESS_CLAUSE.test(clause)) { hints.add('always_on'); break; }
  }
  if (NIGHT_WORK.test(text)) hints.add('night');
  if (WEEKEND_WORK.test(text)) hints.add('weekend');
  return hints;
}

const DAYS = ['Sunday','Monday','Tuesday','Wednesday','Thursday','Friday','Saturday'];

export function crewPlausible(now, description) {
  const hints = scheduleHints(description);
  const local = austinNow(now);
  if (hints.has('always_on')) return [true, 'permit says the work runs continuously'];

  const dow = local.getUTCDay();
  const hh = String(local.getUTCHours()).padStart(2, '0');
  const mm = String(local.getUTCMinutes()).padStart(2, '0');

  if ((dow === 0 || dow === 6) && !hints.has('weekend'))
    return [false, `it is ${DAYS[dow]} and the permit does not mention weekend work`];

  const hour = local.getUTCHours();
  if (hints.has('night')) {
    return hour >= 20 || hour < 6
      ? [true, 'permit describes night work and it is night']
      : [false, 'permit describes night work and it is daytime'];
  }
  if (hour < WORK_START) return [false, `it is ${hh}:${mm} local, before typical working hours`];
  if (hour >= WORK_END) return [false, `it is ${hh}:${mm} local, after typical working hours`];
  return [true, `it is ${DAYS[dow]} at ${hh}:${mm} local, within working hours`];
}

/* A closure's barricades stay up even when no crew is working, so this adds
 * context and never suppresses. */
export function activityNote(zone, now) {
  const [ok, why] = crewPlausible(now, zone.description);
  return ok ? '' : `no crew expected right now — ${why}, though any closure is likely still in place`;
}

/* ----------------------------------------------------------- confidence */

export const CONFIDENCE = { CONFIRMED: 'Confirmed', REPORTED: 'Reported', OVER: 'Probably over' };

const CLOSEOUT = [
  [/\bCLEARED\s+THE\s+ROW\b/i, 'permit notes say the crew cleared the right of way'],
  [/\bWORK\s+(IS\s+)?COMPLETED?\b/i, 'permit notes say the work is complete'],
  [/\b(CANCELL?ED|VOIDED|WITHDRAWN)\b/i, 'permit appears cancelled or voided'],
  [/\bNO\s+LONGER\s+(ACTIVE|NEEDED)\b/i, 'permit notes say it is no longer active'],
];

const LONG_PERMIT_DAYS = 180, NEAR_TERM_DAYS = 30;
const asBool = (v) => v === true || String(v).toLowerCase() === 'true';
const fmtDate = (d) => `${d.getUTCDate()} ${d.toLocaleString('en', { month: 'short', timeZone: 'UTC' })} ${d.getUTCFullYear()}`;

export function scoreWorkZone(zone, now, observation) {
  // A resident sighting outranks everything: the city verifies a date on 126
  // records out of 3,884 and a position on none of them.
  if (observation === 'present') return { level: CONFIDENCE.CONFIRMED, reasons: ['a resident confirmed this is there'] };
  if (observation === 'absent') return { level: CONFIDENCE.OVER, reasons: ['a resident reported this is not there'] };

  const start = zone.start_date ? new Date(zone.start_date) : null;
  const end = zone.end_date ? new Date(zone.end_date) : null;
  const description = zone.description || '';
  const reasons = [];

  if (start && start > now) return { level: CONFIDENCE.OVER, reasons: [`permit does not start until ${fmtDate(start)}`] };
  if (end && end < now) return { level: CONFIDENCE.OVER, reasons: [`permit window closed ${fmtDate(end)}`] };

  for (const [pattern, reason] of CLOSEOUT) {
    if (pattern.test(description)) return { level: CONFIDENCE.OVER, reasons: [reason] };
  }

  if (asBool(zone.are_workers_present)) {
    // Self-reported through a check-in app and not reliably cleared when
    // crews leave, so out of hours it is a stale checkbox, not a sighting.
    const [plausible, why] = crewPlausible(now, description);
    if (!plausible) reasons.push(`crew is checked in, but ${why}`);
    else return { level: CONFIDENCE.CONFIRMED, reasons: ['crew checked in on site'] };
  }

  if (asBool(zone.is_end_date_verified)) {
    reasons.push('end date verified by the city');
    if (asBool(zone.critical_corridor)) reasons.push('flagged as a critical corridor');
    return { level: CONFIDENCE.CONFIRMED, reasons };
  }

  const windowDays = start && end ? Math.round((end - start) / 86400e3) : null;
  if (windowDays != null && windowDays > LONG_PERMIT_DAYS)
    return { level: CONFIDENCE.OVER, reasons: [`permit window runs ${windowDays} days, so the dates say little about today`] };

  if (windowDays != null) reasons.push(`permit window is ${windowDays} days`);
  if (asBool(zone.critical_corridor)) reasons.push('flagged as a critical corridor');
  if (end && (end - now) / 86400e3 <= NEAR_TERM_DAYS) reasons.push(`scheduled to end ${fmtDate(end)}`);
  if (!reasons.length) reasons.push('permit is inside its window but unverified');
  return { level: CONFIDENCE.REPORTED, reasons };
}

/* ------------------------------------------------------------- severity */

export const TIER = { BLOCKING: 2, SLOWING: 1, BACKGROUND: 0 };
export const TIER_LABEL = { 2: 'Blocking', 1: 'Slowing', 0: 'Background' };
export const TIER_ASK = {
  2: 'Change route or leave earlier',
  1: 'Add a few minutes',
  0: 'Nothing — context only',
};

// A driver loses a minute; someone using a wheelchair loses the route. The
// feed has no field for it, so it comes out of the description.
const PEDESTRIAN = /(sidewalk[^.]{0,40}clos|clos[^.]{0,40}sidewalk|pedestrian[^.]{0,30}(detour|clos|reroute)|crosswalk[^.]{0,30}clos|curb ramp|\bADA\b)/i;

export const affectsPedestrians = (zone) =>
  PEDESTRIAN.test(`${zone.name || ''} ${zone.description || ''}`);

export function workZoneTier(zone) {
  const impact = (zone.vehicle_impact || '').trim().toLowerCase();
  if (impact === 'all-lanes-closed') return TIER.BLOCKING;
  if (impact === 'some-lanes-closed') {
    return asBool(zone.critical_corridor) || asBool(zone.are_workers_present) || affectsPedestrians(zone)
      ? TIER.SLOWING : TIER.BACKGROUND;
  }
  return affectsPedestrians(zone) ? TIER.SLOWING : TIER.BACKGROUND;
}

export const signalTier = (signal) =>
  /flash/i.test(signal.operation_text || '') ? TIER.BLOCKING : TIER.BACKGROUND;

/* Probably-over is capped at Slowing — never Blocking, because one false
 * alarm costs more trust than ten missed lane restrictions; but never
 * dropped to Background either, because a visible low-confidence item is
 * what prompts someone to tap "still there?". */
export function combineTier(tier, verdict) {
  return verdict.level === CONFIDENCE.OVER ? Math.min(tier, TIER.SLOWING) : tier;
}

export const isPushable = (tier, verdict) =>
  tier === TIER.BLOCKING && verdict.level === CONFIDENCE.CONFIRMED;

/* ======================================================================
 * The cyclist view
 *
 * Measured against the live feeds: 1,819 of 4,056 work zones sit within
 * 20 m of dedicated bike infrastructure, 614 of those on protected or
 * high-comfort infrastructure — and only 87 of the 1,819 mention bikes
 * anywhere in the description. 4.8%.
 *
 * The second failure is quieter. vehicle_impact is written from a car:
 * 1,593 of those zones say "some-lanes-closed", which for a driver means
 * losing a lane and waiting. If the closed lane IS the bike lane, the rider
 * lost 100% of their lanes. We re-tier rather than inherit that.
 * ====================================================================== */

/* bike_level_of_comfort is undocumented in the portal, so these were
 * decoded by cross-tabbing against bicycle_facility across all 17,753 rows:
 * H is 453 protected one-way + 224 two-way + 48 buffered; HP is 1,066 paved
 * trail; HU is 426 unpaved trail; M is painted lanes; L is shoulders.
 * EL, SS, RT and TC do not separate cleanly, so they stay unranked — with
 * the 3,949 rows carrying no code, that is 30% we decline to rate. */
const COMFORT_RANK = { H: 3, HP: 3, HU: 2, M: 2, L: 1 };

export const DEDICATED = new Set([
  'Bike Lane', 'Bike Lane - Buffered', 'Bike Lane - Protected One-Way',
  'Bike Lane - Protected Two-Way', 'Bike Lane - wParking', 'Bike Lane - Climbing',
  'Trail - Paved', 'Trail - Unpaved', 'Neighborhood Bikeway', 'Sharrows', 'Shared Lane',
]);

/* Something physical, or a whole quiet street, between rider and traffic.
 * Losing one is categorically different from losing paint, because there is
 * nowhere comfortable to fall back to. */
export const SEPARATED = new Set([
  'Bike Lane - Protected One-Way', 'Bike Lane - Protected Two-Way',
  'Bike Lane - Buffered', 'Trail - Paved', 'Neighborhood Bikeway',
]);

/* Interstates and tollways (1), divided highways (2), freeway ramps (10).
 * Bicycles are prohibited on all three in Texas. Not a theoretical guard:
 * routing downtown to south Austin, the I-35 mainlane matched a "Shared
 * Lane" at 0.0 m and a paved trail at 11.9 m, because frontage roads and
 * the shared-use path run within metres of the mainlane centreline. */
const BIKES_PROHIBITED = new Set(['1', '2', '10']);
export const bikesProhibited = (roadClass) => BIKES_PROHIBITED.has((roadClass || '').trim());

export const comfortRank = (code) =>
  code ? (COMFORT_RANK[code.trim().toUpperCase()] ?? null) : null;

export const isDedicated = (f) => DEDICATED.has((f || '').trim());
export const isSeparated = (f) => SEPARATED.has((f || '').trim());

/* The layer's own labels are database values, not English. "Bike Lane -
 * Protected One-Way" is precise and reads terribly in the middle of a
 * sentence, so every facility gets a phrase a person would actually say. */
const FACILITY_LABEL = {
  'Bike Lane': 'bike lane',
  'Bike Lane - Buffered': 'buffered bike lane',
  'Bike Lane - Protected One-Way': 'protected bike lane',
  'Bike Lane - Protected Two-Way': 'two-way protected bike lane',
  'Bike Lane - wParking': 'bike lane beside parking',
  'Bike Lane - Climbing': 'uphill bike lane',
  'Trail - Paved': 'paved trail',
  'Trail - Unpaved': 'unpaved trail',
  'Neighborhood Bikeway': 'neighbourhood bikeway',
  Sharrows: 'shared-lane markings',
  'Shared Lane': 'shared lane',
};

export const facilityLabel = (f) =>
  FACILITY_LABEL[(f || '').trim()] || (f || '').trim().toLowerCase() || 'bike route';

const CYCLIST = /(bike\s*lane|bicycle\s*lane|bikeway|sharrow|shared\s*lane|(bike|bicycle|cycl\w*)[^.]{0,30}(clos|detour|reroute|restrict)|(clos|detour|reroute)[^.]{0,30}(bike|bicycle)|\bshoulder[^.]{0,25}clos)/i;

/* Almost always false. That is the finding, not a weak pattern. */
export const affectsCyclists = (zone) =>
  CYCLIST.test(`${zone.name || ''} ${zone.description || ''} ${zone.road_names || ''}`);

/* Metres per second on the flat. Austin is not flat, but the centreline
 * layer carries no elevation and pretending to model gradient would be
 * invention. */
const CYCLING_SPEED_MS = 4.4, UNPAVED_SPEED_MS = 3.3;
export const cyclingSpeedMs = (f) =>
  (f || '').trim() === 'Trail - Unpaved' ? UNPAVED_SPEED_MS : CYCLING_SPEED_MS;

/* How much worse a metre feels than it measures. Calibrated judgement, not
 * measurement: it follows the shape of the Level of Traffic Stress
 * literature, where stress rises sharply with motor traffic speed once no
 * separation exists. Deliberately steep at the top — a 50 mph arterial with
 * no facility scores 9, so Dijkstra treats 1 km of it as worse than 8 km of
 * neighbourhood street. That is intended, not an artefact. */
const MIXED_TRAFFIC_STRESS = [[25, 1.6], [35, 2.6], [45, 4.5], [Infinity, 9]];

export function stressMultiplier(mph, facility, comfort) {
  const name = (facility || '').trim();
  if (SEPARATED.has(name)) return 1;
  if (name === 'Sharrows' || name === 'Shared Lane') return 1.8;
  if (DEDICATED.has(name)) return 1.35;

  let base = 9;
  for (const [ceiling, penalty] of MIXED_TRAFFIC_STRESS) {
    if (mph <= ceiling) { base = penalty; break; }
  }

  /* An unrated street is not evidence of a bad street. Where the city rated
   * it comfortable, take the rating; where it said nothing, fall back to the
   * traffic-speed estimate rather than assuming the worst. */
  const rank = comfortRank(comfort);
  if (rank === 3) return Math.min(base, 1.2);
  if (rank === 2) return Math.min(base, 1.9);
  return base;
}

export function mphOf(row) {
  const mph = parseFloat(row.speed_limit);
  if (Number.isFinite(mph) && mph > 0) return mph;
  return ROAD_CLASS_SPEED[(row.road_class || '').trim()] ?? 30;
}

/* The layer is an export of the whole Comprehensive Transportation Network,
 * so 11,617 of its 17,753 rows are ordinary street carrying a comfort rating
 * and no bike facility at all. Join against all of them and 74% of work
 * zones appear to "affect cyclists" — true of nothing, useful to nobody. */
export function indexFacilities(rows, { dedicatedOnly = true } = {}) {
  const out = [];
  for (const row of rows) {
    const facility = (row.bicycle_facility || '').trim();
    if (dedicatedOnly && !DEDICATED.has(facility)) continue;
    const points = coordsOf(row.the_geom);
    if (points.length < 2) continue;
    out.push({
      points, box: bbox(points), facility,
      comfort: (row.bike_level_of_comfort || '').trim() || null,
      lineType: (row.line_type || '').trim(),
      street: (row.full_street_name || '').trim().toUpperCase(),
      separated: SEPARATED.has(facility),
    });
  }
  return out;
}

/* Grid-bucketed lookup from a street segment to its bike facility. A linear
 * scan is fine for a handful of work zones and far too slow here: a
 * cross-town corridor is tens of thousands of centreline rows against
 * thousands of facility segments. */
class FacilityIndex {
  constructor(facilities) {
    this.cell = 0.004; // roughly 400 m
    this.cells = new Map();
    for (const f of facilities) {
      const [minLon, minLat, maxLon, maxLat] = f.box;
      for (let cx = Math.floor(minLon / this.cell); cx <= Math.floor(maxLon / this.cell); cx++) {
        for (let cy = Math.floor(minLat / this.cell); cy <= Math.floor(maxLat / this.cell); cy++) {
          const key = cx + ',' + cy;
          if (!this.cells.has(key)) this.cells.set(key, []);
          this.cells.get(key).push(f);
        }
      }
    }
  }

  /* Proximity alone is not enough, and the failure is not hypothetical.
   * Downtown, the I-35 mainlane sits 0.0 m from a service-road shared lane
   * and 11.9 m from the shared-use path, because that is how a stacked
   * urban freeway is built. Two guards: an off-street facility never
   * credits a road segment, and an on-street facility must agree on the
   * street name whenever both names are known. */
  lookup(points, name, tolerance = 15) {
    const mid = points[Math.floor(points.length / 2)];
    const key = Math.floor(mid[0] / this.cell) + ',' + Math.floor(mid[1] / this.cell);

    let best = null, bestD = tolerance;
    for (const f of this.cells.get(key) || []) {
      if (f.lineType.startsWith('Off-Street')) continue;
      if (name && f.street && f.street !== name) continue;
      const d = pathToPath(points, f.points, tolerance);
      if (d < bestD) { best = f; bestD = d; }
    }
    return best;
  }
}

export function nearestFacility(path, facilities, tolerance = 20) {
  if (!path.length) return null;
  const search = padBbox(bbox(path), tolerance + 25);
  let best = null, bestD = tolerance;
  for (const f of facilities) {
    if (!boxesOverlap(search, f.box)) continue;
    const d = pathToPath(path, f.points, tolerance);
    if (d < bestD) { best = f; bestD = d; }
  }
  return best ? { facility: best, distance: bestD } : null;
}

/* Re-read a work zone as a cyclist rather than as a driver.
 *
 * The re-tiering is the point. some-lanes-closed is the feed's most common
 * impact value and it is written from a car. When the closed lane is a bike
 * lane, the rider has not lost some of their options — they have lost all
 * of them, and the fallback is a live traffic lane.
 *
 * We never do the reverse. all-lanes-closed is not softened here, because
 * being wrong in that direction puts someone in front of a truck. */
export function bikeImpact(zone, facilities, tolerance = 20) {
  const hit = nearestFacility(coordsOf(zone.geometry), facilities, tolerance);
  if (!hit) return null;

  const { facility, distance } = hit;
  const impact = (zone.vehicle_impact || '').trim().toLowerCase();
  const saidSo = affectsCyclists(zone);

  const label = facilityLabel(facility.facility);

  let tier, note;
  if (impact === 'all-lanes-closed') {
    tier = TIER.BLOCKING;
    note = `Full closure across the ${label}`;
  } else if (facility.separated) {
    tier = TIER.BLOCKING;
    note = `The ${label} is closed. The feed calls this a partial closure, which is true for a car and not for a bike`;
  } else {
    tier = TIER.SLOWING;
    note = `Work in the ${label} — expect to merge out`;
  }

  return { facility, label, distance, separated: facility.separated, saidSo, tier, note };
}

/* How much of a route runs on infrastructure built for bikes, and how
 * hostile the rest of it is. Both numbers go in front of the rider before
 * they commit to the ride. */
export function routeComfort(graph, indices) {
  let total = 0, onFacility = 0, onSeparated = 0, weighted = 0, worst = 1, hostile = 0;
  for (const i of indices) {
    const e = graph.edges[i];
    const stress = e.stress || 1;
    total += e.length;
    weighted += e.length * stress;
    worst = Math.max(worst, stress);
    if (stress >= 2.6) hostile += e.length;
    if (e.facility) {
      onFacility += e.length;
      if (SEPARATED.has(e.facility)) onSeparated += e.length;
    }
  }
  if (!total) return null;
  return {
    metres: total,
    facilityShare: onFacility / total,
    separatedShare: onSeparated / total,
    avgStress: weighted / total,
    worstStress: worst,
    hostileMetres: hostile,
  };
}
