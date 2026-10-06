'use strict';

// ---------- helpers ----------
const $ = (id) => document.getElementById(id);
const isNum = (v) => typeof v === 'number' && Number.isFinite(v);
const pad = (n) => String(n).padStart(2, '0');
const clamp = (v, a, b) => Math.min(b, Math.max(a, v));
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

function txt(id, v) {
  const el = typeof id === 'string' ? $(id) : id;
  if (el && el.textContent !== v) el.textContent = v;
}
function cls(id, name, on) {
  const el = typeof id === 'string' ? $(id) : id;
  if (el) el.classList.toggle(name, !!on);
}

const store = {
  get(k, d) { try { const v = localStorage.getItem(k); return v == null ? d : JSON.parse(v); } catch { return d; } },
  set(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch { /* storage unavailable */ } },
};
const prefs = Object.assign({ speed: 'kmh', temp: 'c', fuel: 'l', press: 'kpa', delta: 'best', tab: 'dash' }, store.get('prefs', {}));

// ---------- units & formatting ----------
const U = {
  speed: (ms) => (prefs.speed === 'mph' ? ms * 2.23694 : ms * 3.6),
  speedLbl: () => (prefs.speed === 'mph' ? 'mph' : 'km/h'),
  temp: (c) => (prefs.temp === 'f' ? c * 9 / 5 + 32 : c),
  tempLbl: () => (prefs.temp === 'f' ? '°F' : '°C'),
  fuel: (l) => (prefs.fuel === 'gal' ? l / 3.78541 : l),
  fuelLbl: () => (prefs.fuel === 'gal' ? 'gal' : 'L'),
  press: (k) => (prefs.press === 'psi' ? k * 0.145038 : prefs.press === 'bar' ? k / 100 : k),
  pressDp: () => (prefs.press === 'bar' ? 2 : prefs.press === 'psi' ? 1 : 0),
};
const fmtTemp = (c, d = 0) => (isNum(c) ? `${U.temp(c).toFixed(d)}${U.tempLbl()}` : '–');
const fmtFuel = (l, d = 2) => (isNum(l) ? `${U.fuel(l).toFixed(d)} ${U.fuelLbl()}` : '–');
const fmtN = (v, d = 1) => (isNum(v) ? v.toFixed(d) : '–');

function fmtLap(s) {
  if (!isNum(s) || s <= 0) return '-:--.---';
  const m = Math.floor(s / 60);
  return `${m}:${(s - m * 60).toFixed(3).padStart(6, '0')}`;
}
function fmtClock(s) {
  if (!isNum(s) || s < 0) return '--:--';
  s = Math.floor(s);
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), x = s % 60;
  return h ? `${h}:${pad(m)}:${pad(x)}` : `${m}:${pad(x)}`;
}
function fmtSigned(v, d = 3) {
  if (!isNum(v)) return '–';
  const a = Math.abs(v).toFixed(d);
  return Number(a) === 0 ? `±${a}` : `${v > 0 ? '+' : '−'}${a}`;
}
function tempColor(c) {
  if (!isNum(c)) return '#20252d';
  const h = c < 85 ? 210 - clamp((c - 50) / 35, 0, 1) * 70 : 140 - clamp((c - 85) / 25, 0, 1) * 140;
  return `hsl(${h} 55% 33%)`;
}

const STATES = ['', 'Get in car', 'Warmup', 'Parade laps', 'Racing', 'Checkered', 'Cool down'];
const WETNESS = ['–', 'Dry', 'Mostly dry', 'Very lightly wet', 'Lightly wet', 'Moderately wet', 'Very wet', 'Extremely wet'];
// [mask, label, theme] in priority order
const FLAGS = [
  [0x10000 | 0x20000, 'BLACK', 'black'],
  [0x100000, 'MEATBALL', 'meatball'],
  [0x10, 'RED', 'red'],
  [0x1, 'CHECKERED', 'checkered'],
  [0x4000 | 0x8000 | 0x8 | 0x100, 'YELLOW', 'yellow'],
  [0x20, 'BLUE', 'blue'],
  [0x2, 'WHITE', 'white'],
  [0x40, 'DEBRIS', 'debris'],
  [0x4 | 0x400, 'GREEN', 'green'],
];
const WARN = [[0x1, 'Water temp'], [0x2, 'Fuel pressure'], [0x4, 'Oil pressure'], [0x40, 'Oil temp']];

// ---------- state ----------
const S = { fast: null, slow: null, map: null, sock: 'down', lastFast: 0, dirty: false };
const PARAMS = new URLSearchParams(location.search);
const REPLAY = PARAMS.get('replay');
const TR = { n: 400, i: 0, count: 0, thr: new Float32Array(400), brk: new Float32Array(400), spd: new Float32Array(400) };

// ---------- websocket ----------
function connect() {
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  const ws = new WebSocket(`${proto}://${location.host}/ws${location.search}`);
  ws.onopen = () => { S.sock = 'up'; updateConn(); };
  ws.onmessage = (e) => {
    const m = JSON.parse(e.data);
    if (m.t === 'fast') {
      S.fast = m;
      S.lastFast = performance.now();
      if (m.connected) pushTrace(m);
      S.dirty = true;
    } else if (m.t === 'slow') {
      S.slow = m;
      renderSlow();
    } else if (m.t === 'map') {
      S.map = m;
      S.dirty = true;
    }
    updateConn();
  };
  ws.onclose = () => { S.sock = 'down'; updateConn(); setTimeout(connect, 1000); };
}

function updateConn() {
  const live = S.sock === 'up' && S.fast?.connected && performance.now() - S.lastFast < 3000;
  const state = REPLAY ? 'live' : S.sock !== 'up' ? 'down' : live ? 'live' : 'wait';
  if (document.body.dataset.conn !== state) {
    document.body.dataset.conn = state;
    if (state === 'live') renderSlow();  // restore the header text that the waiting state replaced
  }
  if (state === 'down') {
    txt('flag', 'Offline'); document.body.dataset.flag = '';
    txt('track', 'Can’t reach the dashboard server');
    $('sess').innerHTML = '<span>Check that server.py is running on the sim PC</span>';
  } else if (state === 'wait') {
    txt('flag', 'Standby'); document.body.dataset.flag = '';
    txt('track', 'Waiting for iRacing');
    $('sess').innerHTML = '<span>Data appears when you get in the car</span>';
  }
}
setInterval(updateConn, 1000);

// ---------- fast render (rAF) ----------
const leds = $('leds');
for (let i = 0; i < 12; i++) {
  const s = document.createElement('span');
  s.className = i < 4 ? 'g' : i < 8 ? 'y' : 'r';
  leds.appendChild(s);
}

function flagOf(bits) {
  if (!isNum(bits)) return null;
  for (const [mask, label, theme] of FLAGS) if (bits & mask) return { label, theme };
  return null;
}

function renderFast() {
  const f = S.fast;
  if (!f || !f.connected) return;
  const car = S.slow?.car || {};

  txt('speed', String(Math.round(U.speed(f.speed || 0))));
  txt('speed-u', U.speedLbl());
  txt('gear', f.gear === -1 ? 'R' : f.gear === 0 || f.gear == null ? 'N' : String(f.gear));
  txt('rpm', `${Math.round(f.rpm || 0)} rpm`);

  // shift lights
  const first = car.slFirst || (car.redline ? car.redline * 0.8 : 6000);
  const shift = car.slShift || (car.redline ? car.redline * 0.95 : 7500);
  const blink = car.slBlink || shift;
  const lit = clamp(Math.ceil(((f.rpm || 0) - first) / Math.max(1, shift - first) * 12), 0, 12);
  [...leds.children].forEach((el, i) => cls(el, 'on', i < lit));
  cls(leds, 'blink', (f.rpm || 0) >= blink && f.gear > 0);

  // position
  const total = S.slow?.standings?.length || 0;
  txt('pos', f.pos > 0 ? `P${f.pos}` : '–');
  const multi = new Set((S.slow?.standings || []).map((r) => r.cls)).size > 1;
  txt('pos-of', f.pos > 0 ? `of ${total}${multi && f.cpos > 0 ? `, P${f.cpos} in class` : ''}` : 'position');

  // delta
  const useSession = prefs.delta === 'session';
  const d = useSession ? f.sdelta : f.delta;
  const ok = useSession ? f.sdeltaOk : f.deltaOk;
  const bar = $('delta-bar');
  if (ok && isNum(d)) {
    const w = clamp(Math.abs(d), 0, 1) * 50;
    bar.style.width = `${w}%`;
    bar.style.left = d < 0 ? `${50 - w}%` : '50%';
    cls(bar, 'slow', d > 0);
    txt('delta', fmtSigned(d));
    cls('delta', 'fast', d < 0); cls('delta', 'slow', d > 0);
  } else {
    bar.style.width = '0';
    txt('delta', '±0.000');
    cls('delta', 'fast', false); cls('delta', 'slow', false);
  }

  txt('t-cur', fmtLap(f.cur));
  txt('t-last', fmtLap(f.last));
  txt('t-best', fmtLap(f.best));
  const fastest = Math.min(...(S.slow?.standings || []).map((r) => r.best || Infinity));
  cls('t-best', 'ob', isNum(f.best) && f.best <= fastest);
  txt('t-lap', isNum(f.lap) && f.lap > 0 ? String(f.lap) : '–');

  for (const [k, v] of [['thr', f.thr], ['brk', f.brk], ['clu', f.clu]]) {
    const p = Math.round(clamp(v || 0, 0, 1) * 100);
    $(`pb-${k}`).style.width = `${p}%`;
    txt(`pv-${k}`, String(p));
  }

  // badges
  const warn = f.warn || 0;
  const badges = [];
  if (warn & 0x10) badges.push('<span class="badge info">Pit limiter</span>');
  if (f.pit) badges.push('<span class="badge pit">Pit lane</span>');
  if (warn & 0x8) badges.push('<span class="badge">Engine off</span>');  // low pressures are expected then
  else for (const [m, l] of WARN) if (warn & m) badges.push(`<span class="badge warn">${l}</span>`);
  if (f.replay) badges.push('<span class="badge">Replay</span>');
  const html = badges.join('');
  if ($('badges').innerHTML !== html) $('badges').innerHTML = html;
  document.body.dataset.limiter = warn & 0x10 ? '1' : '0';

  // flag + remaining
  const flag = flagOf(f.flags);
  const theme = flag?.theme || '';
  if (document.body.dataset.flag !== theme) document.body.dataset.flag = theme;
  const flagName = flag ? flag.label[0] + flag.label.slice(1).toLowerCase() : null;
  txt('flag', flagName || 'Live');
  txt('s-flag', flagName || 'None');

  const tRem = isNum(f.tRem) && f.tRem >= 0 && f.tRem < 86400 * 3 ? f.tRem : null;
  const lRem = isNum(f.lRem) && f.lRem >= 0 && f.lRem < 32767 ? f.lRem : null;
  txt('h-rem', [tRem != null ? fmtClock(tRem) : null, lRem != null ? `${lRem} laps` : null].filter(Boolean).join(', ') || '--:--');

  // fuel level is live in the fast frame
  if (isNum(f.fuel)) {
    txt('f-level', U.fuel(f.fuel).toFixed(1));
    const avg = S.slow?.fuel?.avg;
    const lapsLeft = avg ? f.fuel / avg : null;
    const g = $('f-gauge');
    g.style.width = `${clamp((f.fuelPct || 0) * 100, 0, 100)}%`;
    cls(g, 'crit', lapsLeft != null && lapsLeft < 1.5);
    cls(g, 'low', lapsLeft != null && lapsLeft >= 1.5 && lapsLeft < 4);
  }
}

// ---------- slow render ----------
function renderSlow() {
  const s = S.slow;
  if (!s || !s.connected) return;
  txt('track', [s.track?.name, s.track?.config].filter(Boolean).join(', ') || 'iRacing');
  const sess = [s.session?.name || s.session?.type, s.car?.name].filter(Boolean).map((v) => `<span>${esc(v)}</span>`);
  if (s.source === 'mock') sess.push('<span class="sim">Simulated data</span>');
  if (REPLAY) sess.push('<span class="sim">Replay</span>');
  const sessHtml = sess.join('');
  if ($('sess').innerHTML !== sessHtml) $('sess').innerHTML = sessHtml;
  renderFuel(s);
  renderSession(s);
  renderSystems(s);
  renderTyres(s);
  renderRelative(s);
  renderStandings(s);
  renderLaps(s);
  txt('map-note', s.mapRec != null ? (s.mapRec > 0 ? `Drawing outline, ${Math.round(s.mapRec * 100)}%` : 'Drive one clean lap to draw the outline') : '');
}

function renderFuel(s) {
  const f = s.fuel || {};
  txt('f-unit', U.fuelLbl());
  txt('f-samples', f.samples ? `Based on ${f.samples} lap${f.samples > 1 ? 's' : ''}` : 'Needs one clean lap');
  txt('f-avg', fmtFuel(f.avg));
  txt('f-last', fmtFuel(f.last));
  txt('f-laps', fmtN(f.lapsInTank, 1));
  txt('f-togo', fmtN(f.lapsToGo, 1));
  txt('f-finish', fmtFuel(f.toFinish, 1));
  txt('f-add', isNum(f.toAdd) ? (f.toAdd > 0 ? fmtFuel(f.toAdd, 1) : 'none') : '–');
  txt('f-stops', isNum(f.stops) ? String(f.stops) : '–');
  cls('f-laps', 'bad', isNum(f.lapsInTank) && isNum(f.lapsToGo) && f.lapsInTank < f.lapsToGo);
  cls('f-add', 'warn-t', f.toAdd > 0);
  const p = s.pitSv || {};
  txt('f-pit', p.flags & 0x10 ? `+${fmtFuel(p.fuel, 1)}` : isNum(p.flags) ? 'off' : '–');
}

function renderSession(s) {
  txt('s-state', STATES[s.session?.state] || '–');
  const inc = s.inc || {};
  txt('s-inc', isNum(inc.me) ? `${inc.me}x${inc.limit ? ` / ${inc.limit}` : ''}` : '–');
  cls('s-inc', 'bad', inc.limit && inc.me >= inc.limit * 0.75);
  const st = s.stint;
  txt('s-stint', st ? `${st.laps ?? '–'} ${st.laps === 1 ? 'lap' : 'laps'}, ${fmtClock(st.time)}` : '–');
  const e = s.env || {};
  txt('s-air', fmtTemp(e.air, 1));
  txt('s-trk', fmtTemp(e.track, 1));
  txt('s-wet', WETNESS[e.wet] || '–');
  txt('s-wind', isNum(e.wind) ? `${U.speed(e.wind).toFixed(0)} ${U.speedLbl()}` : '–');
}

function renderSystems(s) {
  const y = s.sys || {};
  txt('y-water', fmtTemp(y.water));
  txt('y-oil', fmtTemp(y.oil));
  txt('y-oilp', isNum(y.oilP) ? `${y.oilP.toFixed(1)} bar` : '–');
  txt('y-fuelp', isNum(y.fuelP) ? `${y.fuelP.toFixed(1)} bar` : '–');
  txt('y-volt', isNum(y.volt) ? `${y.volt.toFixed(1)} V` : '–');
  txt('y-bias', isNum(y.bias) ? `${y.bias.toFixed(1)}%` : '–');
  txt('y-abs', fmtN(y.abs, 0));
  txt('y-tc', [y.tc, y.tc2].filter(isNum).map((v) => v.toFixed(0)).join(' / ') || '–');
  const p = s.pitSv || {};
  txt('y-rep', isNum(p.repair) ? `${fmtClock(p.repair)} / ${fmtClock(p.optRepair)}` : '–');
  cls('y-rep', 'warn-t', p.repair > 0);
}

const TYRE_NAMES = { LF: 'Left front', RF: 'Right front', LR: 'Left rear', RR: 'Right rear' };
function renderTyres(s) {
  const ty = s.tyres || {};
  const pressLbl = { kpa: 'kPa', psi: 'psi', bar: 'bar' }[prefs.press];
  document.querySelectorAll('.tyre').forEach((el) => {
    const c = el.dataset.c, t = ty[c] || {};
    const temps = t.t || [], wear = (t.w || []).filter(isNum);
    const w = wear.length ? Math.min(...wear) : null;
    el.innerHTML =
      `<div class="tn"><b>${TYRE_NAMES[c]}</b><span>${isNum(w) ? `${Math.round(w * 100)}% tread` : ''}</span></div>` +
      `<div class="tt">${[0, 1, 2].map((k) => `<span style="background:${tempColor(temps[k])}">${isNum(temps[k]) ? U.temp(temps[k]).toFixed(0) : '–'}</span>`).join('')}</div>` +
      `<div class="tm"><span>${isNum(t.p) ? `${U.press(t.p).toFixed(U.pressDp())} ${pressLbl}` : ''}</span><span>${isNum(t.cp) ? `Cold ${U.press(t.cp).toFixed(U.pressDp())}` : ''}</span></div>`;
  });
  const f = s.pitSv?.flags;
  if (isNum(f)) {
    const tyres = ['LF', 'RF', 'LR', 'RR'].filter((_, i) => f & (1 << i));
    const parts = [tyres.length === 4 ? 'all 4 tyres' : tyres.length ? tyres.join(' ') : 'no tyres'];
    if (f & 0x20) parts.push('tear-off');
    if (f & 0x40) parts.push('fast repair');
    txt('ty-pit', `Next stop: ${parts.join(', ')}`);
  }
}

function numTag(r) {
  return `<span class="carno" style="--cc:${esc(r.clsColor)}">${esc(r.num)}</span>`;
}
function fmtIr(r) {
  return r.ir ? `${(r.ir / 1000).toFixed(1)}k` : '';
}

function renderRelative(s) {
  const rows = s.relative || [];
  const body = $('rel').tBodies[0];
  if (!rows.length) { body.innerHTML = '<tr><td class="empty" colspan="4">No cars on track yet</td></tr>'; return; }
  body.innerHTML = rows.map((r) => {
    const me = r.idx === s.me;
    const c = [me ? 'me' : '', r.pit ? 'pit' : '', r.lap > 0 ? 'ahead' : r.lap < 0 ? 'behind' : ''].join(' ');
    return `<tr class="${c}"><td class="p">${r.pos > 0 ? r.pos : ''}</td>` +
      `<td class="name">${numTag(r)}${esc(r.name)}${r.pit ? '<span class="tag pit">Pit</span>' : ''}</td>` +
      `<td class="dim">${fmtIr(r)}</td><td class="gap">${me ? '' : Math.abs(r.gap).toFixed(1)}</td></tr>`;
  }).join('');
}

function renderStandings(s) {
  const rows = s.standings || [];
  txt('st-count', rows.length ? `${rows.length} cars` : '');
  const fastest = Math.min(...rows.map((r) => r.best || Infinity));
  const race = (s.session?.type || '').toLowerCase().startsWith('race');
  const body = $('stand').tBodies[0];
  if (!rows.length) { body.innerHTML = '<tr><td class="empty" colspan="6">Standings appear once cars set a lap</td></tr>'; return; }
  body.innerHTML = rows.map((r) => {
    let gap = '';
    if (race && r.pos === 1) gap = 'Leader';
    else if (r.down) gap = `+${r.down}L`;
    else if (isNum(r.gap)) gap = `+${r.gap.toFixed(race ? 1 : 3)}`;
    const c = [r.idx === s.me ? 'me' : '', r.pit || r.out ? 'pit' : ''].join(' ');
    return `<tr class="${c}"><td class="p">${r.pos > 0 ? r.pos : '–'}</td>` +
      `<td class="name">${numTag(r)}${esc(r.name)}${r.pit ? '<span class="tag pit">Pit</span>' : r.out ? '<span class="tag">Garage</span>' : ''}</td>` +
      `<td>${gap}</td><td class="hide-s">${isNum(r.int) ? r.int.toFixed(1) : ''}</td>` +
      `<td>${fmtLap(r.last)}</td><td class="hide-s"><span class="${r.best && r.best === fastest ? 'ob' : ''}">${fmtLap(r.best)}</span></td></tr>`;
  }).join('');
}

function renderLaps(s) {
  const laps = (s.laps || []).slice().reverse();
  const body = $('laps').tBodies[0];
  if (!laps.length) { body.innerHTML = '<tr><td class="empty" colspan="5">Your completed laps will be listed here</td></tr>'; return; }
  const best = Math.min(...laps.map((l) => l.time || Infinity));
  body.innerHTML = laps.map((l) => {
    const isBest = l.time && l.time === best;
    return `<tr><td>${l.lap}</td><td><span class="${isBest ? 'pb' : ''}">${fmtLap(l.time)}</span></td>` +
      `<td>${l.time && isFinite(best) && !isBest ? fmtSigned(l.time - best) : ''}</td>` +
      `<td>${isNum(l.fuel) ? U.fuel(l.fuel).toFixed(2) : '–'}</td><td>${l.pit ? '<span class="tag pit">Pit</span>' : ''}</td></tr>`;
  }).join('');
}

// ---------- canvases ----------
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

function pushTrace(f) {
  TR.thr[TR.i] = f.thr || 0;
  TR.brk[TR.i] = f.brk || 0;
  TR.spd[TR.i] = f.speed || 0;
  TR.i = (TR.i + 1) % TR.n;
  TR.count = Math.min(TR.n, TR.count + 1);
}

function drawTrace() {
  const fit = fitCanvas($('trace'));
  if (!fit) return;
  const [ctx, w, h] = fit;
  ctx.clearRect(0, 0, w, h);
  ctx.strokeStyle = '#2c3138';
  ctx.lineWidth = 1;
  for (const y of [0.25, 0.5, 0.75]) { ctx.beginPath(); ctx.moveTo(0, h * y); ctx.lineTo(w, h * y); ctx.stroke(); }
  if (!TR.count) return;
  let vmax = 30;
  for (let k = 0; k < TR.count; k++) vmax = Math.max(vmax, TR.spd[(TR.i - TR.count + k + TR.n) % TR.n]);
  const line = (arr, color, scale, width) => {
    ctx.strokeStyle = color; ctx.lineWidth = width; ctx.lineJoin = 'round';
    ctx.beginPath();
    for (let k = 0; k < TR.count; k++) {
      const v = arr[(TR.i - TR.count + k + TR.n) % TR.n] / scale;
      const x = w - (TR.count - 1 - k) * (w / (TR.n - 1));
      const y = h - 2 - v * (h - 4);
      k ? ctx.lineTo(x, y) : ctx.moveTo(x, y);
    }
    ctx.stroke();
  };
  line(TR.spd, '#cfd3da', vmax * 1.05, 1.5);
  line(TR.thr, '#2fd17a', 1, 2);
  line(TR.brk, '#ff4d4f', 1, 2);
}

function mapPoint(pts, pct) {
  const n = pts.length, u = (((pct % 1) + 1) % 1) * n;
  const i = Math.floor(u) % n, j = (i + 1) % n, f = u - Math.floor(u);
  return [pts[i][0] + (pts[j][0] - pts[i][0]) * f, pts[i][1] + (pts[j][1] - pts[i][1]) * f];
}
const CIRCLE = Array.from({ length: 200 }, (_, k) => {
  const a = (k / 200) * Math.PI * 2 - Math.PI / 2;
  return [Math.cos(a) * 0.9, -Math.sin(a) * 0.9];
});

function drawMap() {
  const fit = fitCanvas($('map'));
  if (!fit) return;
  const [ctx, w, h] = fit;
  ctx.clearRect(0, 0, w, h);
  const real = S.map?.points?.length > 10;
  const pts = real ? S.map.points : CIRCLE;
  const R = Math.min(w, h) / 2 - 18;
  const P = (p) => [w / 2 + p[0] * R, h / 2 - p[1] * R];

  ctx.lineJoin = 'round'; ctx.lineCap = 'round';
  const path = () => { ctx.beginPath(); pts.forEach((p, k) => { const [x, y] = P(p); k ? ctx.lineTo(x, y) : ctx.moveTo(x, y); }); ctx.closePath(); };
  path(); ctx.strokeStyle = real ? '#353a43' : '#2a2e35'; ctx.lineWidth = 16; ctx.stroke();
  path(); ctx.strokeStyle = real ? '#4a515c' : '#353a43'; ctx.lineWidth = 1; ctx.setLineDash(real ? [] : [4, 6]); ctx.stroke(); ctx.setLineDash([]);

  // start/finish
  const [sx, sy] = P(mapPoint(pts, 0));
  const [nx, ny] = P(mapPoint(pts, 0.004));
  const ang = Math.atan2(ny - sy, nx - sx) + Math.PI / 2;
  ctx.strokeStyle = '#fff'; ctx.lineWidth = 3;
  ctx.beginPath(); ctx.moveTo(sx - Math.cos(ang) * 10, sy - Math.sin(ang) * 10); ctx.lineTo(sx + Math.cos(ang) * 10, sy + Math.sin(ang) * 10); ctx.stroke();

  const f = S.fast;
  if (!f?.connected) return;
  const info = new Map((S.slow?.standings || []).map((r) => [r.idx, r]));
  const cars = (f.cars || []).slice().sort((a, b) => (a[0] === f.me) - (b[0] === f.me));
  ctx.font = '700 10px Inter, system-ui, sans-serif';
  ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
  for (const [idx, pct, pit, pace] of cars) {
    const [x, y] = P(mapPoint(pts, pct));
    const me = idx === f.me, r = info.get(idx);
    const rad = me ? 11 : 8;
    ctx.globalAlpha = pit ? 0.4 : 1;
    ctx.beginPath(); ctx.arc(x, y, rad, 0, Math.PI * 2);
    ctx.fillStyle = pace ? '#ffffff' : me ? '#f2a93b' : r?.clsColor || '#8a919c';
    ctx.fill();
    if (me) { ctx.lineWidth = 3; ctx.strokeStyle = '#fff'; ctx.stroke(); }
    ctx.fillStyle = '#000';
    ctx.fillText(pace ? 'SC' : r?.pos > 0 ? String(r.pos) : '', x, y + 0.5);
  }
  ctx.globalAlpha = 1;
}

function frame() {
  if (S.dirty) {
    S.dirty = false;
    renderFast();
    drawTrace();
    drawMap();
  }
  requestAnimationFrame(frame);
}

// ---------- UI wiring ----------
function setTab(t) {
  prefs.tab = t;
  document.body.dataset.tab = t;
  store.set('prefs', prefs);
  S.dirty = true;
}
document.querySelectorAll('#tabs [data-go]').forEach((b) => b.addEventListener('click', () => setTab(b.dataset.go)));
setTab(prefs.tab);

function showSettings(open) {
  $('settings').hidden = !open;
  $('btn-set').setAttribute('aria-expanded', String(open));
}
$('btn-set').addEventListener('click', (e) => { e.stopPropagation(); showSettings($('settings').hidden); });
document.addEventListener('click', (e) => { if (!$('settings').contains(e.target)) showSettings(false); });
document.addEventListener('keydown', (e) => { if (e.key === 'Escape') showSettings(false); });
document.querySelectorAll('[data-pref]').forEach((sel) => {
  sel.value = prefs[sel.dataset.pref];
  sel.addEventListener('change', () => {
    prefs[sel.dataset.pref] = sel.value;
    store.set('prefs', prefs);
    renderSlow();
    S.dirty = true;
  });
});

$('nav-hist').href = `history.html${location.search}`;

$('btn-fs').addEventListener('click', () => {
  if (document.fullscreenElement) document.exitFullscreen?.();
  else document.documentElement.requestFullscreen?.().catch(() => {});
});

// keep the phone screen awake (only works on https / localhost; otherwise set auto-lock off)
let wakeLock = null;
async function keepAwake() {
  try { if (navigator.wakeLock && !wakeLock) { wakeLock = await navigator.wakeLock.request('screen'); wakeLock.addEventListener('release', () => { wakeLock = null; }); } } catch { /* not allowed */ }
}
document.addEventListener('click', keepAwake);
document.addEventListener('visibilitychange', () => { if (document.visibilityState === 'visible') keepAwake(); });

window.addEventListener('resize', () => { S.dirty = true; });

// ---------- replay of a recorded session ----------
// Plays stored broadcast frames through the same render path the WebSocket uses.
const R = {
  sid: Number(REPLAY), tl: null, t: 0, playing: false, speed: 1, last: 0, loading: null, drag: false,
  fastT: [], fastF: [], slowT: [], slowF: [], lo: Infinity, hi: -Infinity, fi: -1, sj: -1,
};

function withToken(url) {
  const tok = PARAMS.get('token');
  return tok ? `${url}${url.includes('?') ? '&' : '?'}token=${encodeURIComponent(tok)}` : url;
}
async function getJSON(url) {
  const r = await fetch(withToken(url));
  if (!r.ok) {
    throw new Error(r.status === 404
      ? 'This session has no replay data. Sessions recorded before replay was added can still be analysed in History.'
      : `Server answered ${r.status}`);
  }
  return r.json();
}
function lastAtOrBefore(T, t) {
  let lo = 0, hi = T.length - 1;
  if (!T.length || t < T[0]) return -1;
  while (lo < hi) {
    const mid = (lo + hi + 1) >> 1;
    if (T[mid] <= t) lo = mid; else hi = mid - 1;
  }
  return lo;
}

async function loadFrames(a, b, replace) {
  const data = await getJSON(`/api/sessions/${R.sid}/frames?a=${a.toFixed(2)}&b=${b.toFixed(2)}`);
  if (replace) { R.fastT = []; R.fastF = []; R.slowT = []; R.slowF = []; R.fi = -1; R.sj = -1; }
  for (const [kind, T, F] of [['fast', R.fastT, R.fastF], ['slow', R.slowT, R.slowF]]) {
    for (const [t, f] of data[kind]) {
      if (T.length && t <= T[T.length - 1]) continue;
      T.push(t); F.push(f);
    }
  }
  R.lo = R.fastT.length ? R.fastT[0] : a;
  R.hi = R.fastT.length ? R.fastT[R.fastT.length - 1] : b;
}

function trimFrames() {
  // keep about a minute behind the playhead so long sessions don't grow memory
  for (const [T, F, key] of [[R.fastT, R.fastF, 'fi'], [R.slowT, R.slowF, 'sj']]) {
    const cut = lastAtOrBefore(T, R.t - 60);
    if (cut > 200) { T.splice(0, cut); F.splice(0, cut); R[key] = Math.max(-1, R[key] - cut); }
  }
  if (R.fastT.length) R.lo = R.fastT[0];
}

function applyFast(i) {
  if (i < 0) return;
  R.fi = i;
  S.fast = R.fastF[i];
  S.lastFast = performance.now();
  S.dirty = true;
}
function applySlow(j) {
  if (j < 0 || j === R.sj) return;
  R.sj = j;
  S.slow = R.slowF[j];
  renderSlow();
}
function rebuildAt(t) {
  TR.i = 0; TR.count = 0;
  const i = lastAtOrBefore(R.fastT, t);
  for (let k = Math.max(0, i - TR.n + 1); k <= i; k++) if (R.fastF[k].connected) pushTrace(R.fastF[k]);
  applyFast(i);
  R.sj = -1;
  applySlow(lastAtOrBefore(R.slowT, t));
}

async function seek(t) {
  if (!R.tl) return;
  t = clamp(t, R.tl.t0, R.tl.t1);
  R.t = t;
  if (!(t >= R.lo && t <= R.hi)) {
    R.loading = loadFrames(t - 25, t + 40, true);
    try { await R.loading; } catch (e) { console.error(e); } finally { R.loading = null; }
  }
  rebuildAt(R.t);
  updateBar();
}

function ensureAhead() {
  if (R.loading || !R.tl || R.hi >= R.tl.t1 - 0.01) return;
  if (R.t + Math.max(15, R.speed * 8) < R.hi) return;
  const a = R.hi, b = R.hi + Math.max(40, R.speed * 20);
  R.loading = loadFrames(a, b, false).catch((e) => console.error(e)).finally(() => { R.loading = null; trimFrames(); });
}

function replayTick(now) {
  const dt = R.last ? Math.min(0.25, (now - R.last) / 1000) : 0;
  R.last = now;
  if (R.playing && !R.drag && R.tl) {
    // waits at the edge of what's loaded while the next block arrives
    R.t = Math.min(R.t + dt * R.speed, R.tl.t1, Math.max(R.hi, R.t));
    if (R.t >= R.tl.t1) setPlaying(false);
    const i = lastAtOrBefore(R.fastT, R.t);
    if (i > R.fi) {
      if (i - R.fi > TR.n) rebuildAt(R.t);
      else {
        for (let k = R.fi + 1; k <= i; k++) if (R.fastF[k].connected) pushTrace(R.fastF[k]);
        applyFast(i);
      }
    }
    applySlow(lastAtOrBefore(R.slowT, R.t));
  }
  ensureAhead();
  updateBar();
  requestAnimationFrame(replayTick);
}

function setPlaying(on) {
  if (on && R.tl && R.t >= R.tl.t1) seek(R.tl.t0);
  R.playing = on;
  const b = $('rb-play');
  b.textContent = on ? 'Pause' : 'Play';
  b.setAttribute('aria-pressed', String(on));
}

function lapStarts() { return (R.tl?.laps || []).map((l) => l.t).filter(isNum); }
function prevLap() { const s = lapStarts().filter((t) => t < R.t - 2); seek(s.length ? s[s.length - 1] : R.tl.t0); }
function nextLap() { const s = lapStarts().find((t) => t > R.t + 0.5); if (s != null) seek(s); }

function buildTimeline() {
  const { t0, t1, laps, events } = R.tl;
  const pos = (t) => `${clamp((t - t0) / ((t1 - t0) || 1) * 100, 0, 100)}%`;
  let html = '';
  for (const l of laps) if (isNum(l.t)) html += `<i class="mk lap" style="left:${pos(l.t)}"></i>`;
  for (const e of events) {
    const cls = e.kind === 'incident' ? 'inc' : e.kind.startsWith('pit') ? 'pit' : e.kind === 'flag' && e.data?.flag === 'yellow' ? 'yel' : null;
    if (cls) html += `<i class="mk ${cls}" style="left:${pos(e.t)}"></i>`;
  }
  $('rb-marks').innerHTML = html;
}

function updateBar() {
  if (!R.tl) return;
  const { t0, t1 } = R.tl;
  const f = clamp((R.t - t0) / ((t1 - t0) || 1), 0, 1);
  $('rb-fill').style.width = `${f * 100}%`;
  $('rb-head').style.left = `${f * 100}%`;
  const lap = S.fast?.lap > 0 ? `Lap ${S.fast.lap}    ` : '';
  txt('rb-time', `${lap}${fmtClock(R.t - t0)} of ${fmtClock(t1 - t0)}${R.loading ? '    Loading' : ''}`);
}

function bindReplayControls() {
  $('rb-play').addEventListener('click', () => setPlaying(!R.playing));
  $('rb-back').addEventListener('click', () => seek(R.t - 10));
  $('rb-fwd').addEventListener('click', () => seek(R.t + 10));
  $('rb-prev').addEventListener('click', prevLap);
  $('rb-next').addEventListener('click', nextLap);
  $('rb-speed').addEventListener('change', (e) => { R.speed = Number(e.target.value); });
  const tl = $('rb-tl');
  const tAt = (e) => { const r = tl.getBoundingClientRect(); return R.tl.t0 + clamp((e.clientX - r.left) / r.width, 0, 1) * (R.tl.t1 - R.tl.t0); };
  tl.addEventListener('pointerdown', (e) => { if (!R.tl) return; R.drag = true; tl.setPointerCapture(e.pointerId); R.t = tAt(e); updateBar(); });
  tl.addEventListener('pointermove', (e) => { if (R.drag) { R.t = tAt(e); updateBar(); } });
  tl.addEventListener('pointerup', (e) => { if (!R.drag) return; R.drag = false; seek(tAt(e)); });
  tl.addEventListener('pointercancel', () => { R.drag = false; });
  document.addEventListener('keydown', (e) => {
    if (e.target.closest('select, input')) return;
    if (e.key === ' ') { e.preventDefault(); setPlaying(!R.playing); }
    else if (e.key === 'ArrowLeft') seek(R.t - (e.shiftKey ? 30 : 5));
    else if (e.key === 'ArrowRight') seek(R.t + (e.shiftKey ? 30 : 5));
  });
}

async function startReplay() {
  document.body.classList.add('replaying');
  $('replaybar').hidden = false;
  const tok = PARAMS.get('token');
  $('rb-exit').href = `history.html${tok ? `?token=${encodeURIComponent(tok)}` : ''}#${R.sid}`;
  $('nav-hist').href = $('rb-exit').href;
  S.sock = 'up';
  bindReplayControls();
  try {
    const [tl, detail] = await Promise.all([getJSON(`/api/sessions/${R.sid}/timeline`), getJSON(`/api/sessions/${R.sid}`)]);
    R.tl = tl;
    S.map = { points: detail.map };
  } catch (e) {
    txt('flag', 'Replay');
    txt('track', 'Can’t replay this session');
    $('sess').innerHTML = `<span>${esc(e.message)}</span>`;
    return;
  }
  buildTimeline();
  const start = Number(PARAMS.get('t'));
  await seek(start > 0 ? start : R.tl.t0);
  setPlaying(true);
  requestAnimationFrame(replayTick);
}

if (REPLAY) startReplay(); else connect();
requestAnimationFrame(frame);
