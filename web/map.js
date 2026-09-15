/* The map.
 *
 * The animation is the argument, not decoration. It runs in the order the
 * product reasons in, and each phase starts when its data genuinely arrives:
 *
 *   1. the street network fades in      (centreline loaded, routing possible)
 *   2. your route draws start to end    (we found your way)
 *   3. closures snap onto it            (this is what is broken)
 *   4. the detour traces around them    (and this is the way round)
 *
 * Because each phase is driven by real arrival rather than a timer, the
 * sequence covers the fetch latency instead of adding to it — the map is
 * doing something truthful during the seconds the data takes.
 *
 * Basemap geometry comes from the city's centreline layer, which the router
 * already loaded. Map tiles would be a cross-origin image request and are
 * blocked in a published artifact anyway.
 */

const DPR_CAP = 2;
const reduceMotion = () =>
  window.matchMedia?.('(prefers-reduced-motion: reduce)').matches ?? false;

const cssVar = (name) =>
  getComputedStyle(document.documentElement).getPropertyValue(name).trim();

const easeOut = (t) => 1 - Math.pow(1 - t, 3);

// Lower road_class numbers are bigger roads in Austin's centreline layer.
const MAJOR = { '1': 1, '2': 1, '4': 1, '5': 1 };

/* Labels appear only once there is room to read them. Below this the map is
 * a shape, not a street directory, and crowding it with type would make the
 * one thing that matters — where the closure is — harder to see. */
const LABEL_FROM = 1.9;
const ROUTE_LABEL_FROM = 1;

export function createMap(canvas) {
  const ctx = canvas.getContext('2d');

  let streets = [];        // {line, name, cls}
  let routeNames = new Set();
  let route = [];          // [[lon,lat], ...]
  let closures = [];       // {line, confidence, id}
  let detour = [];
  let signals = [];        // {point, flashing, id}
  let box = null;
  let highlighted = null;

  const phase = { streets: 0, route: 0, closures: 0, detour: 0 };
  let raf = null;
  let layout = { w: 0, h: 0, k: 1, offX: 0, offY: 0, kx: 1 };

  /* Pan and zoom sit on top of the base fit rather than replacing it, so
   * "reset" is always exactly the framing the route was laid out for.
   * Scale is floored at 1: zooming out past the whole route would only ever
   * show empty ground, since the basemap is fetched per corridor. */
  const MIN_SCALE = 1, MAX_SCALE = 14;
  let view = { scale: 1, dx: 0, dy: 0 };

  /* ------------------------------------------------------------ geometry */

  function fit() {
    const cssW = canvas.parentNode.clientWidth || 640;
    const ratio = 0.62;
    const cssH = Math.max(220, Math.min(420, Math.round(cssW * ratio)));
    const dpr = Math.min(window.devicePixelRatio || 1, DPR_CAP);

    canvas.width = Math.round(cssW * dpr);
    canvas.height = Math.round(cssH * dpr);
    canvas.style.height = `${cssH}px`;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);

    if (!box) { layout = { ...layout, w: cssW, h: cssH }; return; }
    const [x0, y0, x1, y1] = box;
    const midLat = (y0 + y1) / 2;
    const kx = Math.cos((midLat * Math.PI) / 180);
    const spanX = Math.max(1e-9, (x1 - x0) * kx);
    const spanY = Math.max(1e-9, y1 - y0);
    const pad = 18;
    const k = Math.min((cssW - pad * 2) / spanX, (cssH - pad * 2) / spanY);
    layout = {
      w: cssW, h: cssH, k, kx,
      offX: (cssW - spanX * k) / 2,
      offY: (cssH - spanY * k) / 2,
    };
  }

  /* Base fit, then the user's pan and zoom. */
  const project = (p) => [
    (layout.offX + (p[0] - box[0]) * layout.kx * layout.k) * view.scale + view.dx,
    (layout.offY + (box[3] - p[1]) * layout.k) * view.scale + view.dy,
  ];

  /* Screen point back to the base (unscaled) plane, for zooming about the
   * cursor rather than the centre. */
  const unview = (x, y) => [(x - view.dx) / view.scale, (y - view.dy) / view.scale];

  /* Keep the drawing from being dragged off the canvas entirely: at least
   * this much of it stays on screen in each axis. */
  function clampView() {
    view.scale = Math.min(MAX_SCALE, Math.max(MIN_SCALE, view.scale));
    const w = layout.w * view.scale, h = layout.h * view.scale;
    const slackX = Math.max(0, w - layout.w), slackY = Math.max(0, h - layout.h);
    view.dx = Math.min(0, Math.max(-slackX, view.dx));
    view.dy = Math.min(0, Math.max(-slackY, view.dy));
  }

  /* World bounds currently on screen, so the basemap can be culled when
   * zoomed. Drawing 4,000 polylines every drag frame is what makes a canvas
   * map feel cheap. */
  function visibleBounds() {
    const [x0, y0] = unview(0, 0);
    const [x1, y1] = unview(layout.w, layout.h);
    const toLon = (px) => box[0] + (px - layout.offX) / (layout.kx * layout.k);
    const toLat = (py) => box[3] - (py - layout.offY) / layout.k;
    const pad = 0.0015;
    return [toLon(x0) - pad, toLat(y1) - pad, toLon(x1) + pad, toLat(y0) + pad];
  }

  const lineBox = (line) => {
    let a = Infinity, b = Infinity, c = -Infinity, d = -Infinity;
    for (const [x, y] of line) {
      if (x < a) a = x; if (x > c) c = x;
      if (y < b) b = y; if (y > d) d = y;
    }
    return [a, b, c, d];
  };
  const overlaps = (m, n) => !(m[2] < n[0] || n[2] < m[0] || m[3] < n[1] || n[3] < m[1]);

  /* Cumulative lengths, so a partial draw advances at a constant speed
   * rather than jumping between unevenly spaced vertices. */
  function measure(line) {
    const steps = [0];
    for (let i = 1; i < line.length; i++) {
      const a = project(line[i - 1]), b = project(line[i]);
      steps.push(steps[i - 1] + Math.hypot(b[0] - a[0], b[1] - a[1]));
    }
    return steps;
  }

  function strokePartial(line, progress, style) {
    if (line.length < 2 || progress <= 0) return;
    const steps = measure(line);
    const total = steps[steps.length - 1];
    if (!total) return;
    const target = total * Math.min(1, progress);

    ctx.save();
    Object.assign(ctx, style.ctx || {});
    ctx.beginPath();
    ctx.strokeStyle = style.colour;
    ctx.lineWidth = style.width;
    ctx.lineCap = 'round';
    ctx.lineJoin = 'round';
    ctx.setLineDash(style.dash || []);
    if (style.alpha != null) ctx.globalAlpha = style.alpha;

    const first = project(line[0]);
    ctx.moveTo(first[0], first[1]);
    for (let i = 1; i < line.length; i++) {
      if (steps[i] <= target) {
        const p = project(line[i]);
        ctx.lineTo(p[0], p[1]);
      } else {
        const span = steps[i] - steps[i - 1];
        const t = span ? (target - steps[i - 1]) / span : 0;
        const a = project(line[i - 1]), b = project(line[i]);
        ctx.lineTo(a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t);
        break;
      }
    }
    ctx.stroke();
    ctx.restore();
  }

  function dot(point, radius, fill, stroke) {
    const p = project(point);
    ctx.beginPath();
    ctx.arc(p[0], p[1], radius, 0, Math.PI * 2);
    ctx.fillStyle = fill;
    ctx.fill();
    if (stroke) { ctx.lineWidth = 1.5; ctx.strokeStyle = stroke; ctx.stroke(); }
  }


  /* ---------------------------------------------------------- labelling
   *
   * Cartography, not a text dump. Four rules do most of the work:
   *
   *   one label per street name, not per segment — the centreline layer
   *     splits a road at every intersection, so naively labelling segments
   *     writes "GUADALUPE ST" eleven times down one street
   *   only where it fits — a label needs a run of road long enough on screen
   *     to sit along, otherwise it reads as noise laid over a junction
   *   the route's own streets first, then arterials, then the rest — when
   *     space runs out the reader should lose a side street, not their road
   *   nothing overlaps — a greedy collision check, because two labels on top
   *     of each other are worth less than one
   */

  /* The centreline layer splits a road at every intersection, so labelling
   * per segment both repeats the name and leaves each piece too short to
   * carry it. Chain the pieces that actually join end to end first, then
   * label the longest resulting run. Chaining rather than simply spanning
   * the extremes matters because a name can reappear on a disconnected
   * stretch across town, and a label bridging that gap would sit on nothing. */
  function longestRuns(group) {
    const key = (p) => `${p[0].toFixed(5)},${p[1].toFixed(5)}`;
    const ends = new Map();
    group.forEach((line, i) => {
      for (const p of [line[0], line[line.length - 1]]) {
        const k = key(p);
        if (!ends.has(k)) ends.set(k, []);
        ends.get(k).push(i);
      }
    });

    const used = new Array(group.length).fill(false);
    const runs = [];

    for (let i = 0; i < group.length; i++) {
      if (used[i]) continue;
      used[i] = true;
      let chain = group[i].slice();

      // Extend from whichever end still has an unused neighbour.
      for (let guard = 0; guard < 80; guard++) {
        let grew = false;
        for (const atEnd of [true, false]) {
          const tip = atEnd ? chain[chain.length - 1] : chain[0];
          const next = (ends.get(key(tip)) || []).find((j) => !used[j]);
          if (next == null) continue;
          used[next] = true;
          let piece = group[next].slice();
          const headMatches = key(piece[0]) === key(tip);
          if (!headMatches) piece.reverse();
          chain = atEnd ? chain.concat(piece.slice(1))
                        : piece.slice().reverse().concat(chain.slice(1));
          grew = true;
        }
        if (!grew) break;
      }
      runs.push(chain);
    }
    return runs;
  }

  function labelCandidates(visible) {
    const byName = new Map();
    for (const st of streets) {
      if (!st.name) continue;
      if (visible && !overlaps(lineBox(st.line), visible)) continue;
      const k = st.name.toUpperCase();
      if (!byName.has(k)) byName.set(k, { name: st.name, cls: st.cls, lines: [] });
      byName.get(k).lines.push(st.line);
    }

    /* Place type on the part of the street the reader can actually see.
     * A chained run chases the road right out of the fetched corridor, and
     * its midpoint can easily land off-canvas — which is what silently
     * suppressed every label on the route's own streets. */
    const m = 10;
    const inView = (p) => p[0] >= m && p[1] >= m && p[0] <= layout.w - m && p[1] <= layout.h - m;

    const out = [];
    for (const [k, entry] of byName) {
      let best = null;
      for (const chain of longestRuns(entry.lines)) {
        const pts = chain.map(project).filter(inView);
        if (pts.length < 2) continue;
        const a = pts[0], b = pts[pts.length - 1];
        const len = Math.hypot(b[0] - a[0], b[1] - a[1]);
        if (!best || len > best.len) best = { a, b, len };
      }
      if (!best) continue;

      const onRoute = routeNames.has(k);
      out.push({ ...best, name: entry.name, onRoute,
                 rank: onRoute ? 0 : MAJOR[entry.cls] ? 1 : 2 });
    }

    return out.sort((a, b) => a.rank - b.rank || b.len - a.len);
  }

  function drawLabels(visible) {
    const showAll = view.scale >= LABEL_FROM;
    const showRouteOnly = view.scale >= ROUTE_LABEL_FROM;
    if (!showAll && !showRouteOnly) return;

    const size = Math.min(13, 10 + view.scale * 0.35);
    ctx.save();
    ctx.font = `600 ${size}px "Barlow Condensed", "Helvetica Neue", sans-serif`;
    ctx.textAlign = 'center';
    ctx.textBaseline = 'middle';
    ctx.lineJoin = 'round';

    const ground = cssVar('--map-bg') || '#E7E5DF';
    const ink = cssVar('--mute') || '#6A665E';
    const routeInk = cssVar('--map-route') || '#3F5C6B';

    const placed = [];
    const hits = (b) => placed.some((p) =>
      !(p.x + p.w < b.x || b.x + b.w < p.x || p.y + p.h < b.y || b.y + b.h < p.y));

    for (const c of labelCandidates(visible)) {
      if (!showAll && !c.onRoute) continue;

      const text = c.name;
      const w = ctx.measureText(text).width;

      // Needs a run of road it can actually sit along. The route's own
      // streets get a shorter bar: the centreline layer splits a road at
      // every intersection, so a label may reach a little past one segment
      // while still sitting squarely on the same street — and the road the
      // reader is actually driving is the one worth that latitude.
      const need = c.onRoute ? w * 0.62 : w + 14;
      if (c.len < need) continue;

      const mx = (c.a[0] + c.b[0]) / 2, my = (c.a[1] + c.b[1]) / 2;

      const box = { x: mx - w / 2 - 3, y: my - size / 2 - 2, w: w + 6, h: size + 4 };
      if (hits(box)) continue;
      placed.push(box);

      // Keep type the right way up: past vertical, flip the baseline rather
      // than asking anyone to read upside down.
      let angle = Math.atan2(c.b[1] - c.a[1], c.b[0] - c.a[0]);
      if (angle > Math.PI / 2) angle -= Math.PI;
      if (angle < -Math.PI / 2) angle += Math.PI;

      ctx.save();
      ctx.translate(mx, my);
      ctx.rotate(angle);
      // Halo first, so the name reads over the road it is sitting on.
      ctx.strokeStyle = ground;
      ctx.lineWidth = 3.5;
      ctx.strokeText(text, 0, 0);
      ctx.fillStyle = c.onRoute ? routeInk : ink;
      ctx.fillText(text, 0, 0);
      ctx.restore();
    }
    ctx.restore();
  }

  /* -------------------------------------------------------------- render */

  function draw() {
    const ground = cssVar('--map-bg') || '#E7E5DF';
    ctx.clearRect(0, 0, layout.w, layout.h);
    ctx.fillStyle = ground;
    ctx.fillRect(0, 0, layout.w, layout.h);
    if (!box) return;

    const street = cssVar('--map-street') || '#CFCCC4';
    const routeColour = cssVar('--map-route') || '#3F5C6B';
    const blocking = cssVar('--blocking') || '#D8471A';
    const over = cssVar('--over') || '#9A958C';
    const event = cssVar('--slowing') || '#A97600';

    // Line widths grow with zoom, but far more slowly than the geometry, so
    // detail opens up instead of the whole picture just getting fatter.
    const z = Math.pow(view.scale, 0.35);
    const visible = view.scale > 1.02 ? visibleBounds() : null;

    if (phase.streets > 0) {
      ctx.save();
      ctx.globalAlpha = 0.9 * easeOut(phase.streets);
      for (const s of streets) {
        if (visible && !overlaps(lineBox(s.line), visible)) continue;
        strokePartial(s.line, 1, { colour: street, width: (MAJOR[s.cls] ? 1.5 : 0.8) * z });
      }
      ctx.restore();
    }

    if (phase.route > 0 && route.length > 1) {
      strokePartial(route, phase.route, { colour: routeColour, width: 3 * z });
    }

    if (phase.closures > 0) {
      closures.forEach((c, i) => {
        // Stagger so they land one after another rather than all at once.
        const local = Math.min(1, Math.max(0, phase.closures * closures.length - i));
        if (local <= 0) return;
        const isOver = c.confidence === 'Probably over';
        const focused = highlighted && highlighted === c.id;
        strokePartial(c.line, 1, {
          colour: isOver ? over : blocking,
          width: (focused ? 6 : 4) * z * (0.6 + 0.4 * easeOut(local)),
          dash: isOver ? [5, 4] : [],
          alpha: easeOut(local),
        });
      });
    }

    if (phase.detour > 0 && detour.length > 1) {
      strokePartial(detour, phase.detour, {
        colour: routeColour, width: 2.4 * z, dash: [6 * z, 5 * z],
      });
    }

    /* Two very different things wear this symbol, and the map has to say so.
     *
     * A flashing signal is a driver-facing hazard: the controller tripped
     * its conflict monitor, and in Texas that intersection is now an
     * all-way stop. It gets a loud filled ring.
     *
     * A communication issue means the city lost telemetry to the cabinet.
     * The signal keeps cycling on its local timer and the driver impact is
     * none. Drawing it as loudly as a hazard — which an earlier version
     * did — puts fifteen alarming rings on a route whose own summary says
     * nothing is blocking. It gets a small quiet dot. */
    if (phase.closures > 0) {
      for (const s of signals) {
        const p = project(s.point);
        const focused = highlighted === s.id;
        ctx.save();
        ctx.globalAlpha = easeOut(phase.closures);

        if (s.flashing) {
          ctx.beginPath();
          ctx.arc(p[0], p[1], (focused ? 9 : 7) * z, 0, Math.PI * 2);
          ctx.strokeStyle = blocking;
          ctx.lineWidth = 2.6 * z;
          ctx.stroke();
          ctx.beginPath();
          ctx.arc(p[0], p[1], 2.6 * z, 0, Math.PI * 2);
          ctx.fillStyle = blocking;
          ctx.fill();
        } else {
          ctx.globalAlpha *= focused ? 0.95 : 0.55;
          ctx.beginPath();
          ctx.arc(p[0], p[1], (focused ? 5 : 3.2) * z, 0, Math.PI * 2);
          ctx.strokeStyle = event;
          ctx.lineWidth = 1.2 * z;
          ctx.stroke();
        }
        ctx.restore();
      }
    }

    if (phase.streets > 0.7) drawLabels(visible);

    if (phase.route > 0 && route.length > 1) {
      dot(route[0], 5 * z, routeColour);
      if (phase.route >= 1) {
        dot(route[route.length - 1], 5 * z, ground, routeColour);
        dot(route[route.length - 1], 2.5 * z, routeColour);
      }
    }
  }

  /* ------------------------------------------------------------ animation */

  const targets = { streets: 0, route: 0, closures: 0, detour: 0 };
  const SPEED = { streets: 0.055, route: 0.022, closures: 0.03, detour: 0.028 };

  function tick() {
    let moving = false;
    for (const key of Object.keys(phase)) {
      const to = targets[key];
      if (Math.abs(phase[key] - to) < 0.002) { phase[key] = to; continue; }
      // Route and detour advance at a steady rate so the line reads as being
      // traced; fades ease toward their target.
      phase[key] = key === 'route' || key === 'detour'
        ? Math.min(to, phase[key] + SPEED[key])
        : phase[key] + (to - phase[key]) * 0.14;
      moving = true;
    }
    draw();
    raf = moving ? requestAnimationFrame(tick) : null;
  }

  /* Animate only when there is someone to watch it.
   *
   * requestAnimationFrame is throttled to a stop in hidden or backgrounded
   * tabs, so a map built there would sit unfinished until the tab was
   * focused — and a link someone shares is very often opened into a
   * background tab. Nobody is owed an animation they cannot see: when the
   * page is hidden, or the viewer asked for reduced motion, jump straight to
   * the finished state. */
  const canAnimate = () =>
    !reduceMotion() && document.visibilityState === 'visible';

  function settle() {
    for (const key of Object.keys(phase)) phase[key] = targets[key];
    if (raf) { cancelAnimationFrame(raf); raf = null; }
    draw();
  }

  function advance(key, value = 1) {
    targets[key] = value;
    if (!canAnimate()) { settle(); return; }
    // Resizing a canvas clears it, so paint once before the loop takes over.
    draw();
    if (!raf) raf = requestAnimationFrame(tick);
  }

  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState !== 'visible') settle();
  });


  /* ---------------------------------------------------------- interaction
   *
   * Wheel zooms about the cursor, drag pans, two fingers pinch, double-click
   * steps in. Panning and zooming bypass the animation loop entirely and
   * paint synchronously: an input that waits a frame to respond feels broken
   * regardless of how fast it actually is.
   */

  const local = (event) => {
    const r = canvas.getBoundingClientRect();
    return [event.clientX - r.left, event.clientY - r.top];
  };

  function zoomAt(factor, sx, sy) {
    const before = unview(sx, sy);
    view.scale *= factor;
    clampView();
    // Hold the point under the cursor still.
    view.dx = sx - before[0] * view.scale;
    view.dy = sy - before[1] * view.scale;
    clampView();
    draw();
  }

  canvas.addEventListener('wheel', (event) => {
    if (!box) return;
    event.preventDefault();
    const [sx, sy] = local(event);
    zoomAt(Math.exp(-event.deltaY * 0.0016), sx, sy);
  }, { passive: false });

  canvas.addEventListener('dblclick', (event) => {
    if (!box) return;
    const [sx, sy] = local(event);
    zoomAt(event.shiftKey ? 1 / 1.8 : 1.8, sx, sy);
  });

  const pointers = new Map();
  let pinchStart = null;
  let panFrom = null;

  canvas.addEventListener('pointerdown', (event) => {
    if (!box) return;
    canvas.setPointerCapture(event.pointerId);
    pointers.set(event.pointerId, local(event));
    if (pointers.size === 1) {
      panFrom = { p: local(event), dx: view.dx, dy: view.dy };
      canvas.style.cursor = 'grabbing';
    } else if (pointers.size === 2) {
      const [a, b] = [...pointers.values()];
      pinchStart = {
        dist: Math.hypot(a[0] - b[0], a[1] - b[1]) || 1,
        mid: [(a[0] + b[0]) / 2, (a[1] + b[1]) / 2],
        scale: view.scale,
      };
      panFrom = null;
    }
  });

  canvas.addEventListener('pointermove', (event) => {
    if (!pointers.has(event.pointerId)) return;
    pointers.set(event.pointerId, local(event));

    if (pointers.size >= 2 && pinchStart) {
      const [a, b] = [...pointers.values()];
      const dist = Math.hypot(a[0] - b[0], a[1] - b[1]) || 1;
      const target = pinchStart.scale * (dist / pinchStart.dist);
      zoomAt(target / view.scale, pinchStart.mid[0], pinchStart.mid[1]);
      return;
    }

    if (panFrom) {
      const [x, y] = local(event);
      view.dx = panFrom.dx + (x - panFrom.p[0]);
      view.dy = panFrom.dy + (y - panFrom.p[1]);
      clampView();
      draw();
    }
  });

  const release = (event) => {
    pointers.delete(event.pointerId);
    if (pointers.size < 2) pinchStart = null;
    if (pointers.size === 0) { panFrom = null; canvas.style.cursor = ''; }
  };
  canvas.addEventListener('pointerup', release);
  canvas.addEventListener('pointercancel', release);

  /* --------------------------------------------------------------- public */

  const api = {
    zoomIn() { zoomAt(1.6, layout.w / 2, layout.h / 2); },
    zoomOut() { zoomAt(1 / 1.6, layout.w / 2, layout.h / 2); },
    fitView() { view = { scale: 1, dx: 0, dy: 0 }; draw(); },
    get zoom() { return view.scale; },

    reset() {
      streets = []; route = []; closures = []; detour = []; signals = [];
      routeNames = new Set();
      box = null; highlighted = null;
      view = { scale: 1, dx: 0, dy: 0 };
      for (const k of Object.keys(phase)) { phase[k] = 0; targets[k] = 0; }
      fit(); draw();
    },

    showNetwork(centerlineLines, bounds) {
      streets = centerlineLines;
      box = bounds;
      fit();
      draw();
      advance('streets');
    },

    showRoute(line, names) {
      route = line;
      routeNames = new Set((names || []).map((n) => n.toUpperCase()));
      fit();
      draw();
      advance('route');
    },

    showDisruptions(closureShapes, signalPoints) {
      closures = closureShapes;
      signals = signalPoints;
      advance('closures');
    },

    showDetour(line) {
      detour = line || [];
      if (detour.length > 1) advance('detour');
    },

    highlight(id) {
      if (highlighted === id) return;
      highlighted = id;
      draw();
    },

    redraw() { fit(); draw(); },
  };

  let resizeTimer;
  window.addEventListener('resize', () => {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(() => api.redraw(), 120);
  });
  window.matchMedia?.('(prefers-color-scheme: dark)')
    .addEventListener?.('change', () => api.redraw());

  fit();
  draw();
  return api;
}
