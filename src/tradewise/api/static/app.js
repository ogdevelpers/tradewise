// Tradewise web UI: plain ES module, no build step. Written for people new to the stock market.
const view = document.getElementById("view");

// ---------------------------------------------------------------- plain words
const PLAN = {
  trend_pullback: {
    name: "Buy the dip", tag: "Short trades of 1–4 weeks · check every evening",
    desc: "Buys a share that has been rising for months when it dips a little and starts rising again. Every trade has a safety exit.",
  },
  momentum_rotation: {
    name: "Hold the strongest", tag: "Long holds · check once a month",
    desc: "Owns the 15 shares that rose most steadily over the past year. Once a month it swaps out any that have weakened.",
  },
  low_volatility: {
    name: "Steady shares", tag: "Calm, long holds · check once a month",
    desc: "Owns the 15 calmest large shares that are still rising. Calm shares have usually fallen less when the market drops. Once a month it swaps out any that changed.",
  },
  combined: { name: "Mix of plans", desc: "Part of the money in each plan." },
  benchmark: { name: "Just buy Nifty 50 and hold", desc: "Doing nothing clever: buy the index once and keep it." },
};
const COLORS = { trend_pullback: "#1F4FD1", momentum_rotation: "#B4430A", low_volatility: "#7A3EB1", combined: "#0B7A5C", benchmark: "#9A9CA3" };
const PLAN_KEYS = ["low_volatility", "momentum_rotation", "trend_pullback"];
const MIX = { low_volatility: 0.4, momentum_rotation: 0.4, trend_pullback: 0.2 };  // default share of money in the mix
const baseKey = (k) => String(k).replace(/:no_safety$/, "");
const planName = (k) => (PLAN[baseKey(k)]?.name || k) + (String(k).endsWith(":no_safety") ? " (safety switch off)" : "");

function plainReason(r = "") {
  const m = r.match(/Momentum rank (\d+)/);
  if (m) return `one of the strongest shares (#${m[1]})`;
  const st = r.match(/Steadiness rank (\d+)/);
  if (st) return `one of the calmest rising shares (#${st[1]})`;
  if (/^Stop-loss hit/.test(r)) return "safety exit (stop-loss) reached";
  if (/^Gap below/.test(r)) return "price opened below the safety exit";
  if (/^Profit target/.test(r)) return "profit step reached, sold half";
  if (/EMA after target/.test(r)) return "sold the rest after the price slipped below its 20-day average";
  if (/^Closed below \d+ DMA/.test(r)) return "trend weakened (closed below its 50-day average)";
  if (/^Pullback/.test(r)) return "bought the dip in a rising share";
  if (/^Dropped out/.test(r)) return "no longer among the strongest shares";
  if (/bearish/.test(r)) return "the whole market started falling";
  return r;
}

// ---------------------------------------------------------------- utilities
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const pct = (x, d = 1) => (x === null || x === undefined || !isFinite(x) ? "—" : `${(x * 100).toFixed(d)}%`);
const spct = (x, d = 1) => (x === null || x === undefined || !isFinite(x) ? "—" : `${x > 0 ? "+" : ""}${(x * 100).toFixed(d)}%`);
const num = (x, d = 2) => (x === null || x === undefined || !isFinite(x) ? "—" : Number(x).toLocaleString("en-IN", { minimumFractionDigits: d, maximumFractionDigits: d }));
const inr = (x, d = 0) => (x === null || x === undefined || !isFinite(x) ? "—" : "₹" + num(x, d));
const price = (x) => inr(x, x >= 1000 ? 0 : 2);
const cls = (x) => (x > 0 ? "pos" : x < 0 ? "neg" : "");
const day = (s) => { if (!s) return "—"; const d = new Date(s + "T00:00:00"); return isNaN(d) ? s : d.toLocaleDateString("en-IN", { weekday: "short", day: "numeric", month: "short", year: "numeric" }); };
const help = (term) => `<a class="q" href="#/learn/${term}" title="What does this mean?" aria-label="What does this mean?">?</a>`;
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const store = {
  get(k, d) { try { const v = localStorage.getItem("tw_" + k); return v === null ? d : JSON.parse(v); } catch { return d; } },
  set(k, v) { try { localStorage.setItem("tw_" + k, JSON.stringify(v)); } catch { /* private mode: ignore */ } },
};
const budget = () => Math.max(10000, Number(store.get("budget", 100000)) || 100000);

async function api(path, opts = {}) {
  const res = await fetch(path, { headers: { "Content-Type": "application/json" }, ...opts });
  if (res.status === 204) return null;
  if (res.status === 401) { location.href = "/login"; throw new Error("Please log in."); }
  let body = null;
  try { body = await res.json(); } catch { /* not JSON */ }
  if (!res.ok) {
    const detail = body?.details ? body.details.map((d) => `${d.field}: ${d.message}`).join("; ") : "";
    throw new Error(body?.error ? `${body.error}${detail ? " — " + detail : ""}` : `Something went wrong (HTTP ${res.status})`);
  }
  return body;
}

async function runJob(path, body) {
  const job = await api(path, { method: "POST", body: body ? JSON.stringify(body) : undefined });
  if (job.status === "done") return job.result;      // hosted (serverless): finished inside the request
  if (job.status === "failed") throw new Error(job.error || "failed");
  for (let k = 0; k < 1200; k++) {
    const j = await api(`/api/jobs/${job.id}`);
    if (j.status === "done") return j.result;
    if (j.status === "failed") throw new Error(j.error || "failed");
    await sleep(500);
  }
  throw new Error("This is taking too long. Please try again.");
}

function toast(msg) {
  const t = document.getElementById("toast");
  t.textContent = msg;
  t.style.display = "block";
  clearTimeout(toast._t);
  toast._t = setTimeout(() => (t.style.display = "none"), 6000);
}
const errorBox = (e) => `<div class="banner err" role="alert">${esc(e.message || e)}</div>`;

let META = null;
async function meta(force = false) {
  if (!META || force) META = await api("/api/meta");
  return META;
}
function demoBanner(m) {
  if (!m.has_data) return `<div class="verdict neutral"><span class="big">Getting prices for the first time…</span><p>Tradewise is downloading about 12 years of prices for 40 large Indian companies. This takes 1–3 minutes, and this page will update by itself.</p></div>`;
  if (m.is_demo) return `<div class="banner"><strong>You are looking at DEMO prices.</strong> They are made up so you can explore safely. Don't buy anything based on them. Tradewise keeps trying to download real prices and switches over by itself.</div>`;
  return "";
}

async function refreshPrices(btn) {
  btn.disabled = true;
  const old = btn.textContent;
  btn.textContent = "Updating…";
  try {
    const r = await runJob("/api/refresh");
    META = null;
    toast(r.message);
    pollStatus(true);
  } catch (e) { toast(e.message); btn.disabled = false; btn.textContent = old; }
}

// ------------------------------------------------------------- line chart
function lineChart(el, series, { height = 300, hlines = [], fmt = (v) => num(v) } = {}) {
  const W = 800, H = height, padL = 86, padR = 12, padT = 12, padB = 26;
  const all = series.flatMap((s) => s.points.filter((p) => p[1] !== null && p[1] !== undefined));
  if (!all.length) { el.innerHTML = '<p class="muted">No data to chart yet.</p>'; return; }
  const dates = [...new Set(series.flatMap((s) => s.points.map((p) => p[0])))].sort();
  const xi = new Map(dates.map((d, i) => [d, i]));
  let lo = Math.min(...all.map((p) => p[1]), ...hlines.map((h) => h.y));
  let hi = Math.max(...all.map((p) => p[1]), ...hlines.map((h) => h.y));
  if (lo === hi) { lo -= 1; hi += 1; }
  const padY = (hi - lo) * 0.05; lo -= padY; hi += padY;
  const x = (d) => padL + (dates.length < 2 ? 0 : (xi.get(d) / (dates.length - 1)) * (W - padL - padR));
  const y = (v) => padT + (1 - (v - lo) / (hi - lo)) * (H - padT - padB);
  const ticks = Array.from({ length: 5 }, (_, k) => lo + ((hi - lo) * k) / 4);
  const step = Math.max(1, Math.floor(dates.length / 6));
  let svg = `<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="${esc(series.map((s) => s.name).join(", "))} chart">`;
  for (const t of ticks) svg += `<line x1="${padL}" x2="${W - padR}" y1="${y(t)}" y2="${y(t)}" stroke="#EFEFEA"/><text x="${padL - 8}" y="${y(t) + 4}" text-anchor="end" font-size="11" fill="#5B5E66" font-family="JetBrains Mono, monospace">${esc(fmt(t))}</text>`;
  for (let i = 0; i < dates.length; i += step) svg += `<text x="${x(dates[i])}" y="${H - 6}" text-anchor="middle" font-size="11" fill="#5B5E66">${esc(dates[i].slice(0, 7))}</text>`;
  for (const h of hlines) svg += `<line x1="${padL}" x2="${W - padR}" y1="${y(h.y)}" y2="${y(h.y)}" stroke="${h.color}" stroke-dasharray="6 5" stroke-width="1.5"/><text x="${W - padR - 4}" y="${y(h.y) - 6}" text-anchor="end" font-size="12" fill="${h.color}">${esc(h.label)}</text>`;
  for (const s of series) {
    let d = "", pen = false;
    for (const [dt, v] of s.points) {
      if (v === null || v === undefined) { pen = false; continue; }
      d += `${pen ? "L" : "M"}${x(dt).toFixed(1)},${y(v).toFixed(1)}`; pen = true;
    }
    svg += `<path d="${d}" fill="none" stroke="${s.color}" stroke-width="${s.width || 2}" ${s.dash ? 'stroke-dasharray="6 5"' : ""}/>`;
  }
  svg += `<line class="cross" x1="0" x2="0" y1="${padT}" y2="${H - padB}" stroke="#16181D" stroke-opacity=".25" visibility="hidden"/></svg>`;
  el.classList.add("chart");
  el.innerHTML = svg + '<div class="tip"></div>';
  const svgEl = el.querySelector("svg"), tip = el.querySelector(".tip"), cross = el.querySelector(".cross");
  const lookup = series.map((s) => new Map(s.points));
  svgEl.addEventListener("mousemove", (ev) => {
    const r = svgEl.getBoundingClientRect();
    const px = ((ev.clientX - r.left) / r.width) * W;
    const i = Math.round(((px - padL) / (W - padL - padR)) * (dates.length - 1));
    if (i < 0 || i >= dates.length) return;
    const d = dates[i];
    cross.setAttribute("x1", x(d)); cross.setAttribute("x2", x(d)); cross.setAttribute("visibility", "visible");
    tip.innerHTML = `${esc(day(d))}<br>` + series.map((s, k) => (lookup[k].get(d) == null ? "" : `<span style="color:${s.color}">■</span> ${esc(s.name)} ${esc(fmt(lookup[k].get(d)))}`)).filter(Boolean).join("<br>");
    tip.style.left = `${Math.min(80, Math.max(20, (x(d) / W) * 100))}%`; tip.style.top = `${(padT / H) * 100 + 10}%`; tip.style.display = "block";
  });
  svgEl.addEventListener("mouseleave", () => { tip.style.display = "none"; cross.setAttribute("visibility", "hidden"); });
}
const legend = (items) => `<div class="legend">${items.map((i) => `<span><i style="background:${i.color}"></i>${esc(i.name)}</span>`).join("")}</div>`;
const checklist = (list) => `<ul class="rules">${list.map((r) => `<li><span class="ic ${r.pass ? "ok" : "no"}" aria-hidden="true">${r.pass ? "✓" : "✗"}</span><span><span class="sr">${r.pass ? "Yes" : "Not yet"}: </span>${esc(r.plain || r.rule)}${r.why ? `<span class="why">${esc(r.why)}</span>` : ""}</span></li>`).join("")}</ul>`;

function planChoice(name, selected, inputName = "plan") {
  return PLAN_KEYS.map((k) => [k, PLAN[k]]).map(([k, p]) => `
    <label class="choice"><input type="radio" name="${inputName}" value="${k}" ${k === selected ? "checked" : ""}>
      <span><strong>${esc(p.name)}</strong><span class="d">${esc(p.tag)}</span><br><span class="d">${esc(p.desc)}</span></span></label>`).join("");
}

function welcome(m = {}) {
  if (store.get("welcome_closed", false)) return "";
  return `<section class="welcome" aria-labelledby="w-h"><h2 id="w-h" style="color:#fff">New to the stock market? Start here</h2>
    <ol><li><a href="#/learn">Learn the basics</a>: 5 minutes on what shares, stop-losses and risk mean.</li>
      <li><a href="#/past">Test on past data</a>: see how each plan would have done since 2016.</li>
      <li><a href="#/practice">Practise with fake money</a> for about 3 months.</li>
      <li>${m.serverless ? "That's all. Tradewise runs online and fetches the latest prices by itself every weekday evening. Open your Tradewise website any time, on your phone too." : "That's all. Tradewise starts with your computer and fetches the latest prices by itself after the market closes. Just open it from the desktop icon whenever you like."}</li></ol>
    <button type="button" id="close-welcome">Got it</button></section>`;
}

// ------------------------------------------------------------------- TODAY
async function todayPage() {
  const m = await meta();
  if (!m.has_data) { view.innerHTML = `<h1>Today</h1>${demoBanner(m)}`; return; }
  const plan = store.get("plan", "trend_pullback");
  const cap = budget();
  const [mk, sig, paper] = await Promise.all([api("/api/market"), api(`/api/signals?strategy=${plan}&capital=${cap}`), api("/api/paper")]);
  const rising = mk.regime === "uptrend";
  const P = PLAN[plan];

  let ideas = "";
  if (plan === "trend_pullback") {
    if (!sig.market_rising) {
      ideas = `<div class="verdict neutral"><span class="big">No buying today</span><p>The overall market is falling, so this plan waits. Not buying is a good decision when the odds are poor. Keep checking each evening.</p></div>`;
    } else if (!sig.entries.length) {
      ideas = `<div class="verdict neutral"><span class="big">Nothing to buy today</span><p>No share passed all 7 checks. That's normal: good chances come a few times a month. Doing nothing is fine.</p></div>`;
    } else {
      ideas = sig.entries.map((e) => e.qty >= 1 ? `
        <article class="idea"><div class="top"><span class="name">${esc(e.symbol)}</span><span class="badge buy">Idea to buy</span></div>
          <span class="small muted">${esc(e.sector)}</span>
          <ul><li>Buy about <strong>${e.qty} shares</strong> at around <strong>${price(e.close)}</strong> when the market opens (about ${inr(e.value)} in total).</li>
            <li>Safety exit ${help("stop-loss")}: if the price falls to <strong>${price(e.stop)}</strong>, sell all of it. The most you would lose is about <strong>${inr(e.risk)}</strong> (1% of your money).</li>
            <li>Profit step ${help("target")}: if it rises to <strong>${price(e.target)}</strong>, sell half. Sell the rest when the price closes below its 20-day average.</li></ul>
          <a href="#/stock/${encodeURIComponent(e.symbol)}">Why this share? See all 7 checks</a></article>` : `
        <article class="idea"><div class="top"><span class="name">${esc(e.symbol)}</span><span class="badge">Too expensive for your budget</span></div>
          <p style="margin:0">One share costs ${price(e.close)}, more than this plan puts into one share (20% of ${inr(cap)}). Skip it.</p></article>`).join("");
    }
  } else {
    const rows = (sig.rebalance_today ? sig.entries.map((e, i) => ({ rank: i + 1, ...e })) : sig.ranking);
    ideas = `${sig.rebalance_today
      ? `<div class="verdict good"><span class="big">Today is the monthly check</span><p>Buy any of these you don't own yet. Sell shares you own that are no longer in the top 30.</p></div>`
      : `<div class="verdict neutral"><span class="big">Nothing to do today</span><p>This plan only buys or sells on the first trading day of each month (next: ${esc(day(sig.next_rebalance))}, unless that is a market holiday). Below are the shares it would hold if you started today.</p></div>`}
      ${!sig.market_rising ? `<div class="banner">The overall market is falling, so this plan would not buy anything new right now. It only sells weak shares.</div>` : ""}
      <div class="scroll"><table><thead><tr><th>#</th><th>Share</th><th class="num">Price</th><th class="num">Shares to buy</th><th class="num">Amount</th></tr></thead><tbody>
      ${rows.map((r) => `<tr><td>${r.rank}</td><td><a href="#/stock/${encodeURIComponent(r.symbol)}"><strong>${esc(r.symbol)}</strong></a><div class="small muted">${esc(r.sector)}</div></td><td class="num">${price(r.close)}</td><td class="num">${r.qty >= 1 ? r.qty : "skip: price too high"}</td><td class="num">${r.qty >= 1 ? inr(r.value) : "—"}</td></tr>`).join("") || '<tr><td colspan="5" class="muted">No share qualifies right now.</td></tr>'}
      </tbody></table></div><p class="small muted" style="margin:0">Each share gets about 1/15 of ${inr(cap)} (${inr(cap / 15 * (sig.safety?.scale ?? 1))}${(sig.safety?.scale ?? 1) < 1 ? ", after the safety switch" : ""}). Shares costing more than that are skipped. There is no fixed safety exit: a share is sold when it drops out of the 30 strongest at a monthly check.</p>`;
  }

  const acct = paper[0];
  const safe = sig.safety;
  const safetyBox = safe ? `<div class="banner" style="${safe.scale < 1 ? "" : "background:#E9EAE4;color:#2E3139"}"><strong>Safety switch ${help("safety-switch")}:</strong> ${esc(safe.message)}${safe.scale < 1 ? " The amounts below already include this." : ""}</div>` : "";
  view.innerHTML = `
    <div><h1>Today</h1><p class="lead">Prices up to the market close on <strong>${esc(day(mk.date))}</strong>. Ideas below are for the next trading day. Tradewise updates prices by itself ${m.serverless ? "every weekday evening" : "after 4 pm on weekdays"}.</p></div>
    ${welcome(m)}
    ${demoBanner(m)}
    <div class="verdict ${rising ? "good" : "bad"}">
      <span class="big">${rising ? "The market is rising" : "The market is falling"}</span>
      <p>The Nifty 50 ${help("nifty")} is ${rising ? "above" : "below"} its average price of the last 200 days ${help("average")}. ${rising ? "This is a good time to look for shares to buy." : "Most shares fall with the market, so be careful and buy less."}</p>
      <p class="small">Nifty 50 today: ${num(mk.benchmark, 0)} (${spct(mk.change_1d, 2)} today, ${spct(mk.change_1m)} this month) · ${pct(mk.breadth_above_200dma, 0)} of shares are in an uptrend</p>
    </div>
    <section class="card" aria-labelledby="set-h"><h2 id="set-h">Your settings</h2>
      <div class="inline"><label class="f">How much money would you invest? (₹)<input id="budget" type="number" min="10000" step="10000" value="${cap}"></label>
        <p class="small muted" style="margin:0 0 10px">Only use money you won't need for the next 3–5 years.</p></div>
      <div class="cards" role="radiogroup" aria-label="Plan">${planChoice("plan", plan)}</div>
    </section>
    <section class="card" aria-labelledby="ideas-h"><h2 id="ideas-h">${esc(P.name)}: what to do next</h2>${safetyBox}${ideas}
      ${plan === "trend_pullback" && sig.watch.length ? `<details><summary>Almost ready (${sig.watch.length} shares close to a buy)</summary><div class="in">${sig.watch.slice(0, 8).map((w) => `<div><a href="#/stock/${encodeURIComponent(w.symbol)}"><strong>${esc(w.symbol)}</strong></a> · ${w.passed} of ${w.total} checks passed. <span class="muted">Still waiting for: ${esc((w.missing_plain || w.missing).join("; ").toLowerCase())}.</span></div>`).join("")}</div></details>` : ""}
    </section>
    <section class="card" aria-labelledby="pr-h"><h2 id="pr-h">Your practice account</h2>
      ${acct ? `<p style="margin:0;font-size:15px">Started with ${inr(acct.config.initial_capital)}. Now worth <strong>${inr(acct.summary.final_equity)}</strong> <span class="${cls(acct.summary.total_return)}">(${spct(acct.summary.total_return)})</span>. It owns ${acct.positions.length} share(s). <a href="#/practice">Open practice</a></p>`
        : `<p style="margin:0;font-size:15px">Before using real money, let the app trade with fake money for a few months. <a href="#/practice">Start practising</a></p>`}
    </section>
    <p class="small muted">Ideas come from fixed rules, not predictions. Expect roughly half of trades to lose a little. The rules keep each loss small so the winners can make up for them.</p>`;

  view.querySelector("#close-welcome")?.addEventListener("click", () => { store.set("welcome_closed", true); route(); });
  view.querySelector("#budget").addEventListener("change", (e) => {
    const v = Number(e.target.value);
    if (!(v >= 10000)) { toast("Please enter at least ₹10,000"); return; }
    store.set("budget", v); route();
  });
  view.querySelectorAll('input[name="plan"]').forEach((r) => r.addEventListener("change", () => { store.set("plan", r.value); route(); }));
}

// ------------------------------------------------------------- CHECK A SHARE
function rsiWords(r) {
  if (r == null) return "—";
  if (r >= 70) return `${num(r, 0)}: running hot. It often takes a rest after this.`;
  if (r <= 30) return `${num(r, 0)}: beaten down. Wait for it to steady before buying.`;
  if (r >= 50) return `${num(r, 0)}: healthy, more buyers than sellers lately.`;
  return `${num(r, 0)}: a bit weak, more sellers than buyers lately.`;
}

async function stockPage(symbol) {
  const m = await meta();
  if (!m.has_data) { view.innerHTML = `<h1>Check a share</h1>${demoBanner(m)}`; return; }
  const stocks = await api("/api/stocks");
  symbol = (symbol || store.get("last_stock", stocks[0]?.symbol) || "").toUpperCase();
  const a = await api(`/api/stocks/${encodeURIComponent(symbol)}`);
  store.set("last_stock", a.symbol);
  const t = a.technicals, s = a.signal, c = a.chart;
  const passed = a.checklist.filter((x) => x.pass).length, total = a.checklist.length;
  const yearAgo = c[0]?.close, hi = Math.max(...c.map((r) => r.high)), lo = Math.min(...c.map((r) => r.low));
  const cap = budget();
  const showAll = store.get("all_lines", false);
  view.innerHTML = `
    <div class="head" style="align-items:flex-end"><div><h1>Check a share</h1><p class="lead">Pick any share to see if it passes the "Buy the dip" checks, and what its price has been doing.</p></div>
      <label class="f" style="min-width:220px">Choose a share<select id="pick">${stocks.map((x) => `<option ${x.symbol === a.symbol ? "selected" : ""}>${esc(x.symbol)}</option>`).join("")}</select></label></div>
    ${demoBanner(m)}
    <div class="row">
      <section class="card grow2" aria-labelledby="ch-h">
        <div class="head"><div><span class="small muted">${esc(a.sector)}</span><h2 id="ch-h" style="font-size:26px">${esc(a.symbol)}</h2></div>
          <div style="text-align:right"><div class="mono" style="font-size:24px">${price(a.close)}</div><div class="small ${cls(a.change_1d)}">${spct(a.change_1d, 2)} on ${esc(day(a.date))}</div></div></div>
        <div id="chart"></div>
        ${legend([{ name: "Price", color: "#16181D" }, { name: "200-day average", color: "#9A9CA3" }, ...(showAll ? [{ name: "20-day average", color: "#1F4FD1" }, { name: "50-day average", color: "#B4430A" }] : [])])}
        <label class="check small"><input type="checkbox" id="lines" ${showAll ? "checked" : ""}> Show the 20-day and 50-day averages too</label>
        <p class="small muted" style="margin:0">How to read it: when the black price line stays above the grey 200-day average ${help("average")}, the share has been in a long-term uptrend.</p>
      </section>
      <section class="card dark grow1" aria-labelledby="sg-h">
        <h2 id="sg-h">${s.action === "BUY" ? "Passes all checks: idea to buy" : "Not a buy today"}</h2>
        <p style="margin:0;color:#D4D5CF">${passed} of ${total} checks passed${s.action === "BUY" ? "." : ". All must pass before the app suggests buying."}</p>
        ${s.action === "BUY" ? (() => {
          const qty = Math.min(Math.floor((cap * 0.01 * (s.size_scale ?? 1)) / (s.entry - s.stop)), Math.floor((cap * 0.2) / s.entry));
          return `<p style="margin:0">With ${inr(cap)}: buy about <strong>${qty >= 1 ? qty : 0} shares</strong> near ${price(s.entry)}. Safety exit at <strong>${price(s.stop)}</strong>, profit step at <strong>${price(s.target)}</strong>.</p>`;
        })() : ""}
        ${checklist(a.checklist)}
      </section>
    </div>
    <section class="card" aria-labelledby="facts-h"><h2 id="facts-h">Quick facts</h2><div class="mini">
      <div><span class="k">One year ago</span><span class="v">${price(yearAgo)}</span><span class="small ${cls(a.close / yearAgo - 1)}">${spct(a.close / yearAgo - 1)} since then</span></div>
      <div><span class="k">Highest in a year</span><span class="v">${price(hi)}</span><span class="small muted">now ${pct(a.close / hi - 1)} from it</span></div>
      <div><span class="k">Lowest in a year</span><span class="v">${price(lo)}</span></div>
      <div><span class="k">Normal daily move</span><span class="v">±${pct(t.atr_pct)}</span><span class="small muted">about ${price(t.atr14)} a day</span></div>
      <div><span class="k">Momentum (RSI) ${help("rsi")}</span><span class="small" style="font-size:13px">${esc(rsiWords(t.rsi14))}</span></div>
      <div><span class="k">vs Nifty, 3 months</span><span class="v ${cls(t.rel_strength_3m)}">${spct(t.rel_strength_3m)}</span><span class="small muted">${t.rel_strength_3m > 0 ? "doing better than the market" : "doing worse than the market"}</span></div>
    </div>
    <details><summary>Advanced numbers (for later)</summary><div class="in"><div class="mini">
      ${[["RSI (14)", num(t.rsi14, 1)], ["MACD / signal", `${num(t.macd)} / ${num(t.macd_signal)}`], ["ATR (14)", num(t.atr14)], ["ADX (14)", num(t.adx14, 1)], ["Volume vs 20-day avg", t.volume_vs_20d == null ? "—" : num(t.volume_vs_20d) + "×"], ["20-day avg (EMA)", num(t.ema20)], ["50-day avg", num(t.sma50)], ["200-day avg", num(t.sma200)]].map(([k, v]) => `<div><span class="k">${esc(k)}</span><span class="v">${esc(v)}</span></div>`).join("")}
    </div></div></details></section>`;
  const ser = (k, name, color, width) => ({ name, color, width, points: c.map((r) => [r.date, r[k]]) });
  const lines = [ser("close", "Price", "#16181D", 2.5), ser("sma200", "200-day average", "#9A9CA3")];
  if (showAll) lines.push(ser("ema20", "20-day average", "#1F4FD1"), ser("sma50", "50-day average", "#B4430A"));
  lineChart(view.querySelector("#chart"), lines, { fmt: (v) => num(v, v >= 1000 ? 0 : 2), hlines: s.action === "BUY" ? [{ y: s.stop, color: "#B4430A", label: "Safety exit" }] : [] });
  view.querySelector("#pick").addEventListener("change", (e) => (location.hash = `#/stock/${encodeURIComponent(e.target.value)}`));
  view.querySelector("#lines").addEventListener("change", (e) => { store.set("all_lines", e.target.checked); route(); });
}

// ---------------------------------------------------------- TEST ON PAST DATA
let LAST = null;
async function pastPage() {
  const m = await meta();
  if (!m.has_data) { view.innerHTML = `<h1>Test on past data</h1>${demoBanner(m)}`; return; }
  const firstYear = Number(m.first_date.slice(0, 4)) + 2, lastYear = Number(m.last_date.slice(0, 4)) - 1;
  const years = []; for (let y = firstYear; y <= lastYear; y++) years.push(y);
  const startYear = Math.min(Math.max(2016, firstYear), lastYear);
  view.innerHTML = `
    <div><h1>Test on past data</h1><p class="lead">What if you had followed these rules in the past? The app replays every trading day, including all charges and taxes ${help("charges")}. This is called a backtest ${help("backtest")}.</p></div>
    ${demoBanner(m)}
    <form id="bt" class="card stack" aria-labelledby="q-h">
      <h2 id="q-h">If I had started with…</h2>
      <div class="inline">
        <label class="f">Amount (₹)<input name="capital" type="number" min="10000" step="10000" value="${budget()}"></label>
        <label class="f">In the year<select name="year">${years.map((y) => `<option ${y === startYear ? "selected" : ""}>${y}</option>`).join("")}</select></label>
      </div>
      <fieldset><legend>Plans to compare</legend>
        <label class="check"><input type="checkbox" name="s" value="low_volatility" checked> <strong>Steady shares</strong> <span class="small muted">calm shares, monthly changes</span></label>
        <label class="check"><input type="checkbox" name="s" value="momentum_rotation" checked> <strong>Hold the strongest</strong> <span class="small muted">monthly changes</span></label>
        <label class="check"><input type="checkbox" name="s" value="trend_pullback" checked> <strong>Buy the dip</strong> <span class="small muted">short trades with a safety exit</span></label>
        <label class="check"><input type="checkbox" name="combined" checked> <strong>Mix of plans</strong> <span class="small muted">money split 40% Steady, 40% Strongest, 20% Dip (between the plans you tick)</span></label>
        <p class="small muted" style="margin:0">"Just buy Nifty 50 and hold" is always shown for comparison.</p>
      </fieldset>
      <fieldset><legend>Protection</legend>
        <label class="check"><input type="checkbox" name="safety" checked> <strong>Safety switch</strong> ${help("safety-switch")} <span class="small muted">buy smaller when the market is jumpy or the account is down</span></label>
        <label class="check"><input type="checkbox" name="nosafety"> <strong>Also show each plan without it</strong> <span class="small muted">to see what the switch changes</span></label>
      </fieldset>
      <details><summary>Advanced settings (optional)</summary><div class="in"><div class="grid2">
        <label class="f">Money at risk per trade (%) ${help("risk")}<input name="risk" type="number" min="0.1" max="5" step="0.1" value="1"></label>
        <label class="f">Price slippage (%)<input name="slip" type="number" min="0" max="2" step="0.05" value="0.1"></label>
        <label class="f">Compare results before and after<input name="split" type="date" value="2022-01-01"></label>
      </div></div></details>
      <button type="submit" id="run" style="align-self:flex-start">Show me what would have happened</button>
    </form>
    <div id="results" class="stack" style="gap:24px"></div>`;
  const form = view.querySelector("#bt");
  form.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const fd = new FormData(form);
    const chosen = fd.getAll("s");
    if (!chosen.length) { toast("Pick at least one plan"); return; }
    const capital = Number(fd.get("capital"));
    if (!(capital >= 10000)) { toast("Please enter at least ₹10,000"); return; }
    const safety = fd.get("safety") === "on";
    const body = {
      strategies: chosen.map((n) => ({ name: n, weight: MIX[n] })),
      combined: fd.get("combined") === "on" && chosen.length > 1,
      without_safety: safety && fd.get("nosafety") === "on",
      split: fd.get("split") || null,
      config: { start: `${fd.get("year")}-01-01`, initial_capital: capital, risk_per_trade: Number(fd.get("risk")) / 100, slippage: Number(fd.get("slip")) / 100, safety_switch: safety },
    };
    const btn = view.querySelector("#run"); btn.disabled = true; btn.textContent = "Replaying the past… (a few seconds)";
    const out = view.querySelector("#results");
    out.innerHTML = "";
    try { LAST = { res: await runJob("/api/backtests", body), capital }; showPast(out, LAST); }
    catch (e) { out.innerHTML = errorBox(e); }
    finally { btn.disabled = false; btn.textContent = "Show me what would have happened"; }
  });
  if (LAST) showPast(view.querySelector("#results"), LAST);
}

function showPast(out, { res, capital }) {
  const rs = res.results.map((r) => ({ ...r, final: r.equity.length ? r.equity[r.equity.length - 1][1] : null, plain: r.key === "combined" ? `Mix of plans (${r.label.replace("Combined ", "")})` : PLAN[baseKey(r.key)] ? planName(r.key) : r.label }));
  const startDate = rs[0]?.metrics.start;
  const best = rs.reduce((a, b) => (b.final > a.final ? b : a), rs[0]);
  const calm = rs.reduce((a, b) => ((b.metrics.max_drawdown ?? -1) > (a.metrics.max_drawdown ?? -1) ? b : a), rs[0]);
  const card = (r) => {
    const mt = r.metrics, dd = mt.max_drawdown;
    const lines = [
      mt.cagr != null ? `${mt.cagr >= 0 ? "Grew" : "Shrank"} about <strong>${pct(Math.abs(mt.cagr))} a year</strong> on average ${help("cagr")}` : "",
      dd != null ? `Worst fall from a high point: <strong>${pct(dd, 0)}</strong> ${help("drawdown")}. Like ₹1,00,000 shrinking to ${inr(100000 * (1 + dd))} before recovering.` : "",
      mt.trades ? `Made ${num(mt.trades, 0)} trades${mt.trades_per_year ? ` (about ${num(mt.trades_per_year, 0)} a year)` : ""}${mt.win_rate != null ? `, and ${num(mt.win_rate * 100, 0)} of every 100 made money ${help("win-rate")}` : ""}` : "",
      mt.total_costs ? `Paid ${inr(mt.total_costs)} in charges and taxes` : "",
      mt.exposure != null ? `On average ${pct(mt.exposure, 0)} of the money was in shares; the rest waited as cash` : "",
    ].filter(Boolean);
    const o = r.outcomes;
    const range = o ? `<div class="small" style="border-top:1px solid #E4E4DE;padding-top:8px">
      <strong>One year could look like</strong> ${help("outcomes")}<br>
      Typical year: <span class="${cls(o.typical_year)}">${spct(o.typical_year, 0)}</span> · Bad year (1 in 20): <span class="${cls(o.bad_year)}">${spct(o.bad_year, 0)}</span> · Good year (1 in 20): <span class="${cls(o.good_year)}">${spct(o.good_year, 0)}</span><br>
      Chance of ending a year with less money: <strong>${pct(o.chance_of_loss, 0)}</strong></div>` : "";
    return `<article class="result" style="border-top:4px ${r.key.endsWith(":no_safety") ? "dashed" : "solid"} ${COLORS[baseKey(r.key)] || "#666"}">
      <div class="head"><strong style="font-size:16px">${esc(r.plain)}</strong><span>${r === best ? '<span class="badge buy">Most money</span> ' : ""}${r === calm ? '<span class="badge">Smoothest ride</span>' : ""}</span></div>
      <div class="money ${cls(r.final - capital)}">${inr(r.final)}</div>
      <span class="small muted">from ${inr(capital)} on ${esc(day(startDate))}</span>
      <ul>${lines.map((l) => `<li>${l}</li>`).join("")}</ul>${range}</article>`;
  };
  const plans = rs.filter((r) => PLAN_KEYS.includes(r.key));
  const idle = rs.filter((r) => r.metrics.exposure != null && r.metrics.exposure < 0.6 && PLAN_KEYS.includes(r.key));
  const ready = (r) => {
    const mt = r.metrics, sp = r.split;
    return [
      { plain: "Made money after charges, over 100 or more trades", why: `${num(mt.trades ?? 0, 0)} trades, average ${spct(mt.expectancy_pct, 2)} per trade.`, pass: (mt.trades ?? 0) >= 100 && mt.expectancy_pct > 0 },
      { plain: `Did about as well from ${res.split ? new Date(res.split + "T00:00:00").toLocaleDateString("en-IN", { month: "long", year: "numeric" }) : "recent years"} onwards as before`, why: "If a plan only worked in the early years, it may have been luck.", pass: sp?.test_vs_train != null && sp.test_vs_train >= 0.7 },
      { plain: "Worst fall was smaller than 25%", why: `It was ${pct(mt.max_drawdown, 0)}. Big falls make people panic and quit at the worst time.`, pass: mt.max_drawdown != null && mt.max_drawdown > -0.25 },
      { plain: "You practised with fake money for 3 months", why: "Checks the plan works on new days, and that you can follow it calmly.", pass: false },
    ];
  };
  out.innerHTML = `
    <section class="card" aria-labelledby="r-h"><h2 id="r-h">What would have happened</h2><div class="cards">${rs.map(card).join("")}</div></section>
    ${idle.length ? `<div class="banner"><strong>Much of the money sat idle.</strong> ${esc(idle.map((r) => r.plain).join(", "))} kept less than 60% of the money in shares on average, often because ${inr(capital)} is too little to buy whole shares of expensive companies once it is split ${res.results.some((r) => r.key === "combined") ? "between plans and " : ""}across up to 15 shares. A larger amount, or one plan instead of a mix, keeps more of it working.</div>` : ""}
    <section class="card" aria-labelledby="g-h"><h2 id="g-h">How the money grew</h2><div id="eq"></div>${legend(rs.map((r) => ({ name: r.plain, color: COLORS[baseKey(r.key)] || "#666" })))}
      <p class="small muted" style="margin:0">Hover over the chart to see values on any date. Falls along the way are normal for every plan.</p></section>
    ${plans.map((r) => { const checks = ready(r); const ok = checks.slice(0, 3).every((c) => c.pass); return `
      <section class="card dark"><h2>Is "${esc(r.plain)}" ready for real money?</h2>
        <p style="margin:0;color:#D4D5CF">${ok ? "It passed the history checks. Next step: practise with fake money for 3 months." : "Not yet. Some history checks failed, so keep testing and practising before using real money."}</p>
        ${checklist(checks)}</section>`; }).join("")}
    <p class="small muted" style="margin:0">Past results never guarantee future results. The real future can be better or worse.</p>
    <details><summary>All the numbers</summary><div class="in scroll"><table>
      <thead><tr><th>Plan</th><th class="num">Final value</th><th class="num">Yearly growth</th><th class="num">Worst fall</th><th class="num">Sharpe</th><th class="num">Wins</th><th class="num">Trades/yr</th><th class="num">Charges</th><th class="num">Invested</th><th class="num">Bad year</th><th class="num">Growth after split</th></tr></thead>
      <tbody>${rs.map((r) => { const mt = r.metrics; return `<tr><td>${esc(r.plain)}</td><td class="num">${inr(r.final)}</td><td class="num">${pct(mt.cagr)}</td><td class="num">${pct(mt.max_drawdown)}</td><td class="num">${num(mt.sharpe)}</td><td class="num">${pct(mt.win_rate, 0)}</td><td class="num">${num(mt.trades_per_year, 0)}</td><td class="num">${mt.total_costs != null ? inr(mt.total_costs) : "—"}</td><td class="num">${pct(mt.exposure, 0)}</td><td class="num">${spct(r.outcomes?.bad_year, 0)}</td><td class="num">${pct(r.split?.test?.cagr)}</td></tr>`; }).join("")}</tbody></table></div></details>
    ${plans.map((r) => `<details><summary>Every trade: ${esc(r.plain)} (latest ${Math.min(50, r.trades.length)})</summary><div class="in scroll"><table>
      <thead><tr><th>Share</th><th>Bought</th><th>Sold</th><th class="num">Shares</th><th class="num">Buy price</th><th class="num">Sell price</th><th class="num">Profit / loss</th><th>Why it was sold</th></tr></thead>
      <tbody>${r.trades.slice(-50).reverse().map((t) => `<tr><td>${esc(t.symbol)}</td><td>${esc(t.entry_date)}</td><td>${esc(t.exit_date)}</td><td class="num">${t.qty}</td><td class="num">${price(t.entry_price)}</td><td class="num">${price(t.exit_price)}</td><td class="num ${cls(t.pnl)}">${inr(t.pnl)}</td><td class="small">${esc(plainReason(t.exit_reason))}</td></tr>`).join("") || '<tr><td colspan="8" class="muted">No finished trades.</td></tr>'}</tbody></table></div></details>`).join("")}`;
  lineChart(out.querySelector("#eq"), rs.map((r) => ({ name: r.plain, color: COLORS[baseKey(r.key)] || "#666", dash: r.key === "benchmark" || r.key.endsWith(":no_safety"), points: r.equity })), { fmt: (v) => (v >= 1e5 ? "₹" + (v / 1e5).toFixed(1) + "L" : inr(v)) });
}

// ------------------------------------------------------------------ PRACTICE
async function practicePage() {
  const m = await meta();
  if (!m.has_data) { view.innerHTML = `<h1>Practice with fake money</h1>${demoBanner(m)}`; return; }
  const accts = await api("/api/paper");
  const startForm = (first) => `
    <form id="np" class="card stack" aria-labelledby="np-h"><h2 id="np-h">${first ? "Start practising" : "Start another practice account"}</h2>
      <div class="cards" role="radiogroup" aria-label="Plan">${planChoice("plan", store.get("plan", "trend_pullback"), "np-plan")}</div>
      <label class="f" style="max-width:300px">Fake money to start with (₹)<input name="capital" type="number" min="10000" step="10000" value="${budget()}"></label>
      <button type="submit" style="align-self:flex-start">Start practising</button>
    </form>`;
  view.innerHTML = `
    <div class="head"><div><h1>Practice with fake money</h1><p class="lead">The app buys and sells exactly as its rules say, using real prices but fake money ${help("practice")}. You see how it behaves, wins and losses included, without risking anything.</p></div>
</div>
    ${demoBanner(m)}
    ${accts.length ? `<div class="banner" style="background:#E9EAE4;color:#2E3139">Nothing to press: after each market close, Tradewise gets the day's prices, makes any trades the rules call for, and plans the next day's. Just come back and look.</div>` : ""}
    ${accts.map(acctCard).join("")}
    ${accts.length ? `<details><summary>Start another practice account</summary><div class="in">${startForm(false)}</div></details>` : startForm(true)}`;
  view.querySelector("#np").addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const fd = new FormData(ev.target);
    const capital = Number(fd.get("capital"));
    if (!(capital >= 10000)) { toast("Please enter at least ₹10,000"); return; }
    const plan = fd.get("np-plan");
    const name = `practice-${Date.now().toString(36)}`;
    try {
      await api("/api/paper", { method: "POST", body: JSON.stringify({ name, strategy: plan, config: { initial_capital: capital } }) });
      toast("Practice account started. It will trade by itself after each market close.");
      route();
    } catch (e) { toast(e.message); }
  });
  accts.forEach((a) => {
    const el = document.getElementById(`eq-${a.name}`);
    if (el && a.equity.length >= 5) lineChart(el, [{ name: "Account value", color: "#0B7A5C", points: a.equity }], { height: 200, fmt: (v) => inr(v) });
  });
  view.querySelectorAll("[data-del]").forEach((b) => b.addEventListener("click", async () => {
    if (!confirm("Remove this practice account? Its history will be deleted.")) return;
    try { await api(`/api/paper/${encodeURIComponent(b.dataset.del)}`, { method: "DELETE" }); toast("Removed"); route(); } catch (e) { toast(e.message); }
  }));
}

function acctCard(a) {
  const s = a.summary, P = PLAN[a.strategy] || { name: a.strategy };
  const fills = a.recent_fills.slice(-12).reverse();
  return `<section class="card" aria-label="Practice account ${esc(P.name)}">
    <div class="head"><div><h2>${esc(P.name)} · practice</h2><div class="small muted">Started ${esc(day(a.created_at.slice(0, 10)))} · prices up to ${esc(day(a.last_date))}</div></div></div>
    <div class="verdict ${s.total_return > 0 ? "good" : s.total_return < 0 ? "bad" : "neutral"}">
      <span class="big">${inr(s.final_equity)}</span>
      <p>Started with ${inr(a.config.initial_capital)}. ${s.total_return == null || s.total_return === 0 ? "No change yet." : `That's ${spct(s.total_return)} so far.`}
        ${s.trades ? ` ${s.trades} finished trade(s), ${num((s.win_rate || 0) * 100, 0)}% made money.` : ""} Charges paid: ${inr(s.total_costs)}.</p>
    </div>
    <h3 style="margin:0;font-size:15px">What it owns now</h3>
    ${a.positions.length ? `<ul class="activity">${a.positions.map((p) => { const ch = p.last_price / p.entry_price - 1; return `<li><strong>${p.qty} shares of ${esc(p.symbol)}</strong>, bought at ${price(p.entry_price)}, now ${price(p.last_price)}: <span class="${cls(ch)}">${spct(ch)} (${inr((p.last_price - p.entry_price) * p.qty)})</span>. ${p.stop ? `Safety exit at ${price(p.stop)}.` : "Reviewed at the monthly check."}</li>`; }).join("")}</ul>` : '<p class="muted" style="margin:0">Nothing yet. That is normal at the start: the app waits for a good chance.</p>'}
    ${a.pending.length ? `<p style="margin:0"><strong>Planned for the next market open:</strong> ${a.pending.map((o) => `${o.side === "buy" ? "buy" : "sell"} ${esc(o.symbol)}`).join(", ")}.</p>` : ""}
    ${a.equity.length >= 5 ? `<div id="eq-${esc(a.name)}"></div>` : ""}
    <h3 style="margin:0;font-size:15px">Recent activity</h3>
    ${fills.length ? `<ul class="activity">${fills.map((f) => `<li>${esc(day(f.date))}: ${f.side === "buy" ? "Bought" : "Sold"} ${f.qty} ${esc(f.symbol)} at ${price(f.price)} (${esc(plainReason(f.reason))})</li>`).join("")}</ul>` : '<p class="muted" style="margin:0">No trades yet.</p>'}
    <details><summary>Remove this practice account</summary><div class="in"><button type="button" class="ghost" data-del="${esc(a.name)}">Remove account</button></div></details>
  </section>`;
}

// -------------------------------------------------------------- REAL TRADING
async function livePage() {
  const st = await api("/api/live");
  if (!st.available) {
    view.innerHTML = `<div><h1>Real trading</h1><p class="lead">Buying and selling real shares through Zerodha.</p></div>
      <div class="verdict neutral"><span class="big">Not available on this website</span><p>${esc(st.reason)} The guide <strong>DEPLOY-AWS.md</strong> sets up a server in Mumbai with a fixed address.</p></div>`;
    return;
  }
  const cfg = st.config, sess = st.session, plan = st.plan;
  const sentence = (o) => o.side === "BUY"
    ? `Buy <strong>${o.qty} ${esc(o.symbol)}</strong> at the next open, paying at most ${price(o.price)} per share (about ${inr(o.value)}). Safety exit at ${price(o.stop)}.`
    : `Sell <strong>${o.qty} ${esc(o.symbol)}</strong> at the next open, at the market price. Reason: ${esc(plainReason(o.reason))}.`;
  const m = location.hostname.match(/^(\d+)-(\d+)-(\d+)-(\d+)\.sslip\.io$/);
  const serverIp = m ? m.slice(1).join(".") : "(this server's public IP address)";
  const setupSteps = `<ol style="margin:0;padding-left:20px;display:flex;flex-direction:column;gap:8px">
      <li>On <strong>developers.kite.trade</strong>, create a Kite Connect app (₹500/month).</li>
      <li>Set the app's <strong>Redirect URL</strong> to <span class="mono">${esc(location.origin)}/api/broker/callback</span></li>
      <li>Under <strong>IP Whitelist</strong>, add <span class="mono">${esc(serverIp)}</span>.</li>
      <li>Copy the app's API key and secret into the form below.</li></ol>
    <form id="creds" class="stack" autocomplete="off"><div class="grid2">
      <label class="f">API key<input name="api_key" required></label>
      <label class="f">API secret<input name="api_secret" type="password" required></label></div>
      <button type="submit" style="align-self:flex-start">Save Zerodha app details</button></form>`;

  view.innerHTML = `
    <div><h1>Real trading</h1><p class="lead">Tradewise can place real buy and sell orders in your Zerodha account. It only ever does so after you review tonight's plan and press the button.</p></div>
    <div class="banner err" role="note"><strong>Real money.</strong> You can lose money. Start with a small limit, and keep practising alongside. Tradewise is not investment advice.</div>
    ${!st.configured ? `<section class="card"><h2>Set up the Zerodha connection</h2>${setupSteps}</section>` : ""}

    <section class="card" aria-labelledby="l1"><h2 id="l1">1 · Log in to Zerodha (once a day)</h2>
      ${sess ? `<p style="margin:0">Logged in as <strong>${esc(sess.user_name || sess.user_id)}</strong>. Valid until ${esc(new Date(sess.expires_at).toLocaleString("en-IN", { weekday: "short", hour: "numeric", minute: "2-digit" }))}.</p>`
             : `<p style="margin:0">The exchange requires you to log in yourself every day. Do it in the evening after 4 pm: Tradewise then checks your account and prepares the plan.</p>`}
      <a class="btn" style="align-self:flex-start;text-decoration:none;display:inline-flex;align-items:center" href="/api/broker/login" ${st.configured ? "" : 'aria-disabled="true" onclick="return false"'}>${sess ? "Log in again" : "Log in to Zerodha"}</a>
    </section>

    <section class="card" aria-labelledby="l2"><h2 id="l2">2 · Your limits</h2>
      <div class="verdict ${cfg.enabled ? "good" : "neutral"}"><span class="big">Real trading is ${cfg.enabled ? "ON" : "OFF"}</span>
        <p>${cfg.enabled ? `Plan: ${esc(planName(cfg.strategy))}. At most ${inr(cfg.capital_limit)} invested, at most ${cfg.max_new_per_day} new buy(s) a day, about ${pct(cfg.risk_per_trade)} of the limit at risk per trade. Safety switch ${cfg.safety_switch === false ? "off" : "on"}: buys shrink when the market is jumpy.` : "No buy orders will be planned or placed. Safety exits for shares you already own stay active at Zerodha."}</p></div>
      <form id="limits" class="stack">
        <div class="grid2">
          <label class="f">Most money Tradewise may use (₹)<input name="capital_limit" type="number" min="10000" step="5000" value="${cfg.capital_limit || 25000}"></label>
          <label class="f">New buys per day, at most<input name="max_new_per_day" type="number" min="0" max="10" value="${cfg.max_new_per_day}"></label>
        </div>
        <div class="cards" role="radiogroup" aria-label="Plan">${planChoice("plan", cfg.strategy, "live-plan")}</div>
        ${cfg.enabled ? `<div style="display:flex;gap:8px;flex-wrap:wrap"><button type="submit">Save limits</button><button type="button" class="ghost" id="stop" style="border-color:#B4430A;color:#8A3306">Emergency stop</button></div>`
          : `<label class="f">To turn it on, type: <span class="mono">${esc(st.confirm_phrase)}</span><input name="confirm" autocomplete="off" placeholder="${esc(st.confirm_phrase)}"></label>
             <button type="submit" style="align-self:flex-start">Save and turn on real trading</button>`}
      </form>
    </section>

    <section class="card" aria-labelledby="l3"><h2 id="l3">3 · Tonight's plan</h2>
      ${plan ? `<p class="small muted" style="margin:0">Made ${esc(new Date(plan.created_at).toLocaleString("en-IN"))} from the close on ${esc(day(plan.data_date))}.</p>
        ${plan.orders.length ? `<ul class="activity">${plan.orders.map((o) => `<li>${sentence(o)}</li>`).join("")}</ul>` : '<p style="margin:0">Nothing to buy or sell tonight. That is normal.</p>'}
        ${plan.notes.length ? `<ul class="small muted" style="margin:0;padding-left:18px">${plan.notes.map((n) => `<li>${esc(n)}</li>`).join("")}</ul>` : ""}
        ${plan.executed_at ? `<div class="verdict good"><span class="big">Orders placed</span><p>Sent to Zerodha at ${esc(new Date(plan.executed_at).toLocaleString("en-IN"))}. They execute at the next market open. You can see them in Kite under Orders → AMO.</p></div>`
          : plan.orders.length ? `<label class="check"><input type="checkbox" id="ack"> I have read these orders and want to place them with real money.</label>
             <button type="button" id="execute" style="align-self:flex-start" ${st.in_order_window ? "" : "disabled"}>Place ${plan.orders.length} order(s) at Zerodha</button>
             ${st.in_order_window ? "" : '<p class="small muted" style="margin:0">Orders can be placed between 4 pm and 8:55 am.</p>'}` : ""}`
        : '<p style="margin:0">No plan yet. After logging in to Zerodha in the evening, the plan appears here.</p>'}
      <button type="button" class="ghost" id="replan" style="align-self:flex-start" ${sess ? "" : "disabled"}>Check my account and make the plan again</button>
    </section>

    <section class="card" aria-labelledby="l4"><h2 id="l4">Shares Tradewise manages for you</h2>
      ${st.positions.length ? `<div class="scroll"><table><thead><tr><th>Share</th><th class="num">Shares</th><th class="num">Bought at</th><th class="num">Safety exit</th><th class="num">Profit step</th><th>Protection at Zerodha</th></tr></thead><tbody>
        ${st.positions.map((p) => `<tr><td><strong>${esc(p.symbol)}</strong></td><td class="num">${p.qty}</td><td class="num">${price(p.entry_price)}</td><td class="num">${p.stop ? price(p.stop) : "monthly review"}</td><td class="num">${p.partial_done ? "done" : p.target ? price(p.target) : "—"}</td><td>${p.stop == null ? "not needed" : p.gtt_id ? '<span class="badge buy">Set</span>' : '<span class="badge warn">Not set yet: log in</span>'}</td></tr>`).join("")}
      </tbody></table></div>` : '<p style="margin:0">None yet.</p>'}
      ${st.open_orders.length ? `<p class="small" style="margin:0"><strong>Waiting at Zerodha:</strong> ${st.open_orders.map((o) => `${o.side === "BUY" ? "buy" : "sell"} ${o.qty} ${esc(o.symbol)}`).join(", ")}</p>` : ""}
    </section>

    <details><summary>Activity log</summary><div class="in"><ul class="activity">${st.events.map((e) => `<li><span class="small muted">${esc(new Date(e.at).toLocaleString("en-IN"))}</span><br>${esc(e.message)}</li>`).join("") || '<li class="muted">Nothing yet.</li>'}</ul></div></details>`;

  view.querySelector("#creds")?.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const fd = new FormData(ev.target);
    try { await api("/api/live/credentials", { method: "POST", body: JSON.stringify({ api_key: fd.get("api_key"), api_secret: fd.get("api_secret") }) }); toast("Saved. Now log in to Zerodha."); route(); }
    catch (e) { toast(e.message); }
  });
  view.querySelector("#limits").addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const fd = new FormData(ev.target);
    const body = { capital_limit: Number(fd.get("capital_limit")), max_new_per_day: Number(fd.get("max_new_per_day")), strategy: fd.get("live-plan") };
    if (!cfg.enabled) { body.enabled = true; body.confirm = fd.get("confirm") || ""; }
    try { await api("/api/live/config", { method: "POST", body: JSON.stringify(body) }); toast(cfg.enabled ? "Limits saved." : "Real trading is ON."); route(); }
    catch (e) { toast(e.message); }
  });
  view.querySelector("#stop")?.addEventListener("click", async () => {
    if (!confirm("Turn real trading off and cancel Tradewise's orders that haven't executed yet?")) return;
    try { const r = await api("/api/live/stop", { method: "POST" }); toast(`Real trading is OFF.${r.cancelled.length ? " Cancelled: " + r.cancelled.join(", ") : ""}`); route(); }
    catch (e) { toast(e.message); }
  });
  view.querySelector("#replan")?.addEventListener("click", async (e) => {
    e.currentTarget.disabled = true;
    try { await api("/api/live/plan", { method: "POST" }); toast("Plan updated."); route(); } catch (err) { toast(err.message); e.currentTarget.disabled = false; }
  });
  view.querySelector("#execute")?.addEventListener("click", async (e) => {
    if (!view.querySelector("#ack").checked) { toast("Please tick the box to confirm you've read the orders."); return; }
    const b = e.currentTarget; b.disabled = true; b.textContent = "Placing…";
    try {
      const r = await api("/api/live/execute", { method: "POST", body: JSON.stringify({ plan_id: plan.plan_id }) });
      toast(r.errors.length ? `Stopped: ${r.errors[0]}` : `Placed ${r.placed.length} order(s).`);
      route();
    } catch (err) { toast(err.message); b.disabled = false; b.textContent = "Try again"; }
  });
}

// --------------------------------------------------------------------- LEARN
const TERMS = [
  ["share", "Share (stock)", "A tiny piece of ownership in a company. If you own 10 shares of a company, you own a small slice of it. When the company does well, people pay more for its shares and the price goes up."],
  ["nse", "Stock exchange (NSE)", "The market where shares are bought and sold, open 9:15 am to 3:30 pm on weekdays except holidays. You trade through a broker app such as Zerodha, Groww or Upstox, using a demat account that holds your shares."],
  ["nifty", "Nifty 50", "A number that tracks the prices of India's 50 biggest companies together. When people say \"the market went up\", they usually mean the Nifty 50 rose. This app uses it to tell whether the market is rising or falling."],
  ["average", "Average price (moving average)", "The average closing price over the last 20, 50 or 200 days. It smooths out daily ups and downs. A price above its 200-day average usually means a long-term uptrend; below it, a downtrend."],
  ["uptrend", "Uptrend", "When a share's price keeps making higher highs over months. Shares in an uptrend are more likely to keep rising than to suddenly turn, so both plans only buy shares in an uptrend."],
  ["dip", "Buying the dip", "Waiting for a rising share to fall back a little before buying, instead of chasing it after a big jump. You get a better price and can place the safety exit closer."],
  ["volume", "Volume", "How many shares changed hands in a day. A price rise on high volume is more trustworthy, because many people are buying."],
  ["rsi", "RSI (momentum)", "A score from 0 to 100 showing how strongly the price moved recently. Above 70: running hot and may rest. Below 30: beaten down. 40–60: calm. The app likes buying when a calm dip starts turning up."],
  ["stop-loss", "Safety exit (stop-loss)", "A price set the moment you buy. If the share falls to it, you sell, no matter what. It keeps a bad trade small instead of letting it grow into a big loss. Most broker apps let you place it as a \"stop-loss order\" so it happens automatically. Never move it lower."],
  ["target", "Profit step (target)", "A price where you sell half to lock in some profit. The other half stays in case the rise continues. In this app it's set so the profit is twice the amount you risked."],
  ["risk", "The 1% rule (risk per trade)", "Each trade is sized so that if the safety exit is hit, you lose only about 1% of your money (₹1,000 out of ₹1,00,000). Even 10 losses in a row would cost only about 10%. This is the most important rule for staying in the game."],
  ["position-size", "How many shares to buy", "Worked out for you: (1% of your money) ÷ (buy price − safety exit). A share with a far-away safety exit gets fewer shares. No single share gets more than 20% of your money."],
  ["volatility", "Volatility (how much a price swings)", "How much a price moves up and down from day to day, written as a yearly percentage. The Nifty 50 normally swings about 15–18% a year; in a crash it can swing 40% or more. Shares that swing less are calmer to hold."],
  ["low-vol", "Steady (low-volatility) shares", "Shares whose prices move less than most. Research across many countries, India included, found they have often earned about as much as the market while falling less in bad years. NSE even runs an index on this idea (Nifty100 Low Volatility 30). \"Steady shares\" uses it."],
  ["safety-switch", "Safety switch", "Two automatic brakes on how much the app buys. 1) When the market is swinging much more than usual, new buys get smaller: at twice the normal swing, half size. Big swings usually come in crashes, so this cuts risk when it matters most. 2) While your account is more than 12% below its best value, new buys are half size, so a bad patch can't snowball. It never forces a sale; it only shrinks new buys. You can switch it off when testing on past data to see the difference."],
  ["outcomes", "One year could look like…", "The app cuts the plan's past daily results into month-long pieces and shuffles them into 2,000 imaginary years. The \"bad year\" is the 1-in-20 worst of those. It is a rough guide to what one unlucky year could feel like, not a forecast."],
  ["momentum", "Momentum", "Shares that have risen steadily for 6–12 months tend to keep doing well for a while. \"Hold the strongest\" uses this: it owns the strongest 15 and swaps them once a month."],
  ["backtest", "Backtest (test on past data)", "Replaying the rules on old prices, day by day, as if you had followed them back then. It shows how a plan behaved in good and bad years. It can't promise the future, but a plan that failed in the past is unlikely to work now."],
  ["cagr", "Yearly growth (CAGR)", "The steady yearly rate that would turn your starting money into the final amount. For example, 12% a year turns ₹1 lakh into about ₹3.1 lakh in 10 years."],
  ["drawdown", "Worst fall (drawdown)", "The biggest drop from a high point before recovering. Every plan has these. A 25% fall means ₹1,00,000 could shrink to ₹75,000 for a while. Know this number so you don't panic and quit at the worst time."],
  ["win-rate", "Win rate", "Out of 100 trades, how many made money. Good plans often win only 40–55 times out of 100. They make money because the winners are bigger than the losers."],
  ["charges", "Charges and taxes", "Every buy and sell costs a little: STT (a tax of 0.1% each side), exchange and SEBI fees, stamp duty on buys, GST, and about ₹15 per share sold from your demat account. The app includes all of these. Profits are also taxed (short-term if held under 1 year)."],
  ["practice", "Practice with fake money (paper trading)", "Following the plan on new days with pretend money. It shows whether the plan still works, and whether you can follow the rules calmly, before real money is at stake."],
  ["delivery", "Delivery vs intraday", "This app only does delivery trades: you buy shares and keep them for days or weeks. Intraday trading (buying and selling the same day) and F&O (futures and options) are much riskier. SEBI found about 9 in 10 individual F&O traders lose money."],
];

async function learnPage(term) {
  view.innerHTML = `
    <div><h1>Learn the basics</h1><p class="lead">Everything you need to understand this app, in plain words. Tap a topic to open it.</p></div>
    <section class="card" aria-labelledby="how-h"><h2 id="how-h">How this app works, in one minute</h2>
      <ol style="margin:0;padding-left:20px;display:flex;flex-direction:column;gap:8px;font-size:15px">
        <li>Every evening it looks at the closing prices of 40 large Indian companies.</li>
        <li>It checks each share against a fixed list of rules. No guessing, no tips, no emotions.</li>
        <li>If a share passes every rule, it suggests buying, with exactly how many shares, a safety exit and a profit step.</li>
        <li>You can see how those rules did in past years (<a href="#/past">Test on past data</a>) and try them with fake money (<a href="#/practice">Practice</a>).</li>
      </ol></section>
    <section class="card" aria-labelledby="plans-h"><h2 id="plans-h">The three plans</h2><div class="cards">
      ${PLAN_KEYS.map((k) => `<div class="result"><strong style="font-size:16px">${esc(PLAN[k].name)}</strong><span class="small muted">${esc(PLAN[k].tag)}</span><p style="margin:0">${esc(PLAN[k].desc)}</p></div>`).join("")}
    </div></section>
    <section class="card" aria-labelledby="gold-h"><h2 id="gold-h">Golden rules for beginners</h2>
      <ul style="margin:0;padding-left:20px;display:flex;flex-direction:column;gap:8px;font-size:15px">
        <li>Only invest money you won't need for the next 3–5 years. Keep an emergency fund separately.</li>
        <li>Every trade gets a safety exit (stop-loss). Never move it lower to "give it more room".</li>
        <li>Risk about 1% of your money per trade. Small losses are part of the plan.</li>
        <li>Expect to lose on roughly half your trades. That is normal, not failure.</li>
        <li>Practise with fake money for about 3 months before using real money, then start small.</li>
        <li>Ignore tips from friends, Telegram groups and TV. Follow your tested rules.</li>
        <li>Avoid intraday and F&amp;O trading while you are learning.</li>
      </ul></section>
    <section class="stack" aria-labelledby="gl-h"><h2 id="gl-h">Words you will see</h2>
      ${TERMS.map(([id, title, text]) => `<details class="term" id="t-${id}" ${id === term ? "open" : ""}><summary>${esc(title)}</summary><div class="in"><p>${esc(text)}</p></div></details>`).join("")}
    </section>
    <p class="small muted">Tradewise follows fixed rules and is not investment advice. Speak to a SEBI-registered adviser for personal advice.</p>`;
  if (term) document.getElementById(`t-${term}`)?.scrollIntoView({ block: "center" });
}

// ------------------------------------------------------- automatic updates
let LOADED_VERSION;  // undefined until the first status arrives
let pollTimer = null;
const pageName = () => location.hash.split("/")[1] || "today";

function renderStatus(st) {
  const el = document.getElementById("status");
  const fresh = st.data_last_date && st.data_last_date >= st.expected_date;
  const kind = st.busy ? "busy" : st.source === "yahoo" && fresh ? "good" : st.source === "yahoo" || st.last_error ? "warn" : "";
  const msg = st.busy ? (st.source ? "Getting the latest prices…" : "Getting prices for the first time (1–3 minutes)…") : st.message;
  el.className = `status ${kind}`;
  el.innerHTML = `<span class="dot" aria-hidden="true"></span><span class="msg">${esc(msg)}</span>${st.busy ? "" : '<button type="button" id="update-now">Update now</button>'}`;
  el.querySelector("#update-now")?.addEventListener("click", (e) => refreshPrices(e.currentTarget));
  const old = document.getElementById("notice");
  if (st.notice && !old && store.get("notice_seen", "") !== st.notice) {
    const n = document.createElement("div");
    n.id = "notice"; n.className = "notice";
    n.innerHTML = `<span>${esc(st.notice)}</span><button type="button">OK</button>`;
    n.querySelector("button").addEventListener("click", () => { store.set("notice_seen", st.notice); n.remove(); });
    el.after(n);
  }
}

async function pollStatus(soon = false) {
  clearTimeout(pollTimer);
  let st = null;
  try { st = await api("/api/status"); } catch { /* app restarting: try again shortly */ }
  if (st) {
    renderStatus(st);
    // Hosted version: nothing runs in the background, so the first visit starts the first download.
    if (st.serverless && !st.source && !st.busy && !pollStatus.started) {
      pollStatus.started = true;
      runJob("/api/refresh").then(() => pollStatus(true)).catch((e) => toast(e.message));
      renderStatus({ ...st, busy: true });
    }
    if (st.login && !document.getElementById("logout")) {
      const a = document.createElement("a");
      a.id = "logout"; a.href = "/logout"; a.textContent = "Log out"; a.className = "side-logout";
      document.querySelector(".side-note")?.before(a);
    }
    if (LOADED_VERSION === undefined) LOADED_VERSION = st.version;
    else if (!st.busy && st.version !== LOADED_VERSION) {
      const firstData = LOADED_VERSION == null;
      LOADED_VERSION = st.version;
      META = null;
      const typing = view.contains(document.activeElement) && /INPUT|SELECT|TEXTAREA/.test(document.activeElement.tagName);
      if ((firstData || ["today", "stock", "practice"].includes(pageName())) && !typing) { toast("New prices arrived. This page has been updated."); route(); }
      else toast("New prices arrived.");
    }
  }
  pollTimer = setTimeout(pollStatus, st?.busy || soon ? 3000 : st ? 60000 : 15000);
}

// ------------------------------------------------------------------- router
async function render(fn) {
  try { await fn(); } catch (e) { view.innerHTML = errorBox(e); }
}
function route() {
  const [, page = "", arg = ""] = location.hash.split("/");
  const name = page || "today";
  document.querySelectorAll(".side a").forEach((a) => a.classList.toggle("active", a.dataset.route === name));
  const pages = { today: todayPage, stock: () => stockPage(decodeURIComponent(arg)), past: pastPage, practice: practicePage, live: livePage, learn: () => learnPage(arg) };
  view.innerHTML = '<p class="muted">Loading…</p>';
  render(pages[name] || todayPage);
  if (name !== "learn" || !arg) window.scrollTo(0, 0);
}
window.addEventListener("hashchange", route);
route();
pollStatus();
