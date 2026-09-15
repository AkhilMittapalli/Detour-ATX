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

export function buildGraph(centerline) {
  const edges = [];
  const out = new Map();
  const nodePoints = new Map();

  const add = (tail, head, length, seconds, points, name, segId) => {
    const i = edges.length;
    edges.push({ tail, head, length, seconds, points, name, segId, blocked: false });
    if (!out.has(tail)) out.set(tail, []);
    out.get(tail).push(i);
    if (!out.has(head)) out.set(head, []);
  };

  for (const row of centerline) {
    const pts = coordsOf(row.the_geom);
    if (pts.length < 2) continue;
    const length = pathLength(pts);
    if (length <= 0) continue;

    const seconds = length / speedMs(row);
    const name = (row.full_street_name || row.street_name || '').trim().toUpperCase();
    const segId = String(row.segment_id || row.objectid || '');
    const tail = nodeKey(pts[0]), head = nodeKey(pts[pts.length - 1]);
    if (tail === head) continue;

    nodePoints.set(tail, pts[0]);
    nodePoints.set(head, pts[pts.length - 1]);

    const [fwd, back] = directions(row);
    if (fwd) add(tail, head, length, seconds, pts, name, segId);
    if (back) add(head, tail, length, seconds, pts.slice().reverse(), name, segId);
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
      const next = cost + edge.seconds;
      if (next < (best.get(edge.head) ?? Infinity)) {
        best.set(edge.head, next);
        came.set(edge.head, [node, i]);
        queue.push([next, edge.head]);
      }
    }
  }

  if (!best.has(goal)) return null;
  const indices = [];
  let metres = 0, cursor = goal;
  while (cursor !== start) {
    const step = came.get(cursor);
    if (!step) return null;
    indices.push(step[1]);
    metres += graph.edges[step[1]].length;
    cursor = step[0];
  }
  indices.reverse();
  return { seconds: best.get(goal), metres, indices };
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
