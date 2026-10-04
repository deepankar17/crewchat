// Draws one animated diagram into the page's <svg>, at any moment of its timeline: render(t).
// The capture script steps t frame by frame, so the animation is exact and repeatable.
//
// A diagram is either a graph (cards, groups and arrows placed by hand) or a sequence (people
// and services in columns, messages appearing one after another). Either way it plays as
// numbered steps: a packet travels along the step's arrow, its number pops up, the cards it
// touches light up, and the caption at the bottom says what is happening.
"use strict";

const W = 1280, H = 720;
const PALETTE = {
  blue:   {c: "#3b6fe0", soft: "#eaf1ff", ink: "#1e3a8a"},
  green:  {c: "#1f9d63", soft: "#e6f7ee", ink: "#14532d"},
  amber:  {c: "#d98a0b", soft: "#fff4d9", ink: "#78350f"},
  purple: {c: "#7c4ddb", soft: "#f1ebff", ink: "#3b0764"},
  pink:   {c: "#d9468a", soft: "#fdebf3", ink: "#831843"},
  teal:   {c: "#0e9aa7", soft: "#e2f6f8", ink: "#134e4a"},
  orange: {c: "#e8622c", soft: "#fdeee6", ink: "#7c2d12"},
  slate:  {c: "#64748b", soft: "#f1f5f9", ink: "#1e293b"},
  red:    {c: "#e0483e", soft: "#fdecea", ink: "#7f1d1d"},
};
const STEP_COLORS = ["blue", "green", "purple", "orange", "teal", "pink", "amber"];

// Icons: 24x24, drawn with strokes in the current colour.
const ICONS = {
  user: '<circle cx="12" cy="8" r="4"/><path d="M4 21c0-4.4 3.6-7 8-7s8 2.6 8 7"/>',
  users: '<circle cx="9" cy="8" r="3.5"/><path d="M2.5 20c0-3.8 3-6 6.5-6s6.5 2.2 6.5 6"/><path d="M16 4.6a3.5 3.5 0 0 1 0 6.8M18 14.3c2.2.6 3.5 2.6 3.5 5.7"/>',
  chat: '<path d="M4 5h16a1 1 0 0 1 1 1v9a1 1 0 0 1-1 1H10l-5 4v-4H4a1 1 0 0 1-1-1V6a1 1 0 0 1 1-1z"/><path d="M8 9.5h8M8 12.5h5"/>',
  server: '<rect x="3.5" y="3" width="17" height="7.5" rx="2"/><rect x="3.5" y="13.5" width="17" height="7.5" rx="2"/><path d="M7.5 6.8h.01M7.5 17.3h.01M11 6.8h5M11 17.3h5"/>',
  db: '<ellipse cx="12" cy="5.5" rx="8" ry="3"/><path d="M4 5.5v13c0 1.7 3.6 3 8 3s8-1.3 8-3v-13"/><path d="M4 12c0 1.7 3.6 3 8 3s8-1.3 8-3"/>',
  laptop: '<rect x="5" y="4" width="14" height="10" rx="1.5"/><path d="M2.5 19h19l-2.2-4.2H4.7z"/>',
  monitor: '<rect x="3" y="4" width="18" height="12" rx="2"/><path d="M8 20h8M12 16v4"/>',
  phone: '<rect x="6.5" y="2" width="11" height="20" rx="2.5"/><path d="M10.5 18h3"/>',
  cloud: '<path d="M7 19a5 5 0 0 1-.7-9.95A6.5 6.5 0 0 1 18.6 9.6 4.7 4.7 0 0 1 17.5 19z"/>',
  lock: '<rect x="4.5" y="10.5" width="15" height="10.5" rx="2"/><path d="M8 10.5V7.5a4 4 0 0 1 8 0v3M12 14.5v2.5"/>',
  key: '<circle cx="8" cy="15" r="4.5"/><path d="M11.3 11.7 20 3M16.5 6.5l3 3M14 9l2.2 2.2"/>',
  terminal: '<rect x="3" y="4" width="18" height="16" rx="2"/><path d="m7 9 3 3-3 3M12.5 15h4.5"/>',
  bot: '<rect x="4" y="8" width="16" height="12" rx="3.5"/><path d="M12 4v4M2 13v3M22 13v3"/><circle cx="12" cy="3.5" r="1"/><path d="M9 13.2v1.6M15 13.2v1.6"/>',
  hook: '<path d="M9 3v10.5a4.5 4.5 0 0 0 9 0V11"/><path d="m15.5 13 2.5-2.5 2.5 2.5"/><circle cx="9" cy="3" r="1.4"/>',
  image: '<rect x="3" y="4" width="18" height="16" rx="2"/><circle cx="9" cy="10" r="2"/><path d="m21 16.5-5-5-9.5 8.5"/>',
  check: '<path d="m5 12.5 4.5 4.5L19 7"/>',
  question: '<circle cx="12" cy="12" r="9"/><path d="M9.6 9.3a2.5 2.5 0 1 1 3.4 2.3c-.7.3-1 .9-1 1.6"/><path d="M12 16.8h.01"/>',
  globe: '<circle cx="12" cy="12" r="9"/><path d="M3 12h18M12 3c2.8 3.3 2.8 14.7 0 18M12 3c-2.8 3.3-2.8 14.7 0 18"/>',
  shield: '<path d="M12 3 20 6v6c0 4.8-3.4 8-8 9-4.6-1-8-4.2-8-9V6z"/><path d="m8.5 12 2.5 2.5 4.5-5"/>',
  task: '<rect x="4" y="3.5" width="16" height="17" rx="2"/><path d="M8.5 3.5v3h7v-3M8 11.5l2 2 4-4M8 17h8"/>',
  crown: '<path d="m3 8 4.5 4L12 5l4.5 7L21 8l-2 11H5z"/>',
  code: '<path d="m8 8-4 4 4 4M16 8l4 4-4 4M13.5 5l-3 14"/>',
  bug: '<rect x="7" y="7" width="10" height="13" rx="5"/><path d="M12 11v9M7 12H3M21 12h-4M7.5 17H4M20 17h-3.5M9 4l1.5 2.5M15 4l-1.5 2.5"/>',
  page: '<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M3 9h18M6 6.5h.01M8.5 6.5h.01"/>',
  clock: '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3.5 2"/>',
  moon: '<path d="M20 14.5A8.5 8.5 0 1 1 9.5 4a6.8 6.8 0 0 0 10.5 10.5z"/>',
  zap: '<path d="M13 2 4 14h7l-1 8 9-12h-7z"/>',
  mail: '<rect x="3" y="5" width="18" height="14" rx="2"/><path d="m3.5 7 8.5 6 8.5-6"/>',
  plus: '<path d="M12 5v14M5 12h14"/>',
  refresh: '<path d="M20 11a8 8 0 0 0-14.3-4.3L4 9M4 4v5h5M4 13a8 8 0 0 0 14.3 4.3L20 15M20 20v-5h-5"/>',
  folder: '<path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v9a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/>',
  flame: '<path d="M12 3c.8 3.6 6 5.6 6 11a6 6 0 0 1-12 0c0-2.8 1.6-4.2 2.2-6.6 1.6 1.2 2.5 2.8 2.5 4.6 1.4-2 1.6-5.2 1.3-9z"/>',
  mesh: '<circle cx="5" cy="5" r="2"/><circle cx="19" cy="5" r="2"/><circle cx="12" cy="19" r="2"/><path d="M7 5h10M6 7l5 10M18 7l-5 10"/>',
  plug: '<path d="M9 2v5M15 2v5M6 7h12v4a6 6 0 0 1-12 0zM12 17v5"/>',
  flag: '<path d="M5 21V4M5 4h11.5l-2 4 2 4H5"/>',
  eye: '<path d="M2.5 12S6 5 12 5s9.5 7 9.5 7-3.5 7-9.5 7-9.5-7-9.5-7z"/><circle cx="12" cy="12" r="3"/>',
  pause: '<rect x="6" y="5" width="4" height="14" rx="1"/><rect x="14" y="5" width="4" height="14" rx="1"/>',
  ban: '<circle cx="12" cy="12" r="9"/><path d="m5.7 5.7 12.6 12.6"/>',
  wifi: '<path d="M2 8.5a15 15 0 0 1 20 0M5 12a10 10 0 0 1 14 0M8.5 15.5a5 5 0 0 1 7 0M12 19h.01"/>',
  doc: '<path d="M6 2.5h8l5 5V21a.5.5 0 0 1-.5.5h-12A.5.5 0 0 1 6 21z"/><path d="M14 2.5v5h5M9 13h6M9 17h6"/>',
};

const NS = "http://www.w3.org/2000/svg";
let svg, spec, timeline, total;

function el(name, attrs, parent, text) {
  const e = document.createElementNS(NS, name);
  for (const k in attrs) e.setAttribute(k, attrs[k]);
  if (text !== undefined) e.textContent = text;
  (parent || svg).appendChild(e);
  return e;
}
const clamp = (x, a = 0, b = 1) => Math.max(a, Math.min(b, x));
const ease = x => x < .5 ? 4 * x * x * x : 1 - Math.pow(-2 * x + 2, 3) / 2;
const pop = x => { x = clamp(x); const c = 1.7; return 1 + (c + 1) * Math.pow(x - 1, 3) + c * Math.pow(x - 1, 2); };
const col = name => PALETTE[name] || PALETTE.blue;

function icon(name, x, y, size, color, parent) {
  if (name === "crewchat") {
    el("image", {href: window.LOGO, x, y, width: size, height: size}, parent);
    return;
  }
  const g = el("g", {transform: `translate(${x},${y}) scale(${size / 24})`, fill: "none", stroke: color,
    "stroke-width": 2, "stroke-linecap": "round", "stroke-linejoin": "round"}, parent);
  g.innerHTML = ICONS[name] || ICONS.question;
}

function lines(text) { return String(text || "").split("\n"); }
function fit(t, width) {
  const w = t.getComputedTextLength();
  if (w > width) t.setAttribute("font-size", parseFloat(t.getAttribute("font-size")) * width / w);
  return t;
}

// ---------------------------------------------------------------------------------------------
// Cards and groups
// ---------------------------------------------------------------------------------------------
function drawGroup(g) {
  const c = col(g.color);
  el("rect", {x: g.x, y: g.y, width: g.w, height: g.h, rx: 20, fill: c.soft, stroke: c.c, "stroke-width": 2,
    "stroke-dasharray": g.solid ? "" : "8 6", opacity: 1});
  const label = el("g", {});
  const t = el("text", {x: g.x + 44, y: g.y + 1, "font-size": 15, "font-weight": 700, fill: "#fff",
    "dominant-baseline": "middle"}, label, g.label);
  const w = t.getComputedTextLength() + 58;
  const chip = el("rect", {x: g.x + 14, y: g.y - 15, width: w, height: 30, rx: 15, fill: c.c}, label);
  label.insertBefore(chip, t);
  icon(g.icon || "folder", g.x + 22, g.y - 9, 18, "#fff", label);
}

function drawNode(n, glow) {
  const c = col(n.color);
  const g = el("g", {});
  const shape = n.shape || "card";
  if (glow > 0) {
    el("rect", {x: n.x - 7, y: n.y - 7, width: n.w + 14, height: n.h + 14, rx: shape === "pill" ? (n.h + 14) / 2 : 20,
      fill: "none", stroke: c.c, "stroke-width": 6, opacity: .28 * glow}, g);
  }
  const rx = shape === "pill" ? n.h / 2 : 14;
  el("rect", {x: n.x, y: n.y, width: n.w, height: n.h, rx, fill: shape === "pill" ? c.soft : "#fff",
    stroke: c.c, "stroke-width": glow > .5 ? 3 : 2, filter: "url(#shadow)"}, g);
  const title = lines(n.label), sub = lines(n.sub).filter(Boolean);
  if (shape === "pill" || !n.icon) {
    const total = title.length * 20 + sub.length * 17;
    let y = n.y + n.h / 2 - total / 2 + 14;
    title.forEach(l => { fit(el("text", {x: n.x + n.w / 2, y, "text-anchor": "middle", "font-size": 16.5, "font-weight": 700, fill: c.ink}, g, l), n.w - 24); y += 20; });
    sub.forEach(l => { el("text", {x: n.x + n.w / 2, y: y - 2, "text-anchor": "middle", "font-size": 13, fill: "#64748b"}, g, l); y += 17; });
    return g;
  }
  const vertical = n.w < 150;
  if (vertical) {
    const tile = 46, tx = n.x + n.w / 2 - tile / 2, ty = n.y + 12;
    el("rect", {x: tx, y: ty, width: tile, height: tile, rx: 12, fill: c.soft}, g);
    icon(n.icon, tx + 10, ty + 10, 26, c.c, g);
    let y = ty + tile + 20;
    title.forEach(l => { el("text", {x: n.x + n.w / 2, y, "text-anchor": "middle", "font-size": 15, "font-weight": 700, fill: "#0f172a"}, g, l); y += 18; });
    sub.forEach(l => { el("text", {x: n.x + n.w / 2, y, "text-anchor": "middle", "font-size": 12, fill: "#64748b"}, g, l); y += 15; });
    return g;
  }
  const tile = Math.min(48, n.h - 20), tx = n.x + 13, ty = n.y + n.h / 2 - tile / 2;
  el("rect", {x: tx, y: ty, width: tile, height: tile, rx: 12, fill: c.soft}, g);
  icon(n.icon, tx + tile * .2, ty + tile * .2, tile * .6, c.c, g);
  const lx = tx + tile + 13;
  const total = title.length * 20 + sub.length * 16;
  let y = n.y + n.h / 2 - total / 2 + 15;
  const room = n.x + n.w - lx - 10;
  title.forEach(l => { fit(el("text", {x: lx, y, "font-size": 16.5, "font-weight": 700, fill: "#0f172a"}, g, l), room); y += 20; });
  sub.forEach(l => { fit(el("text", {x: lx, y: y - 2, "font-size": 12.5, fill: "#64748b"}, g, l), room); y += 16; });
  return g;
}

// ---------------------------------------------------------------------------------------------
// Arrows
// ---------------------------------------------------------------------------------------------
function center(n) { return [n.x + n.w / 2, n.y + n.h / 2]; }
function anchor(n, side) {
  const [cx, cy] = center(n);
  return {l: [n.x, cy], r: [n.x + n.w, cy], t: [cx, n.y], b: [cx, n.y + n.h]}[side];
}
function sides(a, b) {
  const [ax, ay] = center(a), [bx, by] = center(b);
  const dx = bx - ax, dy = by - ay;
  if (Math.abs(dx) * .6 > Math.abs(dy)) return dx > 0 ? ["r", "l"] : ["l", "r"];
  return dy > 0 ? ["b", "t"] : ["t", "b"];
}
// The points of an arrow: straight, or with right-angled bends, or through given points.
function route(e, nodes) {
  const a = nodes[e.from], b = nodes[e.to];
  let [sa, sb] = e.sides ? e.sides.split("") : sides(a, b);
  const p0 = anchor(a, sa), p1 = anchor(b, sb);
  if (e.offset) { // side by side arrows between the same two cards
    const h = "lr".includes(sa);
    if (h) { p0[1] += e.offset; p1[1] += e.offset; } else { p0[0] += e.offset; p1[0] += e.offset; }
  }
  if (e.via) return [p0, ...e.via, p1];
  const horiz = "lr".includes(sa), vert = "tb".includes(sa);
  if (horiz && "lr".includes(sb) && Math.abs(p0[1] - p1[1]) > 2) {
    const mx = e.bend !== undefined ? e.bend : (p0[0] + p1[0]) / 2;
    return [p0, [mx, p0[1]], [mx, p1[1]], p1];
  }
  if (vert && "tb".includes(sb) && Math.abs(p0[0] - p1[0]) > 2) {
    const my = e.bend !== undefined ? e.bend : (p0[1] + p1[1]) / 2;
    return [p0, [p0[0], my], [p1[0], my], p1];
  }
  if (horiz && "tb".includes(sb)) return [p0, [p1[0], p0[1]], p1];
  if (vert && "lr".includes(sb)) return [p0, [p0[0], p1[1]], p1];
  return [p0, p1];
}
function pathD(pts, r = 14) {
  let d = `M${pts[0][0]},${pts[0][1]}`;
  for (let i = 1; i < pts.length - 1; i++) {
    const [px, py] = pts[i - 1], [x, y] = pts[i], [nx, ny] = pts[i + 1];
    const l1 = Math.hypot(x - px, y - py), l2 = Math.hypot(nx - x, ny - y);
    const rr = Math.min(r, l1 / 2, l2 / 2);
    const ax = x - (x - px) / l1 * rr, ay = y - (y - py) / l1 * rr;
    const bx = x + (nx - x) / l2 * rr, by = y + (ny - y) / l2 * rr;
    d += ` L${ax},${ay} Q${x},${y} ${bx},${by}`;
  }
  const last = pts[pts.length - 1];
  return d + ` L${last[0]},${last[1]}`;
}
function lengths(pts) {
  const out = [0];
  for (let i = 1; i < pts.length; i++) out.push(out[i - 1] + Math.hypot(pts[i][0] - pts[i - 1][0], pts[i][1] - pts[i - 1][1]));
  return out;
}
function along(pts, f) {
  const L = lengths(pts), target = clamp(f) * L[L.length - 1];
  for (let i = 1; i < pts.length; i++) {
    if (L[i] >= target) {
      const k = (target - L[i - 1]) / ((L[i] - L[i - 1]) || 1);
      return [pts[i - 1][0] + (pts[i][0] - pts[i - 1][0]) * k, pts[i - 1][1] + (pts[i][1] - pts[i - 1][1]) * k];
    }
  }
  return pts[pts.length - 1];
}
function arrowHead(p, from, color, size = 11) {
  const ang = Math.atan2(p[1] - from[1], p[0] - from[0]);
  const a1 = ang + 2.65, a2 = ang - 2.65;
  el("path", {d: `M${p[0]},${p[1]} L${p[0] + Math.cos(a1) * size},${p[1] + Math.sin(a1) * size} L${p[0] + Math.cos(a2) * size},${p[1] + Math.sin(a2) * size} Z`,
    fill: color});
}
function pillText(x, y, text, opts = {}) {
  const g = el("g", {});
  const fs = opts.size || 13;
  const t = el("text", {x, y: y + 1, "text-anchor": "middle", "dominant-baseline": "middle", "font-size": fs,
    "font-weight": opts.bold ? 700 : 500, fill: opts.ink || "#475569"}, g, text);
  const w = t.getComputedTextLength() + 18, h = fs + 12;
  const r = el("rect", {x: x - w / 2, y: y - h / 2, width: w, height: h, rx: h / 2, fill: opts.fill || "#fff",
    stroke: opts.stroke || "#e2e8f0", "stroke-width": 1.2}, g);
  g.insertBefore(r, t);
  return g;
}
function drawEdge(e, pts, state, color, t) {
  // state: 0 = not yet, 1 = the step is playing, 2 = done
  const c = state ? col(color).c : "#c3ccd8";
  const d = pathD(pts);
  el("path", {d, fill: "none", stroke: c, "stroke-width": state ? 3 : 2.4, "stroke-dasharray": e.dashed ? "7 6" : "",
    "stroke-linecap": "round", opacity: state === 2 ? .85 : 1});
  if (state === 1 && !window.STILL_LINES) { // flowing dots while it plays (videos only: they make GIFs big)
    el("path", {d, fill: "none", stroke: "#fff", "stroke-width": 2, "stroke-dasharray": "2 12",
      "stroke-dashoffset": -t * 60, "stroke-linecap": "round", opacity: .9});
  }
  const n = pts.length;
  if (!e.noHead) arrowHead(pts[n - 1], pts[n - 2], c);
  if (e.both) arrowHead(pts[0], pts[1], c);
  if (e.label) {
    const [lx, ly] = e.labelAt ? (Array.isArray(e.labelAt) ? e.labelAt : along(pts, e.labelAt)) : along(pts, .5);
    const g = pillText(lx, ly, e.label, {size: 14, ink: state ? col(color).ink : "#64748b", stroke: state ? col(color).c : "#e2e8f0"});
    return [lx - g.getBBox().width / 2 - 16, ly];
  }
}

// ---------------------------------------------------------------------------------------------
// Steps, captions, packets
// ---------------------------------------------------------------------------------------------
function badge(x, y, number, color, scale) {
  if (scale <= 0) return;
  const g = el("g", {transform: `translate(${x},${y}) scale(${scale})`});
  el("circle", {cx: 0, cy: 0, r: 15, fill: col(color).c, stroke: "#fff", "stroke-width": 3}, g);
  el("text", {x: 0, y: 1, "text-anchor": "middle", "dominant-baseline": "middle", "font-size": 15, "font-weight": 800, fill: "#fff"}, g, number);
}
function packet(x, y, color, label, iconName) {
  const c = col(color).c;
  if (label) {
    const g = pillText(x, y, label, {fill: c, stroke: "#fff", ink: "#fff", bold: true, size: 13});
    g.setAttribute("filter", "url(#shadow)");
    return;
  }
  el("circle", {cx: x, cy: y, r: 15, fill: c, stroke: "#fff", "stroke-width": 3, filter: "url(#shadow)"});
  icon(iconName || "mail", x - 8, y - 8, 16, "#fff");
}
function caption(step, k, a) {
  if (!step || !step.caption) return;
  const c = col(step.color);
  const g = el("g", {opacity: a});
  el("rect", {x: 40, y: 650, width: W - 80, height: 52, rx: 26, fill: "#fff", stroke: "#e2e8f0", "stroke-width": 1.5, filter: "url(#shadow)"}, g);
  el("circle", {cx: 70, cy: 676, r: 17, fill: c.c}, g);
  el("text", {x: 70, y: 677, "text-anchor": "middle", "dominant-baseline": "middle", "font-size": 16, "font-weight": 800, fill: "#fff"}, g, step.n || k + 1);
  el("text", {x: 100, y: 677, "dominant-baseline": "middle", "font-size": 19.5, "font-weight": 600, fill: "#0f172a"}, g, step.caption);
}
function header() {
  el("text", {x: 48, y: 58, "font-size": 30, "font-weight": 800, fill: "#0f172a", "letter-spacing": "-0.5"}, svg, spec.title);
  if (spec.subtitle) el("text", {x: 48, y: 86, "font-size": 16, fill: "#64748b"}, svg, spec.subtitle);
  el("image", {href: window.LOGO, x: W - 178, y: 30, width: 30, height: 30});
  el("text", {x: W - 142, y: 52, "font-size": 21, "font-weight": 800, fill: "#0f172a", "letter-spacing": "-0.5"}, svg, "crew");
  el("text", {x: W - 94, y: 52, "font-size": 21, "font-weight": 800, fill: "#3b6fe0", "letter-spacing": "-0.5"}, svg, "chat");
}

// The timeline: when each step starts, and the hold at the end.
function plan() {
  let t = spec.lead || .6;
  timeline = spec.steps.map((s, i) => {
    s.color = s.color || STEP_COLORS[i % STEP_COLORS.length];
    const dur = s.dur || spec.stepDur || 2.4;
    const item = {start: t, dur, s};
    t += dur;
    return item;
  });
  total = t + (spec.hold || 2.8);
  return total;
}
function current(t) {
  let k = -1;
  timeline.forEach((it, i) => { if (t >= it.start) k = i; });
  return k;
}

// ---------------------------------------------------------------------------------------------
// Graph diagrams
// ---------------------------------------------------------------------------------------------
function renderGraph(t) {
  const nodes = {};
  spec.nodes.forEach(n => nodes[n.id] = n);
  const edges = {};
  spec.edges.forEach((e, i) => { e.id = e.id || `${e.from}-${e.to}`; edges[e.id] = e; });
  const k = current(t);
  (spec.groups || []).forEach(drawGroup);
  // An edge's state comes from the steps that use it.
  const state = {}, color = {};
  timeline.forEach((it, i) => {
    (it.s.edges || []).forEach(ref => {
      const id = typeof ref === "string" ? ref : ref.id;
      if (i < k || (i === k && t >= it.start)) { state[id] = i < k ? 2 : 1; color[id] = it.s.color; }
    });
  });
  if (k >= 0 && t > timeline[k].start + timeline[k].dur * .9) {
    (timeline[k].s.edges || []).forEach(ref => state[typeof ref === "string" ? ref : ref.id] = 2);
  }
  const routes = {};
  spec.edges.forEach(e => routes[e.id] = route(e, nodes));
  const labelAt = {};
  spec.edges.forEach(e => labelAt[e.id] = drawEdge(e, routes[e.id], state[e.id] || 0, color[e.id], t));
  // Cards light up while a step touches them.
  const glow = {};
  if (k >= 0) {
    const it = timeline[k], f = (t - it.start) / it.dur;
    (it.s.edges || []).forEach(ref => {
      const id = typeof ref === "string" ? ref : ref.id, e = edges[id], rev = ref.reverse;
      const src = rev ? e.to : e.from, dst = rev ? e.from : e.to;
      glow[src] = Math.max(glow[src] || 0, 1 - clamp((f - .25) / .3));
      glow[dst] = Math.max(glow[dst] || 0, clamp((f - .7) / .15));
    });
    (it.s.glow || []).forEach(id => glow[id] = Math.max(glow[id] || 0, clamp(f / .15)));
  }
  spec.nodes.forEach(n => drawNode(n, glow[n.id] || 0));
  // Number badges, then packets on top.
  timeline.forEach((it, i) => {
    if (t < it.start || it.s.badge === false) return;
    const ref = (it.s.edges || [])[0];
    let pos = it.s.badgeAt;
    if (!pos && ref) {
      const id = typeof ref === "string" ? ref : ref.id, e = edges[id];
      pos = labelAt[id] || along(routes[id], e.badgeAt !== undefined ? e.badgeAt : .5);
    }
    if (!pos && it.s.glow) { const n = nodes[it.s.glow[0]]; pos = [n.x + n.w - 4, n.y + 4]; }
    if (pos) badge(pos[0], pos[1], it.s.n || i + 1, it.s.color, pop((t - it.start) / .35));
  });
  if (k >= 0) {
    const it = timeline[k], f = (t - it.start) / it.dur;
    (it.s.edges || []).forEach((ref, j) => {
      const id = typeof ref === "string" ? ref : ref.id;
      let pts = routes[id];
      if (ref.reverse) pts = pts.slice().reverse();
      const [a, b] = ref.span || [.18, .8];
      const m = ease(clamp((f - a) / (b - a)));
      if (f > a - .04 && f < b + .13 && (!ref.span || f <= b + .02)) {
        const [x, y] = along(pts, m);
        packet(x, y, it.s.color, ref.packet !== undefined ? ref.packet : it.s.packet, ref.icon || it.s.icon);
      }
    });
  }
  return k;
}

// ---------------------------------------------------------------------------------------------
// Sequence diagrams
// ---------------------------------------------------------------------------------------------
function renderSequence(t) {
  const ps = spec.participants, n = ps.length;
  const left = spec.left || 120, right = spec.right || W - 120;
  const xs = ps.map((p, i) => n === 1 ? W / 2 : left + (right - left) * i / (n - 1));
  const x = {};
  ps.forEach((p, i) => x[p.id] = xs[i]);
  const top = 180, bottom = 632, rowsTop = spec.rowsTop || 232;
  const msgs = spec.steps;
  const gap = Math.min(spec.rowGap || 84, (bottom - rowsTop - 24) / Math.max(1, msgs.length - 1));
  const k = current(t);
  // Boxes behind the rows (loops, notes spanning columns).
  (spec.boxes || []).forEach(b => {
    const y0 = rowsTop + b.from * gap - 26, y1 = rowsTop + b.to * gap + 16;
    const shown = timeline[b.from] && t >= timeline[b.from].start;
    if (!shown) return;
    el("rect", {x: left - 70, y: y0, width: right - left + 140, height: y1 - y0, rx: 14, fill: "#f8fafc",
      stroke: "#94a3b8", "stroke-width": 1.5, "stroke-dasharray": "6 5"});
    pillText(left - 70 + 40, y0, b.label, {size: 12, bold: true, ink: "#334155"});
  });
  const active = {};
  if (k >= 0) {
    const s = timeline[k].s, f = (t - timeline[k].start) / timeline[k].dur;
    if (s.from) active[s.from] = Math.max(active[s.from] || 0, 1 - clamp((f - .3) / .3));
    if (s.to) active[s.to] = Math.max(active[s.to] || 0, clamp((f - .65) / .15));
    (s.over || []).forEach(id => active[id] = 1);
  }
  ps.forEach((p, i) => {
    el("path", {d: `M${xs[i]},${top} L${xs[i]},${bottom}`, stroke: "#cbd5e1", "stroke-width": 2, "stroke-dasharray": "5 6"});
  });
  ps.forEach((p, i) => {
    const w = spec.cardW || 176, h = 70;
    drawNode({...p, x: xs[i] - w / 2, y: 104, w, h}, active[p.id] || 0);
  });
  timeline.forEach((it, i) => {
    if (t < it.start) return;
    const s = it.s, f = clamp((t - it.start) / it.dur), y = rowsTop + i * gap;
    const c = col(s.color);
    if (s.note) {
      const ids = s.over || [];
      const xa = Math.min(...ids.map(id => x[id])) - 90, xb = Math.max(...ids.map(id => x[id])) + 90;
      const g = el("g", {opacity: clamp(f / .2)});
      el("rect", {x: xa, y: y - 17, width: xb - xa, height: 32, rx: 9, fill: "#fff7d6", stroke: "#e9c46a", "stroke-width": 1.5}, g);
      el("text", {x: (xa + xb) / 2, y: y + 1, "text-anchor": "middle", "dominant-baseline": "middle", "font-size": 15, "font-weight": 600, fill: "#78350f"}, g, s.note);
      badge(xa, y - 16, s.n || i + 1, s.color, pop((t - it.start) / .35));
      return;
    }
    const x0 = x[s.from], x1 = x[s.to];
    const playing = i === k;
    const grow = playing ? ease(clamp((f - .12) / .6)) : 1;
    const stroke = c.c;
    if (s.from === s.to) {
      const r = 46;
      const pts = [[x0, y - 12], [x0 + r, y - 12], [x0 + r, y + 12], [x0 + 6, y + 12]];
      const L = lengths(pts), cut = grow * L[L.length - 1];
      el("path", {d: pathD(pts, 10), fill: "none", stroke, "stroke-width": 2.6, "stroke-dasharray": `${cut} 9999`, opacity: i < k ? .8 : 1});
      if (grow > .97) arrowHead(pts[3], pts[2], stroke, 10);
      el("text", {x: x0 + r + 12, y: y + 1, "dominant-baseline": "middle", "font-size": 15.5, "font-weight": 600, fill: "#1e293b", opacity: clamp(f / .25)}, svg, s.label);
      badge(x0 - 22, y, s.n || i + 1, s.color, pop((t - it.start) / .35));
      if (playing && f > .1 && f < .85) { const p = along(pts, grow); packet(p[0], p[1], s.color, null, s.icon); }
      return;
    }
    const dir = x1 > x0 ? 1 : -1;
    const xa = x0 + dir * 6, xb = x1 - dir * 8;
    const xe = xa + (xb - xa) * grow;
    el("path", {d: `M${xa},${y} L${xe},${y}`, stroke, "stroke-width": 2.6, "stroke-dasharray": s.reply ? "8 6" : "",
      "stroke-linecap": "round", opacity: i < k ? .8 : 1});
    if (grow > .97) arrowHead([xb + dir * 2, y], [xa, y], stroke, 11);
    const mid = (x0 + x1) / 2;
    const tx = el("text", {x: mid, y: y - 12, "text-anchor": "middle", "font-size": 15.5, "font-weight": 600, fill: "#1e293b",
      opacity: clamp(f / .25)}, svg, s.label);
    const tw = tx.getComputedTextLength();
    const bg = el("rect", {x: mid - tw / 2 - 6, y: y - 30, width: tw + 12, height: 23, rx: 6, fill: "#fff", opacity: clamp(f / .25)});
    svg.insertBefore(bg, tx);
    badge(xa + dir * 24, y, s.n || i + 1, s.color, pop((t - it.start) / .35));
    if (playing && f > .12 && f < .85) packet(xe, y, s.color, s.packet, s.icon);
  });
  return k;
}

// ---------------------------------------------------------------------------------------------
function render(t) {
  svg.innerHTML = `<defs><filter id="shadow" x="-20%" y="-20%" width="140%" height="160%">
    <feDropShadow dx="0" dy="3" stdDeviation="5" flood-color="#0f2350" flood-opacity=".10"/></filter>
    <pattern id="dots" width="24" height="24" patternUnits="userSpaceOnUse"><circle cx="2" cy="2" r="1.1" fill="#e6ebf3"/></pattern></defs>`;
  el("rect", {x: 0, y: 0, width: W, height: H, fill: "#ffffff"});
  el("rect", {x: 0, y: 0, width: W, height: H, fill: "url(#dots)"});
  header();
  const k = spec.type === "sequence" ? renderSequence(t) : renderGraph(t);
  // The caption cross-fades between steps; at the end it shows the summary, if any.
  if (k >= 0) {
    const it = timeline[k];
    const a = clamp((t - it.start) / .3);
    if (k === timeline.length - 1 && spec.summary && t > it.start + it.dur) {
      const f = clamp((t - it.start - it.dur) / .3);
      caption({...it.s, caption: spec.summary, n: "✓", color: "green"}, k, f);
    } else {
      caption(it.s, k, a);
    }
  }
  return total;
}

function setup(s) {
  spec = s;
  svg = document.getElementById("d");
  plan();
  return total;
}
