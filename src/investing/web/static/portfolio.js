/* /portfolio: a Zerodha Coin tradebook audit that runs in the browser.
 *
 * The CSV is read with the File API and never sent anywhere; requests fetch only public reference data.
 *
 * PE is the engine: FIFO lots, XIRR by bisection, and the "same rupees on the same days into another
 * fund" replay. Pure functions, no DOM, no fetch. Days are integers since 1970-01-01 UTC; money is
 * rupees; rates are fractions (0.12 = 12%).
 */
const PE = (() => {
  'use strict';
  const DAY = 86400000;

  // ---- dates -------------------------------------------------------------------------------------
  function ymd(y, mo, d) {   // a real calendar date, or null (2024-02-31 is not 2 March)
    const t = Date.UTC(y, mo - 1, d);
    if (!Number.isFinite(t)) return null;
    const dt = new Date(t);
    return dt.getUTCFullYear() === y && dt.getUTCMonth() === mo - 1 && dt.getUTCDate() === d ? Math.round(t / DAY) : null;
  }
  function dayNum(iso) {
    const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(String(iso || '').trim());
    return m ? ymd(+m[1], +m[2], +m[3]) : null;
  }
  function isoOf(day) { return new Date(day * DAY).toISOString().slice(0, 10); }
  // 'YYYY-MM-DD', 'YYYY-MM-DD HH:MM:SS', 'DD-MM-YYYY', 'DD/MM/YYYY', 'DD-Mon-YYYY' -> day number or null
  const MONTHS = { jan: 1, feb: 2, mar: 3, apr: 4, may: 5, jun: 6, jul: 7, aug: 8, sep: 9, oct: 10, nov: 11, dec: 12 };
  function parseDay(text) {
    const s = String(text || '').trim();
    let m;
    const TIME = '(?:[ T](?:[01]\\d|2[0-3]):[0-5]\\d(?::[0-5]\\d)?(?:\\.\\d+)?Z?)?$';
    if ((m = new RegExp('^(\\d{4})-(\\d{2})-(\\d{2})' + TIME).exec(s))) return ymd(+m[1], +m[2], +m[3]);
    if ((m = new RegExp('^(\\d{1,2})[-/](\\d{1,2})[-/](\\d{4})' + TIME).exec(s))) return ymd(+m[3], +m[2], +m[1]);
    if ((m = new RegExp('^(\\d{1,2})-([A-Za-z]{3})-(\\d{4})' + TIME).exec(s))) {
      const mo = MONTHS[m[2].toLowerCase()];
      return mo ? ymd(+m[3], mo, +m[1]) : null;
    }
    return null;
  }
  function yearsBetween(d0, d1) { return (d1 - d0) / 365.0; }
  // last calendar day of each month from `from` to `to` inclusive of the month containing `to`,
  // capped at `to` for the final point
  function monthEnds(from, to) {
    const out = [];
    if (!(from <= to)) return out;
    let d = new Date(from * DAY);
    let y = d.getUTCFullYear(), mo = d.getUTCMonth();
    for (;;) {
      const end = Date.UTC(y, mo + 1, 0) / DAY;  // day 0 of next month = last day of this one
      if (end >= to) { out.push(to); break; }
      out.push(end);
      mo += 1; if (mo === 12) { mo = 0; y += 1; }
    }
    return out;
  }

  // ---- CSV ---------------------------------------------------------------------------------------
  function parseCSV(text) {
    const rows = [];
    let row = [], field = '', quoted = false;
    const src = String(text).replace(/^﻿/, '');
    for (let i = 0; i < src.length; i++) {
      const c = src[i];
      if (quoted) {
        if (c === '"') {
          if (src[i + 1] === '"') { field += '"'; i++; } else quoted = false;
        } else field += c;
      } else if (c === '"') quoted = true;
      else if (c === ',') { row.push(field); field = ''; }
      else if (c === '\n' || c === '\r') {
        if (c === '\r' && src[i + 1] === '\n') i++;
        row.push(field); field = '';
        if (row.some(v => v.trim() !== '')) rows.push(row);
        row = [];
      } else field += c;
    }
    row.push(field);
    if (row.some(v => v.trim() !== '')) rows.push(row);
    return rows;
  }

  // Zerodha Console tradebook (segment: mutual funds). Columns as exported:
  // symbol, isin, trade_date, exchange, segment, series, trade_type, auction, quantity, price, trade_id,
  // order_id, order_execution_time. Only isin, trade_date, trade_type, quantity and price are required.
  const COLUMN_ALIASES = {
    isin: ['isin'], symbol: ['symbol', 'scheme', 'scheme_name', 'fund', 'name'],
    trade_date: ['trade_date', 'date', 'order_execution_time'], trade_type: ['trade_type', 'type', 'transaction_type'],
    quantity: ['quantity', 'qty', 'units'], price: ['price', 'nav', 'average_price', 'avg_price'],
    trade_id: ['trade_id', 'id', 'order_id'], exchange: ['exchange'],
  };
  let parseSeq = 0;
  function parseTradebook(text, fileName) {
    const name = fileName || 'tradebook';
    parseSeq++;
    const rows = parseCSV(text);
    if (rows.length < 2) return { trades: [], errors: [`${name}: no rows`], columns: [] };
    const header = rows[0].map(h => h.trim().toLowerCase().replace(/\s+/g, '_'));
    const col = {};
    for (const [key, names] of Object.entries(COLUMN_ALIASES)) {
      const i = names.map(n => header.indexOf(n)).find(i => i >= 0);
      if (i !== undefined) col[key] = i;
    }
    const missing = ['isin', 'trade_date', 'trade_type', 'quantity', 'price'].filter(k => col[k] === undefined);
    if (missing.length) {
      return { trades: [], columns: header,
        errors: [`${name}: not a Coin tradebook (missing ${missing.join(', ')}); columns found: ${header.join(', ')}`] };
    }
    const trades = [], errors = [];
    for (let r = 1; r < rows.length; r++) {
      const cells = rows[r];
      const get = k => (col[k] === undefined ? '' : (cells[col[k]] || '')).trim();
      const isin = get('isin').toUpperCase(), type = get('trade_type').toLowerCase();
      const day = parseDay(get('trade_date'));
      const num = v => /^\s*-?\d+(?:\.\d+)?(?:e-?\d+)?\s*$/i.test(v) ? Number(v) : NaN;   // a whole number, not a numeric prefix
      const qty = Math.abs(num(get('quantity'))), price = num(get('price'));
      if (!isin) { errors.push(`${name} row ${r + 1}: no ISIN for ${get('symbol') || 'this row'}${get('exchange') ? ' (' + get('exchange') + ')' : ''}: an exchange-traded unit, not a scheme this page can value; skipped`); continue; }
      if (!/^[A-Z0-9]{12}$/.test(isin)) { errors.push(`${name} row ${r + 1}: '${isin}' is not an ISIN; skipped`); continue; }
      if (type !== 'buy' && type !== 'sell') { errors.push(`${name} row ${r + 1}: trade_type '${type}' is not buy or sell`); continue; }
      if (day === null) { errors.push(`${name} row ${r + 1}: unreadable trade_date '${get('trade_date')}'`); continue; }
      if (!Number.isFinite(qty) || !(qty > 0) || !Number.isFinite(price) || !(price > 0)) { errors.push(`${name} row ${r + 1}: quantity/price not a positive number`); continue; }
      const id = get('trade_id') || `${name}#${r}#${parseSeq}`;   // no id: keep the row; the parse counter keeps same-named uploads apart
      trades.push({ isin, symbol: get('symbol'), day, date: isoOf(day), qty, price, type, id, file: name });
    }
    return { trades, errors, columns: header };
  }
  // Financial years (April to March) with no trades between the first and last trade, which usually
  // means a missing yearly Console download
  function missingYears(trades) {
    if (trades.length < 2) return [];
    const fy = day => { const d = new Date(day * DAY); const y = d.getUTCFullYear(); return d.getUTCMonth() >= 3 ? y : y - 1; };
    const present = new Set(trades.map(t => fy(t.day)));
    const lo = Math.min(...present), hi = Math.max(...present), out = [];
    for (let y = lo + 1; y < hi; y++) if (!present.has(y)) out.push(`FY${y}-${String(y + 1).slice(-2)}`);
    return out;
  }

  // one list across files, trade ids seen once (Console lets a date range be downloaded twice)
  function mergeTrades(lists) {
    const seen = new Set(), out = [];
    let duplicates = 0;
    for (const list of lists) for (const t of list) {
      if (seen.has(t.id)) { duplicates++; continue; }
      seen.add(t.id); out.push(t);
    }
    out.sort((a, b) => a.day - b.day || (a.type === 'sell') - (b.type === 'sell'));
    return { trades: out, duplicates };
  }

  // ---- NAV series --------------------------------------------------------------------------------
  // {days: int[] ascending, navs: number[]}
  function seriesFromCompact(o) {
    if (o && Array.isArray(o.dd) && o.d0 && Array.isArray(o.v) && o.v.length === o.dd.length + 1) {
      const days = new Array(o.dd.length + 1);
      days[0] = dayNum(o.d0);
      if (days[0] === null) return null;
      for (let i = 0; i < o.dd.length; i++) { if (!(o.dd[i] > 0)) return null; days[i + 1] = days[i] + o.dd[i]; }
      const navs = o.v.map(Number);
      if (!navs.every(v => Number.isFinite(v) && v > 0)) return null;
      return finish({ days, navs, name: o.name, code: o.code });
    }
    if (o && Array.isArray(o.d) && Array.isArray(o.v) && o.v.length === o.d.length) {
      const days = o.d.map(dayNum), navs = o.v.map(Number);
      if (days.some((d, i) => d === null || (i && d <= days[i - 1])) || !navs.every(v => Number.isFinite(v) && v > 0)) return null;
      return finish({ days, navs, name: o.name, code: o.code });
    }
    return null;
  }
  function finish(s) {
    if (!s.days.length) return null;
    s.first = s.days[0]; s.last = s.days[s.days.length - 1];
    return s;
  }
  // index of the last day <= d, or -1
  function upperIndex(s, d) {
    let lo = 0, hi = s.days.length - 1, ans = -1;
    while (lo <= hi) { const mid = (lo + hi) >> 1; if (s.days[mid] <= d) { ans = mid; lo = mid + 1; } else hi = mid - 1; }
    return ans;
  }
  function navOnOrBefore(s, d, maxBack) {
    const i = upperIndex(s, d);
    if (i < 0 || d - s.days[i] >= (maxBack === undefined ? 12 : maxBack)) return null;   // ages 0..maxBack-1, as nav.nav_on
    return { nav: s.navs[i], day: s.days[i] };
  }
  function navOnOrAfter(s, d, maxForward) {
    const i = upperIndex(s, d);
    const j = i >= 0 && s.days[i] === d ? i : i + 1;
    if (j >= s.days.length || s.days[j] - d > (maxForward === undefined ? 12 : maxForward)) return null;
    return { nav: s.navs[j], day: s.days[j] };
  }

  // ---- FIFO positions ----------------------------------------------------------------------------
  // Per ISIN: live lots after FIFO sells, every buy (rupees on a day), every sell as the fraction of
  // the units held at that moment, and the units-held timeline.
  function buildPositions(trades, asOfDay) {
    const positions = new Map(), errors = [];
    const sorted = trades.filter(t => t.day <= asOfDay).slice().sort((a, b) => a.day - b.day || (a.type === 'sell') - (b.type === 'sell'));
    for (const t of sorted) {
      let p = positions.get(t.isin);
      if (!p) { p = { isin: t.isin, symbol: t.symbol, lots: [], buys: [], sells: [], events: [], flows: [], units: 0, tradeCount: 0 }; positions.set(t.isin, p); }
      p.tradeCount++;
      if (t.symbol) p.symbol = t.symbol;
      const amount = t.qty * t.price;
      if (t.type === 'buy') {
        p.lots.push({ day: t.day, qty: t.qty, price: t.price });
        p.buys.push({ day: t.day, amount, qty: t.qty, price: t.price });
        p.flows.push([t.day, -amount]);
        p.units += t.qty;
        p.events.push({ day: t.day, units: p.units });
        continue;
      }
      const before = p.units;
      if (t.qty - before > 1e-6) {   // selling more than held makes the position unusable
        p.broken = `sell of ${t.qty} units on ${t.date} exceeds the ${before.toFixed(3)} held; is a tradebook year missing?`;
        errors.push(`${t.symbol || t.isin} (ISIN ${t.isin}): ${p.broken}`);
        continue;
      }
      let left = t.qty;
      for (const lot of p.lots) {
        const used = Math.min(left, lot.qty);
        lot.qty -= used; left -= used;
        if (left <= 1e-8) break;
      }
      p.lots = p.lots.filter(l => l.qty > 1e-8);
      p.units = Math.max(0, before - t.qty);
      p.sells.push({ day: t.day, qty: t.qty, price: t.price, amount, fraction: before > 0 ? Math.min(1, t.qty / before) : 0 });
      p.flows.push([t.day, amount]);
      p.events.push({ day: t.day, units: p.units });
    }
    for (const p of positions.values()) {
      p.invested = p.lots.reduce((a, l) => a + l.qty * l.price, 0);   // cost of the units still held
      p.bought = p.buys.reduce((a, b) => a + b.amount, 0);
      p.realised = p.sells.reduce((a, s) => a + s.amount, 0);
      p.firstBuy = p.buys.length ? p.buys[0].day : null;
      p.lastTrade = p.events.length ? p.events[p.events.length - 1].day : null;
    }
    return { positions, errors };
  }

  // ---- XIRR --------------------------------------------------------------------------------------
  // Day basis is actual/365, as Zerodha Console and Coin report XIRR; server-side metrics.xirr uses the same.
  // flows: [[day, amount]] with investments negative; bisection between -99.99% and +1000%.
  // Returns null when the flows do not bracket a root or are too few.
  function xirr(flows) {
    if (!flows || flows.length < 2) return null;
    const t0 = Math.min(...flows.map(f => f[0]));
    if (flows.every(f => f[0] === t0)) return null;   // one date: no holding period, no rate
    if (!flows.some(f => f[1] < 0) || !flows.some(f => f[1] > 0)) return null;   // nothing in, or nothing out: no rate
    if (!flows.every(f => Number.isFinite(f[0]) && Number.isFinite(f[1]))) return null;
    const npv = r => flows.reduce((a, [d, v]) => a + v / Math.pow(1 + r, (d - t0) / 365.0), 0);
    let lo = -0.9999, hi = 10.0, fLo = npv(lo);
    const fHi = npv(hi);
    if (fLo * fHi > 0 || (fLo === 0 && fHi === 0)) return null;   // no bracket, or an NPV that is zero everywhere
    // one NPV per step, and it stops once the interval stops shrinking
    for (let i = 0; i < 200; i++) {
      const mid = (lo + hi) / 2;
      if (mid === lo || mid === hi || hi - lo < 1e-12) break;
      const fMid = npv(mid);
      if (fLo * fMid <= 0) hi = mid; else { lo = mid; fLo = fMid; }
    }
    return (lo + hi) / 2;
  }

  // ---- valuation and replay ----------------------------------------------------------------------
  // value the actual position: units held times the last NAV on or before asOfDay
  function valueActual(p, series, asOfDay, maxStale) {
    // a sold-out position needs no recent NAV: its outcome is settled, so the last NAV the series has will do
    const at = navOnOrBefore(series, asOfDay, maxStale === undefined ? 12 : maxStale) || (p.units <= 1e-8 ? navOnOrBefore(series, asOfDay, Infinity) : null);
    if (!at) return null;
    const value = p.units * at.nav;
    // the terminal flow is dated on the requested day, so every series compared
    // on the same as-of day carries the same terminal date whatever its last NAV day was
    const flows = p.flows.concat(p.units > 1e-8 ? [[asOfDay, value]] : []);
    return { value, nav: at.nav, navDay: at.day, xirr: xirr(flows), flows, gain: value - p.invested };
  }

  // Same rupees on the same days into `series`: each buy becomes amount / NAV on that day (or the next
  // NAV within 12 days, for holidays); each sell removes the same fraction of the units held. Returns
  // null when the series starts after a buy.
  function replay(p, series, asOfDay, maxStale) {
    let units = 0, basis = 0;   // basis: rupees paid for the units still held, scaled down by each proportional sell
    const flows = [], events = [];
    const timeline = p.buys.map(b => ({ day: b.day, kind: 'buy', amount: b.amount }))
      .concat(p.sells.map(s => ({ day: s.day, kind: 'sell', fraction: s.fraction })))
      .sort((a, b) => a.day - b.day || (a.kind === 'sell') - (b.kind === 'sell'));
    for (const e of timeline) {
      if (e.kind === 'buy') {
        if (e.day < series.first) return null;
        let at = navOnOrAfter(series, e.day, 12);
        if (at && at.day > asOfDay) at = navOnOrBefore(series, e.day, 12);   // never a NAV from after the valuation day
        if (!at) return null;
        units += e.amount / at.nav;
        basis += e.amount;
        flows.push([e.day, -e.amount]);
      } else {
        if (units <= 1e-12) { flows.push([e.day, 0]); events.push({ day: e.day, units: 0 }); continue; }   // nothing held: nothing to sell, no NAV needed
        const at = navOnOrBefore(series, e.day, 12);
        if (!at) return null;
        const sold = units * e.fraction;
        units -= sold;
        basis *= (1 - e.fraction);
        flows.push([e.day, sold * at.nav]);
      }
      events.push({ day: e.day, units });
    }
    const at = navOnOrBefore(series, asOfDay, maxStale === undefined ? 12 : maxStale);
    if (!at) return null;
    const value = units * at.nav;
    if (units > 1e-12) flows.push([asOfDay, value]);
    return { value, units, navDay: at.day, xirr: xirr(flows), flows, events, invested: basis, gain: value - basis };
  }

  // Rupee flows into one series: each negative flow buys units at that day's NAV (next NAV within 12 days),
  // each positive flow sells units worth that many rupees (capped at what is held; the shortfall is reported).
  // For "everything I invested through Coin, into fund X". Null when the series starts after the first flow.
  function replayFlows(flows, series, asOfDay, maxStale) {
    const sorted = flows.filter(f => f[0] <= asOfDay).slice().sort((a, b) => a[0] - b[0] || a[1] - b[1]);
    if (!sorted.length) return null;
    let units = 0, basis = 0, shortfall = 0;
    const out = [], events = [];
    for (const [day, amount] of sorted) {
      if (day < series.first) return null;   // any flow before the fund existed: it cannot be replayed
      if (amount < 0) {
        let at = navOnOrAfter(series, day, 12);
        if (at && at.day > asOfDay) at = navOnOrBefore(series, day, 12);   // never a NAV from after the valuation day
        if (!at) return null;
        units += -amount / at.nav;
        basis += -amount;
        out.push([day, amount]);
      } else if (amount > 0) {
        if (units <= 1e-12) { shortfall += amount; out.push([day, 0]); events.push({ day, units: 0 }); continue; }   // nothing held: the whole withdrawal is shortfall, no NAV needed
        const at = navOnOrBefore(series, day, 12);
        if (!at) return null;
        const held = units * at.nav;
        const take = Math.min(amount, held);
        shortfall += amount - take;
        const frac = held > 0 ? take / held : 0;
        units -= units * frac;
        basis *= (1 - frac);
        out.push([day, take]);
      }
      events.push({ day, units });
    }
    const at = navOnOrBefore(series, asOfDay, maxStale === undefined ? 12 : maxStale);
    if (!at) return null;
    const value = units * at.nav;
    if (units > 1e-12) out.push([asOfDay, value]);
    return { value, units, navDay: at.day, xirr: xirr(out), flows: out, events, invested: basis, gain: value - basis, shortfall };
  }

  // units held on a day from an events timeline (step function)
  function unitsOn(events, day) {
    let u = 0;
    for (const e of events) { if (e.day > day) break; u = e.units; }
    return u;
  }
  // value path: [[day, value]] on the given days (null where the series has no NAV yet)
  function valuePath(events, series, days) {
    return days.map(d => {
      const at = navOnOrBefore(series, d, 12);
      return [d, at ? unitsOn(events, d) * at.nav : null];
    });
  }
  // rupees invested (net of what was realised) on each day: a step function of the flows
  function investedPath(flows, days) {
    const sorted = flows.slice().sort((a, b) => a[0] - b[0]);
    return days.map(d => { let s = 0; for (const [fd, v] of sorted) { if (fd > d) break; s -= v; } return [d, s]; });
  }

  // percentile rank of `value` within `values` (share of peers at or below it), 0..1
  function rankAmong(value, values) {
    if (!Number.isFinite(value)) return null;
    const v = values.filter(x => Number.isFinite(x));
    if (!v.length) return null;
    return v.filter(x => x <= value).length / v.length;
  }
  function median(values) {
    const v = values.filter(x => x !== null && x !== undefined && Number.isFinite(x)).sort((a, b) => a - b);
    if (!v.length) return null;
    const m = v.length >> 1;
    return v.length % 2 ? v[m] : (v[m - 1] + v[m]) / 2;
  }

  return { dayNum, isoOf, parseDay, yearsBetween, monthEnds, parseCSV, parseTradebook, mergeTrades,
           seriesFromCompact, navOnOrBefore, navOnOrAfter, buildPositions, xirr,
           valueActual, replay, replayFlows, unitsOn, valuePath, investedPath, rankAmong, median, missingYears };
})();

(() => {
'use strict';
const $ = id => document.getElementById(id);
const h = s => String(s == null ? '' : s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const inr = v => v == null || !Number.isFinite(v) ? '·' : (v < 0 ? '-' : '') + '₹' + Math.round(Math.abs(v)).toLocaleString('en-IN');
const inrS = v => v == null || !Number.isFinite(v) ? '·' : (v < 0 ? '-' : '+') + '₹' + Math.round(Math.abs(v)).toLocaleString('en-IN');
const lakh = v => v == null ? '·' : Math.abs(v) >= 1e7 ? '₹' + (v / 1e7).toFixed(2) + ' cr' : Math.abs(v) >= 1e5 ? '₹' + (v / 1e5).toFixed(2) + ' L' : inr(v);
const lakhS = v => v == null ? '·' : (v < 0 ? '-' : '+') + lakh(Math.abs(v));
const pct = r => r == null || !Number.isFinite(r) ? '·' : (100 * r).toFixed(1) + '%';
const pp = r => r == null || !Number.isFinite(r) ? '·' : (r >= 0 ? '+' : '') + (100 * r).toFixed(1) + ' pp';
const sign = v => v == null || !Number.isFinite(v) ? '' : v > 0 ? 'up' : v < 0 ? 'down' : '';
const SUP = '⁰¹²³⁴⁵⁶⁷⁸⁹';
const big = n => { if (n < 1e6) return n.toLocaleString('en-IN'); const e = Math.floor(Math.log10(n)); return `${(n / Math.pow(10, e)).toFixed(1)} × 10${String(e).split('').map(c => SUP[+c]).join('')}`; };
const MON = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
const fmtDay = d => { const [y, m, dd] = PE.isoOf(d).split('-'); return `${+dd} ${MON[+m - 1]} ${y}`; };
const monthYear = d => { const s = PE.isoOf(d); return MON[+s.slice(5, 7) - 1] + ' ' + s.slice(0, 4); };
const ord = n => n + (n % 100 >= 11 && n % 100 <= 13 ? 'th' : ['th', 'st', 'nd', 'rd'][n % 10] || 'th');
const TODAY = Math.floor(Date.now() / 86400000);
const API = '/api/v1/portfolio';

// ---- public data: the map, and one series per fund or index, each fetched once ------------------------
let MAP = null, mapPromise = null;
const SERIES = new Map(), SYNC = new Map();
function loadMap() {
  if (!mapPromise) mapPromise = fetch(`${API}/map`).then(r => { if (!r.ok) throw new Error('map ' + r.status); return r.json(); }).then(m => { MAP = m; return m; });
  return mapPromise;
}
function loadSeries(kind, code) {
  const key = kind + ':' + code;
  if (!SERIES.has(key)) {
    const p = fetch(`${API}/${kind === 'index' ? 'index' : 'nav'}/${encodeURIComponent(code)}`)
      .then(r => r.ok ? r.json() : null).then(o => o && PE.seriesFromCompact(o)).catch(() => null);
    p.then(s => { if (s) SYNC.set(key, s); });
    SERIES.set(key, p);
  }
  return SERIES.get(key);
}
const seriesSync = (kind, code) => SYNC.get(kind + ':' + code) || null;
const benchNameOf = stem => { for (const c of Object.values(MAP.categories)) if (c.bench === stem) return c.bench_name; return (MAP.indexes[stem] || {}).n || stem; };
async function mapLimit(items, limit, fn, onEach) {
  const out = new Array(items.length);
  let next = 0, done = 0;
  const worker = async () => { while (next < items.length) { const i = next++; out[i] = await fn(items[i], i); done++; if (onEach) onEach(done, items.length); } };
  await Promise.all(Array.from({ length: Math.min(limit, items.length) }, worker));
  return out;
}

let RESULT = null, ORDER = [];
const status = (html, err) => { $('status').innerHTML = err ? `<span class="down">${html}</span>` : html; };
const progress = (done, total, what) => { $('status').innerHTML = `${h(what)} ${done}/${total}<progress max="${total}" value="${done}"></progress>`; };
const problemList = problems => problems.length ? `<ul>${problems.map(p => `<li>${h(p)}</li>`).join('')}</ul>` : '';

// ---- analysis ---------------------------------------------------------------------------------------
async function analyse(files) {
  $('results').hidden = true; $('kpis').innerHTML = '';
  status('Reading files…');
  const parsed = [], problems = [];
  for (const f of files) {
    const r = PE.parseTradebook(await f.text(), f.name);
    problems.push(...r.errors);
    parsed.push(r.trades);
  }
  const merged = PE.mergeTrades(parsed);
  const gaps = PE.missingYears(merged.trades);
  if (gaps.length) problems.unshift(`No trades at all in ${gaps.join(', ')} although there are trades before and after: a year's tradebook is probably missing, and every sell after it will look larger than the units held.`);
  if (!merged.trades.length) { status(`<span class="down">No trades found.</span>${problemList(problems)}`); return; }
  status('Loading the ISIN map…');
  let map;
  try { map = await loadMap(); } catch (e) { status('Could not load the fund map from this site; try again in a moment.', true); return; }
  const built = PE.buildPositions(merged.trades, TODAY);
  problems.push(...built.errors);
  const positions = [];
  for (const p of built.positions.values()) {
    if (p.broken) {   // already in problems; a sell beyond the units held invalidates later figures
      const f = map.funds[map.isin[p.isin] || ''];
      if (f) problems.push(`  ${p.isin} is ${f.n}, ${f.p} plan${f.o === 'idcw' ? ', IDCW' : ''}${p.buys.length ? '' : ': no purchase of it appears in the file at all, so those units came from outside these tradebooks'}.`);
      continue;
    }
    const code = map.isin[p.isin], meta = code ? map.funds[code] : null;
    if (!meta) { problems.push(map.ambiguous[p.isin] ? `${p.symbol || p.isin}: AMFI lists ISIN ${p.isin} under ${map.ambiguous[p.isin].length} scheme codes (${map.ambiguous[p.isin].join(', ')}), so it cannot be valued safely; skipped` : `${p.symbol || p.isin}: ISIN ${p.isin} is not in AMFI's NAV file; skipped`); continue; }
    const cat = meta.c ? map.categories[meta.c] : null;
    const benchStem = meta.b || (cat ? cat.bench : null);   // the fund's own index first (overseas, index funds), then the category's
    // the plan: what the tradebook says first, then AMFI's
    const plan = /\bdirect\b/i.test(p.symbol || '') ? 'direct' : /\bregular\b/i.test(p.symbol || '') ? 'regular' : meta.p;
    const name = (p.symbol || meta.n).replace(/\s*-\s*(direct|regular)\s*plan\s*/i, ' ').replace(/\s*-\s*(growth|idcw|dividend)(\s+option)?\s*$/i, '').replace(/\s{2,}/g, ' ').trim() || meta.n;
    positions.push({ p, code, meta, cat, key: meta.c || null, plan, name, passive: !!meta.pv && !!(meta.b || meta.k),
      benchNote: meta.c === 'international' && !meta.b ? 'home market: no matching overseas index on this site' : '',
      benchStem, benchName: benchStem ? benchNameOf(benchStem) : null, twin: plan === 'regular' ? meta.d : null });
  }
  if (!positions.length) { status(`<span class="down">None of the ISINs could be mapped.</span>${problemList(problems)}`); return; }

  await mapLimit([...new Set(positions.map(x => x.code))], 6, c => loadSeries('fund', c), (d, t) => progress(d, t, 'Fetching NAV history'));
  for (const x of positions) x.series = seriesSync('fund', x.code);
  for (const x of positions.filter(x => !x.series)) problems.push(`${x.name}: no usable NAV history (scheme ${x.code}); skipped`);
  const live = positions.filter(x => x.series);
  if (!live.length) { status(`<span class="down">No NAV history could be loaded.</span>${problemList(problems)}`); return; }
  const asOf = Math.min(TODAY, Math.max(...live.map(x => x.series.last)));

  for (const x of live) {
    x.actual = PE.valueActual(x.p, x.series, asOf, 400);
    x.stale = x.actual && asOf - x.actual.navDay > 7 ? asOf - x.actual.navDay : 0;
    if (x.actual && x.p.units > 1e-8) {   // the terminal flow sits on the fund's own last NAV day, as every counterfactual's does
      const last = x.actual.flows[x.actual.flows.length - 1];
      if (last[0] !== x.actual.navDay) { last[0] = x.actual.navDay; x.actual.xirr = PE.xirr(x.actual.flows); }
    }
    if (x.actual && x.p.lastTrade - x.series.last > 30) {   // traded after the series ends (merged or moved ISIN)
      problems.push(`${x.name}: traded on ${PE.isoOf(x.p.lastTrade)}, after its last NAV on ${PE.isoOf(x.series.last)}; skipped`);
      x.actual = null;
    }
  }
  const valued = live.filter(x => x.actual);
  for (const x of live.filter(x => !x.actual)) problems.push(`${x.name}: no NAV near ${PE.isoOf(asOf)}; skipped`);

  // counterfactuals: benchmark, inflation, direct twin, peers
  const benchStems = [...new Set(valued.map(x => x.benchStem).filter(Boolean)), 'cpi-india'];
  await mapLimit(benchStems, 6, s => loadSeries('index', s), (d, t) => progress(d, t, 'Fetching benchmarks'));
  const cpi = seriesSync('index', 'cpi-india');
  const peerCodes = new Set();
  for (const x of valued) {
    for (const c of (x.cat && x.cat.pool[x.plan]) || []) if (String(c) !== x.code) peerCodes.add(String(c));
    if (x.twin) peerCodes.add(x.twin);
  }
  await mapLimit([...peerCodes], 8, c => loadSeries('fund', c), (d, t) => progress(d, t, 'Fetching peer histories'));
  for (const x of valued) {
    const end = x.actual.navDay;   // the fund's last NAV day: every counterfactual is closed on the same day
    x.bench = x.benchStem ? seriesSync('index', x.benchStem) : null;
    x.benchReplay = x.bench ? PE.replay(x.p, x.bench, end, 400) : null;
    x.years = x.bench ? yearByYear(x.series, x.bench, end) : [];
    x.behindStreak = streakBehind(x.years);
    x.cpiReplay = cpi ? PE.replay(x.p, cpi, end, 40) : null;
    const twin = x.twin && seriesSync('fund', x.twin), tr = twin ? PE.replay(x.p, twin, end, 400) : null;
    x.direct = tr ? { code: x.twin, replay: tr } : null;
    x.peers = []; x.peersSkipped = 0;
    if (x.p.units > 1e-8 && x.cat && x.cat.pool[x.plan]) {   // a closed position has no "value today" to rank
      for (const c of x.cat.pool[x.plan].map(String)) {
        if (c === x.code) continue;
        const s = seriesSync('fund', c), r = s ? PE.replay(x.p, s, end, 400) : null;
        if (!r) { x.peersSkipped++; continue; }
        x.peers.push({ code: c, name: (map.funds[c] || {}).n || c, replay: r });
      }
      x.peers.sort((a, b) => b.replay.value - a.replay.value);
      const values = x.peers.map(q => q.replay.value).concat([x.actual.value]);
      x.rank = values.filter(v => v > x.actual.value + 1e-6).length + 1;   // 1 = best
      x.peerCount = values.length;
      x.peerMedian = PE.median(values);
      x.best = x.peers.length && x.peers[0].replay.value > x.actual.value + 1e-6 ? x.peers[0] : null;
    }
  }
  RESULT = { asOf, all: valued, excluded: new Set(), problems, merged, files: files.length };
  applyExclusions();
}

function computeTotals(valued) {
  const merged = RESULT.merged;
  const sum = (arr, f) => arr.reduce((a, v) => a + (f(v) || 0), 0);
  const flowsOf = (arr, f) => arr.flatMap(v => (f(v) ? f(v).flows : []));
  const withBench = valued.filter(x => x.benchReplay), withCpi = valued.filter(x => x.cpiReplay);
  const open = valued.filter(x => x.p.units > 1e-8), ranked = open.filter(x => x.peerCount > 1);
  const T = {
    invested: sum(valued, x => x.p.invested), bought: sum(valued, x => x.p.bought), realised: sum(valued, x => x.p.realised),
    value: sum(valued, x => x.actual.value), xirr: PE.xirr(flowsOf(valued, x => x.actual)),
    benchValue: withBench.length ? sum(withBench, x => x.benchReplay.value) : null,
    benchActual: sum(withBench, x => x.actual.value), benchXirr: withBench.length ? PE.xirr(flowsOf(withBench, x => x.benchReplay)) : null,
    benchCoverage: withBench.length,
    cpiValue: withCpi.length ? sum(withCpi, x => x.cpiReplay.value) : null, cpiActual: sum(withCpi, x => x.actual.value),
    cpiCoverage: withCpi.length, cpiXirr: withCpi.length ? PE.xirr(flowsOf(withCpi, x => x.cpiReplay)) : null,
    lifetime: sum(valued, x => x.actual.value + x.p.realised - x.p.bought),   // value now plus everything sold, less everything put in
    directDrag: sum(open.filter(x => x.direct), x => x.direct.replay.value - x.actual.value), directCount: open.filter(x => x.direct).length,
    medianPeerValue: open.length && open.every(x => x.peerMedian != null) ? sum(open, x => x.peerMedian) + sum(valued.filter(x => x.p.units <= 1e-8), x => x.actual.value) : null,
    bestValue: valued.some(x => x.peerCount) ? sum(valued, x => x.best ? x.best.replay.value : x.actual.value) : null,
    bestCount: valued.filter(x => x.best).length,
    // money-weighted share of same-category peers your picks beat, and the odds of a blind pick landing on every category's best
    beatShare: (() => { const w = sum(ranked, x => x.p.bought); return w ? ranked.reduce((a, x) => a + x.p.bought * (x.peerCount - x.rank) / (x.peerCount - 1), 0) / w : null; })(),
    bestOdds: ranked.reduce((a, x) => a * x.peerCount, 1), ranked: ranked.length, open: open.length,
    trades: merged.trades.length, duplicates: merged.duplicates, files: RESULT.files,
  };
  T.gain = T.value - T.invested;
  return T;
}
// a fund switched off leaves every figure on the page: totals, benchmarks, chart, the swap table
function applyExclusions() {
  RESULT.positions = RESULT.all.filter((_, i) => !RESULT.excluded.has(i));
  RESULT.closed = RESULT.positions.filter(x => x.p.units <= 1e-8);
  RESULT.totals = computeTotals(RESULT.positions);
  render();
}
async function toggleExcluded(i) {
  if (RESULT.excluded.has(i)) { RESULT.excluded.delete(i); SCOPE.add(i); } else RESULT.excluded.add(i);
  const keep = COMPARE.map(c => ({ kind: c.kind, code: c.code, label: c.label }));
  COMPARE = [];
  applyExclusions();
  const box = document.querySelector(`input[data-inc="${i}"]`); if (box) box.focus();   // the keyboard stays where it was
  for (const c of keep) await addComparator(c);
}

// calendar-year NAV returns of a fund and its benchmark: the last five completed years and this one so far
function yearReturn(series, y0, y1) {
  const a = PE.navOnOrBefore(series, y0, 10), b = PE.navOnOrBefore(series, y1, 10);
  return a && b && a.day < b.day ? b.nav / a.nav - 1 : null;
}
function yearByYear(fund, bench, asOf) {
  const year = new Date(asOf * 86400000).getUTCFullYear(), out = [];
  for (let y = year - 5; y <= year; y++) {
    const y0 = PE.dayNum(`${y - 1}-12-31`), y1 = y === year ? asOf : PE.dayNum(`${y}-12-31`);
    const f = yearReturn(fund, y0, y1), b = yearReturn(bench, y0, y1);
    if (f != null && b != null) out.push({ year: y, partial: y === year, fund: f, bench: b, gap: f - b });
  }
  return out;
}
function streakBehind(years) {   // consecutive completed years behind, counted back from the latest
  const done = years.filter(r => !r.partial);
  let n = 0;
  for (let i = done.length - 1; i >= 0 && done[i].gap < 0; i--) n++;
  return n;
}
function yearsBlock(years, series, bench, benchName, asOf, note) {
  if (!years.length) return '';
  const y0 = PE.dayNum(`${years[0].year - 1}-12-31`);
  const f = yearReturn(series, y0, asOf), b = yearReturn(bench, y0, asOf), yrs = (asOf - y0) / 365.25;
  const cagr = r => r == null ? null : Math.pow(1 + r, 1 / yrs) - 1;
  const net = f != null && b != null ? `<tr><td>Net, ${years[0].year} to now</td><td class="n">${pct(f)}<br><small>${pct(cagr(f))} a year</small></td><td class="n">${pct(b)}<br><small>${pct(cagr(b))} a year</small></td><td class="n ${sign(f - b)}">${pp(f - b)}<br><small>${pp(cagr(f) - cagr(b))} a year</small></td></tr>` : '';
  return `<div class="scroll-x"><table><thead><tr><th>Year</th><th class="n">Fund</th><th class="n">${h(benchName)}</th><th class="n">Gap</th></tr></thead><tbody>${years.map(r => `<tr><td>${r.year}${r.partial ? ' so far' : ''}</td><td class="n">${pct(r.fund)}</td><td class="n">${pct(r.bench)}</td><td class="n ${sign(r.gap)}">${pp(r.gap)}</td></tr>`).join('')}${net}</tbody></table></div>
    <p class="sub">Calendar-year NAV returns of the fund and its benchmark, not your cash flows; the net row compounds from 31 Dec ${years[0].year - 1}.${note || ''}</p>`;
}

// ---- rendering --------------------------------------------------------------------------------------
const kpi = (v, l, cls) => `<div class="stat"><span class="big ${cls || ''}">${v}</span><span class="sub">${l}</span></div>`;
function render() {
  const R = RESULT, T = R.totals, open = R.positions.filter(x => x.p.units > 1e-8);
  status(`${T.trades} trades in ${T.files} file${T.files === 1 ? '' : 's'}${T.duplicates ? `, ${T.duplicates} repeated rows ignored` : ''} · ${open.length} open position${open.length === 1 ? '' : 's'}${R.closed.length ? `, ${R.closed.length} closed` : ''}${R.excluded.size ? ` · ${R.excluded.size} switched off` : ''} · valued on ${fmtDay(R.asOf)}`);
  const noBench = R.positions.length - T.benchCoverage;
  const cards = [
    kpi(lakh(T.value), `value now · ${lakh(T.invested)} in (cost of units held) · unrealised gain ${lakhS(T.gain)} · lifetime P&amp;L ${lakhS(T.lifetime)} incl. sales`),
    kpi(pct(T.xirr), `XIRR, every buy and sell${T.realised ? ` · ${lakh(T.realised)} realised` : ''}`),
  ];
  if (T.benchValue != null) cards.push(kpi(lakhS(T.benchActual - T.benchValue), `vs each fund's own benchmark: ${lakh(T.benchValue)}, XIRR ${pct(T.benchXirr)}${noBench ? ` · ${noBench} without one` : ''}`, sign(T.benchActual - T.benchValue)));
  if (T.medianPeerValue != null) cards.push(kpi(lakhS(T.value - T.medianPeerValue), `vs the median peer of each category: ${lakh(T.medianPeerValue)}`, sign(T.value - T.medianPeerValue)));
  if (T.beatShare != null) cards.push(kpi(pct(T.beatShare), `of same-category peers beaten, weighted by money in, open positions${T.ranked < T.open ? ` · ${T.open - T.ranked} without peers` : ''}`));
  if (T.bestValue != null && T.bestCount) cards.push(kpi(lakhS(T.bestValue - T.value), `with hindsight: the best fund of each category would total ${lakh(T.bestValue)} (${T.bestCount} had a better peer) · blind odds 1 in ${big(T.bestOdds)}`));
  if (T.cpiValue != null) cards.push(kpi(lakhS(T.cpiActual - T.cpiValue), `above inflation: CPI would make it ${lakh(T.cpiValue)} (${pct(T.cpiXirr)} a year)${T.cpiCoverage < R.positions.length ? ` · ${R.positions.length - T.cpiCoverage} not covered` : ''}`, sign(T.cpiActual - T.cpiValue)));
  if (T.directCount) cards.push(kpi(lakhS(-T.directDrag), `cost so far of ${T.directCount} regular plan${T.directCount === 1 ? '' : 's'} instead of direct`, sign(-T.directDrag)));
  $('kpis').innerHTML = cards.join('');
  // the swap scope indexes the included positions, so it is reset before anything draws from it
  const keep = SCOPE.size ? new Set([...SCOPE].filter(i => !R.excluded.has(i))) : null;
  SCOPE = keep && keep.size ? keep : new Set(R.positions.map(x => R.all.indexOf(x)));
  renderScope(); renderCompare();
  $('results').hidden = false;   // before drawing: the chart measures its box
  renderTable(); renderGrowth(); renderClosed();
  $('reset').hidden = false;
  const stale = R.positions.filter(x => x.stale);
  $('warnings').innerHTML = (R.problems.length ? `<div class="pf-warn"><b>Notes</b>${problemList(R.problems)}</div>` : '') +
    (stale.length ? `<div class="pf-warn">NAV older than a week on the valuation day: ${stale.map(x => `${h(x.name)} (${fmtDay(x.actual.navDay)})`).join(', ')}.</div>` : '');
}
function nameCell(x, ai, off, label) {
  return `<td class="pf-name"><div class="pf-nmrow"><label class="pf-inc"><input type="checkbox" data-inc="${ai}" ${off ? '' : 'checked'} aria-label="Count ${h(x.name)}"></label><span><button type="button" class="pf-link" data-open="${ai}">${h(x.name)}</button>${x.plan === 'regular' ? '<span class="tag">regular</span>' : ''}${x.meta.o === 'idcw' ? '<span class="tag">IDCW</span>' : ''}${x.stale ? '<span class="tag">stale NAV</span>' : ''}<br><small>${label}</small></span></div></td>`;
}
const catLabel = x => `${h(x.cat ? x.cat.label : (MAP.amfi_categories[x.meta.a] || 'uncategorised').replace(/^.*? - /, ''))} · ${x.plan}`;
function wire(table) {
  table.querySelectorAll('[data-open]').forEach(b => b.addEventListener('click', () => openDrawer(+b.dataset.open)));
  table.querySelectorAll('[data-peer]').forEach(b => b.addEventListener('click', () => openPeerDrawer(+b.dataset.peer, b.dataset.code)));
  table.querySelectorAll('[data-inc]').forEach(c => c.addEventListener('change', () => toggleExcluded(+c.dataset.inc)));
}
function renderClosed() {
  const R = RESULT, rows = R.all.filter(x => x.p.units <= 1e-8);
  $('closed-box').hidden = !rows.length;
  if (!rows.length) return;
  const off = rows.filter(x => R.excluded.has(R.all.indexOf(x))).length;
  $('closed-sum').textContent = `${rows.length} closed position${rows.length === 1 ? '' : 's'} · ${inr(rows.reduce((a, x) => a + x.p.bought, 0))} put in, ${inr(rows.reduce((a, x) => a + x.p.realised, 0))} realised${off ? ` · ${off} switched off` : ''}`;
  rows.sort((a, b) => b.p.lastTrade - a.p.lastTrade);
  $('closed').innerHTML = `<thead><tr><th>Fund</th><th>Held</th><th class="n">Put in</th><th class="n">Realised</th><th class="n">Gain</th><th class="n">XIRR</th><th class="n">Benchmark XIRR</th><th class="n">vs benchmark</th></tr></thead><tbody>${rows.map(x => {
    const ai = R.all.indexOf(x), gain = x.p.realised - x.p.bought, b = x.benchReplay, gap = b && x.actual.xirr != null && b.xirr != null ? x.actual.xirr - b.xirr : null;
    return `<tr class="${R.excluded.has(ai) ? 'pf-off' : ''}">${nameCell(x, ai, R.excluded.has(ai), catLabel(x))}<td>${fmtDay(x.p.firstBuy)} to ${fmtDay(x.p.lastTrade)}</td><td class="n">${inr(x.p.bought)}</td><td class="n">${inr(x.p.realised)}</td><td class="n ${sign(gain)}">${inrS(gain)}</td><td class="n">${pct(x.actual.xirr)}</td><td class="n">${b ? `${pct(b.xirr)}<br><small>${h(x.benchName)}</small>` : '·'}</td><td class="n ${sign(gap)}">${pp(gap)}</td></tr>`;
  }).join('')}</tbody>`;
  wire($('closed'));
}

const COLS = [
  { k: 'name', label: 'Fund' }, { k: 'first', label: 'First buy' }, { k: 'invested', label: 'Invested', n: 1 },
  { k: 'value', label: 'Value', n: 1 }, { k: 'gain', label: 'Unrealised gain', n: 1 }, { k: 'xirr', label: 'XIRR', n: 1 },
  { k: 'bxirr', label: 'Benchmark XIRR', n: 1 }, { k: 'vsb', label: 'vs benchmark', n: 1 }, { k: 'rank', label: 'Rank among peers' },
  { k: 'years', label: 'Beat index, last 5 years' }, { k: 'best', label: 'Best in category' }, { k: 'direct', label: 'Direct plan gap', n: 1 },
];
let sortKey = 'value', sortAsc = false, sortFocus = null;
function rowData(x) {
  const done = x.years.filter(r => !r.partial);
  return { name: x.name, first: x.p.firstBuy, invested: x.p.invested, value: x.actual.value, gain: x.actual.gain, xirr: x.actual.xirr,
           bxirr: x.benchReplay ? x.benchReplay.xirr : null, vsb: x.benchReplay ? x.actual.value - x.benchReplay.value : null,
           rank: x.rank ? -(x.rank / x.peerCount) : null, best: x.best ? x.best.replay.value - x.actual.value : (x.peerCount ? 0 : null),
           years: !done.length ? null : x.passive ? done.reduce((a, r) => a + r.gap, 0) / done.length : -x.behindStreak + done.filter(r => r.gap >= 0).length / 100,
           direct: x.direct ? x.actual.value - x.direct.replay.value : null };
}
function yearsCell(x) {
  const done = x.years.filter(r => !r.partial);
  if (!done.length) return '·';
  if (x.passive) { const lag = done.reduce((a, r) => a + r.gap, 0) / done.length; return `<span class="${lag < -0.01 ? 'down' : ''}">${pp(lag)} a year</span><br><small>index fund: lag behind its index, ${done.length}-year average</small>`; }
  const won = done.filter(r => r.gap >= 0).length, cur = x.years.find(r => r.partial);
  return `<span class="${x.behindStreak >= 2 ? 'down' : won >= done.length / 2 ? 'up' : ''}">${won} of ${done.length}</span><br><small>${x.behindStreak >= 2 ? `behind ${x.behindStreak} years running` : cur ? (cur.gap >= 0 ? 'ahead' : 'behind') + ' so far this year' : ''}</small>`;
}
function renderTable() {
  const rows = RESULT.all.filter(x => x.p.units > 1e-8).map(x => ({ x, d: rowData(x) }));
  rows.sort((a, b) => { const va = a.d[sortKey], vb = b.d[sortKey]; if (va == null && vb == null) return 0; if (va == null) return 1; if (vb == null) return -1; return (typeof va === 'string' ? va.localeCompare(vb) : va - vb) * (sortAsc ? 1 : -1); });
  ORDER = rows.map(r => RESULT.all.indexOf(r.x));
  const head = `<thead><tr>${COLS.map(c => `<th data-k="${c.k}" class="sortable${c.n ? ' n' : ''}" tabindex="0" aria-sort="${sortKey === c.k ? (sortAsc ? 'ascending' : 'descending') : 'none'}">${c.label}</th>`).join('')}</tr></thead>`;
  const body = rows.map(({ x, d }) => {
    const ai = RESULT.all.indexOf(x), off = RESULT.excluded.has(ai);
    const rank = x.rank ? `${ord(x.rank)} of ${x.peerCount}<br><small>${x.rank === 1 ? 'the best' : 'top ' + Math.ceil(100 * x.rank / x.peerCount) + '%'}${x.peersSkipped ? `, ${x.peersSkipped} younger left out` : ''}</small>` : (x.key ? '<small>no peers</small>' : '·');
    return `<tr class="${off ? 'pf-off' : ''}">${nameCell(x, ai, off, catLabel(x))}
      <td>${fmtDay(d.first)}</td><td class="n">${inr(d.invested)}</td><td class="n">${inr(d.value)}</td>
      <td class="n ${sign(d.gain)}">${inrS(d.gain)}</td><td class="n ${sign(d.xirr)}">${pct(d.xirr)}</td>
      <td class="n" title="${h(x.benchNote)}">${x.benchReplay ? `${pct(d.bxirr)}<br><small>${h(x.benchName)}${x.benchNote ? ' *' : ''}</small>` : '·'}</td>
      <td class="n ${sign(d.vsb)}" title="${x.benchReplay ? `same money in ${h(x.benchName)} would be ${inr(x.benchReplay.value)}` : 'no benchmark'}">${x.benchReplay ? inrS(d.vsb) : '·'}</td>
      <td>${rank}</td><td>${yearsCell(x)}</td>
      <td>${x.best ? `<button type="button" class="pf-link" data-peer="${ai}" data-code="${h(x.best.code)}">${h(x.best.name)}</button><br><small class="up">${inrS(d.best)} more</small>` : (x.peerCount ? '<small>this one</small>' : '·')}</td>
      <td class="n ${sign(d.direct)}" title="${x.direct ? `the direct plan would be worth ${inr(x.direct.replay.value)}` : ''}">${x.direct ? inrS(d.direct) : (x.plan === 'direct' ? '<small>direct</small>' : '·')}</td></tr>`;
  }).join('');
  $('pos').innerHTML = head + `<tbody>${body}</tbody>`;
  $('pos').querySelectorAll('th[data-k]').forEach(th => {
    const go = () => { if (sortKey === th.dataset.k) sortAsc = !sortAsc; else { sortKey = th.dataset.k; sortAsc = th.dataset.k === 'name' || th.dataset.k === 'first'; } sortFocus = th.dataset.k; renderTable(); };
    th.addEventListener('click', go);
    th.addEventListener('keydown', e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); go(); } });
  });
  wire($('pos'));
  if (sortFocus) { const th = $('pos').querySelector(`th[data-k="${sortFocus}"]`); if (th) th.focus(); sortFocus = null; }
}

// ---- charts -----------------------------------------------------------------------------------------
const lakhShort = v => v >= 1e7 ? (v / 1e7).toFixed(v % 1e7 ? 1 : 0) + ' cr' : v >= 1e5 ? (v / 1e5).toFixed(v % 1e5 ? 1 : 0) + ' L' : v >= 1e3 ? (v / 1e3).toFixed(0) + 'k' : String(Math.round(v));
const niceStep = raw => { const p = Math.pow(10, Math.floor(Math.log10(raw || 1))), m = raw / p; return (m <= 1 ? 1 : m <= 2 ? 2 : m <= 5 ? 5 : 10) * p; };
// series: [{points: [[day, value|null]], cls, label, stroke}], y from 0; the hover readout is a line of text under the chart
function lineChart(el, series, opts) {
  const W = Math.max(300, el.clientWidth || 640), H = opts.height || 260, L = 56, R = 10, T = 10, B = 26;
  const all = series.flatMap(s => s.points.filter(p => p[1] != null));
  if (!all.length) { el.innerHTML = '<p class="empty">Nothing to draw.</p>'; return; }
  const x0 = Math.min(...all.map(p => p[0])), x1 = Math.max(...all.map(p => p[0]));
  const yMax = Math.max(0, ...all.map(p => p[1])) * 1.05 || 1, yMin = Math.min(0, ...all.map(p => p[1])) * 1.05;
  const X = d => L + (W - L - R) * (x1 === x0 ? 0 : (d - x0) / (x1 - x0));
  const Y = v => T + (H - T - B) * (1 - (v - yMin) / (yMax - yMin));
  const step = niceStep((yMax - yMin) / 4);
  let grid = '';
  for (let v = Math.ceil(yMin / step) * step; v <= yMax; v += step) grid += `<line x1="${L}" x2="${W - R}" y1="${Y(v).toFixed(1)}" y2="${Y(v).toFixed(1)}"/><text x="${L - 6}" y="${(Y(v) + 4).toFixed(1)}" text-anchor="end">${v < 0 ? '-' : ''}${lakhShort(Math.abs(v))}</text>`;
  const first = new Date(x0 * 86400000), span = (x1 - x0) / 365;
  let axis = '';
  for (let y = first.getUTCFullYear() + 1; ; y++) {
    const jan = Date.UTC(y, 0, 1) / 86400000;
    if (jan > x1) break;
    if (span > 12 && y % 2) continue;   // a phone cannot fit twenty year labels
    axis += `<text x="${X(jan).toFixed(1)}" y="${H - 8}" text-anchor="middle">${y}</text>`;
  }
  const paths = series.map(s => {
    let d = '', pen = false;
    for (const [day, v] of s.points) { if (v == null) { pen = false; continue; } d += (pen ? 'L' : 'M') + X(day).toFixed(1) + ' ' + Y(v).toFixed(1); pen = true; }
    return `<path class="${s.cls}" d="${d}"${s.stroke ? ` style="stroke:${s.stroke}"` : ''}/>`;
  }).join('');
  el.innerHTML = `<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="${h(opts.label || '')}"><g class="pf-axis">${grid}${axis}</g>${paths}<line class="pf-hover" x1="0" x2="0" y1="${T}" y2="${H - B}" style="display:none"/><rect x="${L}" y="${T}" width="${W - L - R}" height="${H - T - B}" fill="transparent"/></svg><p class="pf-readout"></p>`;
  const svg = el.querySelector('svg'), zone = el.querySelector('rect'), line = el.querySelector('.pf-hover'), out = el.querySelector('.pf-readout');
  const days = series[0].points.map(p => p[0]);
  const show = e => {
    const pt = e.touches ? e.touches[0] : e, rect = svg.getBoundingClientRect();
    const day = x0 + ((pt.clientX - rect.left) / rect.width * W - L) / (W - L - R) * (x1 - x0);
    let best = 0; for (let i = 1; i < days.length; i++) if (Math.abs(days[i] - day) < Math.abs(days[best] - day)) best = i;
    line.setAttribute('x1', X(days[best]).toFixed(1)); line.setAttribute('x2', X(days[best]).toFixed(1)); line.style.display = '';
    out.innerHTML = `<b>${fmtDay(days[best])}</b> · ` + series.map(s => `${h(s.label)} <b>${inr(s.points[best][1])}</b>`).join(' · ');
  };
  zone.addEventListener('mousemove', show);
  zone.addEventListener('touchstart', show, { passive: true });
  zone.addEventListener('touchmove', show, { passive: true });
  zone.addEventListener('mouseleave', () => { line.style.display = 'none'; out.textContent = ''; });
}
function sumPaths(paths) {   // a null point (before the fund existed) adds nothing
  if (!paths.length) return [];
  return paths[0].map((_, i) => { const vals = paths.map(p => p[i][1]).filter(v => v != null); return [paths[0][i][0], vals.length ? vals.reduce((a, v) => a + v, 0) : null]; });
}
const legend = items => items.map(([cls, style, label]) => `<span><i class="pf-sw ${cls}"${style ? ` style="${style}"` : ''}></i>${label}</span>`).join('');
function renderGrowth() {
  const R = RESULT;
  if (!R.positions.length) { $('growth-chart').innerHTML = '<p class="empty">Every fund is switched off.</p>'; $('growth-legend').innerHTML = ''; return; }
  const days = PE.monthEnds(Math.min(...R.positions.map(x => x.p.firstBuy)), R.asOf);
  const withBench = R.positions.filter(x => x.benchReplay);
  const bench = withBench.length === R.positions.length ? sumPaths(withBench.map(x => PE.valuePath(x.benchReplay.events, x.bench, days))) : null;
  const series = [{ points: sumPaths(R.positions.map(x => PE.valuePath(x.p.events, x.series, days))), cls: 'pf-fl', label: 'Your funds' }];
  if (bench) series.push({ points: bench, cls: 'pf-bl', label: 'Benchmarks' });
  const rest = unscoped(), restPath = rest.length ? sumPaths(rest.map(x => PE.valuePath(x.p.events, x.series, days))) : null;
  for (const c of COMPARE) if (c.result) {
    const moved = PE.valuePath(c.result.events, c.series, days);
    series.push({ points: restPath ? moved.map(([d, v], i) => [d, v == null && restPath[i][1] == null ? null : (v || 0) + (restPath[i][1] || 0)]) : moved, cls: 'pf-fl', label: (restPath ? 'With the moved funds in ' : 'All of it in ') + c.label, stroke: c.color });
  }
  series.push({ points: PE.investedPath(R.positions.flatMap(x => x.p.flows), days), cls: 'pf-inv', label: 'Net invested' });
  lineChart($('growth-chart'), series, { label: 'Portfolio value by month' });
  $('growth-legend').innerHTML = legend([['pf-fl', '', 'Your funds'], ...(bench ? [['pf-bl', '', "Same money in each fund's benchmark"]] : []),
    ...COMPARE.filter(c => c.result).map(c => ['pf-fl', `background:${c.color}`, (restPath ? 'With the moved funds in ' : 'All of it in ') + h(c.label)]), ['pf-inv', '', 'Net invested (buys less sells)']]) +
    (bench ? '' : '<span>Benchmark line left out: not every fund has one</span>');
}

// ---- compare with any fund or index -----------------------------------------------------------------
const CMP_COLORS = ['#7c3aed', '#0d9488', '#b45309', '#be185d', '#4d7c0f', '#0369a1'];
let COMPARE = [];        // [{kind: 'fund'|'index', code, label, result, color}]
let SCOPE = new Set();   // indices into RESULT.all whose flows are moved into the pick
const scoped = () => RESULT.positions.filter(x => SCOPE.has(RESULT.all.indexOf(x)));
const unscoped = () => RESULT.positions.filter(x => !SCOPE.has(RESULT.all.indexOf(x)));
function renderScope() {
  const all = scoped().length === RESULT.positions.length;
  $('scope').innerHTML = `<button type="button" data-scope="all" aria-pressed="${all}">All ${RESULT.positions.length}</button>` +
    RESULT.positions.map(x => { const i = RESULT.all.indexOf(x); return `<button type="button" data-scope="${i}" aria-pressed="${SCOPE.has(i)}" title="${h(x.name)}">${h(x.name.length > 34 ? x.name.slice(0, 32) + '…' : x.name)}</button>`; }).join('');
  $('scope').querySelectorAll('button').forEach(b => b.addEventListener('click', async () => {
    if (b.dataset.scope === 'all') { if (all) SCOPE.clear(); else RESULT.positions.forEach(x => SCOPE.add(RESULT.all.indexOf(x))); }
    else { const i = +b.dataset.scope; if (SCOPE.has(i)) SCOPE.delete(i); else SCOPE.add(i); }
    renderScope();
    for (const c of COMPARE) { c.result = null; c.note = ''; await computeComparator(c); }
    renderCompare(); renderGrowth();
  }));
}
async function computeComparator(entry) {
  const series = entry.series || await loadSeries(entry.kind, entry.code);
  if (!series) { entry.note = 'no usable history'; return; }
  entry.series = series;
  const flows = scoped().flatMap(x => x.p.flows);
  if (!flows.length) { entry.note = 'choose at least one of your funds to move'; return; }
  const first = Math.min(...flows.map(f => f[0])), lastFlow = Math.max(...flows.map(f => f[0]));
  if (series.first > first) { entry.note = `not around for the first purchase you are moving (history starts ${fmtDay(series.first)})`; return; }
  if (series.last < lastFlow || RESULT.asOf - series.last > 30) { entry.note = `history ends ${fmtDay(series.last)}${series.last < lastFlow ? ', before your last transaction' : ''}: cannot be valued today`; return; }
  entry.result = PE.replayFlows(flows, series, RESULT.asOf, 400);
  if (!entry.result) entry.note = 'could not replay every flow';
  else if (entry.result.shortfall > 1) entry.note = `${inr(entry.result.shortfall)} of your withdrawals exceeded what this would have held`;
}
function pickCandidates(q) {
  const words = q.toLowerCase().split(/\s+/).filter(Boolean);
  if (!words.length || !MAP) return [];
  // every word must appear; a word that is a whole token ("50" in "NIFTY 50", not in "250") scores higher
  const match = s => { const l = s.toLowerCase(), toks = l.split(/[^a-z0-9]+/); if (!words.every(w => l.includes(w))) return 0; return 1 + words.filter(w => toks.includes(w)).length; };
  const idx = [];
  for (const [stem, ix] of Object.entries(MAP.indexes)) { const m = match(ix.n + ' ' + stem); if (m) idx.push({ kind: 'index', code: stem, label: ix.n, sub: 'index', score: m, first: PE.dayNum(ix.f) }); }
  idx.sort((a, b) => b.score - a.score || a.label.length - b.label.length);
  const funds = [];
  for (const [code, f] of Object.entries(MAP.funds)) {
    const m = match(f.n);
    if (m) funds.push({ kind: 'fund', code, label: f.n, first: null, sub: `${(MAP.amfi_categories[f.a] || '').replace(/^.*? - /, '')} · scheme ${code}`, score: m * 10 + (f.o === 'growth' ? 2 : 0) + (f.p === 'direct' ? 1 : 0) + (f.k ? 2 : 0) });
  }
  funds.sort((a, b) => b.score - a.score || a.label.localeCompare(b.label));
  return idx.slice(0, 4).concat(funds.slice(0, 12));
}
async function addComparator(c) {
  if (!RESULT || COMPARE.some(x => x.kind === c.kind && x.code === c.code) || COMPARE.length >= 6) return;
  const entry = { ...c, color: CMP_COLORS[COMPARE.length % CMP_COLORS.length], result: null, note: '' };
  COMPARE.push(entry);
  renderCompare();
  await computeComparator(entry);
  renderCompare(); renderGrowth();
}
function renderCompare() {
  $('chips').innerHTML = COMPARE.map((c, i) => `<span class="tag"><i class="pf-sw" style="background:${c.color}"></i>${h(c.label)} <button type="button" data-rm="${i}" aria-label="Remove ${h(c.label)}">×</button></span>`).join('');
  $('chips').querySelectorAll('[data-rm]').forEach(b => b.addEventListener('click', () => { COMPARE.splice(+b.dataset.rm, 1); COMPARE.forEach((c, n) => c.color = CMP_COLORS[n % CMP_COLORS.length]); renderCompare(); renderGrowth(); }));
  const T = RESULT.totals, sel = scoped(), rest = unscoped(), all = !rest.length;
  const selValue = sel.reduce((a, x) => a + x.actual.value, 0), restValue = rest.reduce((a, x) => a + x.actual.value, 0);
  const selXirr = PE.xirr(sel.flatMap(x => x.actual.flows));
  const rows = COMPARE.map(c => {
    const sw = `<i class="pf-sw" style="background:${c.color}"></i>`;
    if (!c.result) return `<tr><td>${sw}${h(c.label)}</td><td colspan="5"><small>${h(c.note || 'loading…')}</small></td></tr>`;
    const r = c.result, gain = r.value - selValue, swapped = all ? r.value : restValue + r.value;
    return `<tr><td>${sw}${h(c.label)}${c.note ? `<br><small>${h(c.note)}</small>` : ''}</td><td class="n">${inr(r.value)}</td><td class="n">${pct(r.xirr)}</td><td class="n ${sign(gain)}">${inrS(gain)}</td><td class="n ${sign(gain)}">${r.xirr != null && selXirr != null ? pp(r.xirr - selXirr) : '·'}</td><td class="n">${inr(swapped)}<br><small>${inrS(swapped - T.value)} vs today</small></td></tr>`;
  });
  $('cmp-card').hidden = !COMPARE.length;
  $('cmp').innerHTML = `<thead><tr><th>Money from ${all ? 'all your funds' : `${sel.length} of ${RESULT.positions.length} funds`} (${inr(sel.reduce((a, x) => a + x.p.bought, 0))} put in), if it had been in</th><th class="n">Value now</th><th class="n">XIRR</th><th class="n">Swap would give</th><th class="n">XIRR difference</th><th class="n">Whole portfolio if swapped</th></tr></thead><tbody>
    <tr><td>As you actually held ${all ? 'them' : 'those'}</td><td class="n">${inr(selValue)}</td><td class="n">${pct(selXirr)}</td><td class="n">·</td><td class="n">·</td><td class="n">${inr(T.value)}</td></tr>${rows.join('')}</tbody>`;
  $('cmp-note').textContent = COMPARE.length ? `Buys on your days at that day's NAV; sells take the same rupees out on your sell days.${all ? '' : ` The other ${rest.length} fund${rest.length === 1 ? ' stays' : 's stay'} as held.`} Valued on ${fmtDay(RESULT.asOf)}.` : '';
}
const pickInput = $('pick'), menu = $('pick-menu');
let menuIdx = -1, menuItems = [];
function showMenu() {
  menuItems = pickCandidates(pickInput.value);
  const flows = RESULT ? scoped().flatMap(x => x.p.flows) : [], firstBuy = flows.length ? Math.min(...flows.map(f => f[0])) : null;
  for (const c of menuItems) c.tooYoung = firstBuy != null && c.first != null && c.first > firstBuy;
  menu.innerHTML = menuItems.map((c, i) => `<button type="button" role="option" id="opt-${i}" ${c.tooYoung ? 'aria-disabled="true" class="pf-too-young"' : ''}>${h(c.label)}<small>${h(c.sub)}${c.first != null ? ' · ' + (c.tooYoung ? `starts ${monthYear(c.first)}, after your first purchase (${monthYear(firstBuy)})` : 'since ' + monthYear(c.first)) : ''}</small></button>`).join('');
  menu.hidden = !menuItems.length; menuIdx = menuItems.length ? 0 : -1; pickInput.setAttribute('aria-expanded', String(!menu.hidden));
  highlight();
  menu.querySelectorAll('button').forEach((b, i) => b.addEventListener('mousedown', e => { e.preventDefault(); choose(menuItems[i]); }));
}
function highlight() {
  menu.querySelectorAll('button').forEach((b, i) => b.classList.toggle('on', i === menuIdx));
  if (menuIdx >= 0) pickInput.setAttribute('aria-activedescendant', `opt-${menuIdx}`); else pickInput.removeAttribute('aria-activedescendant');
}
function choose(c) { if (!c || c.tooYoung) return; pickInput.value = ''; menu.hidden = true; pickInput.setAttribute('aria-expanded', 'false'); addComparator(c); }
pickInput.addEventListener('input', showMenu);
pickInput.addEventListener('blur', () => setTimeout(() => { menu.hidden = true; pickInput.setAttribute('aria-expanded', 'false'); }, 150));
pickInput.addEventListener('keydown', e => {
  if (e.key === 'ArrowDown' || e.key === 'ArrowUp') { e.preventDefault(); if (menu.hidden) showMenu(); menuIdx = Math.max(0, Math.min(menuItems.length - 1, menuIdx + (e.key === 'ArrowDown' ? 1 : -1))); highlight(); }
  else if (e.key === 'Enter' && !menu.hidden) { e.preventDefault(); choose(menuItems[Math.max(0, menuIdx)]); }
  else if (e.key === 'Escape') { menu.hidden = true; pickInput.setAttribute('aria-expanded', 'false'); }
});

// ---- the detail drawer: a native modal dialog (focus, Escape and inert page handled by the browser) ----
const drawer = $('drawer');
function show(html, chartLines, wireUp) {
  $('drawer-inner').innerHTML = html;
  if (!drawer.open) drawer.showModal();
  drawer.scrollTop = 0;
  lineChart($('d-chart'), chartLines, { label: 'Value by month', height: 220 });
  drawer.querySelectorAll('[data-close]').forEach(b => b.addEventListener('click', () => drawer.close()));
  wireUp();
}
drawer.addEventListener('click', e => { if (e.target === drawer) drawer.close(); });   // a click on the backdrop
const tradeRows = x => RESULT.merged.trades.filter(t => t.isin === x.p.isin).map(t => `<tr><td>${fmtDay(t.day)}</td><td>${t.type}</td><td class="n">${t.qty.toFixed(3)}</td><td class="n">${t.price.toFixed(4)}</td><td class="n">${inr(t.qty * t.price)}</td></tr>`).join('');
function openDrawer(i) {
  const x = RESULT.all[i]; if (!x) return;
  const A = x.actual, B = x.benchReplay, pos = ORDER.indexOf(i), closed = x.p.units <= 1e-8;
  const peerRows = x.peers.map((q, n) => `<tr><td>${n + 1 + (x.rank && n + 1 >= x.rank ? 1 : 0)}</td><td><button type="button" class="pf-link" data-peer-code="${h(q.code)}">${h(q.name)}</button></td><td class="n">${inr(q.replay.value)}</td><td class="n">${pct(q.replay.xirr)}</td><td class="n ${sign(A.value - q.replay.value)}">${inrS(q.replay.value - A.value)}</td></tr>`);
  if (x.rank) peerRows.splice(x.rank - 1, 0, `<tr class="pf-you"><td>${x.rank}</td><td><b>${h(x.name)}</b> (yours)</td><td class="n">${inr(A.value)}</td><td class="n">${pct(A.xirr)}</td><td class="n">·</td></tr>`);
  const nb = x.p.buys.length, ns = x.p.sells.length;
  const kpis = [
    closed ? kpi(inr(x.p.realised), `realised from ${inr(x.p.bought)} put in · gain ${inrS(x.p.realised - x.p.bought)} · sold out ${fmtDay(x.p.lastTrade)}`)
           : kpi(inr(A.value), `value of ${x.p.units.toFixed(3)} units · cost ${inr(x.p.invested)} · gain ${inrS(A.gain)}`),
    kpi(pct(A.xirr), `XIRR${x.p.realised && !closed ? ` · ${inr(x.p.realised)} realised in sells` : closed ? ' over the holding period' : ''}`),
    B ? kpi(inrS(A.value - B.value), `against ${h(x.benchName)}: same money would be ${inr(B.value)} (XIRR ${pct(B.xirr)})`, sign(A.value - B.value)) : kpi('·', 'no benchmark for this fund on this site'),
    x.rank ? kpi(`${ord(x.rank)} of ${x.peerCount}`, `among ${h(x.cat.label)} ${x.plan} peers with your exact cash flows · median peer ${inr(x.peerMedian)}`) : '',
    x.cpiReplay ? kpi(inrS(A.value - x.cpiReplay.value), `above inflation: the same rupees indexed to CPI would be ${inr(x.cpiReplay.value)}`, sign(A.value - x.cpiReplay.value)) : '',
    x.direct ? kpi(inrS(A.value - x.direct.replay.value), `versus the direct plan (${inr(x.direct.replay.value)}): the cost of the regular plan so far`, sign(A.value - x.direct.replay.value)) : '',
  ].join('');
  const peers = x.peers.length ? `<h3>Every ${h(x.cat.label)} ${x.plan} peer with your cash flows</h3><div class="scroll-x"><table><thead><tr><th>#</th><th>Fund</th><th class="n">Value</th><th class="n">XIRR</th><th class="n">vs yours</th></tr></thead><tbody>${peerRows.join('')}</tbody></table></div><p class="sub">Each peer received your buys on your days and had the same fraction of units sold on your sell days.${x.peersSkipped ? ` ${x.peersSkipped} peer${x.peersSkipped === 1 ? ' was' : 's were'} not around for your first purchase and ${x.peersSkipped === 1 ? 'is' : 'are'} left out.` : ''} Peers are the funds on <a href="/compare/${encodeURIComponent(x.key + (x.plan === 'regular' ? '-regular' : ''))}">the ${h(x.cat.label)} comparison</a>.</p>`
    : `<p class="sub">${x.key ? `No peer set for ${h(x.cat.label)} ${x.plan} plans on this site.` : 'This fund is outside every category this site compares, so there is no peer set.'}</p>`;
  const lines = [{ points: null, cls: 'pf-fl', label: x.name }];
  const days = PE.monthEnds(x.p.firstBuy, RESULT.asOf);
  lines[0].points = PE.valuePath(x.p.events, x.series, days);
  if (B) lines.push({ points: PE.valuePath(B.events, x.bench, days), cls: 'pf-bl', label: x.benchName });
  if (x.direct) lines.push({ points: PE.valuePath(x.direct.replay.events, seriesSync('fund', x.direct.code), days), cls: 'pf-fl pf-direct', label: 'Direct plan' });
  lines.push({ points: PE.investedPath(x.p.flows, days), cls: 'pf-inv', label: 'Net invested' });
  show(`<div class="pf-dhead"><div><p class="sub">${h(x.cat ? x.cat.group : 'Fund')} · ${x.plan} plan${x.benchName ? ` · vs ${h(x.benchName)}` : ''}</p>
      <h2 id="drawer-title">${h(x.name)}</h2>
      <p class="sub"><a href="/mf/${h(x.code)}">scheme ${h(x.code)}</a> · ISIN ${h(x.p.isin)} · ${nb} buy${nb === 1 ? '' : 's'}${ns ? `, ${ns} sell${ns === 1 ? '' : 's'}` : ''} since ${fmtDay(x.p.firstBuy)} · NAV ${A.nav.toFixed(4)} on ${fmtDay(A.navDay)}</p></div>
      <div class="pf-dnav"><button type="button" class="button ghost small" id="d-prev" ${pos <= 0 ? 'disabled' : ''}>Prev</button><button type="button" class="button ghost small" id="d-next" ${pos < 0 || pos >= ORDER.length - 1 ? 'disabled' : ''}>Next</button><button type="button" class="button ghost small" data-close aria-label="Close">Close</button></div></div>
    <div class="stat-row pf-kpis">${kpis}</div>
    <h3>Value by month</h3><div class="pf-chart" id="d-chart"></div><div class="pf-legend">${legend([['pf-fl', '', h(x.name)], ...(B ? [['pf-bl', '', h(x.benchName)]] : []), ...(x.direct ? [['pf-fl pf-direct', '', 'Direct plan']] : []), ['pf-inv', '', 'Net invested']])}</div>
    ${peers}
    ${x.years.length ? `<h3>Year by year against ${h(x.benchName)}</h3>${x.benchNote ? `<p class="pf-warn">${h(x.benchNote)}</p>` : ''}${yearsBlock(x.years, x.series, x.bench, x.benchName, A.navDay, x.passive ? ' An index fund lags its own total-return index by its costs and tracking error every year; the gap is the tracking difference.' : x.behindStreak >= 2 ? ` <b>Behind the benchmark ${x.behindStreak} completed years running.</b>` : '')}` : ''}
    <h3>Trades</h3><div class="scroll-x"><table><thead><tr><th>Date</th><th>Type</th><th class="n">Units</th><th class="n">NAV</th><th class="n">Amount</th></tr></thead><tbody>${tradeRows(x)}</tbody></table></div>`,
    lines, () => {
      drawer.querySelectorAll('[data-peer-code]').forEach(b => b.addEventListener('click', () => openPeerDrawer(i, b.dataset.peerCode)));
      $('d-prev').addEventListener('click', () => openDrawer(ORDER[pos - 1]));
      $('d-next').addEventListener('click', () => openDrawer(ORDER[pos + 1]));
    });
}
function openPeerDrawer(i, code) {
  const x = RESULT.all[i]; if (!x) return;
  const q = x.peers.find(q => q.code === code), series = seriesSync('fund', code);
  if (!q || !series) return;
  const years = x.bench ? yearByYear(series, x.bench, x.actual.navDay) : [], streak = streakBehind(years), r = q.replay, n = x.peers.indexOf(q) + 1;
  const days = PE.monthEnds(x.p.firstBuy, RESULT.asOf);
  const lines = [{ points: PE.valuePath(r.events, series, days), cls: 'pf-fl pf-direct', label: q.name }, { points: PE.valuePath(x.p.events, x.series, days), cls: 'pf-fl', label: x.name }];
  if (x.benchReplay) lines.push({ points: PE.valuePath(x.benchReplay.events, x.bench, days), cls: 'pf-bl', label: x.benchName });
  lines.push({ points: PE.investedPath(x.p.flows, days), cls: 'pf-inv', label: 'Net invested' });
  show(`<div class="pf-dhead"><div><p class="sub">${h(x.cat.label)} · ${x.plan} plan · a peer of ${h(x.name)}</p>
      <h2 id="drawer-title">${h(q.name)}</h2>
      <p class="sub"><a href="/mf/${h(code)}">scheme ${h(code)}</a> · NAV since ${fmtDay(series.first)} · with your ${x.p.buys.length} buy${x.p.buys.length === 1 ? '' : 's'}${x.p.sells.length ? ` and ${x.p.sells.length} sell${x.p.sells.length === 1 ? '' : 's'}` : ''} in ${h(x.name)}</p></div>
      <div class="pf-dnav"><button type="button" class="button ghost small" id="d-back">Back</button><button type="button" class="button ghost small" data-close aria-label="Close">Close</button></div></div>
    <div class="stat-row pf-kpis">
      ${kpi(inr(r.value), `your cash flows in this fund · cost ${inr(r.invested)} · ${inrS(r.value - x.actual.value)} vs ${h(x.name)}`, sign(r.value - x.actual.value))}
      ${kpi(pct(r.xirr), `XIRR with your cash flows · yours ${pct(x.actual.xirr)}`)}
      ${x.rank ? kpi(`${ord(n + (n > x.rank - 1 ? 1 : 0))} of ${x.peerCount}`, `among ${h(x.cat.label)} ${x.plan} funds with your cash flows · yours is ${ord(x.rank)}`) : ''}
    </div>
    <h3>Value by month, your cash flows</h3><div class="pf-chart" id="d-chart"></div><div class="pf-legend">${legend([['pf-fl pf-direct', '', h(q.name)], ['pf-fl', '', h(x.name) + ' as held'], ...(x.benchReplay ? [['pf-bl', '', h(x.benchName)]] : []), ['pf-inv', '', 'Net invested']])}</div>
    ${years.length ? `<h3>Year by year against ${h(x.benchName)}</h3>${yearsBlock(years, series, x.bench, x.benchName, x.actual.navDay, streak >= 2 ? ` <b>Behind the benchmark ${streak} completed years running.</b>` : '')}` : ''}`,
    lines, () => $('d-back').addEventListener('click', () => openDrawer(i)));
}

// ---- input: the File API only; the file's text goes only to PE.parseTradebook -----------------------
const input = $('file'), drop = $('drop');
const run = files => analyse(files).catch(err => status('Something went wrong: ' + h(err.message), true));
input.addEventListener('change', () => { if (input.files.length) run([...input.files]); input.value = ''; });
['dragenter', 'dragover'].forEach(ev => drop.addEventListener(ev, e => { e.preventDefault(); drop.classList.add('over'); }));
['dragleave', 'drop'].forEach(ev => drop.addEventListener(ev, e => { e.preventDefault(); drop.classList.remove('over'); }));
drop.addEventListener('drop', e => { const files = [...e.dataTransfer.files].filter(f => /\.csv$/i.test(f.name) || f.type === 'text/csv'); if (files.length) run(files); else status('Drop .csv files exported from Console.', true); });
$('reset').addEventListener('click', () => { RESULT = null; COMPARE = []; $('results').hidden = true; $('kpis').innerHTML = ''; $('warnings').innerHTML = ''; $('reset').hidden = true; status(''); });
loadMap().catch(() => {});
let frame = 0;
window.addEventListener('resize', () => { if (!RESULT) return; cancelAnimationFrame(frame); frame = requestAnimationFrame(renderGrowth); });
})();
