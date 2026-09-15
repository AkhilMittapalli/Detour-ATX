/* Austin open-data clients.
 *
 * Every endpoint below sends CORS headers and Socrata filters server-side by
 * bounding box, so a route's worth of data is about a megabyte and no backend
 * is needed.
 *
 * The timestamp handling is the part that bites. Work zones and incidents are
 * UTC with a trailing Z. Signal status is naive local Central with no offset
 * at all — read it as UTC and you claim a signal has been flashing five hours
 * longer than it has.
 */

const SOCRATA = 'https://data.austintexas.gov/resource';
const GEOCODER =
  'https://maps.austintexas.gov/arcgis/rest/services/Geocode/COA_Locator/GeocodeServer';
const LOCATOR = `${GEOCODER}/findAddressCandidates`;

export const DATASETS = {
  workZones: 'qyfh-gwei',
  signals: '5zpr-dehc',
  incidents: 'dx9v-zd7x',
  centerline: '8hf2-pdmb',
};

// Free, and it lifts the shared anonymous throttle. Optional.
let appToken = null;
export const setAppToken = (t) => { appToken = (t || '').trim() || null; };

async function soda(dataset, params, { signal } = {}) {
  const url = new URL(`${SOCRATA}/${dataset}.json`);
  for (const [k, v] of Object.entries(params)) url.searchParams.set(k, v);

  const headers = appToken ? { 'X-App-Token': appToken } : {};
  const response = await fetch(url, { headers, signal });
  if (!response.ok) {
    const body = await response.text();
    throw new Error(`${dataset}: HTTP ${response.status} ${body.slice(0, 120)}`);
  }
  const rows = await response.json();
  if (!Array.isArray(rows)) throw new Error(`${dataset}: ${JSON.stringify(rows).slice(0, 140)}`);
  return rows;
}

/* Socrata's within_box takes (north lat, west lon, south lat, east lon). */
const withinBox = (field, [minLon, minLat, maxLon, maxLat]) =>
  `within_box(${field}, ${maxLat}, ${minLon}, ${minLat}, ${maxLon})`;

/* ------------------------------------------------------------- geocode */

export async function geocode(address, { signal } = {}) {
  const query = (address || '').trim();
  if (!query) throw new Error('Enter an address.');

  const url = new URL(LOCATOR);
  url.searchParams.set('SingleLine', query);
  url.searchParams.set('f', 'json');
  url.searchParams.set('outSR', '4326');
  url.searchParams.set('maxLocations', '3');

  const response = await fetch(url, { signal });
  if (!response.ok) throw new Error(`Address lookup failed (HTTP ${response.status}).`);
  const body = await response.json();
  if (body.error) throw new Error(body.error.message || 'Address lookup failed.');

  const best = (body.candidates || [])
    .filter((c) => c.location)
    .sort((a, b) => (b.score || 0) - (a.score || 0))[0];

  if (!best) {
    throw new Error(
      `No Austin address matches “${query}”. Try the street number and type, e.g. 301 W 2nd St.`
    );
  }
  if ((best.score || 0) < 80) {
    throw new Error(
      `Closest match was “${best.address}”, which is too uncertain to use. Add more detail.`
    );
  }
  return { address: best.address, point: [best.location.x, best.location.y], score: best.score };
}

/* The locator advertises Geocode, ReverseGeocode and Suggest, so the address
 * field can behave the way people already expect: type a few characters, pick
 * a real Austin address, or start from where you are. */

export async function suggestAddresses(text, { signal } = {}) {
  const query = (text || '').trim();
  if (query.length < 3) return [];

  const url = new URL(`${GEOCODER}/suggest`);
  url.searchParams.set('text', query);
  url.searchParams.set('f', 'json');
  url.searchParams.set('maxSuggestions', '6');

  try {
    const response = await fetch(url, { signal });
    if (!response.ok) return [];
    const body = await response.json();
    return (body.suggestions || []).map((s) => s.text).filter(Boolean);
  } catch {
    // A failed suggestion never deserves an error message — the field still
    // accepts a typed address.
    return [];
  }
}

export async function reverseGeocode(lon, lat, { signal } = {}) {
  const url = new URL(`${GEOCODER}/reverseGeocode`);
  url.searchParams.set(
    'location',
    JSON.stringify({ x: lon, y: lat, spatialReference: { wkid: 4326 } })
  );
  url.searchParams.set('f', 'json');
  url.searchParams.set('outSR', '4326');

  const response = await fetch(url, { signal });
  if (!response.ok) throw new Error('Could not find an address for that location.');
  const body = await response.json();
  if (body.error) throw new Error(body.error.message || 'Could not find an address there.');

  const a = body.address || {};
  const label = a.Match_addr || a.LongLabel || a.ShortLabel;
  if (!label) throw new Error('No Austin address at that location.');
  return label;
}

/* --------------------------------------------------------------- feeds */

const CENTERLINE_FIELDS =
  'the_geom,segment_id,full_street_name,street_name,one_way,speed_limit,road_class';

/* A cross-town corridor holds more street than a row cap allows.
 *
 * Anderson Mill Rd to the airport spans 33 km and 31,312 centreline
 * segments; at a 12,000 cap the network came back arbitrarily truncated,
 * the graph lost connectivity, and the app said "could not find a route"
 * for a trip anyone can drive. Silently returning a broken network is the
 * worst of the options here.
 *
 * So the cap is raised, and beyond it the query drops residential and
 * service roads rather than dropping whatever happens to sort last. A
 * 30 km trip is carried by arterials and highways; the side streets only
 * matter near each end, which the endpoint fetch below covers. */
const CENTERLINE_CAP = 45000;
const MAJOR_ROAD_CLASSES = "('1','2','4','5','6','8')";

export async function fetchCenterline(box, opts) {
  const where = withinBox('the_geom', box);
  const rows = await soda(DATASETS.centerline, {
    $select: CENTERLINE_FIELDS,
    $where: where,
    $limit: String(CENTERLINE_CAP),
  }, opts);

  if (rows.length < CENTERLINE_CAP) return rows;

  // Still at the ceiling: fall back to the through-road network.
  return soda(DATASETS.centerline, {
    $select: CENTERLINE_FIELDS,
    $where: `${where} AND road_class in ${MAJOR_ROAD_CLASSES}`,
    $limit: String(CENTERLINE_CAP),
  }, opts);
}

const ZONE_FIELDS =
  'id,road_names,name,description,vehicle_impact,direction,critical_corridor,' +
  'are_workers_present,is_end_date_verified,start_date,end_date,geometry';

export const fetchWorkZones = (box, opts) =>
  soda(DATASETS.workZones, {
    $select: ZONE_FIELDS,
    $where: withinBox('geometry', box),
    $limit: '4000',
  }, opts);

/* The signals feed is an exception table — every row is a signal the city
 * considers degraded — and it is small enough to take whole. */
export async function fetchSignals(opts) {
  const rows = await soda(DATASETS.signals, { $limit: '2000' }, opts);
  for (const row of rows) row._since = parseCentral(row.operation_state_datetime);
  return rows;
}

export async function fetchIncidents(opts) {
  const rows = await soda(DATASETS.incidents, {
    traffic_report_status: 'ACTIVE',
    $order: 'published_date DESC',
    $limit: '500',
  }, opts);
  for (const row of rows) row._published = row.published_date ? new Date(row.published_date) : null;
  return rows;
}

/* ---------------------------------------------------------- timestamps */

/* A naive Central timestamp, converted to a real instant. US DST runs from
 * the second Sunday in March to the first Sunday in November. */
export function parseCentral(value) {
  if (!value) return null;
  const m = String(value).match(/^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2}):(\d{2})/);
  if (!m) return null;
  const [, y, mo, d, hh, mm, ss] = m.map(Number);

  const nth = (month, weekday, n) => {
    const first = new Date(Date.UTC(y, month, 1));
    const offset = (weekday - first.getUTCDay() + 7) % 7;
    return Date.UTC(y, month, 1 + offset + 7 * (n - 1), 2);
  };
  const naive = Date.UTC(y, mo - 1, d, hh, mm, ss);
  const offset = naive >= nth(2, 0, 2) && naive < nth(10, 0, 1) ? 5 : 6;
  return new Date(naive + offset * 3600e3);
}

/* ------------------------------------------------- the nightly sweep */

/* Most "communication issue" rows carry the moment a nightly batch last
 * counted them, not the moment the fault began. On one observed day 90 of
 * 131 shared a single timestamp across a 26 km spread, every one with
 * second == 59. Detection is by shape: many signals, one instant, spread
 * wider than any single cause reaches. */
const BATCH_MIN_MEMBERS = 8;
const BATCH_MIN_SPREAD_DEG = 0.09; // roughly 10 km

export function sweepTimestamps(signals) {
  const byInstant = new Map();
  for (const s of signals) {
    if (!s._since) continue;
    const key = s._since.getTime();
    if (!byInstant.has(key)) byInstant.set(key, []);
    byInstant.get(key).push(s);
  }

  const swept = new Set();
  for (const [key, members] of byInstant) {
    if (members.length < BATCH_MIN_MEMBERS) continue;
    const pts = members
      .map((m) => m.location && m.location.coordinates)
      .filter(Boolean)
      .map(([x, y]) => [+x, +y]);
    if (pts.length < 2) continue;
    const lons = pts.map((p) => p[0]), lats = pts.map((p) => p[1]);
    const spread = Math.max(Math.max(...lons) - Math.min(...lons), Math.max(...lats) - Math.min(...lats));
    if (spread >= BATCH_MIN_SPREAD_DEG) swept.add(key);
  }
  return swept;
}

export const onsetIsReal = (signal, swept) =>
  !signal._since || !swept.has(signal._since.getTime());
