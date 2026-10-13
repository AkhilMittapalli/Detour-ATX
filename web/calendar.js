/* The brief as a calendar event.
 *
 * The surface that asks least of anyone. A notification needs you to read
 * it; this app needs you to open it. A calendar entry arrives inside a
 * habit that already exists — you are going to look at tomorrow morning
 * anyway — and the alert fires before you leave rather than while you are
 * already sitting in the closure.
 *
 * It is also the only honest way a page with no backend can reach you at
 * 07:10. Nothing here can send an email tomorrow. A calendar can.
 *
 * A note on the format, because iCalendar punishes carelessness quietly.
 * RFC 5545 wants CRLF line endings, content lines folded at 75 octets, and
 * commas, semicolons, backslashes and newlines escaped inside TEXT values.
 * Get any of those wrong and Google Calendar does not complain — it simply
 * declines to import the file, which is a far worse failure than an error.
 */

const PRODID = '-//Detour ATX//Route briefing//EN';

/* Long enough to change your mind about the route, short enough that you
 * have not forgotten by the time you leave. */
export const ALARM_LEAD_MIN = 20;

/* When no departure time is given there is nothing to attach to, so the
 * event lands here and reads as a heads-up for the day rather than
 * pretending to know when you travel. */
const DEFAULT_HOUR = 8;

/* Backslash first, or it eats the escapes you just added. */
const escape = (text) => String(text ?? '')
  .replace(/\\/g, '\\\\')
  .replace(/\n/g, '\\n')
  .replace(/,/g, '\\,')
  .replace(/;/g, '\\;');

/* Folding counts octets, not characters. The rewritten descriptions carry
 * em dashes — three bytes each — and folding one mid-codepoint delivers
 * mojibake rather than an error. */
function fold(line) {
  const bytes = new TextEncoder().encode(line).length;
  if (bytes <= 75) return line;

  const out = [];
  let chunk = '';
  let used = 0;
  let limit = 75;
  for (const ch of line) {
    const size = new TextEncoder().encode(ch).length;
    if (used + size > limit) {
      out.push(chunk);
      chunk = '';
      used = 0;
      limit = 74; // the continuation space costs one
    }
    chunk += ch;
    used += size;
  }
  out.push(chunk);
  return out.join('\r\n ');
}

const pad = (n) => String(n).padStart(2, '0');

const stampUTC = (d) =>
  `${d.getUTCFullYear()}${pad(d.getUTCMonth() + 1)}${pad(d.getUTCDate())}T` +
  `${pad(d.getUTCHours())}${pad(d.getUTCMinutes())}${pad(d.getUTCSeconds())}Z`;

/* Floating local time — no zone, no Z. Deliberate: the brief is about
 * Austin and the reader is looking at an Austin trip, so an event at 08:30
 * should read 08:30 wherever their laptop thinks it is. */
const stampLocal = (d) =>
  `${d.getFullYear()}${pad(d.getMonth() + 1)}${pad(d.getDate())}T` +
  `${pad(d.getHours())}${pad(d.getMinutes())}${pad(d.getSeconds())}`;

/* Stable per route per day, so re-importing updates rather than
 * duplicating. No crypto needed — this only has to be deterministic. */
function uid(name, day) {
  const key = `${name}|${day}`;
  let h1 = 0x811c9dc5, h2 = 0x01000193;
  for (let i = 0; i < key.length; i++) {
    h1 = Math.imul(h1 ^ key.charCodeAt(i), 16777619) >>> 0;
    h2 = Math.imul(h2 + key.charCodeAt(i), 2246822519) >>> 0;
  }
  return `${h1.toString(16).padStart(8, '0')}${h2.toString(16).padStart(8, '0')}@detour-atx`;
}

/* The departure this route now needs, given what the detour costs.
 *
 * "Leave five minutes earlier" is advice with no anchor — earlier than
 * what? Once a departure time exists, the same fact becomes a time, which
 * is a thing someone can act on without doing arithmetic. */
export function leaveBy(departAt, delta) {
  if (!departAt) return null;
  if (!delta || !delta.affected || delta.implausible) return null;
  const extra = Math.max(0, Math.round((delta.extraS || 0) / 60));
  if (!extra) return null;
  return new Date(departAt.getTime() - extra * 60000);
}

function describe(brief) {
  const { plan, items, delta } = brief;
  const blocking = items.filter((i) => i.tier === 2);
  const name = `${plan.origin.address} to ${plan.destination.address}`;

  const summary = blocking.length
    ? `${blocking.length} thing${blocking.length > 1 ? 's' : ''} to plan around`
    : `${items.length} update${items.length === 1 ? '' : 's'} on your route`;

  const body = items.slice(0, 6).map((i) => `${i.headline}.`);
  if (items.length > 6) body.push(`and ${items.length - 6} more.`);
  if (delta && delta.affected && !delta.implausible) {
    body.push(`Route impact: ${brief.deltaText}.`);
  }
  body.push('Derived from City of Austin open data. Nothing here is confirmed unless it says so.');

  return { name, summary, description: body.join('\n') };
}

/* One event, or null when there is nothing worth saying.
 *
 * Returning null is the point. A calendar that fills up with "nothing is
 * wrong" is a calendar someone turns off, and then the one morning it
 * matters they do not see that either. */
export function eventFor(brief, { departAt = null, mapsUrl = '' } = {}) {
  if (!brief || !brief.items || !brief.items.length) return null;

  const base = departAt ? new Date(departAt) : new Date();
  if (!departAt) base.setHours(DEFAULT_HOUR, 0, 0, 0);

  const moved = leaveBy(departAt, brief.delta);
  const start = moved || base;

  const { name, summary, description } = describe(brief);
  const title = moved
    ? `Leave by ${pad(start.getHours())}:${pad(start.getMinutes())} — ${summary}`
    : summary;

  const body = mapsUrl ? `${description}\n\nOpen in Google Maps: ${mapsUrl}` : description;

  const seconds = brief.plan.seconds + ((brief.delta && brief.delta.extraS) || 0);
  return {
    uid: uid(name, start.toDateString()),
    start,
    minutes: Math.max(5, Math.round(seconds / 60)),
    summary: title,
    description: body,
    url: mapsUrl,
  };
}

export function render(events, now = new Date()) {
  const lines = [
    'BEGIN:VCALENDAR',
    'VERSION:2.0',
    `PRODID:${PRODID}`,
    'CALSCALE:GREGORIAN',
    'METHOD:PUBLISH',
    'X-WR-CALNAME:Detour ATX',
  ];

  for (const e of events) {
    const end = new Date(e.start.getTime() + Math.max(5, e.minutes) * 60000);
    lines.push(
      'BEGIN:VEVENT',
      `UID:${e.uid}`,
      `DTSTAMP:${stampUTC(now)}`,
      `DTSTART:${stampLocal(e.start)}`,
      `DTEND:${stampLocal(end)}`,
      `SUMMARY:${escape(e.summary)}`,
      `DESCRIPTION:${escape(e.description)}`
    );
    // URL is not a TEXT value, so it is not escaped — escaping it would
    // turn every comma in a waypoint list into a backslash and break it.
    if (e.url) lines.push(`URL:${e.url}`);
    lines.push(
      'BEGIN:VALARM',
      'ACTION:DISPLAY',
      `TRIGGER:-PT${ALARM_LEAD_MIN}M`,
      `DESCRIPTION:${escape(e.summary)}`,
      'END:VALARM',
      'END:VEVENT'
    );
  }

  lines.push('END:VCALENDAR');
  return lines.map(fold).join('\r\n') + '\r\n';
}

/* One click into Google Calendar, no file and no import step.
 *
 * Downloading an .ics and then finding Settings > Import is three screens
 * of work for something the reader asked for once. This is the same
 * handoff the Maps button makes: do not ask anyone to adopt a new place
 * to keep things, just put it where they already keep them.
 *
 * `ctz` pins the event to Austin regardless of the viewer's own calendar
 * timezone, which is the same reasoning as the floating times in the .ics:
 * an 08:30 Austin departure should read 08:30.
 */
const GCAL = 'https://calendar.google.com/calendar/render';

export function googleUrl(event) {
  if (!event) return '';
  const end = new Date(event.start.getTime() + Math.max(5, event.minutes) * 60000);
  const params = new URLSearchParams({
    action: 'TEMPLATE',
    text: event.summary,
    dates: `${stampLocal(event.start)}/${stampLocal(end)}`,
    details: event.description,
    ctz: 'America/Chicago',
  });
  return `${GCAL}?${params.toString()}`;
}

/* Hand the file to the browser. A Blob rather than the downloads
 * capability, so this works the same on localhost and when published.
 * Kept for Apple Calendar and Outlook, which have no equivalent URL. */
export function download(text, filename = 'detour-atx.ics') {
  const blob = new Blob([text], { type: 'text/calendar;charset=utf-8' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  // Revoking immediately races the download in some browsers.
  setTimeout(() => URL.revokeObjectURL(url), 10000);
}
