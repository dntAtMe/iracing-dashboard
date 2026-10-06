'use strict';

// ---------- helpers ----------
const $ = (id) => document.getElementById(id);
const isNum = (v) => typeof v === 'number' && Number.isFinite(v);
const clamp = (v, a, b) => Math.min(b, Math.max(a, v));
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

function fmtLap(s) {
  if (!isNum(s) || s <= 0) return '-:--.---';
  const m = Math.floor(s / 60);
  return `${m}:${(s - m * 60).toFixed(3).padStart(6, '0')}`;
}
function fmtSigned(v, d = 3) {
  if (!isNum(v)) return '–';
  const a = Math.abs(v).toFixed(d);
  return Number(a) === 0 ? `±${a}` : `${v > 0 ? '+' : '−'}${a}`;
}
function fmtDist(m) {
  return m >= 1000 ? `${(m / 1000).toFixed(2)} km` : `${Math.round(m)} m`;
}

let prefs = { speed: 'kmh', temp: 'c', fuel: 'l' };
try { prefs = Object.assign(prefs, JSON.parse(localStorage.getItem('prefs') || '{}')); } catch { /* storage unavailable */ }
const U = {
  speed: (ms) => (prefs.speed === 'mph' ? ms * 2.23694 : ms * 3.6),
  speedLbl: () => (prefs.speed === 'mph' ? 'mph' : 'km/h'),
  temp: (c) => (prefs.temp === 'f' ? c * 9 / 5 + 32 : c),
  tempLbl: () => (prefs.temp === 'f' ? '°F' : '°C'),
  fuel: (l) => (prefs.fuel === 'gal' ? l / 3.78541 : l),
  fuelLbl: () => (prefs.fuel === 'gal' ? 'gal' : 'L'),
};

const QS = location.search;
$('nav-live').href = `./${QS}`;
async function api(path, opts) {
  const r = await fetch(QS && path.includes('?') ? `${path}&${QS.slice(1)}` : path + QS, opts);
  if (!r.ok) {
    let msg = `${r.status}`;
    try { msg = (await r.json()).detail || msg; } catch { /* not JSON */ }
    throw new Error(msg);
  }
  return r.json();
}

function fitCanvas(c) {
  const r = c.getBoundingClientRect();
  if (!r.width || !r.height) return null;
  const dpr = window.devicePixelRatio || 1;
  const w = Math.round(r.width * dpr), h = Math.round(r.height * dpr);
  if (c.width !== w || c.height !== h) { c.width = w; c.height = h; }
  const ctx = c.getContext('2d');
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  return [ctx, r.width, r.height];
}

const LAP_COLORS = ['#e9ecef', '#f2a93b', '#3d9bff'];
const C = { rule: '#2a2e35', rule2: '#3a3f47', muted: '#8d949e', faint: '#5d646e', purple: '#a45cff', red: '#f0483e', blue: '#3d9bff', amber: '#f2a93b' };
const FONT = '500 11px Archivo, system-ui, sans-serif';

// ---------- state ----------
const S = {
  sessions: [], sid: null, detail: null,
  sel: [],               // selected lap ids, first = reference
  cache: new Map(),      // lap id -> prepared lap
  len: 1000,             // track length in metres
  x0: 0, x1: 1000,       // visible distance range
  cursor: null,          // cursor distance in metres
  paceHits: [], paceHover: null,
  poll: null,
};

// ---------- lap data ----------
function idxAt(d, x) {
  let lo = 0, hi = d.length - 1;
  if (x < d[0]) return -1;
  while (lo < hi) {
    const mid = (lo + hi + 1) >> 1;
    if (d[mid] <= x) lo = mid; else hi = mid - 1;
  }
  return lo;
}
function interp(L, arr, x, step = false) {
  if (!arr) return null;
  const i = idxAt(L.d, x);
  if (i < 0) return arr[0];
  if (i >= L.n - 1) return arr[L.n - 1];
  const a = arr[i], b = arr[i + 1];
  if (step || a == null || b == null) return a ?? b;
  const span = L.d[i + 1] - L.d[i];
  return span > 0 ? a + (b - a) * (x - L.d[i]) / span : a;
}
function distAtTime(L, t) {
  const ts = L.cols.t;
  let lo = 0, hi = L.n - 1;
  if (t <= ts[0]) return L.d[0];
  if (t >= ts[hi]) return L.d[hi];
  while (lo < hi - 1) {
    const mid = (lo + hi) >> 1;
    if (ts[mid] <= t) lo = mid; else hi = mid;
  }
  const f = ts[hi] > ts[lo] ? (t - ts[lo]) / (ts[hi] - ts[lo]) : 0;
  return L.d[lo] + (L.d[hi] - L.d[lo]) * f;
}

async function loadLap(id) {
  if (S.cache.has(id)) return S.cache.get(id);
  const cols = await api(`/api/laps/${id}`);
  const n = cols.t.length;
  const d = new Float64Array(n);
  let prev = 0, max = -Infinity;
  for (let i = 0; i < n; i++) {
    const p = cols.pct[i] ?? prev;
    prev = p;
    max = Math.max(max, p * S.len);   // keep distance monotonic for lookups
    d[i] = max;
  }
  const meta = S.detail.laps.find((l) => l.id === id);
  const L = { id, n, d, cols, meta, delta: null, color: '#fff' };
  S.cache.set(id, L);
  return L;
}
const selLaps = () => S.sel.map((id) => S.cache.get(id)).filter(Boolean);

function computeDeltas() {
  const laps = selLaps();
  const ref = laps[0];
  laps.forEach((L, k) => {
    L.color = LAP_COLORS[k];
    L.delta = null;
    if (!ref || L === ref || L.meta.partial || ref.meta.partial) return;
    L.delta = new Float32Array(L.n);
    for (let i = 0; i < L.n; i++) L.delta[i] = L.cols.t[i] - interp(ref, ref.cols.t, L.d[i]);
  });
}

// ---------- charts ----------
const CHARTS = [
  { key: 'speed', label: 'Speed', unit: () => U.speedLbl(), conv: (v) => U.speed(v), fmt: (v) => v.toFixed(0), h: 150 },
  { key: 'thr', label: 'Throttle', unit: () => '%', conv: (v) => v * 100, min: 0, max: 100, fmt: (v) => v.toFixed(0), h: 76 },
  { key: 'brk', label: 'Brake', unit: () => '%', conv: (v) => v * 100, min: 0, max: 100, fmt: (v) => v.toFixed(0), h: 76 },
  { key: 'gear', label: 'Gear', unit: () => '', conv: (v) => v, step: true, fmt: (v) => (v < 0 ? 'R' : v === 0 ? 'N' : String(Math.round(v))), h: 60 },
  { key: 'rpm', label: 'RPM', unit: () => '', conv: (v) => v, fmt: (v) => v.toFixed(0), h: 76 },
  { key: 'steer', label: 'Steering', unit: () => '°', conv: (v) => v * 57.2958, sym: true, fmt: (v) => v.toFixed(0), h: 80 },
  { key: 'latG', label: 'Lateral G', unit: () => 'g', conv: (v) => v / 9.81, sym: true, fmt: (v) => v.toFixed(2), h: 76 },
  { key: 'lonG', label: 'Longitudinal G', unit: () => 'g', conv: (v) => v / 9.81, sym: true, fmt: (v) => v.toFixed(2), h: 76 },
  { key: 'delta', label: 'Delta to reference', unit: () => 's', sym: true, fmt: (v) => fmtSigned(v, 3), h: 96 },
];

function buildCharts() {
  const host = $('charts');
  host.innerHTML = '';
  for (const ch of CHARTS) {
    const el = document.createElement('div');
    el.className = 'chart';
    el.innerHTML = `<div class="chart-h"><span class="lbl">${ch.label}</span><span class="vals"></span></div>` +
      `<div class="stack" style="height:${ch.h}px"><canvas></canvas><canvas></canvas></div>`;
    host.appendChild(el);
    ch.el = el;
    [ch.base, ch.over] = el.querySelectorAll('canvas');
    ch.vals = el.querySelector('.vals');
    bindChartInput(el.querySelector('.stack'));
  }
}

function series(ch, L) {
  if (ch.key === 'delta') return L.delta ? { arr: L.delta, conv: (v) => v } : null;
  const arr = L.cols[ch.key];
  return arr ? { arr, conv: ch.conv } : null;
}
function chartVal(ch, L, x) {
  const s = series(ch, L);
  if (!s) return null;
  const v = interp(L, s.arr, x, ch.step);
  return v == null ? null : s.conv(v);
}

function drawChartBase(ch) {
  const laps = selLaps().filter((L) => series(ch, L));
  ch.el.hidden = !laps.length;
  if (!laps.length) return;
  const fit = fitCanvas(ch.base);
  if (!fit) return;
  const [ctx, w, h] = fit;
  ctx.clearRect(0, 0, w, h);

  let lo = Infinity, hi = -Infinity;
  const ranges = laps.map((L) => {
    const { arr, conv } = series(ch, L);
    const a = Math.max(0, idxAt(L.d, S.x0)), b = Math.min(L.n - 1, idxAt(L.d, S.x1) + 1);
    for (let i = a; i <= b; i++) {
      if (arr[i] == null) continue;
      const v = conv(arr[i]);
      if (v < lo) lo = v;
      if (v > hi) hi = v;
    }
    return [a, b];
  });
  if (ch.min != null) { lo = ch.min; hi = ch.max; }
  if (!isFinite(lo)) { lo = 0; hi = 1; }
  if (ch.sym) { const m = Math.max(Math.abs(lo), Math.abs(hi), ch.key === 'delta' ? 0.05 : 0.01); lo = -m; hi = m; }
  if (hi - lo < 1e-9) hi = lo + 1;
  const pad = (hi - lo) * 0.08;
  lo -= pad; hi += pad;
  ch.y = { lo, hi };

  const X = (d) => (d - S.x0) / (S.x1 - S.x0) * w;
  const Y = (v) => h - 2 - (v - lo) / (hi - lo) * (h - 4);

  ctx.strokeStyle = C.rule; ctx.lineWidth = 1;
  for (const f of [0.25, 0.5, 0.75]) { ctx.beginPath(); ctx.moveTo(0, Math.round(h * f) + 0.5); ctx.lineTo(w, Math.round(h * f) + 0.5); ctx.stroke(); }
  if (ch.sym) { ctx.strokeStyle = C.rule2; ctx.beginPath(); ctx.moveTo(0, Math.round(Y(0)) + 0.5); ctx.lineTo(w, Math.round(Y(0)) + 0.5); ctx.stroke(); }

  // reference last so it sits on top
  for (let k = laps.length - 1; k >= 0; k--) {
    const L = laps[k];
    const { arr, conv } = series(ch, L);
    const [a, b] = ranges[k];
    ctx.strokeStyle = L.color; ctx.lineWidth = k === 0 ? 1.6 : 1.3; ctx.lineJoin = 'round';
    ctx.beginPath();
    if (ch.step) {
      let prev = null;
      for (let i = a; i <= b; i++) {
        if (arr[i] == null) continue;
        const x = X(L.d[i]), y = Y(conv(arr[i]));
        if (prev == null) ctx.moveTo(x, y);
        else if (y !== prev) { ctx.lineTo(x, prev); ctx.lineTo(x, y); }
        prev = y;
      }
      if (prev != null) ctx.lineTo(X(L.d[b]), prev);
    } else {
      // one vertical min/max span per half pixel keeps dense laps fast and faithful
      let started = false, px = null, mn = 0, mx = 0, last = 0;
      const flush = () => {
        if (px == null) return;
        if (!started) { ctx.moveTo(px, Y(mn)); started = true; } else ctx.lineTo(px, Y(mn));
        if (mx !== mn) ctx.lineTo(px, Y(mx));
        ctx.lineTo(px, Y(last));
      };
      for (let i = a; i <= b; i++) {
        if (arr[i] == null) continue;
        const v = conv(arr[i]);
        const x = Math.round(X(L.d[i]) * 2) / 2;
        if (x !== px) { flush(); px = x; mn = mx = last = v; }
        else { if (v < mn) mn = v; if (v > mx) mx = v; last = v; }
      }
      flush();
    }
    ctx.stroke();
  }

  // scale labels on a backing so traces never hide them
  ctx.font = FONT; ctx.textBaseline = 'middle';
  for (const [txt, y] of [[ch.fmt(hi - pad), 9], [ch.fmt(lo + pad), h - 9]]) {
    const tw = ctx.measureText(txt).width + 8;
    ctx.fillStyle = 'rgba(32,35,41,.85)'; ctx.fillRect(0, y - 8, tw, 16);
    ctx.fillStyle = C.muted; ctx.fillText(txt, 4, y);
  }
}

function drawAxis() {
  const fit = fitCanvas($('axis'));
  if (!fit) return;
  const [ctx, w, h] = fit;
  ctx.clearRect(0, 0, w, h);
  const span = S.x1 - S.x0;
  const raw = span / Math.max(2, w / 110);
  const pow = 10 ** Math.floor(Math.log10(raw));
  const step = [1, 2, 5, 10].map((m) => m * pow).find((s) => s >= raw);
  ctx.font = FONT; ctx.fillStyle = C.muted; ctx.strokeStyle = C.rule2; ctx.textBaseline = 'top'; ctx.textAlign = 'center';
  for (let d = Math.ceil(S.x0 / step) * step; d <= S.x1; d += step) {
    const x = Math.round((d - S.x0) / span * w) + 0.5;
    ctx.beginPath(); ctx.moveTo(x, 0); ctx.lineTo(x, 5); ctx.stroke();
    ctx.fillText(fmtDist(d), clamp(x, 24, w - 24), 8);
  }
}

function drawChartsBase() {
  const any = S.sel.length > 0;
  $('charts-empty').hidden = any;
  document.querySelector('.axis').hidden = !any;
  CHARTS.forEach(drawChartBase);
  if (any) drawAxis();
}

function drawOverlays() {
  const laps = selLaps();
  const x = S.cursor;
  for (const ch of CHARTS) {
    if (ch.el.hidden) continue;
    const fit = fitCanvas(ch.over);
    if (!fit) continue;
    const [ctx, w, h] = fit;
    ctx.clearRect(0, 0, w, h);
    let html = '';
    if (x != null && x >= S.x0 && x <= S.x1 && ch.y) {
      const px = Math.round((x - S.x0) / (S.x1 - S.x0) * w) + 0.5;
      ctx.strokeStyle = 'rgba(233,236,239,.45)'; ctx.lineWidth = 1;
      ctx.beginPath(); ctx.moveTo(px, 0); ctx.lineTo(px, h); ctx.stroke();
      const { lo, hi } = ch.y;
      for (const L of laps) {
        const v = chartVal(ch, L, x);
        if (v == null) continue;
        const py = h - 2 - (v - lo) / (hi - lo) * (h - 4);
        ctx.fillStyle = L.color;
        ctx.beginPath(); ctx.arc(px, py, 3, 0, Math.PI * 2); ctx.fill();
        html += `<span style="color:${L.color}">${esc(ch.fmt(v))}</span>`;
      }
      if (html && ch.unit()) html += `<span class="u">${ch.unit()}</span>`;
    }
    if (ch.vals.innerHTML !== html) ch.vals.innerHTML = html;
  }
  const at = $('cursor-at');
  if (x == null || !laps.length) at.textContent = '';
  else at.innerHTML = `${fmtDist(x)} ` + laps.map((L) => `<span style="color:${L.color}">${fmtLap(interp(L, L.cols.t, x))}</span>`).join(' ');
  drawMapOverlay();
}

// ---------- chart input: hover, zoom, pan ----------
function setRange(x0, x1) {
  const span = clamp(x1 - x0, 25, S.len);
  x0 = clamp(x0, 0, S.len - span);
  S.x0 = x0; S.x1 = x0 + span;
  drawChartsBase(); drawMapBase(); drawOverlays();
}
function zoomAt(x, factor) {
  const span = (S.x1 - S.x0) * factor;
  const f = (x - S.x0) / (S.x1 - S.x0);
  setRange(x - f * span, x - f * span + span);
}
function bindChartInput(el) {
  const toX = (e) => { const r = el.getBoundingClientRect(); return S.x0 + (e.clientX - r.left) / r.width * (S.x1 - S.x0); };
  let drag = null;
  el.addEventListener('pointermove', (e) => {
    if (drag && e.pointerType === 'mouse') {
      const r = el.getBoundingClientRect();
      const dx = (e.clientX - drag.cx) / r.width * (drag.x1 - drag.x0);
      setRange(drag.x0 - dx, drag.x1 - dx);
      return;
    }
    S.cursor = toX(e);
    drawOverlays();
  });
  el.addEventListener('pointerdown', (e) => {
    if (e.pointerType !== 'mouse') { S.cursor = toX(e); drawOverlays(); return; }
    drag = { cx: e.clientX, x0: S.x0, x1: S.x1 };
    el.setPointerCapture(e.pointerId);
    el.classList.add('grabbing');
  });
  const end = () => { drag = null; el.classList.remove('grabbing'); };
  el.addEventListener('pointerup', end);
  el.addEventListener('pointercancel', end);
  el.addEventListener('pointerleave', () => { if (!drag) { S.cursor = null; drawOverlays(); } });
  el.addEventListener('wheel', (e) => { e.preventDefault(); zoomAt(toX(e), e.deltaY > 0 ? 1.25 : 0.8); }, { passive: false });
  el.addEventListener('dblclick', () => setRange(0, S.len));
}
$('z-in').addEventListener('click', () => zoomAt(S.cursor ?? (S.x0 + S.x1) / 2, 0.6));
$('z-out').addEventListener('click', () => zoomAt(S.cursor ?? (S.x0 + S.x1) / 2, 1.6));
$('z-reset').addEventListener('click', () => setRange(0, S.len));

// ---------- track map ----------
function mapPoint(pts, pct) {
  const n = pts.length, u = (((pct % 1) + 1) % 1) * n;
  const i = Math.floor(u) % n, j = (i + 1) % n, f = u - Math.floor(u);
  return [pts[i][0] + (pts[j][0] - pts[i][0]) * f, pts[i][1] + (pts[j][1] - pts[i][1]) * f];
}
function mapGeom(w, h) {
  const R = Math.min(w, h) / 2 - 18;
  return (p) => [w / 2 + p[0] * R, h / 2 - p[1] * R];
}
function speedColor(f) {
  // slow red -> yellow -> fast green
  const stops = [[240, 72, 62], [244, 208, 63], [57, 211, 83]];
  const s = clamp(f, 0, 1) * 2, i = Math.min(1, Math.floor(s)), t = s - i;
  const c = stops[i].map((a, k) => Math.round(a + (stops[i + 1][k] - a) * t));
  return `rgb(${c[0]},${c[1]},${c[2]})`;
}

function drawMapBase() {
  const fit = fitCanvas($('map-b'));
  if (!fit) return;
  const [ctx, w, h] = fit;
  ctx.clearRect(0, 0, w, h);
  const pts = S.detail?.map;
  const legend = $('map-legend');
  if (!pts) {
    ctx.fillStyle = C.muted; ctx.font = '400 13px Archivo, system-ui, sans-serif'; ctx.textAlign = 'center';
    ctx.fillText('No outline for this track yet.', w / 2, h / 2 - 8);
    ctx.fillText('The live dashboard draws it after one clean lap.', w / 2, h / 2 + 12);
    legend.innerHTML = '';
    return;
  }
  const P = mapGeom(w, h);
  const N = pts.length;
  const ref = selLaps()[0];
  ctx.lineCap = 'round'; ctx.lineJoin = 'round';

  // zoom window band
  if (S.x1 - S.x0 < S.len * 0.98) {
    ctx.strokeStyle = 'rgba(242,169,59,.28)'; ctx.lineWidth = 18;
    ctx.beginPath();
    const k0 = Math.floor(S.x0 / S.len * N), k1 = Math.ceil(S.x1 / S.len * N);
    for (let k = k0; k <= k1; k++) { const [x, y] = P(pts[k % N]); k === k0 ? ctx.moveTo(x, y) : ctx.lineTo(x, y); }
    ctx.stroke();
  }

  ctx.strokeStyle = '#30353d'; ctx.lineWidth = 11;
  ctx.beginPath(); pts.forEach((p, k) => { const [x, y] = P(p); k ? ctx.lineTo(x, y) : ctx.moveTo(x, y); }); ctx.closePath(); ctx.stroke();

  if (ref && ref.cols.speed) {
    const sp = pts.map((_, k) => interp(ref, ref.cols.speed, k / N * S.len));
    const lo = Math.min(...sp), hi = Math.max(...sp);
    ctx.lineWidth = 5;
    for (let k = 0; k < N; k++) {
      const [x0, y0] = P(pts[k]), [x1, y1] = P(pts[(k + 1) % N]);
      ctx.strokeStyle = speedColor((sp[k] - lo) / ((hi - lo) || 1));
      ctx.beginPath(); ctx.moveTo(x0, y0); ctx.lineTo(x1, y1); ctx.stroke();
    }
    legend.innerHTML = `<span>Lap ${ref.meta.lap} speed</span><span class="ramp"></span>` +
      `<span>${U.speed(lo).toFixed(0)} to ${U.speed(hi).toFixed(0)} ${U.speedLbl()}</span>`;
  } else {
    legend.innerHTML = '';
  }

  // start/finish
  const [sx, sy] = P(mapPoint(pts, 0)), [nx, ny] = P(mapPoint(pts, 0.004));
  const ang = Math.atan2(ny - sy, nx - sx) + Math.PI / 2;
  ctx.strokeStyle = '#fff'; ctx.lineWidth = 3; ctx.lineCap = 'butt';
  ctx.beginPath(); ctx.moveTo(sx - Math.cos(ang) * 11, sy - Math.sin(ang) * 11); ctx.lineTo(sx + Math.cos(ang) * 11, sy + Math.sin(ang) * 11); ctx.stroke();

  // events on the selected laps
  const lapNums = new Set(selLaps().map((L) => L.meta.lap));
  for (const e of S.detail.events) {
    if (!lapNums.has(e.lap) || !isNum(e.pct) || !['incident', 'pit_in', 'pit_out', 'flag'].includes(e.kind)) continue;
    const [x, y] = P(mapPoint(pts, e.pct));
    if (e.kind === 'incident') {
      ctx.strokeStyle = C.red; ctx.lineWidth = 2.5;
      ctx.beginPath(); ctx.moveTo(x - 6, y - 6); ctx.lineTo(x + 6, y + 6); ctx.moveTo(x + 6, y - 6); ctx.lineTo(x - 6, y + 6); ctx.stroke();
      ctx.fillStyle = C.red; ctx.font = '700 11px Archivo, system-ui'; ctx.textAlign = 'left';
      ctx.fillText(`${e.data?.added ?? ''}x`, x + 8, y - 6);
    } else if (e.kind === 'flag' && e.data?.flag === 'yellow') {
      ctx.fillStyle = '#f4d03f'; ctx.fillRect(x - 4, y - 4, 8, 8);
    } else if (e.kind.startsWith('pit')) {
      ctx.fillStyle = C.blue; ctx.fillRect(x - 4, y - 4, 8, 8);
    }
  }
}

function drawMapOverlay() {
  const fit = fitCanvas($('map-o'));
  if (!fit) return;
  const [ctx, w, h] = fit;
  ctx.clearRect(0, 0, w, h);
  const pts = S.detail?.map;
  const laps = selLaps();
  if (!pts || S.cursor == null || !laps.length) return;
  const P = mapGeom(w, h);
  const ref = laps[0];
  // other laps drawn where they were at the reference lap's time, so the gap shows on track
  const tRef = interp(ref, ref.cols.t, S.cursor);
  for (let k = laps.length - 1; k >= 0; k--) {
    const L = laps[k];
    const d = k === 0 || L.meta.partial || ref.meta.partial ? S.cursor : distAtTime(L, tRef);
    const [x, y] = P(mapPoint(pts, d / S.len));
    ctx.beginPath(); ctx.arc(x, y, k === 0 ? 8 : 6.5, 0, Math.PI * 2);
    ctx.fillStyle = L.color; ctx.fill();
    ctx.lineWidth = 2; ctx.strokeStyle = '#16191d'; ctx.stroke();
  }
}

function bindMapInput() {
  const el = $('map-o');
  el.addEventListener('pointermove', (e) => {
    const pts = S.detail?.map;
    if (!pts || !S.sel.length) return;
    const r = el.getBoundingClientRect();
    const P = mapGeom(r.width, r.height);
    const mx = e.clientX - r.left, my = e.clientY - r.top;
    let best = -1, bd = 40 * 40;
    pts.forEach((p, k) => { const [x, y] = P(p); const dd = (x - mx) ** 2 + (y - my) ** 2; if (dd < bd) { bd = dd; best = k; } });
    S.cursor = best < 0 ? null : best / pts.length * S.len;
    drawOverlays();
  });
  el.addEventListener('pointerleave', () => { S.cursor = null; drawOverlays(); });
  el.addEventListener('click', () => { if (S.cursor != null) zoomAt(S.cursor, 0.35); });
}

// ---------- session pace ----------
function drawPace() {
  const fit = fitCanvas($('pace'));
  if (!fit || !S.detail) return;
  const [ctx, w, h] = fit;
  ctx.clearRect(0, 0, w, h);
  const d = S.detail, me = d.session.car_idx;
  const own = d.laps.filter((l) => !l.partial && l.time > 0);
  const field = d.field.filter((f) => f.car_idx !== me && f.time > 0);
  S.paceHits = [];
  if (!own.length && !field.length) {
    ctx.fillStyle = C.muted; ctx.font = '400 13px Archivo, system-ui'; ctx.textAlign = 'center';
    ctx.fillText('No timed laps yet.', w / 2, h / 2);
    return;
  }
  const times = (own.length ? own : field).map((l) => l.time).sort((a, b) => a - b);
  const all = own.concat(field).map((l) => l.time);
  const lo0 = Math.min(...all);
  const med = times[Math.floor(times.length / 2)];
  const hi0 = Math.max(lo0 * 1.02, Math.min(Math.max(...all), med * 1.06));
  const pad = (hi0 - lo0) * 0.1;
  const lo = lo0 - pad, hi = hi0 + pad;
  const maxLap = Math.max(2, ...own.map((l) => l.lap), ...field.map((f) => f.lap));
  const L = 54, R = 12, T = 10, B = 24;
  const X = (lap) => L + (lap - 1) / (maxLap - 1) * (w - L - R);
  const Y = (t) => T + (1 - (clamp(t, lo, hi) - lo) / (hi - lo)) * (h - T - B);

  ctx.font = FONT; ctx.fillStyle = C.muted; ctx.strokeStyle = C.rule; ctx.lineWidth = 1;
  ctx.textAlign = 'right'; ctx.textBaseline = 'middle';
  for (let k = 0; k <= 3; k++) {
    const t = lo0 + (hi0 - lo0) * k / 3, y = Math.round(Y(t)) + 0.5;
    ctx.beginPath(); ctx.moveTo(L, y); ctx.lineTo(w - R, y); ctx.stroke();
    ctx.fillText(fmtLap(t), L - 6, y);
  }
  ctx.textAlign = 'center'; ctx.textBaseline = 'top';
  const lapStep = Math.max(1, Math.ceil(maxLap / Math.max(2, (w - L) / 40)));
  for (let lap = 1; lap <= maxLap; lap += lapStep) ctx.fillText(String(lap), X(lap), h - B + 7);

  ctx.fillStyle = 'rgba(141,148,158,.35)';
  for (const f of field) {
    const x = X(f.lap), y = Y(f.time);
    ctx.fillRect(x - 1.5, y - 1.5, 3, 3);
    const drv = d.drivers[f.car_idx];
    S.paceHits.push({ x, y, text: `Lap ${f.lap}, ${fmtLap(f.time)}, #${drv?.num ?? ''} ${drv?.name ?? ''}` });
  }

  const best = Math.min(...own.filter((l) => !l.pit).map((l) => l.time));
  ctx.strokeStyle = '#c9ced5'; ctx.lineWidth = 1.5; ctx.beginPath();
  own.forEach((l, k) => { const x = X(l.lap), y = Y(l.time); k ? ctx.lineTo(x, y) : ctx.moveTo(x, y); });
  ctx.stroke();
  for (const l of own) {
    const x = X(l.lap), y = Y(l.time);
    const selIdx = S.sel.indexOf(l.id);
    ctx.beginPath(); ctx.arc(x, y, 3.5, 0, Math.PI * 2);
    ctx.fillStyle = l.time === best ? C.purple : l.pit ? '#16191d' : '#e9ecef';
    ctx.fill();
    if (l.pit) { ctx.strokeStyle = C.blue; ctx.lineWidth = 1.5; ctx.stroke(); }
    if (selIdx >= 0) { ctx.beginPath(); ctx.arc(x, y, 7, 0, Math.PI * 2); ctx.strokeStyle = LAP_COLORS[selIdx]; ctx.lineWidth = 2; ctx.stroke(); }
    if (l.time > hi) { ctx.fillStyle = C.muted; ctx.fillText('▲', x, T - 2); }
    S.paceHits.push({ x, y, lap: l, text: `Lap ${l.lap}, ${fmtLap(l.time)}${l.pit ? ', pit' : ''}${l.incidents ? `, ${l.incidents}x` : ''}` });
  }

  const hv = S.paceHover;
  if (hv) {
    ctx.font = '600 12px Archivo, system-ui'; ctx.textBaseline = 'middle';
    const tw = ctx.measureText(hv.text).width + 14;
    const bx = clamp(hv.x - tw / 2, 2, w - tw - 2), by = hv.y - 28 < 2 ? hv.y + 12 : hv.y - 28;
    ctx.fillStyle = '#2c3138'; ctx.fillRect(bx, by, tw, 20);
    ctx.fillStyle = '#e9ecef'; ctx.textAlign = 'left'; ctx.fillText(hv.text, bx + 7, by + 10);
  }
}
function bindPaceInput() {
  const el = $('pace');
  const hit = (e) => {
    const r = el.getBoundingClientRect();
    const mx = e.clientX - r.left, my = e.clientY - r.top;
    let best = null, bd = 14 * 14;
    for (const p of S.paceHits) { const dd = (p.x - mx) ** 2 + (p.y - my) ** 2; if (dd < bd || (p.lap && best && !best.lap && dd <= bd * 1.5)) { bd = dd; best = p; } }
    return best;
  };
  el.addEventListener('pointermove', (e) => { const h = hit(e); if (h !== S.paceHover) { S.paceHover = h; el.style.cursor = h?.lap ? 'pointer' : ''; drawPace(); } });
  el.addEventListener('pointerleave', () => { S.paceHover = null; drawPace(); });
  el.addEventListener('click', (e) => { const h = hit(e); if (h?.lap) toggleLap(h.lap.id); });
}

// ---------- lists ----------
function renderSessions() {
  $('s-count').textContent = S.sessions.length ? `${S.sessions.length}` : '';
  const host = $('sessions');
  if (!S.sessions.length) {
    host.innerHTML = '<div class="note pad">Nothing recorded yet. Sessions are saved automatically while the dashboard server runs and you drive.</div>';
    return;
  }
  host.innerHTML = S.sessions.map((s) => {
    const when = new Date(s.started * 1000).toLocaleString([], { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' });
    const live = !s.ended;
    return `<button class="sess${s.id === S.sid ? ' on' : ''}" data-id="${s.id}">` +
      `<span class="s1">${esc(s.track)}${s.config ? `<span class="muted">, ${esc(s.config)}</span>` : ''}</span>` +
      `<span class="s2"><span>${esc(when)}</span><span>${esc(s.type || s.name || '')}</span><span>${esc(s.car || '')}</span></span>` +
      `<span class="s2"><span>${s.laps} ${s.laps === 1 ? 'lap' : 'laps'}</span><span>Best ${fmtLap(s.best)}</span>${live ? '<span class="rec">Recording</span>' : ''}${s.source === 'mock' ? '<span class="sim">Simulated</span>' : ''}</span>` +
      '</button>';
  }).join('');
  host.querySelectorAll('.sess').forEach((b) => b.addEventListener('click', () => openSession(Number(b.dataset.id))));
}

function bestLapTime() {
  const ok = S.detail.laps.filter((l) => !l.partial && l.time > 0);
  const clean = ok.filter((l) => !l.pit);
  return Math.min(...(clean.length ? clean : ok).map((l) => l.time));
}

function renderLapTable() {
  const body = $('laptbl').tBodies[0];
  const laps = S.detail?.laps || [];
  if (!laps.length) { body.innerHTML = '<tr><td class="empty" colspan="6">No laps in this session yet.</td></tr>'; return; }
  const best = bestLapTime();
  body.innerHTML = laps.slice().reverse().map((l) => {
    const k = S.sel.indexOf(l.id);
    const chip = k >= 0 ? `<i class="chip" style="background:${LAP_COLORS[k]}"></i>` : '<i class="chip"></i>';
    const timed = !l.partial && l.time > 0;
    const tags = (l.partial ? '<span class="tag">Partial</span>' : '') + (l.pit ? '<span class="tag pit">Pit</span>' : '');
    return `<tr data-id="${l.id}" class="${k >= 0 ? 'sel' : ''}"><td class="name">${chip}${l.lap}${tags}</td>` +
      `<td><span class="${timed && l.time === best ? 'ob' : ''}">${timed ? fmtLap(l.time) : '–'}</span></td>` +
      `<td class="dim">${timed && l.time !== best && isFinite(best) ? fmtSigned(l.time - best) : ''}</td>` +
      `<td>${isNum(l.fuel_used) ? U.fuel(l.fuel_used).toFixed(2) : '–'}</td>` +
      `<td class="${l.incidents ? 'bad' : ''}">${l.incidents ?? '–'}</td>` +
      `<td>${l.position || '–'}</td></tr>`;
  }).join('');
  body.querySelectorAll('tr[data-id]').forEach((tr) => tr.addEventListener('click', () => toggleLap(Number(tr.dataset.id))));
}

function renderLapSummary() {
  const body = $('lapsum').tBodies[0];
  const laps = selLaps();
  if (!laps.length) { body.innerHTML = ''; return; }
  const avgTemp = (c) => { const t = (c?.t || []).filter(isNum); return t.length ? U.temp(t.reduce((a, b) => a + b, 0) / t.length).toFixed(0) : '–'; };
  const head = '<tr class="head"><th class="l">Lap</th><th>Time</th><th>Top speed</th><th>Fuel</th><th>Inc</th><th>Track</th><th class="hide-s">Tyres LF RF LR RR</th><th></th></tr>';
  const canReplay = (S.sessions.find((x) => x.id === S.sid)?.frames ?? 0) > 0;
  body.innerHTML = head + laps.map((L) => {
    const m = L.meta, ty = m.tyres || {};
    return `<tr><td class="name"><i class="chip" style="background:${L.color}"></i>${m.lap}${L === laps[0] ? '<span class="tag">Reference</span>' : ''}</td>` +
      `<td>${m.time > 0 && !m.partial ? fmtLap(m.time) : 'Partial'}</td>` +
      `<td>${isNum(m.max_speed) ? `${U.speed(m.max_speed).toFixed(0)} ${U.speedLbl()}` : '–'}</td>` +
      `<td>${isNum(m.fuel_used) ? `${U.fuel(m.fuel_used).toFixed(2)} ${U.fuelLbl()}` : '–'}</td>` +
      `<td>${m.incidents ?? '–'}</td>` +
      `<td>${isNum(m.track_temp) ? `${U.temp(m.track_temp).toFixed(1)}${U.tempLbl()}` : '–'}</td>` +
      `<td class="hide-s">${['LF', 'RF', 'LR', 'RR'].map((c) => avgTemp(ty[c])).join(' ')}</td>` +
      `<td>${canReplay && isNum(m.started_at) ? `<a class="replay-link" href="${replayUrl(S.sid, m.started_at)}">Replay</a>` : ''}</td></tr>`;
  }).join('');
}

function replayUrl(sid, t) {
  const p = new URLSearchParams(QS);
  p.set('replay', sid);
  if (t != null) p.set('t', t.toFixed(2)); else p.delete('t');
  return `./?${p}`;
}

function renderHeader() {
  const s = S.detail?.session;
  if (!s) { $('h-title').textContent = 'History'; $('h-sub').innerHTML = ''; return; }
  $('h-title').textContent = [s.track, s.config].filter(Boolean).join(', ');
  const when = new Date(s.started * 1000).toLocaleString([], { dateStyle: 'medium', timeStyle: 'short' });
  const parts = [when, s.name || s.type, s.car, s.driver].filter(Boolean).map((v) => `<span>${esc(v)}</span>`);
  if (S.detail.live) parts.push('<span class="rec">Recording</span>');
  $('h-sub').innerHTML = parts.join('');
  $('btn-del').disabled = !!S.detail.live;
  const listed = S.sessions.find((x) => x.id === s.id);
  $('btn-replay').hidden = !(listed && listed.frames > 0);
  $('btn-replay').href = replayUrl(s.id);
  $('map-meta').textContent = s.length_m ? fmtDist(s.length_m) : '';
}

// ---------- actions ----------
async function toggleLap(id) {
  const k = S.sel.indexOf(id);
  if (k >= 0) S.sel.splice(k, 1);
  else { if (S.sel.length >= 3) S.sel.pop(); S.sel.push(id); }
  try { await Promise.all(S.sel.map(loadLap)); } catch (e) { console.error(e); }
  computeDeltas();
  renderLapTable(); renderLapSummary();
  redrawAll();
}

function redrawAll() {
  drawChartsBase(); drawMapBase(); drawPace(); drawOverlays();
}

async function openSession(sid) {
  clearTimeout(S.poll);
  S.sid = sid; S.sel = []; S.cache.clear(); S.cursor = null;
  S.detail = await api(`/api/sessions/${sid}`);
  S.len = S.detail.session.length_m || 1000;
  S.x0 = 0; S.x1 = S.len;
  try { history.replaceState(null, '', `#${sid}`); } catch { /* sandboxed */ }
  renderHeader(); renderSessions(); renderLapTable(); renderLapSummary();
  const best = bestLapTime();
  const bestLap = S.detail.laps.find((l) => l.time === best && !l.partial);
  if (bestLap) await toggleLap(bestLap.id);
  else redrawAll();
  if (S.detail.live) S.poll = setTimeout(refreshLive, 5000);
}

async function refreshLive() {
  try {
    const d = await api(`/api/sessions/${S.sid}`);
    if (S.sid !== d.session.id) return;
    const known = S.detail.laps.length;
    S.detail = d;
    for (const L of S.cache.values()) L.meta = d.laps.find((l) => l.id === L.id) || L.meta;
    if (d.laps.length !== known) {
      renderLapTable(); renderLapSummary(); drawPace(); drawMapBase();
      S.sessions = await api('/api/sessions'); renderSessions();
    }
    renderHeader();
  } catch (e) { console.error(e); }
  if (S.detail.live) S.poll = setTimeout(refreshLive, 5000);
}

$('btn-del').addEventListener('click', async () => {
  const s = S.detail?.session;
  if (!s || S.detail.live) return;
  if (!confirm(`Delete the ${s.track} session from ${new Date(s.started * 1000).toLocaleString()}? This can't be undone.`)) return;
  await api(`/api/sessions/${s.id}`, { method: 'DELETE' });
  S.sessions = await api('/api/sessions');
  S.detail = null; S.sid = null;
  renderSessions();
  if (S.sessions.length) openSession(S.sessions[0].id);
  else { renderHeader(); renderLapTable(); redrawAll(); }
});

let resizeT;
window.addEventListener('resize', () => { clearTimeout(resizeT); resizeT = setTimeout(redrawAll, 80); });

async function init() {
  buildCharts(); bindMapInput(); bindPaceInput();
  try {
    S.sessions = await api('/api/sessions');
  } catch (e) {
    $('sessions').innerHTML = `<div class="note pad">Couldn't load history: ${esc(e.message)}</div>`;
    return;
  }
  renderSessions();
  const fromHash = Number(location.hash.slice(1));
  const pick = S.sessions.find((s) => s.id === fromHash) || S.sessions.find((s) => s.laps > 0) || S.sessions[0];
  if (pick) openSession(pick.id);
  else redrawAll();
}
// ---------- data folder ----------
const ST = { info: null, browse: null };

async function openStorage() {
  const dlg = $('store-dlg');
  $('store-msg').textContent = '';
  try {
    ST.info = await api('/api/storage');
  } catch (e) {
    $('store-cur').textContent = '';
    $('store-msg').textContent = `Couldn't read the data folder: ${e.message}`;
    dlg.showModal();
    return;
  }
  renderStorageInfo();
  $('store-pick').hidden = !ST.info.canChange;
  if (!ST.info.canChange) {
    $('store-msg').textContent = 'The folder can only be changed on the PC running the dashboard. Open this page there at localhost.';
  } else {
    await browseTo(ST.info.dir);
  }
  dlg.showModal();
}

function renderStorageInfo() {
  const i = ST.info;
  $('store-cur').textContent = `${i.dir}${i.dir.endsWith('\\') || i.dir.endsWith('/') ? '' : (i.dir.includes('\\') ? '\\' : '/')}${i.file}`;
  const warn = [];
  if (i.network) warn.push('Network folder: works, but keep the dashboard on one PC writing to it at a time.');
  if (i.cliOverride) warn.push('The server was started with --data-dir, so a change here lasts until it restarts with that option.');
  $('store-warn').textContent = warn.join(' ');
}

async function browseTo(path) {
  try {
    ST.browse = await api(`/api/storage/browse?path=${encodeURIComponent(path)}`);
  } catch (e) {
    $('store-msg').textContent = e.message;
    return;
  }
  const b = ST.browse;
  $('store-msg').textContent = '';
  $('store-path').value = b.path;
  $('store-up').disabled = !b.parent;
  const drives = $('store-drives');
  const places = [...b.drives.map((d) => ({ path: d, label: d })), ...(b.places || [])];
  drives.hidden = !places.length;
  const cur = places.filter((pl) => b.path.toLowerCase().startsWith(pl.path.toLowerCase().replace(/\\$/, '')))
    .sort((x, y) => y.path.length - x.path.length)[0];
  drives.innerHTML = (cur ? '' : '<option value="" selected>Go to…</option>') + places.map((pl, k) =>
    `${k === b.drives.length && k ? '<option disabled>──────────</option>' : ''}<option value="${esc(pl.path)}" ${pl === cur ? 'selected' : ''}>${esc(pl.label)}</option>`).join('');
  $('store-list').innerHTML = b.dirs.length
    ? b.dirs.map((d) => `<button type="button" class="dir" data-name="${esc(d)}">${esc(d)}</button>`).join('')
    : `<div class="note pad">${b.canUse === false ? 'No shares found on this server' : 'No subfolders'}</div>`;
  $('store-list').querySelectorAll('.dir').forEach((el) => el.addEventListener('click', () => {
    const sep = b.path.includes('\\') ? '\\' : '/';
    browseTo(b.path.endsWith(sep) ? b.path + el.dataset.name : b.path + sep + el.dataset.name);
  }));
  $('store-here').textContent = b.canUse === false ? 'Pick a share on this server.'
    : b.hasHistory ? 'This folder already has recorded races.' : 'No races here yet. New sessions will be saved here.';
  $('store-use').disabled = b.canUse === false || b.path === ST.info.dir;
}

async function useFolder() {
  const dir = ST.browse?.path;
  if (!dir) return;
  try {
    const r = await fetch(`/api/storage${QS}`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ dir }) });
    const body = await r.json();
    if (!r.ok) throw new Error(body.detail || r.status);
    ST.info = { ...ST.info, ...body };
  } catch (e) {
    $('store-msg').textContent = `Couldn't use that folder: ${e.message}`;
    return;
  }
  renderStorageInfo();
  $('store-use').disabled = true;
  $('store-msg').textContent = 'Saved. New sessions are recorded here from now on.';
  S.sessions = await api('/api/sessions');
  S.detail = null; S.sid = null; S.sel = []; S.cache.clear();
  renderSessions();
  const pick = S.sessions.find((s) => s.laps > 0) || S.sessions[0];
  if (pick) openSession(pick.id);
  else { renderHeader(); renderLapTable(); renderLapSummary(); redrawAll(); }
}

$('btn-store').addEventListener('click', openStorage);
$('store-up').addEventListener('click', () => ST.browse?.parent && browseTo(ST.browse.parent));
$('store-drives').addEventListener('change', (e) => { if (e.target.value) browseTo(e.target.value); });
$('store-go').addEventListener('click', () => browseTo($('store-path').value.trim()));
$('store-path').addEventListener('keydown', (e) => { if (e.key === 'Enter') { e.preventDefault(); browseTo(e.target.value.trim()); } });
$('store-use').addEventListener('click', useFolder);

init();
