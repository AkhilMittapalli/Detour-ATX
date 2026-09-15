/* Detour ATX - the app.
 *
 * Two Austin addresses in, a briefing of what is broken on the way out.
 * Runs entirely in the browser: Austin's endpoints send CORS headers and
 * Socrata filters server-side by bounding box, so there is no backend.
 */

import * as E from './engine.js';
import * as D from './data.js';
import { createMap } from './map.js';
import { recommend, planningOptions } from './advice.js';

const CORRIDOR_PAD_M = 2000;
const WAYPOINT_SPACING_M = 110;
const MATCH = { zone: 45, signal: 55, incident: 70 };
const SUGGEST_DEBOUNCE_MS = 180;

const el = (id) => document.getElementById(id);
const esc = (s) => String(s ?? '').replace(/[&<>"]/g, (c) =>
  ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));

const state = { db: null, sample: null, routes: [], brief: null, busy: false,
                map: null, raw: null, when: null };

/* ---------------------------------------------------------- capabilities */

async function connect() {
  try { state.db = await window.claude?.use?.('db'); } catch { state.db = null; }
  try { state.sample = await window.claude?.use?.('sample'); } catch { state.sample = null; }
  if (state.db) loadRoutes();
}

async function loadRoutes() {
  if (!state.db) return;
  try {
    const snap = await state.db.collection('data/users/me/routes').orderBy('saved').get();
    state.routes = (snap.docs || snap || []).map((d) => ({ id: d.id, ...(d.data ? d.data() : d) }));
  } catch { state.routes = []; }
  renderRoutes();
}

/* ------------------------------------------------------------- typeahead */

function attachTypeahead(inputId, listId) {
  const input = el(inputId);
  const list = el(listId);
  let timer = null;
  let controller = null;
  let active = -1;

  const close = () => { list.hidden = true; list.innerHTML = ''; active = -1; };

  const choose = (text) => {
    input.value = text;
    close();
    input.focus();
  };

  const paint = (items) => {
    if (!items.length) return close();
    list.innerHTML = items
      .map((t, i) => `<li role="option" tabindex="-1" data-i="${i}"${i === active ? ' aria-selected="true"' : ''}>${esc(t)}</li>`)
      .join('');
    list.hidden = false;
  };

  input.addEventListener('input', () => {
    clearTimeout(timer);
    controller?.abort();
    const text = input.value;
    if (text.trim().length < 3) return close();

    timer = setTimeout(async () => {
      controller = new AbortController();
      let items = [];
      try {
        items = await D.suggestAddresses(text, { signal: controller.signal });
      } catch { items = []; }
      active = -1;
      paint(items);
    }, SUGGEST_DEBOUNCE_MS);
  });

  input.addEventListener('keydown', (event) => {
    const options = [...list.querySelectorAll('li')];
    if (list.hidden || !options.length) {
      if (event.key === 'Enter') el('run').click();
      return;
    }
    if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
      event.preventDefault();
      active = (active + (event.key === 'ArrowDown' ? 1 : -1) + options.length) % options.length;
      paint(options.map((o) => o.textContent));
    } else if (event.key === 'Enter') {
      event.preventDefault();
      choose(options[active >= 0 ? active : 0].textContent);
    } else if (event.key === 'Escape') {
      close();
    }
  });

  list.addEventListener('mousedown', (event) => {
    const option = event.target.closest('li');
    if (option) { event.preventDefault(); choose(option.textContent); }
  });

  input.addEventListener('blur', () => setTimeout(close, 120));
}

/* --------------------------------------------------------------- routing */

function thin(points, spacing) {
  if (points.length < 3) return points.slice();
  const kept = [points[0]];
  for (const p of points.slice(1, -1)) {
    if (E.haversine(kept[kept.length - 1], p) >= spacing) kept.push(p);
  }
  kept.push(points[points.length - 1]);
  return kept;
}

async function planRoute(fromText, toText, note, { avoidTolls = false } = {}) {
  note('Looking up addresses');
  const [origin, destination] = await Promise.all([D.geocode(fromText), D.geocode(toText)]);

  if (E.haversine(origin.point, destination.point) < 200) {
    throw new Error('Those two addresses are the same place.');
  }

  const box = E.padBbox(E.bbox([origin.point, destination.point]), CORRIDOR_PAD_M);

  note('Loading the street network');
  const centerline = await D.fetchCenterline(box);
  if (!centerline.length) {
    throw new Error('No street data for that corridor. Both addresses need to be in Austin.');
  }

  // Draw the basemap the moment it exists, so the map is doing something
  // truthful while the rest of the data is still arriving.
  state.map.showNetwork(
    centerline
      .map((r) => ({
        line: E.coordsOf(r.the_geom),
        name: (r.full_street_name || r.street_name || '').trim(),
        cls: (r.road_class || '6').trim(),
      }))
      .filter((s) => s.line.length > 1),
    E.padBbox(box, -300)
  );

  const graph = E.buildGraph(centerline);
  const start = E.nearestNode(graph, origin.point);
  const goal = E.nearestNode(graph, destination.point);

  // Always route once with tolls allowed, so the cost of avoiding them can
  // be stated rather than merely asserted.
  const withTolls = start && goal && E.shortestPath(graph, start, goal, { avoidBlocked: false });
  if (!withTolls) throw new Error('Could not find a road route between those addresses.');

  let found = withTolls;
  let toll = { avoided: false, usesTolls: false, extraS: 0, extraM: 0 };

  const tollEdges = new Set();
  graph.edges.forEach((e, i) => { if (E.isTollRoad(e.name)) tollEdges.add(i); });
  toll.usesTolls = withTolls.indices.some((i) => tollEdges.has(i));

  if (avoidTolls) {
    graph.edges.forEach((e) => { e.blocked = false; });
    E.blockTolls(graph);
    const free = E.shortestPath(graph, start, goal, { avoidBlocked: true });
    graph.edges.forEach((e) => { e.blocked = false; });

    if (free) {
      found = free;
      toll.avoided = true;
      toll.extraS = free.seconds - withTolls.seconds;
      toll.extraM = free.metres - withTolls.metres;
    } else {
      // Honest failure: say the toll-free route does not exist rather than
      // silently handing back a route that uses them.
      toll.impossible = true;
    }
  }

  const points = [];
  for (const i of found.indices) {
    for (const p of graph.edges[i].points) {
      if (!points.length || E.haversine(points[points.length - 1], p) > 1) points.push(p);
    }
  }

  const waypoints = thin(points, WAYPOINT_SPACING_M);
  state.map.showRoute(points, E.streetSequence(graph, found.indices));

  return {
    origin, destination, graph, box, waypoints, toll,
    path: E.densify(waypoints),
    line: points,
    via: E.streetSequence(graph, found.indices),
    metres: found.metres,
    seconds: found.seconds,
  };
}

/* ------------------------------------------------------------- the brief */

function matchAll(path, rows, geometryKey, tolerance) {
  const routeBox = E.padBbox(E.bbox(path), tolerance + 50);
  const out = [];
  for (const row of rows) {
    const pts = E.coordsOf(row[geometryKey]);
    if (!pts.length || !E.boxesOverlap(routeBox, E.bbox(pts))) continue;
    const distance = E.pathToPath(path, pts, tolerance);
    if (distance <= tolerance) out.push({ row, distance, pts });
  }
  return out.sort((a, b) => a.distance - b.distance);
}

/* A datetime-local field reads as the viewer's own wall clock. Austin is the
 * only place this app works, so the two only differ for someone planning a
 * trip from another timezone — and reading their input literally as Austin
 * time is what they meant. */
function chosenTime() {
  const raw = el('leave-at').value;
  if (!raw) return null;
  const parsed = new Date(raw);
  return Number.isNaN(parsed.getTime()) ? null : parsed;
}

async function buildBrief(plan, note) {
  const now = chosenTime() || new Date();

  note('Reading closures, signals and incidents');
  const [zones, signals, incidents] = await Promise.all([
    D.fetchWorkZones(plan.box),
    D.fetchSignals(),
    D.fetchIncidents(),
  ]);

  const swept = D.sweepTimestamps(signals);
  const observations = await loadObservations();

  // Keep the raw feeds so choosing a different travel time re-scores rather
  // than re-fetches: the confidence model and the work calendar are both
  // functions of the moment you ask about.
  state.raw = { plan, zones, signals, incidents, swept, observations, now };

  const items = [];
  const shapes = [];
  const markers = [];

  // Work zones arrive one row per direction; group so one closure is not
  // reported as two.
  const groups = new Map();
  for (const hit of matchAll(plan.path, zones, 'geometry', MATCH.zone)) {
    const key = (hit.row.name || hit.row.id || '').trim().toUpperCase();
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(hit);
  }

  for (const group of groups.values()) {
    const lead = group[0].row;
    const ids = group.map((g) => g.row.id).filter(Boolean);
    const seen = ids.map((id) => observations.get(id)).find(Boolean) || null;

    const verdict = E.scoreWorkZone(lead, now, seen);
    const tier = E.combineTier(E.workZoneTier(lead), verdict);

    const road = (lead.road_names || 'this street').trim();
    const impact = { 'all-lanes-closed': 'fully closed', 'some-lanes-closed': 'down to reduced lanes' }
      [(lead.vehicle_impact || '').toLowerCase()] || 'affected by work';

    const headings = [...new Set(group
      .map((g) => (g.row.direction || '').trim().toLowerCase())
      .filter((d) => d && d !== 'unknown'))];
    const heading = headings.length > 1 ? 'both directions' : headings[0] || '';

    const clauses = [];
    if (lead.description) clauses.push(cleanDescription(lead.description));
    clauses.push(verdict.reasons.join('; '));
    if (E.affectsPedestrians(lead)) clauses.push('also closes a sidewalk, crossing or ramp');
    const activity = E.activityNote(lead, now);
    if (activity) clauses.push(activity);

    const id = lead.id;
    items.push({
      kind: 'work_zone', id, tier, verdict,
      zone: lead, shapes: group.map((g) => g.pts),
      headline: `${road} ${impact}${heading ? `, ${heading}` : ''}`,
      detail: clauses.filter(Boolean).join(' - '),
      raw: lead.description || '',
      distance: group[0].distance,
      observed: seen,
    });
    for (const g of group) shapes.push({ id, line: g.pts, confidence: verdict.level });
  }

  /* Flashing signals are listed one by one: each is a driver-facing hazard
   * at a named intersection. Communication issues are collapsed into a
   * single line, because seventeen separate cards saying "the city cannot
   * see this signal, it works fine" is not a briefing, it is wallpaper —
   * and it buries the one item that actually blocks you. */
  const telemetry = [];

  for (const { row, distance, pts } of matchAll(plan.path, signals, 'location', MATCH.signal)) {
    const where = (row.location_name || 'An intersection on your route').trim();
    const flashing = /flash/i.test(row.operation_text || '');
    const onsetKnown = D.onsetIsReal(row, swept);
    const since = row._since ? describeAge(row._since, now) : null;
    const id = `signal:${row.signal_id}`;

    if (pts[0]) markers.push({ id, point: pts[0], flashing });

    if (!flashing) { telemetry.push({ id, where, distance }); continue; }

    items.push({
      kind: 'signal', id,
      tier: E.TIER.BLOCKING,
      verdict: { level: E.CONFIDENCE.CONFIRMED, reasons: [] },
      headline: `${where} - signal flashing${onsetKnown && since ? ` for ${since}` : ''}`,
      detail: 'The controller tripped its conflict monitor and fell back to flash. In Texas a flashing red is a stop, so treat it as a four-way stop.',
      distance,
    });
  }

  if (telemetry.length) {
    const names = telemetry.map((t) => t.where).filter(Boolean);
    const shown = names.slice(0, 6).join(' · ');
    const more = names.length > 6 ? ` and ${names.length - 6} more` : '';

    items.push({
      kind: 'signal_group',
      id: 'signals:telemetry',
      tier: E.TIER.BACKGROUND,
      verdict: { level: E.CONFIDENCE.REPORTED, reasons: [] },
      headline: telemetry.length === 1
        ? 'The city has lost its connection to 1 signal on your route'
        : `The city has lost its connection to ${telemetry.length} signals on your route`,
      detail: `The lights themselves are fine. Each one runs its own timing from a cabinet at the intersection, and you will not notice anything driving through. What is missing is the link back to Austin's traffic centre, so engineers cannot check on these lights or retime them from the office if there is a crash nearby. ${shown}${more}.`,
      distance: Math.min(...telemetry.map((t) => t.distance)),
      members: telemetry.map((t) => t.id),
    });
  }

  for (const { row, distance } of matchAll(plan.path, incidents, 'location', MATCH.incident)) {
    items.push({
      kind: 'incident', id: `incident:${row.traffic_report_id}`,
      tier: E.TIER.SLOWING,
      verdict: { level: E.CONFIDENCE.CONFIRMED, reasons: [] },
      headline: `${titleCase(row.issue_reported || 'Incident')} - ${titleCase(row.address || 'on your route')}`,
      detail: `Reported by Austin dispatch${row._published ? ` ${describeAge(row._published, now)} ago` : ''}.`,
      distance,
    });
  }

  const order = { [E.CONFIDENCE.CONFIRMED]: 0, [E.CONFIDENCE.REPORTED]: 1, [E.CONFIDENCE.OVER]: 2 };
  items.sort((a, b) => b.tier - a.tier || order[a.verdict.level] - order[b.verdict.level] || a.distance - b.distance);

  state.map.showDisruptions(shapes, markers);
  state.markers = markers;

  note('Working out the detour');
  const fullClosures = zones.filter((z) => (z.vehicle_impact || '').toLowerCase() === 'all-lanes-closed');
  const delta = E.detourDelta(plan.graph, plan.waypoints, fullClosures);

  if (delta && delta.affected && delta.detourLine) state.map.showDetour(delta.detourLine);

  return {
    plan, items, delta, now, when: now,
    advice: recommend({ items, delta, zones, now, when: now }),
    counts: { zones: zones.length, signals: signals.length, swept: swept.size },
  };
}

/* Re-score everything for a different travel time.
 *
 * No network call: the permits, signals and incidents are already in hand.
 * What changes is the question being asked of them, and both the confidence
 * model and the work calendar take the moment as an argument. */
function rescore(when) {
  const raw = state.raw;
  if (!raw || !state.brief) return;

  const items = [];
  for (const item of state.brief.items) {
    if (item.kind !== 'work_zone' || !item.zone) { items.push(item); continue; }

    const verdict = E.scoreWorkZone(item.zone, when, item.observed);
    const tier = E.combineTier(E.workZoneTier(item.zone), verdict);

    const clauses = [];
    if (item.zone.description) clauses.push(cleanDescription(item.zone.description));
    clauses.push(verdict.reasons.join('; '));
    if (E.affectsPedestrians(item.zone)) clauses.push('also closes a sidewalk, crossing or ramp');
    const activity = E.activityNote(item.zone, when);
    if (activity) clauses.push(activity);

    items.push({ ...item, verdict, tier, detail: clauses.filter(Boolean).join(' - ') });
  }

  const order = { [E.CONFIDENCE.CONFIRMED]: 0, [E.CONFIDENCE.REPORTED]: 1, [E.CONFIDENCE.OVER]: 2 };
  items.sort((a, b) => b.tier - a.tier || order[a.verdict.level] - order[b.verdict.level] || a.distance - b.distance);

  state.brief = {
    ...state.brief,
    items,
    when,
    advice: recommend({ items, delta: state.brief.delta, zones: raw.zones, now: raw.now, when }),
  };
  renderBrief(state.brief);
  state.map.showDisruptions(
    items.filter((i) => i.kind === 'work_zone' && i.shapes).flatMap((i) =>
      i.shapes.map((line) => ({ id: i.id, line, confidence: i.verdict.level }))),
    state.brief.markers || []
  );
}

/* Permit narrative is mostly administration. The model rewrite is offered
 * separately and on demand, because it costs the viewer. */
function cleanDescription(text) {
  let out = String(text)
    .replace(/[\u00bf\ufffd]/g, ' ')
    .replace(/\b[\w ]{0,40}Permit has been issued for this location\.?/gi, ' ')
    .replace(/\*\*\*.*?\*\*\*/gs, ' ')
    .replace(/\bEXTENDED\s+PER\s+[A-Z][A-Za-z.'-]*(\s+[A-Z][A-Za-z.'-]*)*/gi, ' ')
    .replace(/\b\d+\s+extension\s+from\s+\w+\s+\d{1,2},?\s+\d{4}\s+to\s+\w+\s+\d{1,2},?\s+\d{4}/gi, ' ')
    .replace(/\bWR\s*\d{4,}\s*-?\s*/gi, ' ')
    .replace(/\bDetails\s*:\s*/gi, ' ')
    .replace(/\s+/g, ' ')
    .trim();
  if (out.length > 200) out = out.slice(0, 197).trimEnd() + '…';
  return out;
}

const titleCase = (s) => String(s).trim().toLowerCase().replace(/\b[a-z]/g, (c) => c.toUpperCase());

function describeAge(then, now) {
  const days = Math.floor((now - then) / 86400e3);
  if (days >= 730) return `${Math.floor(days / 365)} years`;
  if (days >= 365) return 'over a year';
  if (days >= 2) return `${days} days`;
  return `${Math.max(1, Math.round((now - then) / 3600e3))} hours`;
}

/* ----------------------------------------------------------- ground truth */

async function loadObservations() {
  const map = new Map();
  if (!state.db) return map;
  try {
    const snap = await state.db.collection('observations').limit(500).get();
    const now = Date.now();
    for (const doc of snap.docs || snap || []) {
      const d = doc.data ? doc.data() : doc;
      // A sighting expires, so a stale confirmation stops pinning a verdict.
      if (d.at && now - d.at < 48 * 3600e3) map.set(d.recordId, d.claim);
    }
  } catch { /* shared store unavailable; the brief still works */ }
  return map;
}

async function observe(recordId, claim) {
  if (!state.db) return false;
  try {
    await state.db.doc(`observations/${recordId}`).set({ recordId, claim, at: Date.now() });
    return true;
  } catch { return false; }
}

/* ------------------------------------------------------------- rendering */

function renderRoutes() {
  const box = el('saved');
  if (!state.routes.length) { box.innerHTML = ''; return; }
  box.innerHTML =
    '<span class="saved-label">Saved</span>' +
    state.routes.map((r) =>
      `<button class="chip-btn" data-from="${esc(r.from)}" data-to="${esc(r.to)}">${esc(r.name)}</button>`
    ).join('');
}

function renderBrief(brief) {
  const { plan, items, delta, counts } = brief;
  const blocking = items.filter((i) => i.tier === E.TIER.BLOCKING);

  const summary = blocking.length
    ? `${blocking.length} thing${blocking.length > 1 ? 's' : ''} to plan around`
    : items.length ? `${items.length} update${items.length > 1 ? 's' : ''}, nothing blocking`
    : 'Nothing reported on your route';

  const local = E.austinNow(brief.now);
  const when = local.toUTCString().slice(0, 22).replace(/ GMT.*/, '');

  let html = `
    <div class="brief-head">
      <div>
        <div class="summary ${blocking.length ? 'hot' : ''}">${esc(summary)}</div>
        <div class="sub">${(plan.metres / 1609.344).toFixed(1)} mi &middot;
          about ${Math.round(plan.seconds / 60)} min &middot; ${esc(when)} Austin time</div>
      </div>
      <button class="ghost" id="again">New route</button>
    </div>
    <div class="via">${esc(plan.origin.address)} &rarr; ${esc(plan.destination.address)}</div>`;

  const { options } = planningOptions(brief.now);
  const activeWhen = (brief.when || brief.now).getTime();
  html += `
    <div class="plan">
      <span class="plan-label">Planning for</span>
      ${options.map((o) => `<button class="plan-btn${Math.abs(o.at.getTime() - activeWhen) < 60e3 ? ' on' : ''}"
        data-when="${o.at.getTime()}">${esc(o.label)}</button>`).join('')}
    </div>`;

  if (brief.advice && brief.advice.length) {
    html += `<div class="advice">
      <div class="advice-label">What to do</div>
      ${brief.advice.map((a) => `
        <div class="rec r-${a.kind}">
          <h4>${esc(a.title)}</h4>
          <p>${esc(a.body)}</p>
        </div>`).join('')}
    </div>`;
  }

  const toll = plan.toll || {};
  if (toll.impossible) {
    html += `<div class="tollnote warn">No toll-free route exists between these two
      addresses in the street data, so this route still uses a toll road.</div>`;
  } else if (toll.avoided) {
    const mins = Math.round(toll.extraS / 60);
    const miles = toll.extraM / 1609.344;
    html += `<div class="tollnote">${
      Math.abs(toll.extraS) < 30 && Math.abs(miles) < 0.1
        ? 'Routed around toll roads at no real cost.'
        : `Avoiding tolls costs about ${mins > 0 ? `${mins} min` : 'no extra time'}${
            miles > 0.05 ? ` and ${miles.toFixed(1)} mi` : ''}.`
    }</div>`;
  } else if (toll.usesTolls) {
    html += `<div class="tollnote">This route uses a toll road. Tick
      &ldquo;avoid toll roads&rdquo; to see what going without costs.</div>`;
  }

  if (delta && delta.affected) {
    html += `
      <div class="impact">
        <div class="impact-label">Route impact</div>
        <div class="impact-line">${esc(E.deltaSummary(delta))}</div>
        <div class="impact-via">via ${esc((delta.via || []).slice(0, 5).join(' → '))}</div>
      </div>`;
  }

  if (!items.length) {
    html += `<p class="empty">Nothing on this route is closed, flashing or under an active
      incident right now. Quiet is the normal state.</p>`;
  }

  let tier = null;
  for (const item of items) {
    if (item.tier !== tier) {
      tier = item.tier;
      html += `<div class="tier t${tier}"><span>${E.TIER_LABEL[tier]}</span>
        <span class="ask">${E.TIER_ASK[tier]}</span></div>`;
    }
    const confidence = item.verdict.level;
    const confSlug = confidence.replace(/\s+/g, '-').toLowerCase();
    html += `
      <article class="item t${item.tier} conf-${confSlug}" data-id="${esc(item.id)}">
        <div class="stripe"></div>
        <div class="body">
          <div class="chips">
            <span class="chip c-${confSlug}">${esc(confidence)}</span>
            ${item.observed ? '<span class="chip c-you">reported by a resident</span>' : ''}
          </div>
          <h3>${esc(item.headline)}</h3>
          <p>${esc(item.detail)}</p>
          ${item.kind === 'work_zone' ? `
            <div class="actions">
              <span class="ask-q">Driven past it?</span>
              <button class="tiny" data-observe="${esc(item.id)}" data-claim="present">Still there</button>
              <button class="tiny" data-observe="${esc(item.id)}" data-claim="absent">Gone</button>
              ${state.sample && item.raw ? `<button class="tiny alt" data-explain="${esc(item.id)}">Explain simply</button>` : ''}
            </div>
            <p class="explained" id="x-${esc(item.id)}" hidden></p>` : ''}
        </div>
      </article>`;
  }

  html += `<p class="foot">Built live from the City of Austin open data portal &mdash;
    ${counts.zones.toLocaleString()} work-zone records in this corridor,
    ${counts.signals} degraded signals citywide${counts.swept ? ', batch-restamped ones excluded' : ''}.
    Nothing here is confirmed unless it says so: of Austin&rsquo;s work-zone records, <strong>the city has
    verified a position on none of them</strong>.</p>`;

  el('brief').innerHTML = html;
  el('brief').hidden = false;
}

/* --------------------------------------------------------------- actions */

async function go(fromText, toText) {
  if (state.busy) return;
  state.busy = true;

  el('error').hidden = true;
  el('run').disabled = true;
  el('status').hidden = false;
  el('brief').hidden = true;
  el('stage').hidden = false;
  state.map.reset();

  const note = (msg) => { el('status').textContent = msg; };

  try {
    const plan = await planRoute(fromText, toText, note,
                                 { avoidTolls: el('avoid-tolls').checked });
    const brief = await buildBrief(plan, note);
    state.brief = brief;
    renderBrief(brief);
    el('setup').hidden = true;

    if (state.db) {
      const id = `${fromText} to ${toText}`.toLowerCase().replace(/[^a-z0-9]+/g, '-').slice(0, 60);
      try {
        await state.db.doc(`data/users/me/routes/${id}`).set({
          name: `${plan.origin.address.split(',')[0]} → ${plan.destination.address.split(',')[0]}`,
          from: fromText, to: toText, saved: Date.now(),
        });
        loadRoutes();
      } catch { /* saving is a convenience, never a reason to fail the brief */ }
    }
  } catch (err) {
    el('error').textContent = err.message || String(err);
    el('error').hidden = false;
    el('stage').hidden = true;
  } finally {
    state.busy = false;
    el('run').disabled = false;
    el('status').hidden = true;
  }
}

async function useMyLocation() {
  const button = el('locate');
  if (!navigator.geolocation) {
    el('error').textContent = 'This browser cannot share a location.';
    el('error').hidden = false;
    return;
  }
  button.disabled = true;
  button.textContent = 'Locating';
  try {
    const position = await new Promise((resolve, reject) =>
      navigator.geolocation.getCurrentPosition(resolve, reject, { timeout: 10000 }));
    const address = await D.reverseGeocode(position.coords.longitude, position.coords.latitude);
    el('from').value = address;
  } catch (err) {
    el('error').textContent =
      err && err.code === 1 ? 'Location permission was declined.' : 'Could not work out where you are.';
    el('error').hidden = false;
  } finally {
    button.disabled = false;
    button.textContent = 'Use my location';
  }
}

async function explain(id) {
  const item = state.brief?.items.find((i) => i.id === id);
  const target = el(`x-${id}`);
  if (!item || !target || !state.sample) return;

  target.hidden = false;
  target.textContent = 'Asking…';
  try {
    const { text } = await state.sample(
      'Rewrite this Austin right-of-way permit note as ONE plain sentence for a driver: ' +
      'who is doing what, and how the road is affected. Under 25 words. No permit numbers, ' +
      'no contractor or staff names.\n\nNote: ' + item.raw
    );
    target.textContent = text.trim();
  } catch {
    target.textContent = 'Could not rewrite that one.';
  }
}

/* ------------------------------------------------------------------ wire */

document.addEventListener('click', async (event) => {
  const chip = event.target.closest('.chip-btn');
  if (chip) {
    el('from').value = chip.dataset.from;
    el('to').value = chip.dataset.to;
    go(chip.dataset.from, chip.dataset.to);
    return;
  }

  if (event.target.id === 'again') {
    el('brief').hidden = true;
    el('stage').hidden = true;
    el('setup').hidden = false;
    return;
  }

  const plan = event.target.closest('[data-when]');
  if (plan) {
    rescore(new Date(Number(plan.dataset.when)));
    return;
  }

  if (event.target.id === 'swap') {
    const from = el('from').value;
    el('from').value = el('to').value;
    el('to').value = from;
    return;
  }

  if (event.target.id === 'locate') { useMyLocation(); return; }
  if (event.target.id === 'now') { el('leave-at').value = ''; return; }
  if (event.target.id === 'zin') { state.map.zoomIn(); return; }
  if (event.target.id === 'zout') { state.map.zoomOut(); return; }
  if (event.target.id === 'zfit') { state.map.fitView(); return; }

  const obs = event.target.closest('[data-observe]');
  if (obs) {
    const ok = await observe(obs.dataset.observe, obs.dataset.claim);
    obs.textContent = ok ? 'Thanks' : 'Not saved';
    obs.disabled = true;
    return;
  }

  const ex = event.target.closest('[data-explain]');
  if (ex) explain(ex.dataset.explain);
});

// Hovering an item lights up its geometry on the map, so the list and the
// picture are obviously the same thing.
document.addEventListener('pointerover', (event) => {
  const item = event.target.closest('.item');
  if (item) state.map?.highlight(item.dataset.id);
});
document.addEventListener('pointerleave', () => state.map?.highlight(null), true);

el('run').addEventListener('click', () => go(el('from').value, el('to').value));

state.map = createMap(el('map'));
attachTypeahead('from', 'from-list');
attachTypeahead('to', 'to-list');
connect();
