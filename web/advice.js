/* What should I actually do about this?
 *
 * Listing what is broken is only half a product. A brief that ends at
 * "E 2nd St is closed" leaves the reader to do the work; the point is to
 * close that gap with advice that is *derived*, never invented.
 *
 * Every recommendation below traces to something in the data:
 *
 *   leave earlier      the detour delta, in minutes
 *   go around via      the re-routed street sequence
 *   travel off-peak    the permit's declared schedule and the work calendar
 *   it clears on       the permit end dates on this route
 *   help us be sure    the count of low-confidence items
 *
 * Nothing here guesses at traffic. This app has no congestion data and says
 * so; what it has is the paperwork, which is the half nobody else reads.
 */

import { CONFIDENCE, TIER, crewPlausible, austinNow } from './engine.js';

const MIN_EARLIER = 5;
const CLEARS_SOON_DAYS = 21;

const fmtDay = (d) =>
  d.toLocaleDateString('en-US', { weekday: 'short', month: 'short', day: 'numeric', timeZone: 'UTC' });

/* Rank matters: the first card is the one a reader acts on. */
const PRIORITY = { act: 0, avoid: 1, timing: 2, ahead: 3, help: 4 };

export function recommend({ items, delta, zones = [], now, when }) {
  const out = [];
  const at = when || now;

  const blocking = items.filter((i) => i.tier === TIER.BLOCKING);
  const flashing = items.filter((i) => i.kind === 'signal' && /flashing/i.test(i.headline));
  const uncertain = items.filter((i) => i.verdict.level === CONFIDENCE.OVER);

  /* --- the detour, in the terms a commuter thinks in ------------------- */
  if (delta && delta.affected && !delta.implausible) {
    const minutes = Math.max(MIN_EARLIER, Math.ceil(delta.extraS / 60));
    const miles = delta.extraM / 1609.344;

    out.push({
      kind: 'act',
      title: `Leave about ${minutes} minutes earlier`,
      body: delta.extraS / 60 < 1
        ? `Going around costs roughly ${miles.toFixed(1)} extra miles. It should not cost much time, but the turns are unfamiliar, so give yourself the margin.`
        : `Your usual way is cut, and the way around adds about ${Math.round(delta.extraS / 60)} minutes and ${miles.toFixed(1)} miles.`,
    });

    if (delta.via && delta.via.length) {
      out.push({
        kind: 'avoid',
        title: `Go around via ${delta.via.slice(0, 3).join(', ')}`,
        body: 'Derived by removing the closed block from the street network and routing again. Austin publishes the closure but never the detour, so this is computed, not official — follow posted signs where they differ.',
      });
    }
  } else if (delta && delta.unreachable) {
    out.push({
      kind: 'act',
      title: 'There may be no way through',
      body: 'Every route around this closure is also cut in the data. Check posted signage before setting out, and allow for a long diversion.',
    });
  }

  /* --- signals are a driving instruction, not a delay ------------------ */
  if (flashing.length) {
    out.push({
      kind: 'act',
      title: flashing.length > 1
        ? `Treat ${flashing.length} intersections as four-way stops`
        : 'Treat that flashing signal as a four-way stop',
      body: 'In Texas a flashing red is a stop and a dark signal is an all-way stop. Most drivers do not know this, which is what makes these intersections dangerous rather than merely slow.',
    });
  }

  /* --- when to travel, from the permits themselves --------------------- */
  const active = items.filter(
    (i) => i.kind === 'work_zone' && i.verdict.level !== CONFIDENCE.OVER
  );
  if (active.length) {
    const [plausibleNow] = crewPlausible(at, '');
    out.push({
      kind: 'timing',
      title: plausibleNow
        ? 'Quieter before 7am or after 6pm'
        : 'No crews expected at this hour',
      body: plausibleNow
        ? `${active.length} permit${active.length > 1 ? 's are' : ' is'} live on this route and crews keep roughly daytime hours on weekdays. The lane restrictions usually stay up, but the flaggers, trucks and stop-and-go do not.`
        : 'Barricades and cones stay up outside working hours, so the road may still be narrowed — but you should not meet a crew, a flagger or a queue behind a truck.',
    });
  }

  /* --- the thing no live traffic app can tell you ----------------------- */
  const ending = zones
    .map((z) => (z.end_date ? new Date(z.end_date) : null))
    .filter((d) => d && d > at && (d - at) / 86400e3 <= CLEARS_SOON_DAYS)
    .sort((a, b) => a - b);

  if (ending.length) {
    const soonest = ending[0];
    const days = Math.max(1, Math.round((soonest - at) / 86400e3));
    out.push({
      kind: 'ahead',
      title: `One of these is due to clear ${fmtDay(soonest)}`,
      body: `${ending.length} permit${ending.length > 1 ? 's on this route are' : ' on this route is'} scheduled to end within three weeks, the soonest in about ${days} day${days > 1 ? 's' : ''}. Permit dates are windows rather than promises, so treat that as the earliest it could clear, not a guarantee.`,
    });
  }

  /* --- the honest ask -------------------------------------------------- */
  if (uncertain.length) {
    out.push({
      kind: 'help',
      title: `${uncertain.length} of these may already be finished`,
      body: 'Their own permit dates or notes suggest the work is done, but the city has verified a position on none of its work-zone records. If you drive past and the road is clear, tap Gone and the next person gets a better answer.',
    });
  }

  if (!out.length) {
    out.push({
      kind: 'act',
      title: 'Nothing to plan around',
      body: 'No closure, flashing signal or active incident sits on this route right now. Quiet is the normal state, and it is worth saying so plainly rather than inventing an alert.',
    });
  }

  out.sort((a, b) => PRIORITY[a.kind] - PRIORITY[b.kind]);
  return out;
}

/* ------------------------------------------------------------ planning */

/* The reason a future time is answerable at all: permits carry start and end
 * dates, and the work calendar is a function of the local day and hour. So
 * "what will this route look like on Thursday at 8am" is a real question
 * here, and it is one a live-traffic app cannot answer at any price —
 * congestion data does not exist until the congestion does. */
export function planningOptions(now) {
  const local = austinNow(now);
  const options = [{ id: 'now', label: 'Right now', at: now }];

  const tomorrow = new Date(now.getTime() + 86400e3);
  const tomorrow8 = shiftToLocalHour(tomorrow, 8);
  options.push({ id: 'tomorrow-am', label: 'Tomorrow, 8am', at: tomorrow8 });

  const inAWeek = shiftToLocalHour(new Date(now.getTime() + 7 * 86400e3), 8);
  options.push({ id: 'next-week', label: 'This time next week', at: inAWeek });

  return { options, local };
}

/* Set an instant to a given Austin-local hour, without a tz database. */
function shiftToLocalHour(instant, hour) {
  const local = austinNow(instant);
  const delta = hour - local.getUTCHours();
  return new Date(instant.getTime() + delta * 3600e3 - local.getUTCMinutes() * 60e3);
}
