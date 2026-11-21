import {
  POLICY_ROLES, STATUS_LABELS, TYPE_LABELS, benchmark, cohort, elapsed, esc, indexEpisode, metric, money,
  policyOrder, recordId, timestamp,
} from "./lib.js";

const S = {
  index: null, bundles: new Map(), episodes: new Map(), activity: new Map(),
  scroll: new Map(), lastHash: null, lastRoute: null, notice: "", focus: null, showAll: false,
  runsQuery: {},
};
const main = document.getElementById("main");
const live = document.getElementById("live");

/* ------------------------------------------------------------------ data */

async function getJSON(path) {
  const response = await fetch(path, {cache: "no-store"});
  if (!response.ok) { const error = new Error(`${response.status} for ${path}`); error.status = response.status; throw error; }
  return response.json();
}
async function loadIndex() {
  if (!S.index) S.index = await getJSON("/data/index.json");
  return S.index;
}
async function loadBundle(id) {
  if (!S.bundles.has(id)) S.bundles.set(id, getJSON(`/data/bundles/${encodeURIComponent(id)}/bundle.json`));
  return S.bundles.get(id);
}
function loadEpisode(bundle, run) {
  const key = `${bundle.id}/${run.split}/${run.episode_id}`;
  if (!S.episodes.has(key)) {
    const meta = bundle.episodes[`${run.split}/${run.episode_id}`] || {status: "missing"};
    S.episodes.set(key, meta.status !== "ok" ? Promise.resolve({meta}) :
      getJSON(`/data/bundles/${enc(bundle.id)}/episodes/${enc(run.split)}/${enc(run.episode_id)}.json`)
        .then((data) => ({meta, index: indexEpisode(data)}))
        .catch(() => ({meta: {status: "missing", message: "The record file for this episode could not be loaded."}})));
  }
  return S.episodes.get(key);
}
function loadActivity(bundle, run) {
  const key = `${bundle.id}/${run.key}`;
  if (!S.activity.has(key)) {
    S.activity.set(key, getJSON(`/data/bundles/${enc(bundle.id)}/activity/${enc(run.split)}/${enc(run.episode_id)}/${enc(run.policy)}__${enc(run.mode)}.json`).catch(() => null));
  }
  return S.activity.get(key);
}
const enc = encodeURIComponent;

/* ------------------------------------------------------------------ routing */

function parseRoute() {
  const hash = location.hash.replace(/^#/, "") || "/benchmark";
  const [path, query = ""] = hash.split("?");
  return {view: path.replace(/^\//, "") || "benchmark", params: Object.fromEntries(new URLSearchParams(query))};
}
function href(view, params) {
  const clean = Object.fromEntries(Object.entries(params).filter(([, v]) => v !== undefined && v !== null && v !== ""));
  const query = new URLSearchParams(clean).toString();
  return `#/${view}${query ? "?" + query : ""}`;
}
function go(view, params, {replace = false, focus = null} = {}) {
  S.focus = focus;
  const target = href(view, params);
  if (replace) { history.replaceState(null, "", target); render(); } else if (location.hash === target) render(); else location.hash = target;
}
function announce(text) { live.textContent = ""; setTimeout(() => { live.textContent = text; }, 30); }

/* ------------------------------------------------------------------ helpers */

const statusClass = (s) => s === "submitted" ? "good" : s === "invalid_submission" ? "bad" : s === "unknown" ? "" : "warn";
const statusHtml = (s) => `<span class="status ${statusClass(s)}">${esc(STATUS_LABELS[s] || s)}</span>`;
const typeLabel = (t) => TYPE_LABELS[t] || t;
const splitLabel = (s) => s === "unspecified" ? "Unspecified" : s;
function sliceRuns(bundle, ctx) { return bundle.runs.filter((r) => r.split === ctx.s && r.mode === ctx.m); }
function ctxParams(ctx) { return {b: ctx.b, s: ctx.s, m: ctx.m, p: ctx.full ? "full" : undefined}; }
function rewardVisible(bundle) { return bundle.audience === "reviewer"; }

function resolveContext(params, index, bundle) {
  const splits = bundle.splits;
  const s = splits.includes(params.s) ? params.s : (splits.includes("dev") ? "dev" : splits[0]);
  const modes = [...new Set(bundle.runs.filter((r) => r.split === s).map((r) => r.mode))].sort();
  const m = modes.includes(params.m) ? params.m : (modes.includes("closed") ? "closed" : modes[0]);
  return {b: bundle.id, s, m, modes, full: params.p === "full"};
}

function renderContext(index, bundle, ctx, view) {
  const el = document.getElementById("context");
  if (!bundle) { el.innerHTML = ""; return; }
  const option = (value, label, selected) => `<option value="${esc(value)}" ${selected ? "selected" : ""}>${esc(label)}</option>`;
  const demoLine = bundle.kind === "demo" ? `<span class="tag">Synthetic two-episode demo</span>` : bundle.synthetic ? `<span class="tag">Synthetic data</span>` : "";
  el.innerHTML = `
    <label>Source <select id="ctx-source">${index.bundles.map((b) => option(b.id, b.label, b.id === bundle.id)).join("")}</select></label>
    <label>Split <select id="ctx-split" ${bundle.splits.length < 2 ? "disabled" : ""}>${bundle.splits.map((s) => option(s, splitLabel(s), s === ctx.s)).join("")}</select></label>
    <label>Mode <select id="ctx-mode" ${ctx.modes.length < 2 ? "disabled" : ""}>${ctx.modes.map((m) => option(m, m, m === ctx.m)).join("")}</select></label>
    <span class="tag">${bundle.audience === "reviewer" ? "Reviewer export" : "Public view: evaluator metrics withheld"}</span>
    ${demoLine}`;
  const change = (patch) => {
    const params = {...ctxParams(ctx), ...patch};
    const destination = view === "run" ? "runs" : view;
    if (view === "run") S.notice = "The source changed, so the run view returned to the run list.";
    go(destination === "start" ? "benchmark" : destination, params);
  };
  el.querySelector("#ctx-source").onchange = (e) => change({b: e.target.value, s: undefined, m: undefined});
  el.querySelector("#ctx-split").onchange = (e) => change({s: e.target.value, m: undefined});
  el.querySelector("#ctx-mode").onchange = (e) => change({m: e.target.value});
}

function setNav(view) {
  for (const a of document.querySelectorAll("[data-nav]")) {
    const active = a.dataset.nav === view || (view === "run" && a.dataset.nav === "runs");
    if (active) a.setAttribute("aria-current", "page"); else a.removeAttribute("aria-current");
  }
}

function noticeHtml() {
  if (!S.notice) return "";
  const html = `<div class="callout info" role="status">${esc(S.notice)}</div>`;
  S.notice = "";
  return html;
}

/* ------------------------------------------------------------------ render */

async function render() {
  const route = parseRoute();
  const previous = S.lastRoute;
  if (S.lastHash !== null) S.scroll.set(S.lastHash, window.scrollY);
  let index;
  try { index = await loadIndex(); } catch (error) { return renderFatal("The dashboard data index could not be loaded. Prepare a source and restart the server."); }
  if (!index.bundles || !index.bundles.length) { renderContext(null); setNav(""); return renderStart(); }

  let bundleId = route.params.b;
  if (!index.bundles.some((b) => b.id === bundleId)) {
    if (bundleId) S.notice = `Source “${bundleId}” is not available; showing ${index.bundles[0].label} instead.`;
    bundleId = index.bundles[0].id;
  }
  let bundle;
  try { bundle = await loadBundle(bundleId); } catch (error) { return renderFatal(`The source “${bundleId}” could not be loaded. Run its prepare command again.`); }
  const ctx = resolveContext(route.params, index, bundle);
  renderContext(index, bundle, ctx, route.view);
  setNav(route.view);
  document.querySelectorAll("[data-nav]").forEach((a) => { a.href = href(a.dataset.nav, a.dataset.nav === "runs" ? {...ctxParams(ctx), ...S.runsQuery} : ctxParams(ctx)); });

  if (route.view === "runs") renderRuns(bundle, ctx, route.params);
  else if (route.view === "run") await renderRun(bundle, ctx, route.params);
  else renderBenchmark(bundle, ctx, route.params);

  const sameRun = previous && previous.view === route.view && previous.params.r === route.params.r && route.view === "run" && previous.params.tab === route.params.tab;
  const hash = location.hash;
  if (!sameRun) window.scrollTo(0, S.scroll.get(hash) || 0);
  S.lastHash = hash;
  S.lastRoute = route;
  const target = S.focus && document.querySelector(S.focus);
  if (target) target.focus();
  else if (!sameRun) { const h1 = main.querySelector("h1"); if (h1) { h1.setAttribute("tabindex", "-1"); h1.focus({preventScroll: true}); } }
  S.focus = null;
  animateEnter(route, previous);
}

const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)");
const EASE = "cubic-bezier(0.2, 0, 0, 1)";

/** Corporate motion: 300ms page rise, bars grow from zero (40ms stagger), 200ms inspector slide. */
function animateEnter(route, previous) {
  if (reduceMotion.matches || typeof main.animate !== "function") return;
  const p = previous ? previous.params : null;
  const r = route.params;
  const pageChange = !previous || previous.view !== route.view || ["b", "s", "m", "r"].some((k) => p[k] !== r[k])
    || (route.view === "run" && p.tab !== r.tab);
  if (pageChange) {
    main.animate([{opacity: 0, transform: "translateY(6px)"}, {opacity: 1, transform: "none"}], {duration: 300, easing: EASE});
    main.querySelectorAll(".plot .hbar").forEach((bar, i) => bar.animate([{transform: "scaleX(0)"}, {transform: "scaleX(1)"}],
      {duration: 300, delay: 80 + i * 40, easing: EASE, fill: "backwards"}));
    return;
  }
  const target = r.e !== p.e || r.rec !== p.rec ? document.getElementById("record-section")
    : r.f !== p.f ? main.querySelector(".inspector") : null;
  if (target) target.animate([{opacity: 0, transform: "translateX(10px)"}, {opacity: 1, transform: "none"}], {duration: 200, easing: EASE});
}

function renderFatal(message) {
  main.innerHTML = `<div class="callout error" role="alert">${esc(message)}</div>`;
}

function renderStart() {
  main.innerHTML = `<div class="start">
    <h1>No Evaluation Loaded</h1>
    <p class="muted">The dashboard reads prepared, sanitized bundles. Prepare the synthetic demo with:</p>
    <pre><code>python3 -m dashboard demo</code></pre>
    <p class="muted">Or import existing outputs, then run <code>python3 -m dashboard serve</code>:</p>
    <pre><code>python3 -m audit_sim run episodes --split dev --output .audit-sim/dev-results
python3 -m dashboard prepare --run .audit-sim/dev-results --episodes episodes --audience reviewer --name dev

python3 -m audit_sim release --output .audit-sim/release
python3 -m dashboard prepare --release .audit-sim/release --audience reviewer --name release</code></pre>
  </div>`;
}

/* ------------------------------------------------------------------ benchmark */

function renderBenchmark(bundle, ctx, params) {
  const runs = sliceRuns(bundle, ctx);
  const c = cohort(runs);
  const useCommon = params.c !== "all";
  const rows = benchmark(runs, {useCommon});
  const reviewer = rewardVisible(bundle);
  const full = ctx.full;
  const coverage = c.policies.map((p) => `${p} ${c.coverage.get(p).size}`).join(" · ");
  let cohortNote;
  if (!runs.length) cohortNote = "";
  else if (c.equal) cohortNote = `Every policy ran on the same ${c.common.size} episode${c.common.size === 1 ? "" : "s"}; means are per-episode arithmetic means of the report rows.`;
  else cohortNote = useCommon
    ? `Coverage differs (${coverage} episodes). Means use only the ${c.common.size} episode${c.common.size === 1 ? "" : "s"} every policy ran on. <a href="${href("benchmark", {...ctxParams(ctx), c: "all"})}">Include All Runs</a>`
    : `Coverage differs (${coverage} episodes), so these means compare unequal samples. <a href="${href("benchmark", ctxParams(ctx))}">Restrict to the ${c.common.size}-Episode Common Cohort</a>`;
  const runsLink = (policy) => href("runs", {...ctxParams(ctx), pol: policy});
  const metricCell = (v, opt = false) => `<td class="num${opt ? " col-opt" : ""}" title="${v === null ? "" : esc(String(v))}">${metric(v, full)}</td>`;
  const focal = focalPolicy(rows);
  const table = rows.length ? `
    <div class="table-wrap"><table>
      <caption>${esc(splitLabel(ctx.s))} split · ${esc(ctx.m)} mode · ${c.equal ? `${c.common.size} episode${c.common.size === 1 ? "" : "s"} per policy` : useCommon ? `common cohort of ${c.common.size} episode${c.common.size === 1 ? "" : "s"}` : "all runs, unequal coverage"}</caption>
      <thead><tr>
        <th scope="col">Policy</th><th scope="col" class="num col-opt">Episodes</th>
        ${reviewer ? `<th scope="col" class="num">Mean F1</th><th scope="col" class="num">Mean Reward</th>` : ""}
        <th scope="col" class="num col-opt">Mean Steps</th>
        ${reviewer ? `<th scope="col" class="num col-opt">Precision</th><th scope="col" class="num col-opt">Recall</th>` : ""}
        <th scope="col" class="num col-opt">Mean Elapsed</th><th scope="col" class="num col-opt">Not Submitted</th>
      </tr></thead>
      <tbody>${rows.map((r) => `<tr>
        <th scope="row"><a href="${runsLink(r.policy)}" translate="no">${esc(r.policy)}</a> <span class="role">${esc(r.role)}</span></th>
        <td class="num col-opt">${r.episodes}</td>
        ${reviewer ? metricCell(r.f1) + metricCell(r.reward) : ""}
        <td class="num col-opt">${r.steps === null ? "—" : r.steps.toFixed(1)}</td>
        ${reviewer ? metricCell(r.precision, true) + metricCell(r.recall, true) : ""}
        <td class="num col-opt">${elapsed(r.seconds)}</td>
        <td class="num col-opt">${reliability(r)}</td>
      </tr>`).join("")}</tbody>
    </table></div>` : `<div class="empty"><h2>No Runs</h2><p>This split and mode have no runs. Choose another split or source above.</p></div>`;

  main.innerHTML = `${noticeHtml()}
    <div class="page-head"><div><p class="eyebrow">${esc(bundle.label)}</p><h1>Benchmark</h1></div>
      <label class="check"><input type="checkbox" id="full" name="full" ${full ? "checked" : ""}> Full Precision</label></div>
    ${reviewer && rows.length ? keyResult(bundle, ctx, rows, runs, focal) : ""}
    <section class="panel" aria-label="Policy comparison">
      ${rows.length ? benchmarkChart(rows, reviewer, focal, full) : ""}
      ${table}
      <div class="panel-body note">${cohortNote}${reviewer ? "" : " Evaluator metrics (F1, reward, TP/FP/FN) are withheld in this public view."}
        Oracle is an evaluator control that reads the answer key, not an agent.</div>
    </section>
    ${definitions()}
    ${bundle.validation ? validationPanel(bundle.validation, full) : ""}
    ${notesPanel(bundle)}`;
  main.querySelector("#full").onchange = (e) => go("benchmark", {...ctxParams({...ctx, full: e.target.checked}), c: params.c}, {replace: true, focus: "#full"});
}

/** The policy the page is about: the reference policy when present, else the best agent by reward. */
function focalPolicy(rows) {
  const agents = rows.filter((r) => r.policy !== "oracle");
  return (rows.find((r) => r.policy === "reconcile") || [...agents].sort((a, b) => (b.reward ?? -9) - (a.reward ?? -9))[0] || rows[0] || {}).policy;
}

/** Answer first: one sentence a judge can repeat, and a direct path to verify it. */
function keyResult(bundle, ctx, rows, runs, focal) {
  const f = rows.find((r) => r.policy === focal);
  if (!f || f.reward === null) return "";
  const control = rows.find((r) => r.policy === "mark_all");
  const traced = runs.filter((r) => r.policy === focal && r.findings && r.findings.length)
    .sort((a, b) => b.findings.length - a.findings.length || a.episode_id.localeCompare(b.episode_id))[0];
  const role = (f.role || "agent policy").toLowerCase();
  const sentence = `<span translate="no">${esc(focal)}</span>, the ${esc(role)}, averages reward <b>${metric(f.reward, ctx.full)}</b> and F1 <b>${metric(f.f1, ctx.full)}</b> over ${f.episodes} episode${f.episodes === 1 ? "" : "s"}`
    + (control && control.policy !== focal ? `; the negative control <span translate="no">mark_all</span> averages <b>${metric(control.reward, ctx.full)}</b>.` : ".");
  const link = traced
    ? `<a class="lede-link" href="${href("run", {...ctxParams(ctx), r: traced.key, f: 1})}">Trace Its Findings on <span class="mono" translate="no">${esc(traced.episode_id)}</span> →</a>`
    : `<a class="lede-link" href="${href("runs", {...ctxParams(ctx), pol: focal})}">Open Its Runs →</a>`;
  return `<p class="lede">${sentence}</p><p>${link}</p>`;
}

/** Horizontal bars on an untruncated axis: reward spans −1…1 with a zero line; steps start at 0. */
function benchmarkChart(rows, reviewer, focal, full) {
  const field = reviewer ? "reward" : "steps";
  const values = rows.map((r) => r[field]).filter((v) => typeof v === "number");
  let floor = 0, ceil = 1, ticks;
  if (reviewer) { floor = -1; ceil = 1; ticks = [-1, -0.5, 0, 0.5, 1]; }
  else {
    const max = Math.max(1, ...values);
    const step = Math.pow(10, Math.floor(Math.log10(max))) * (max / Math.pow(10, Math.floor(Math.log10(max))) > 5 ? 2 : 1);
    ceil = Math.ceil(max / step) * step;
    ticks = Array.from({length: Math.round(ceil / step) + 1}, (_, i) => i * step);
  }
  const pos = (v) => ((v - floor) / (ceil - floor)) * 100;
  const zero = pos(0);
  const fmt = (v) => reviewer ? metric(v, full) : v.toFixed(1);
  const bars = rows.map((r, i) => {
    const v = r[field];
    const label = `<div class="chart-label"><b translate="no">${esc(r.policy)}</b><span>${esc(r.role)}</span></div>`;
    if (typeof v !== "number") return `<div class="chart-row">${label}<div class="plot"><span class="val" style="left:${zero}%">—</span></div></div>`;
    const end = pos(v), left = Math.min(end, zero), width = Math.abs(end - zero);
    const cls = r.policy === focal ? "focal" : r.policy === "oracle" ? "control" : "";
    const positive = v >= 0;
    const inside = positive ? end > 86 : end < 14;
    const valPos = positive
      ? (inside ? `left:calc(${end}% - 6px);transform:translateX(-100%)` : `left:calc(${end}% + 6px)`)
      : (inside ? `left:calc(${end}% + 6px)` : `left:calc(${end}% - 6px);transform:translateX(-100%)`);
    return `<div class="chart-row">${label}<div class="plot">
      ${ticks.map((t) => `<span class="grid${t === 0 ? " zero" : ""}" style="left:${pos(t)}%"></span>`).join("")}
      <span class="hbar ${cls}" style="left:${left}%;width:max(${width}%, 1px);--origin:${positive ? "left" : "right"};--i:${i}"></span>
      <span class="val ${r.policy === focal ? "focal" : ""}" style="${valPos}">${esc(fmt(v))}</span></div></div>`;
  }).join("");
  const summary = rows.map((r) => `${r.policy} ${typeof r[field] === "number" ? fmt(r[field]) : "unavailable"}`).join(", ");
  return `<figure class="chart" style="margin:0" role="img" aria-label="Mean ${reviewer ? "reward" : "steps"} by policy: ${esc(summary)}">
      <div aria-hidden="true">${bars}
      <div class="chart-axis"><span></span><div class="plot">${ticks.map((t, i) => `<span class="tick${i === 0 ? " first" : i === ticks.length - 1 ? " last" : ""}" style="left:${pos(t)}%">${reviewer ? metric(t).replace(".000", "").replace(".500", ".5") : t}</span>`).join("")}</div></div></div>
    </figure>
    <div class="chart-foot" aria-hidden="true"><span class="eyebrow">Mean ${reviewer ? "reward per episode · axis −1 to 1" : "steps per episode · axis from 0"}</span><span class="eyebrow">Accent: ${esc(focal || "")} · Dashed: evaluator control</span></div>`;
}

function reliability(r) {
  const parts = [];
  if (r.timeout) parts.push(`${r.timeout} timed out`);
  if (r.step_limit) parts.push(`${r.step_limit} step limit`);
  if (r.invalid) parts.push(`${r.invalid} invalid`);
  return parts.length ? esc(parts.join(", ")) : "0";
}

function definitions() {
  return `<details class="panel panel-body disclose" style="margin-top:16px"><summary>Metric Definitions</summary>
    <dl class="kv" style="margin-top:8px">
      <dt>Precision</dt><dd>TP / (TP + FP); 0 when nothing was submitted.</dd>
      <dt>Recall</dt><dd>TP / (TP + FN); 0 when the episode has no planted issues.</dd>
      <dt>F1</dt><dd>2TP / (2TP + FP + FN); an empty valid submission on an episode with no issues scores F1 = 1 while precision and recall are 0.</dd>
      <dt>Reward</dt><dd>clip(2 × F1 − 1 − 0.002 × inspection steps, −1, 1); an invalid submission scores −1.</dd>
      <dt>Not submitted</dt><dd>Runs that ended by timeout or step limit, or whose submission was structurally invalid.</dd>
    </dl></details>`;
}

function validationPanel(v, full) {
  const dev = v.dev || {};
  const policies = policyOrder(Object.keys(dev));
  return `<section class="panel"><div class="panel-head"><h2>Release Validation</h2><span class="note">From validation.json, as recorded by the release command</span></div>
    <div class="panel-body"><ul class="facts">
      ${[["Train episodes", v.train_episodes], ["Dev episodes", v.dev_episodes], ["Verified pairs", v.verified_pairs], ["Dev runs", v.dev_runs], ["Recorded train steps", v.recorded_train_steps], ["Slowest dev episode", v.max_dev_episode_seconds !== undefined ? elapsed(v.max_dev_episode_seconds) : undefined]]
        .filter(([, value]) => value !== undefined).map(([k, value]) => `<li>${esc(k)} <b>${esc(value)}</b></li>`).join("")}
    </ul></div>
    ${policies.length ? `<div class="table-wrap"><table><caption>Release summary means (coverage per policy as listed)</caption>
      <thead><tr><th scope="col">Policy</th><th scope="col" class="num">Episodes</th><th scope="col" class="num">Mean F1</th><th scope="col" class="num">Mean return</th><th scope="col" class="num">Mean steps</th><th scope="col" class="num col-opt">Truncated</th><th scope="col" class="num col-opt">Invalid</th></tr></thead>
      <tbody>${policies.map((p) => { const d = dev[p] || {}; return `<tr><th scope="row">${esc(p)}</th><td class="num">${esc(d.episodes ?? "—")}</td><td class="num">${metric(d.mean_f1, full)}</td><td class="num">${metric(d.mean_return, full)}</td><td class="num">${d.mean_steps ?? "—"}</td><td class="num col-opt">${d.truncated ?? "—"}</td><td class="num col-opt">${d.invalid_submissions ?? "—"}</td></tr>`; }).join("")}</tbody>
    </table></div>` : ""}
  </section>`;
}

function notesPanel(bundle) {
  if (!bundle.notes || !bundle.notes.length) return "";
  return `<section class="panel"><div class="panel-head"><h2>Import Notes</h2><span class="note">Prepared ${esc(bundle.prepared_at)}</span></div>
    <div class="panel-body"><ul class="note" style="margin:0;padding-left:18px">${bundle.notes.map((n) => `<li>${esc(n)}</li>`).join("")}</ul></div></section>`;
}

/* ------------------------------------------------------------------ runs */

const SORTS = {
  episode: (r) => r.episode_id, policy: (r) => r.policy, status: (r) => STATUS_LABELS[r.status] || r.status,
  findings: (r) => (r.findings ? r.findings.length : -1), f1: (r) => r.metrics?.f1 ?? -Infinity,
  reward: (r) => r.metrics?.reward ?? -Infinity, steps: (r) => r.steps ?? -1, elapsed: (r) => r.duration_seconds ?? -1,
};

function renderRuns(bundle, ctx, params) {
  const all = sliceRuns(bundle, ctx);
  const reviewer = rewardVisible(bundle);
  const q = (params.q || "").trim();
  const sort = SORTS[params.sort] ? params.sort : "episode";
  const dir = params.dir === "desc" ? "desc" : "asc";
  S.runsQuery = {q: params.q, pol: params.pol, st: params.st, sort: params.sort, dir: params.dir};
  let runs = all.filter((r) => (!q || r.episode_id.toLowerCase().includes(q.toLowerCase()))
    && (!params.pol || r.policy === params.pol) && (!params.st || r.status === params.st));
  const key = SORTS[sort];
  runs = [...runs].sort((a, b) => {
    const x = key(a), y = key(b);
    const order = typeof x === "string" ? x.localeCompare(y) : x - y;
    return (dir === "asc" ? order : -order) || a.episode_id.localeCompare(b.episode_id) || policyOrder([a.policy, b.policy]).indexOf(a.policy) - policyOrder([a.policy, b.policy]).indexOf(b.policy);
  });
  const base = {...ctxParams(ctx), q: params.q, pol: params.pol, st: params.st};
  const header = (id, label, numeric = false, opt = false) => {
    const active = sort === id;
    const next = active && dir === "asc" ? "desc" : "asc";
    const aria = active ? ` aria-sort="${dir === "asc" ? "ascending" : "descending"}"` : "";
    return `<th scope="col" class="${numeric ? "num" : ""} ${opt ? "col-opt" : ""}"${aria}><a class="sort" href="${href("runs", {...base, sort: id, dir: id === sort ? next : (numeric ? "desc" : "asc")})}">${esc(label)}<span aria-hidden="true">${active ? (dir === "asc" ? "▲" : "▼") : ""}</span></a></th>`;
  };
  const policies = policyOrder(all.map((r) => r.policy));
  const statuses = [...new Set(all.map((r) => r.status))];
  const filtered = q || params.pol || params.st;
  const selected = S.lastRunKey;
  main.innerHTML = `${noticeHtml()}
    <div class="page-head"><div><h1>Runs</h1><p>${esc(bundle.label)}</p></div></div>
    <section class="panel">
      <form class="toolbar" id="filters" role="search" aria-label="Filter runs">
        <label><span>Episode</span><input type="search" name="q" value="${esc(q)}" placeholder="EP-…" aria-label="Search episode ID" autocomplete="off" spellcheck="false"></label>
        <label><span>Policy</span><select name="pol"><option value="">All policies</option>${policies.map((p) => `<option ${p === params.pol ? "selected" : ""}>${esc(p)}</option>`).join("")}</select></label>
        <label><span>Status</span><select name="st"><option value="">All statuses</option>${statuses.map((s) => `<option value="${esc(s)}" ${s === params.st ? "selected" : ""}>${esc(STATUS_LABELS[s] || s)}</option>`).join("")}</select></label>
        ${filtered ? `<a class="btn" href="${href("runs", {...ctxParams(ctx), sort: params.sort, dir: params.dir})}">Reset Filters</a>` : ""}
        <span class="count" aria-live="polite">Showing ${runs.length} of ${all.length} runs · ${esc(splitLabel(ctx.s))} split · ${esc(ctx.m)} mode</span>
      </form>
      ${runs.length ? `<div class="table-wrap"><table>
        <caption class="visually-hidden">Runs, sorted by ${esc(sort)} ${dir === "asc" ? "ascending" : "descending"}</caption>
        <thead><tr>${header("episode", "Episode")}${header("policy", "Policy")}<th scope="col" class="col-opt">Mode</th>${header("status", "Status")}${header("findings", "Findings", true, true)}${reviewer ? header("f1", "F1", true) + header("reward", "Reward", true) : ""}${header("steps", "Steps", true)}${header("elapsed", "Elapsed", true, true)}</tr></thead>
        <tbody>${runs.map((r) => `<tr class="${r.key === selected ? "is-selected" : ""}">
          <td><a class="mono" translate="no" href="${href("run", {...ctxParams(ctx), r: r.key})}" data-run="${esc(r.key)}">${esc(r.episode_id)}</a></td>
          <td>${esc(r.policy)} <span class="role">${esc(r.role || "")}</span></td>
          <td class="col-opt">${esc(r.mode)}</td>
          <td>${statusHtml(r.status)}</td>
          <td class="num col-opt">${r.findings ? r.findings.length : `<span class="muted" title="${esc(findingsStateText(r))}">—</span>`}</td>
          ${reviewer ? `<td class="num" title="${esc(String(r.metrics?.f1 ?? ""))}">${metric(r.metrics?.f1, ctx.full)}</td><td class="num" title="${esc(String(r.metrics?.reward ?? ""))}">${metric(r.metrics?.reward, ctx.full)}</td>` : ""}
          <td class="num">${r.steps ?? "—"}</td>
          <td class="num col-opt">${elapsed(r.duration_seconds)}</td>
        </tr>`).join("")}</tbody></table></div>`
      : `<div class="empty"><h2>${all.length ? "No Runs Match These Filters" : "No Runs"}</h2><p>${all.length ? "Clear a filter to see more runs." : "This split and mode have no runs. Choose another split or source above."}</p>${filtered ? `<a class="btn" href="${href("runs", ctxParams(ctx))}">Clear Filters</a>` : ""}</div>`}
    </section>`;
  const form = main.querySelector("#filters");
  const apply = (focusName) => {
    const data = Object.fromEntries(new FormData(form));
    go("runs", {...ctxParams(ctx), ...data, sort: params.sort, dir: params.dir}, {replace: true, focus: `#filters [name=${focusName}]`});
  };
  form.onsubmit = (e) => { e.preventDefault(); apply("q"); };
  form.q.oninput = debounce(() => apply("q"), 250);
  form.pol.onchange = () => apply("pol");
  form.st.onchange = () => apply("st");
  if (q) { const input = form.q; input.setSelectionRange?.(input.value.length, input.value.length); }
}

function debounce(fn, ms) { let t; return () => { clearTimeout(t); t = setTimeout(fn, ms); }; }

/* ------------------------------------------------------------------ run detail */

function findingsStateText(run) {
  return {
    oracle: "Oracle runs on the evaluator side; its submission is not published to this dashboard.",
    invalid_submission: "The submission was structurally invalid, so the evaluator recorded no findings for it.",
    timeout: "The run timed out before submitting; no findings were recorded.",
    step_limit: "The run reached the 100-step limit before submitting; no findings were recorded.",
    unknown: "The run ended for an unrecorded reason; no findings list is available.",
    missing: "The findings list is unavailable in this report.",
  }[run.findings_state] || "";
}

async function renderRun(bundle, ctx, params) {
  const run = bundle.runs.find((r) => r.key === params.r);
  if (!run) {
    S.notice = "That run is not in the selected source, so the run list is shown instead.";
    return go("runs", {...ctxParams(ctx), ...S.runsQuery}, {replace: true});
  }
  if (run.split !== ctx.s || run.mode !== ctx.m) { ctx = {...ctx, s: run.split, m: run.mode}; }
  S.lastRunKey = run.key;
  const tab = ["findings", "activity", "records"].includes(params.tab) ? params.tab : "findings";
  const reviewer = rewardVisible(bundle);
  const m = run.metrics || {};
  const full = ctx.full;
  const meta = bundle.episodes[`${run.split}/${run.episode_id}`] || {};
  const runParams = {...ctxParams(ctx), r: run.key};
  const siblings = bundle.runs.filter((r) => r.episode_id === run.episode_id && r.split === run.split && r.mode === run.mode);
  const findingsCount = run.findings ? run.findings.length : null;

  const metricsStrip = `<dl class="metrics">
      <div><dt>Outcome</dt><dd class="small">${statusHtml(run.status)}</dd></div>
      ${reviewer ? `<div><dt>F1</dt><dd title="${esc(String(m.f1 ?? ""))}">${metric(m.f1, full)}</dd></div><div><dt>Reward</dt><dd title="${esc(String(m.reward ?? ""))}">${metric(m.reward, full)}</dd></div>` : `<div><dt>Evaluator metrics</dt><dd class="small">Withheld in public view</dd></div>`}
      <div><dt>Steps</dt><dd>${run.steps ?? "—"}</dd></div>
    </dl>
    <ul class="facts">
      ${reviewer ? `<li>TP <b>${m.tp ?? "—"}</b></li><li>FP <b>${m.fp ?? "—"}</b></li><li>FN <b>${m.fn ?? "—"}</b></li><li>Precision <b>${metric(m.precision, full)}</b></li><li>Recall <b>${metric(m.recall, full)}</b></li>` : ""}
      <li>Inspection steps <b>${run.inspection_steps ?? "—"}</b></li>
      <li>Elapsed <b>${elapsed(run.duration_seconds)}</b></li>
      <li>Split <b>${esc(splitLabel(run.split))}</b></li><li>Mode <b>${esc(run.mode)}</b></li>
    </ul>
    ${run.clean_established ? `<div class="callout info">The evaluator report shows TP 0, FP 0 and FN 0 for an empty, valid submission, so this episode has no planted issues. Under this scorer that gives F1 = 1 while precision and recall are 0; reward is reduced only by inspection steps.</div>` : ""}`;

  const others = siblings.length > 1 ? `<details class="panel panel-body disclose" style="margin-top:12px" ${run.clean_established ? "open" : ""}>
      <summary>Other Policies on <span class="mono" translate="no">${esc(run.episode_id)}</span> (${siblings.length - 1})</summary>
      <div class="table-wrap" style="margin-top:8px"><table class="mini"><thead><tr><th scope="col">Policy</th><th scope="col">Status</th><th scope="col" class="num">Findings</th>${reviewer ? `<th scope="col" class="num">F1</th><th scope="col" class="num">Reward</th>` : ""}<th scope="col" class="num">Steps</th></tr></thead>
      <tbody>${policyOrder(siblings.map((r) => r.policy)).map((p) => siblings.find((r) => r.policy === p)).map((r) => `<tr class="${r.key === run.key ? "is-selected" : ""}">
        <td>${r.key === run.key ? `${esc(r.policy)} <span class="role">this run</span>` : `<a href="${href("run", {...ctxParams(ctx), r: r.key, tab})}">${esc(r.policy)}</a>`} <span class="role">${esc(r.role || "")}</span></td>
        <td>${statusHtml(r.status)}</td><td class="num">${r.findings ? r.findings.length : "—"}</td>
        ${reviewer ? `<td class="num">${metric(r.metrics?.f1, full)}</td><td class="num">${metric(r.metrics?.reward, full)}</td>` : ""}<td class="num">${r.steps ?? "—"}</td></tr>`).join("")}</tbody></table></div></details>` : "";

  const tabLink = (id, label) => `<a href="${href("run", {...runParams, tab: id})}" ${tab === id ? 'aria-current="page"' : ""}>${label}</a>`;
  main.innerHTML = `${noticeHtml()}
    <nav class="crumbs" aria-label="Breadcrumb"><ol>
      <li><a href="${href("benchmark", ctxParams(ctx))}">Benchmark</a></li>
      <li><a href="${href("runs", {...ctxParams(ctx), ...S.runsQuery})}">Runs</a></li>
      <li><span class="mono" translate="no">${esc(run.episode_id)}</span></li>
      <li aria-current="page">${esc(run.policy)}</li></ol></nav>
    <h1 class="run-title"><span class="mono" translate="no">${esc(run.episode_id)}</span><span translate="no">${esc(run.policy)}</span></h1>
    <p class="muted" style="margin:2px 0 0">${esc(run.role || "Agent policy")}${meta.company ? ` · ${esc(meta.company)}` : ""}</p>
    ${metricsStrip}${others}
    <nav class="tabs" aria-label="Run sections">${tabLink("findings", `Findings${findingsCount !== null ? `<span class="count">${findingsCount}</span>` : ""}`)}${tabLink("activity", "Activity")}${tabLink("records", "Episode Records")}</nav>
    <div id="tab-body"><p class="loading" style="padding:16px 0">Loading…</p></div>`;

  const body = main.querySelector("#tab-body");
  if (tab === "activity") return renderActivity(body, bundle, run);
  const episode = await loadEpisode(bundle, run);
  if (tab === "records") return renderRecords(body, bundle, ctx, run, params, episode);
  return renderFindings(body, bundle, ctx, run, params, episode);
}

/* ------------------------------------------------------------------ findings + inspector */

function recordsUnavailable(meta) {
  const problems = meta.problems && meta.problems.length ? `<ul>${meta.problems.map((p) => `<li>${esc(p)}</li>`).join("")}</ul>` : "";
  const message = meta.message || "Public records for this episode are unavailable in this bundle.";
  return `<div class="callout ${meta.status === "invalid" ? "error" : "warn"}">${esc(message)}${meta.status === "unsupported" ? " Re-generate the episode with the current generator to inspect its records." : ""}${problems}</div>`;
}

function renderFindings(body, bundle, ctx, run, params, episode) {
  const runParams = {...ctxParams(ctx), r: run.key};
  if (run.findings_state !== "available") {
    body.innerHTML = `<div class="panel" style="margin-top:16px"><div class="empty"><h2>${run.findings_state === "oracle" ? "Findings Are Evaluator-Side" : run.findings_state === "missing" ? "Findings Unavailable" : "No Submission Recorded"}</h2><p>${esc(findingsStateText(run))}</p></div></div>`;
    return;
  }
  const findings = run.findings;
  if (!findings.length) {
    body.innerHTML = `<div class="panel" style="margin-top:16px"><div class="empty"><h2>No Submitted Findings</h2><p>The policy submitted an empty, valid findings list${run.clean_established ? ", which is the correct answer for this episode" : ""}.</p></div></div>`;
    return;
  }
  const fIndex = Number(params.f) >= 1 && Number(params.f) <= findings.length ? Number(params.f) : null;
  const finding = fIndex ? findings[fIndex - 1] : null;
  const ep = episode.index;
  const exp = (cur) => (ep ? ep.exponent(cur) : 2);
  body.innerHTML = `
    <p class="note" style="margin-top:12px">TP, FP and FN are run-level totals. The report does not say which submission matched, so each finding is labelled Submitted and its records are described as cited evidence.</p>
    ${episode.meta.status !== "ok" ? recordsUnavailable(episode.meta) : ""}
    <div class="workbench ${finding ? "inspecting" : ""}">
      <div class="panel list-pane"><div class="table-wrap"><table>
        <caption>Submitted findings (${findings.length})</caption>
        <thead><tr><th scope="col" class="num">#</th><th scope="col">Type</th><th scope="col">Affected</th><th scope="col" class="num">Claimed amount</th><th scope="col" class="num col-opt">Evidence</th></tr></thead>
        <tbody>${findings.map((f, i) => `<tr class="${fIndex === i + 1 ? "is-selected" : ""}">
          <td class="num">${i + 1}</td>
          <td><a href="${href("run", {...runParams, tab: "findings", f: i + 1})}" data-finding="${i + 1}" ${fIndex === i + 1 ? 'aria-current="true"' : ""}>${esc(typeLabel(f.type))}</a></td>
          <td class="mono" translate="no">${esc(f.affected_ids.join(", "))}</td>
          <td class="num">${esc(money(f.claimed_amount_minor, exp(f.currency)))} ${esc(f.currency)}</td>
          <td class="num col-opt">${f.evidence_ids.length}</td></tr>`).join("")}</tbody></table></div></div>
      <aside class="panel inspector" aria-labelledby="inspector-title">${finding ? inspectorHtml(run, runParams, finding, fIndex, params.e, episode) : `<div class="panel-body"><h2 id="inspector-title" tabindex="-1">Evidence Inspector</h2><p class="note" style="margin-top:6px">Select a finding to see its explanation and the records it cites.</p></div>`}</aside>
    </div>`;
  wireCopy(body);
}

function inspectorHtml(run, runParams, f, fIndex, evidenceId, episode) {
  const ep = episode.index;
  const exp = ep ? ep.exponent(f.currency) : 2;
  const selected = evidenceId || null;
  return `<div class="panel-head"><h2 id="inspector-title" tabindex="-1">Finding ${fIndex} · Submitted</h2>
      <a class="btn small back-mobile" href="${href("run", {...runParams, tab: "findings"})}" data-back="finding-list">Back to Findings</a></div>
    <section><dl class="kv">
      <dt>Type</dt><dd>${esc(typeLabel(f.type))} <code class="muted" translate="no">${esc(f.type)}</code></dd>
      <dt>Affected</dt><dd class="mono" translate="no">${esc(f.affected_ids.join(", "))}</dd>
      <dt>Claimed amount</dt><dd><b>${esc(money(f.claimed_amount_minor, exp))} ${esc(f.currency)}</b> <span class="muted mono" translate="no">${esc(f.claimed_amount_minor)} minor units</span></dd>
    </dl></section>
    <section><h3 class="eyebrow sect-title">Policy Explanation</h3>${f.explanation ? `<p class="explanation">${esc(f.explanation)}</p>` : `<p class="note">No explanation was submitted.</p>`}</section>
    <section><h3 class="eyebrow sect-title">Evidence Trail · ${f.evidence_ids.length} Cited</h3>${f.evidence_ids.length ? evidenceTrail(f, fIndex, runParams, selected, ep, exp) : `<p class="note">This finding cites no evidence.</p>`}</section>
    ${selected ? `<section id="record-section">${recordPanel(selected, ep, (id) => href("run", {...runParams, tab: "findings", f: fIndex, e: id}), href("run", {...runParams, tab: "findings", f: fIndex}), "Back to Finding")}</section>` : ""}`;
}

/** Finding → cited records. One straight orthogonal connector per record, each with its own attach point. */
function evidenceTrail(f, fIndex, runParams, selected, ep, exp) {
  const LIMIT = 8, ROW = 56, GAP = 8, PITCH = ROW + GAP;
  const shown = f.evidence_ids.slice(0, LIMIT), rest = f.evidence_ids.slice(LIMIT);
  const height = shown.length * PITCH - GAP;
  const nodes = shown.map((id) => ({id, hit: ep ? ep.resolve(id) : null}));
  const connectors = nodes.map((n, i) => {
    const y = i * PITCH + ROW / 2;
    return `<path class="${n.hit ? "" : "missing"}" d="M0 ${y} H34"/><path class="tip" d="M34 ${y - 4} L40 ${y} L34 ${y + 4} Z"/>`;
  }).join("");
  const link = (id) => href("run", {...runParams, tab: "findings", f: fIndex, e: id});
  const item = ({id, hit}) => {
    const d = hit ? nodeText(hit, ep) : {kind: ep ? "Unavailable" : "Records unavailable", name: "Record unavailable in this bundle", sub: id};
    return `<li><a class="node ${hit ? "" : "missing"}" href="${link(id)}" data-ev="${esc(id)}" ${id === selected ? 'aria-current="true"' : ""}>
      <span class="eyebrow">${esc(d.kind)}</span><span class="name">${esc(d.name)}</span><span class="sub" translate="no">${esc(d.sub)}</span></a></li>`;
  };
  return `<div class="trail">
      <div class="trail-source" style="height:${height}px"><span class="eyebrow">Finding ${fIndex}</span><b>${esc(typeLabel(f.type))}</b><span class="mono" translate="no">${esc(money(f.claimed_amount_minor, exp))} ${esc(f.currency)}</span></div>
      <svg viewBox="0 0 40 ${height}" height="${height}" preserveAspectRatio="none" aria-hidden="true" focusable="false">${connectors}</svg>
      <ol aria-label="Cited evidence">${nodes.map(item).join("")}</ol>
    </div>${rest.length ? `<p class="trail-more">${rest.length} more cited: ${rest.map((id) => `<a class="mono" translate="no" href="${link(id)}" data-ev="${esc(id)}">${esc(id)}</a>`).join(", ")}</p>` : ""}`;
}

function nodeText(hit, ep) {
  const r = hit.record, m = (v, c) => `${money(v, ep.exponent(c))} ${c}`;
  if (hit.kind === "invoice") return {kind: "Invoice", name: `${r.invoice_id}${r.entity_id ? ` · ${r.entity_id}` : ""}`, sub: `${r.doc_id} · ${m(r.total_minor, r.currency)}`};
  if (hit.kind === "payment") return {kind: "Payment", name: `${r.payment_id}${r.entity_id ? ` · ${r.entity_id}` : ""}`, sub: `${r.doc_id} · ${m(r.amount_minor, r.currency)}`};
  if (hit.kind === "ledger") return {kind: "Ledger row", name: accountText(ep, r.account), sub: `${r.row_id} · ${r.debit_minor ? "Dr " + m(r.debit_minor, r.currency) : "Cr " + m(r.credit_minor, r.currency)}`};
  return {kind: "FX rate", name: `${r.source_currency} → ${r.target_currency} on ${r.date}`, sub: `rate ${r.rate}`};
}

function accountText(ep, code) {
  const role = ep.roles.get(code);
  const label = {ar: "Receivables", bank: "Bank", revenue: "Revenue", unapplied: "Unapplied", expense: "Expense"}[role] || role;
  return label ? `${code} ${label}` : code;
}

function recordSummary(hit, ep) {
  const r = hit.record;
  if (hit.kind === "invoice") return `<span>Invoice ${esc(r.invoice_id)} · ${esc(money(r.total_minor, ep.exponent(r.currency)))} ${esc(r.currency)}</span>`;
  if (hit.kind === "payment") return `<span>Payment ${esc(r.payment_id)} · ${esc(money(r.amount_minor, ep.exponent(r.currency)))} ${esc(r.currency)}</span>`;
  if (hit.kind === "ledger") return `<span>Ledger ${esc(accountText(ep, r.account))} · ${r.debit_minor ? "Dr " + esc(money(r.debit_minor, ep.exponent(r.currency))) : "Cr " + esc(money(r.credit_minor, ep.exponent(r.currency)))} ${esc(r.currency)}</span>`;
  return `<span>FX ${esc(r.source_currency)}→${esc(r.target_currency)} ${esc(r.date)} · ${esc(r.rate)}</span>`;
}

function recordPanel(id, ep, linkFor, backHref, backLabel) {
  const back = `<a class="btn" href="${backHref}" data-back="record">${esc(backLabel)}</a>`;
  if (!ep) return `<h3>Record</h3><p class="mono" translate="no">${esc(id)}</p><p class="note">Public records are unavailable for this episode.</p>${back}`;
  const hit = ep.resolve(id);
  if (!hit) return `<h3 id="record-title" tabindex="-1">Record Unavailable in This Bundle</h3><p><span class="mono" translate="no">${esc(id)}</span> ${copyBtn(id)}</p>
    <p class="note">No invoice, payment, ledger row or FX rate in this episode has this evidence ID. Business IDs (invoice or payment numbers, entry or counterparty IDs) are not evidence records.</p>${back}`;
  const r = hit.record;
  const cur = r.currency;
  const m = (v) => `${esc(money(v, ep.exponent(cur)))} ${esc(cur)}`;
  const link = (rid) => `<a class="mono" translate="no" href="${linkFor(rid)}">${esc(rid)}</a>`;
  const ledgerTable = (rows, title) => rows && rows.length ? `<h3 class="eyebrow sect-title" style="margin-top:16px">${esc(title)}</h3><div class="table-wrap"><table class="mini"><thead><tr><th scope="col">Row</th><th scope="col">Local time</th><th scope="col">Account</th><th scope="col" class="num">Debit</th><th scope="col" class="num">Credit</th></tr></thead><tbody>${rows.map((row) => `<tr><td>${link(row.row_id)}</td><td class="mono" translate="no">${esc(timestamp(row.timestamp))}</td><td>${esc(accountText(ep, row.account))}</td><td class="num">${row.debit_minor ? esc(money(row.debit_minor, ep.exponent(row.currency))) : ""}</td><td class="num">${row.credit_minor ? esc(money(row.credit_minor, ep.exponent(row.currency))) : ""}</td></tr>`).join("")}</tbody></table></div>` : `<p class="note" style="margin-top:12px">${esc(title)}: none.</p>`;
  let fields = "", extra = "";
  const kind = {invoice: "Invoice", payment: "Payment", ledger: "Ledger row", fx: "FX rate"}[hit.kind];
  if (hit.kind === "invoice") {
    fields = kv([["Document", `<span class="mono" translate="no">${esc(r.doc_id)}</span>`], ["Invoice number", `<span class="mono" translate="no">${esc(r.invoice_id)}</span>`], ["Counterparty", esc(r.entity_id ?? "—")], ["Issued (local)", `<span class="mono" translate="no">${esc(timestamp(r.issued_at))}</span>`], ["Due (local)", `<span class="mono" translate="no">${esc(timestamp(r.due_at))}</span>`], ["Total", m(r.total_minor)]]);
    extra = `<h3 class="eyebrow sect-title" style="margin-top:16px">Line items</h3><div class="table-wrap"><table class="mini"><thead><tr><th scope="col">Line</th><th scope="col">Description</th><th scope="col" class="num">Qty</th><th scope="col" class="num">Unit price</th><th scope="col" class="num">Amount</th></tr></thead><tbody>${(r.line_items || []).map((l) => `<tr><td class="mono" translate="no">${esc(l.line_id)}</td><td>${esc(l.description)}</td><td class="num">${esc(l.quantity)}</td><td class="num">${esc(money(l.unit_price_minor, ep.exponent(cur)))}</td><td class="num">${esc(money(l.amount_minor, ep.exponent(cur)))}</td></tr>`).join("")}</tbody></table></div>`
      + ledgerTable(ep.ledgerByInvoice.get(r.invoice_id), "Ledger rows referencing this invoice")
      + (() => { const pays = ep.paymentsByInvoice.get(r.invoice_id) || []; return `<h3 class="eyebrow sect-title" style="margin-top:16px">Payments allocating to this invoice</h3>${pays.length ? `<ul class="cites">${pays.map((p) => `<li>${link(p.payment.doc_id)} Payment ${esc(p.payment.payment_id)} · ref ${p.order + 1} · ${esc(money(p.ref.amount_minor, ep.exponent(p.payment.currency)))} ${esc(p.payment.currency)}</li>`).join("")}</ul>` : `<p class="note">None.</p>`}`; })();
  } else if (hit.kind === "payment") {
    fields = kv([["Document", `<span class="mono" translate="no">${esc(r.doc_id)}</span>`], ["Payment number", `<span class="mono" translate="no">${esc(r.payment_id)}</span>`], ["Counterparty", esc(r.entity_id ?? "—")], ["Received (local)", `<span class="mono" translate="no">${esc(timestamp(r.timestamp))}</span>`], ["Amount", m(r.amount_minor)]]);
    extra = `<h3 class="eyebrow sect-title" style="margin-top:16px">Allocation (invoice_refs, in recorded order)</h3>${r.invoice_refs === null ? `<p class="note">No allocation recorded (null).</p>` : r.invoice_refs.length ? `<div class="table-wrap"><table class="mini"><thead><tr><th scope="col" class="num">#</th><th scope="col">Invoice</th><th scope="col" class="num">Amount</th></tr></thead><tbody>${r.invoice_refs.map((ref, i) => { const inv = ep.invoiceById.get(ref.invoice_id); return `<tr><td class="num">${i + 1}</td><td>${inv ? `${link(inv.doc_id)} ${esc(ref.invoice_id)}` : `<span class="mono" translate="no">${esc(ref.invoice_id)}</span> <span class="muted">invoice not in this episode</span>`}</td><td class="num">${esc(money(ref.amount_minor, ep.exponent(cur)))} ${esc(cur)}</td></tr>`; }).join("")}</tbody></table></div>` : `<p class="note">Empty allocation list.</p>`}`
      + ledgerTable(ep.ledgerByPayment.get(r.payment_id), "Ledger rows referencing this payment");
  } else if (hit.kind === "ledger") {
    const inv = r.invoice_id ? ep.invoiceById.get(r.invoice_id) : null, pay = r.payment_id ? ep.paymentById.get(r.payment_id) : null;
    fields = kv([["Row", `<span class="mono" translate="no">${esc(r.row_id)}</span>`], ["Entry", `<span class="mono" translate="no">${esc(r.entry_id)}</span>`], ["Local time", `<span class="mono" translate="no">${esc(timestamp(r.timestamp))}</span>`], ["Account", esc(accountText(ep, r.account))], ["Debit", r.debit_minor ? m(r.debit_minor) : "0"], ["Credit", r.credit_minor ? m(r.credit_minor) : "0"], ["Counterparty", esc(r.entity_id ?? "—")],
      ["Invoice", r.invoice_id ? (inv ? `${link(inv.doc_id)} ${esc(r.invoice_id)}` : `<span class="mono" translate="no">${esc(r.invoice_id)}</span> <span class="muted">not in this episode</span>`) : "—"],
      ["Payment", r.payment_id ? (pay ? `${link(pay.doc_id)} ${esc(r.payment_id)}` : `<span class="mono" translate="no">${esc(r.payment_id)}</span> <span class="muted">not in this episode</span>`) : "—"]]);
    extra = ledgerTable((ep.ledgerByEntry.get(r.entry_id) || []).filter((row) => row.row_id !== r.row_id), `Other rows in entry ${r.entry_id}`);
  } else {
    const fx = ep.data.policy && ep.data.policy.fx;
    fields = kv([["Date", `<span class="mono" translate="no">${esc(r.date)}</span>`], ["Pair", `${esc(r.source_currency)} → ${esc(r.target_currency)}`], ["Rate", `<span class="mono" translate="no">${esc(r.rate)}</span>`], ["Meaning", `1 ${esc(r.source_currency)} = ${esc(r.rate)} ${esc(r.target_currency)}${fx && fx.date_rule ? ` · date rule <code>${esc(fx.date_rule)}</code>` : ""}`]]);
  }
  return `<div style="display:flex;justify-content:space-between;align-items:center;gap:8px;flex-wrap:wrap;margin-bottom:8px">
      <h3 id="record-title" tabindex="-1" style="margin:0;color:var(--ink);font-size:13px">${esc(kind)} <span class="mono" translate="no">${esc(id)}</span></h3>
      <span style="display:inline-flex;gap:20px;align-items:baseline">${copyBtn(id)}${back}</span></div>
    ${fields}${extra}
    <details class="raw" style="margin-top:12px"><summary>Raw Record JSON</summary><pre>${esc(JSON.stringify(r, null, 2))}</pre></details>`;
}

function kv(pairs) { return `<dl class="kv">${pairs.map(([k, v]) => `<dt>${esc(k)}</dt><dd>${v}</dd>`).join("")}</dl>`; }
function copyBtn(id) { return `<button type="button" class="btn small" data-copy="${esc(id)}" aria-label="Copy ID ${esc(id)}">Copy ID</button>`; }
function wireCopy(root) {
  root.addEventListener("click", async (e) => {
    const button = e.target.closest("[data-copy]");
    if (!button) return;
    const value = button.dataset.copy;
    try { await navigator.clipboard.writeText(value); announce(`Copied ${value}`); button.textContent = "Copied"; }
    catch { announce(`Copy failed; select the ID ${value} manually`); }
    setTimeout(() => { button.textContent = "Copy ID"; }, 1500);
  });
}

/* ------------------------------------------------------------------ records tab */

const TABLES = {
  ledger: {label: "Ledger", kind: "ledger", rows: (d) => d.ledger},
  invoices: {label: "Invoices", kind: "invoice", rows: (d) => d.invoices},
  payments: {label: "Payments", kind: "payment", rows: (d) => d.payments},
  fx: {label: "FX rates", kind: "fx", rows: (d) => d.fx},
  rules: {label: "Accounting rules"},
};
const ROW_LIMIT = 200;

function renderRecords(body, bundle, ctx, run, params, episode) {
  if (episode.meta.status !== "ok") { body.innerHTML = `<div style="margin-top:16px">${recordsUnavailable(episode.meta)}</div>`; return; }
  const ep = episode.index, data = ep.data;
  const t = TABLES[params.t] ? params.t : "ledger";
  const base = {...ctxParams(ctx), r: run.key, tab: "records"};
  const sub = Object.entries(TABLES).map(([id, spec]) => `<a href="${href("run", {...base, t: id})}" ${id === t ? 'aria-current="page"' : ""}>${esc(spec.label)}${spec.rows ? ` <span class="muted">${spec.rows(data).length}</span>` : ""}</a>`).join("");
  const man = data.manifest;
  const head = `<p class="note" style="margin-top:12px">${esc(man.company?.name || man.episode_id)} · base currency ${esc(man.base_currency)} · period ${esc(timestamp(man.period_start))} to ${esc(timestamp(man.period_end))} (end exclusive) · ${esc(man.timezone)} ${esc(man.utc_offset)}</p>`;
  if (t === "rules") {
    body.innerHTML = `${head}<div class="panel" style="margin-top:12px"><nav class="subtabs" aria-label="Record tables">${sub}</nav><div class="panel-body">${rulesHtml(data)}</div></div>`;
    return;
  }
  const spec = TABLES[t];
  const q = (params.rq || "").trim().toLowerCase();
  const rows = spec.rows(data).filter((r) => !q || JSON.stringify(r).toLowerCase().includes(q));
  const shown = S.showAll ? rows : rows.slice(0, ROW_LIMIT);
  const rec = params.rec || null;
  const recHref = (id) => href("run", {...base, t, rq: params.rq, rec: id});
  const exp = (c) => ep.exponent(c);
  const cols = {
    ledger: [["Row", (r) => `<a class="mono" translate="no" href="${recHref(r.row_id)}" data-rec="${esc(r.row_id)}">${esc(r.row_id)}</a>`], ["Entry", (r) => `<span class="mono" translate="no">${esc(r.entry_id)}</span>`, true], ["Local time", (r) => `<span class="mono" translate="no">${esc(timestamp(r.timestamp))}</span>`, true], ["Account", (r) => esc(accountText(ep, r.account))], ["Debit", (r) => r.debit_minor ? esc(money(r.debit_minor, exp(r.currency))) : "", false, true], ["Credit", (r) => r.credit_minor ? esc(money(r.credit_minor, exp(r.currency))) : "", false, true], ["Cur", (r) => esc(r.currency)], ["Invoice", (r) => `<span class="mono" translate="no">${esc(r.invoice_id ?? "")}</span>`, true], ["Payment", (r) => `<span class="mono" translate="no">${esc(r.payment_id ?? "")}</span>`, true]],
    invoices: [["Document", (r) => `<a class="mono" translate="no" href="${recHref(r.doc_id)}" data-rec="${esc(r.doc_id)}">${esc(r.doc_id)}</a>`], ["Invoice", (r) => `<span class="mono" translate="no">${esc(r.invoice_id)}</span>`], ["Issued (local)", (r) => `<span class="mono" translate="no">${esc(timestamp(r.issued_at))}</span>`, true], ["Counterparty", (r) => esc(r.entity_id ?? ""), true], ["Total", (r) => esc(money(r.total_minor, exp(r.currency))), false, true], ["Cur", (r) => esc(r.currency)]],
    payments: [["Document", (r) => `<a class="mono" translate="no" href="${recHref(r.doc_id)}" data-rec="${esc(r.doc_id)}">${esc(r.doc_id)}</a>`], ["Payment", (r) => `<span class="mono" translate="no">${esc(r.payment_id)}</span>`], ["Received (local)", (r) => `<span class="mono" translate="no">${esc(timestamp(r.timestamp))}</span>`, true], ["Amount", (r) => esc(money(r.amount_minor, exp(r.currency))), false, true], ["Cur", (r) => esc(r.currency)], ["Allocates to", (r) => r.invoice_refs === null ? `<span class="muted">null</span>` : esc(r.invoice_refs.map((x) => x.invoice_id).join(", ")), true]],
    fx: [["Evidence ID", (r) => { const id = recordId("fx", r); return `<a class="mono" translate="no" href="${recHref(id)}" data-rec="${esc(id)}">${esc(id)}</a>`; }], ["Date", (r) => `<span class="mono" translate="no">${esc(r.date)}</span>`, true], ["Pair", (r) => `${esc(r.source_currency)} → ${esc(r.target_currency)}`], ["Rate", (r) => `<span class="mono" translate="no">${esc(r.rate)}</span>`, false, true]],
  }[t];
  body.innerHTML = `${head}
    <div class="workbench ${rec ? "inspecting" : ""}">
      <div class="panel list-pane">
        <nav class="subtabs" aria-label="Record tables">${sub}</nav>
        <form class="toolbar" id="rec-filter" role="search" aria-label="Filter ${esc(spec.label)}" style="border-bottom:none">
          <label><span>Filter</span><input type="search" name="rq" value="${esc(params.rq || "")}" placeholder="ID, amount, date…" autocomplete="off" spellcheck="false"></label>
          <span class="count">${rows.length === spec.rows(data).length ? `${rows.length} rows` : `${rows.length} of ${spec.rows(data).length} rows`}${shown.length < rows.length ? ` · showing first ${shown.length}` : ""}</span>
        </form>
        ${rows.length ? `<div class="table-wrap tall" tabindex="0" role="region" aria-label="${esc(spec.label)} table, scrolls horizontally and vertically"><table>
          <thead><tr>${cols.map(([label, , opt, num]) => `<th scope="col" class="${num ? "num" : ""} ${opt ? "col-opt" : ""}">${esc(label)}</th>`).join("")}</tr></thead>
          <tbody>${shown.map((r) => `<tr class="${recordId(spec.kind, r) === rec ? "is-selected" : ""}">${cols.map(([, fn, opt, num]) => `<td class="${num ? "num" : ""} ${opt ? "col-opt" : ""}">${fn(r)}</td>`).join("")}</tr>`).join("")}</tbody>
        </table></div>${shown.length < rows.length ? `<div class="panel-body"><button type="button" class="btn" id="show-all">Show All ${rows.length} Rows</button></div>` : ""}`
        : `<div class="empty"><p>No ${esc(spec.label.toLowerCase())} match “${esc(params.rq)}”.</p><a class="btn" href="${href("run", {...base, t})}">Clear Filter</a></div>`}
      </div>
      <aside class="panel inspector" aria-labelledby="inspector-title">${rec ? `<div class="panel-head"><h2 id="inspector-title" tabindex="-1">Record Inspector</h2></div><section id="record-section">${recordPanel(rec, ep, (id) => href("run", {...base, t, rq: params.rq, rec: id}), href("run", {...base, t, rq: params.rq}), "Back to Table")}</section>` : `<div class="panel-body"><h2 id="inspector-title" tabindex="-1">Record Inspector</h2><p class="note" style="margin-top:6px">Select a record ID to see its fields and linked records.</p></div>`}</aside>
    </div>`;
  const form = body.querySelector("#rec-filter");
  if (form) {
    const apply = () => { S.showAll = false; go("run", {...base, t, rq: form.rq.value}, {replace: true, focus: "#rec-filter [name=rq]"}); };
    form.onsubmit = (e) => { e.preventDefault(); apply(); };
    form.rq.oninput = debounce(apply, 250);
  }
  const showAll = body.querySelector("#show-all");
  if (showAll) showAll.onclick = () => { S.showAll = true; render(); };
  wireCopy(body);
}

function rulesHtml(data) {
  const p = data.policy || {};
  const rows = [];
  if (p.period) rows.push(["Reporting period", `${esc(timestamp(p.period.start))} to ${esc(timestamp(p.period.end))}, end ${p.period.end_exclusive ? "exclusive" : "inclusive"}, ${esc(p.period.timezone)} (${esc(p.period.utc_offset)})`]);
  if (p.recognition) rows.push(["Recognition", Object.entries(p.recognition).map(([k, v]) => `${esc(k)}: <code>${esc(v)}</code>`).join("; ")]);
  if (p.fx) rows.push(["FX conversion", `target ${esc(p.fx.target_currency)}, rate date <code>${esc(p.fx.date_rule)}</code>, direction <code>${esc(p.fx.rate_direction)}</code>`]);
  if (p.rounding) rows.push(["Rounding", `<code>${esc(p.rounding.mode)}</code> applied <code>${esc(p.rounding.scope)}</code>`]);
  if (p.allocation) rows.push(["Payment allocation", Object.entries(p.allocation).map(([k, v]) => `${esc(k)}: <code>${esc(v)}</code>`).join("; ")]);
  if (p.accounts) rows.push(["Accounts", Object.entries(p.accounts).map(([role, code]) => `${esc(role)} <span class="mono" translate="no">${esc([].concat(code).join(", "))}</span>`).join(" · ")]);
  const exps = data.manifest.currency_exponents || {};
  rows.push(["Currency exponents", Object.entries(exps).map(([c, e]) => `${esc(c)} ${e}`).join(" · ")]);
  return `<dl class="kv">${rows.map(([k, v]) => `<dt>${esc(k)}</dt><dd>${v}</dd>`).join("")}</dl>
    <details class="raw" style="margin-top:12px"><summary>Raw Accounting Rules (policy.json)</summary><pre>${esc(JSON.stringify(p, null, 2))}</pre></details>`;
}

/* ------------------------------------------------------------------ activity */

async function renderActivity(body, bundle, run) {
  const state = run.activity_state;
  const message = {
    oracle: "Oracle runs on the evaluator side. An agent activity log is unavailable.",
    release_dev: "No activity log was included for this dev run. The release export records train-split trajectories only, and those are not joined to dev runs.",
    missing: "Activity unavailable for this run: the source has no trajectory rows for it.",
  }[state];
  if (message) { body.innerHTML = `<div class="panel" style="margin-top:16px"><div class="empty"><h2>Activity Unavailable for This Run</h2><p>${esc(message)}</p></div></div>`; return; }
  const rows = await loadActivity(bundle, run);
  if (!rows) { body.innerHTML = `<div class="callout error" style="margin-top:16px">The activity file for this run could not be loaded.</div>`; return; }
  const closed = run.mode === "closed";
  body.innerHTML = `
    ${closed ? `<div class="callout info" style="margin-top:16px">Closed mode: the environment withholds reward during the run, so every recorded step shows reward 0. The final evaluator reward is in the run metrics above.</div>` : `<div class="callout info" style="margin-top:16px">Train mode: the terminal step records the episode reward; inspection steps record 0.</div>`}
    ${state === "partial" ? `<div class="callout warn">This log is partial: ${rows.length} step${rows.length === 1 ? "" : "s"} recorded, the report counts ${esc(run.steps ?? "unknown")}. Missing steps are not shown.</div>` : ""}
    <div class="panel"><div class="panel-head"><h2>Recorded Actions</h2><span class="note">${rows.length} step${rows.length === 1 ? "" : "s"}, ordered by step number</span></div>
    <ol class="activity">${rows.map((row) => `<li><span class="n">${row.step}</span><div>
      <div><span class="act">${esc(actionSummary(row.action))}</span></div>
      <div class="obs">${esc(observationSummary(row))}</div>
      <details><summary>Structured detail</summary><pre>${esc(JSON.stringify({action: row.action, observation: row.observation, reward: row.reward, terminated: row.terminated, truncated: row.truncated}, null, 2))}</pre></details>
    </div></li>`).join("")}</ol></div>`;
}

function actionSummary(a) {
  if (!a || !a.action) return "unrecognised action";
  if (a.action === "list_records") return `list_records ${a.table} · ${a.cursor ? `from ${a.cursor}` : "from start"} · limit ${a.limit ?? "default"}`;
  if (a.action === "get_record") return `get_record ${a.table} ${a.id}`;
  if (a.action === "get_document") return `get_document ${a.doc_id}`;
  if (a.action === "calculator") return `calculator ${a.expression}`;
  if (a.action === "submit") { const n = a.findings?.findings?.length ?? 0; return `submit · ${n} finding${n === 1 ? "" : "s"}`; }
  return a.action;
}

function observationSummary(row) {
  const o = row.observation || {};
  const parts = [];
  if (o.error) parts.push(`error: ${o.error}`);
  else if (Array.isArray(o.result)) parts.push(`${o.result.length} row${o.result.length === 1 ? "" : "s"}${o.next_cursor ? `, next ${o.next_cursor}` : ", last page"}`);
  else if (o.result === "episode_finished") parts.push("episode finished");
  else if (o.result && typeof o.result === "object") parts.push("record returned");
  else if (o.result !== undefined) parts.push(`= ${o.result}`);
  if (o.steps_remaining !== undefined) parts.push(`${o.steps_remaining} steps left`);
  if (row.terminated) parts.push("terminated");
  if (row.truncated) parts.push("truncated");
  if (row.reward !== undefined) parts.push(`recorded reward ${metric(row.reward)}`);
  return parts.join(" · ");
}

/* ------------------------------------------------------------------ boot */

window.addEventListener("hashchange", () => render());
document.addEventListener("click", (e) => {
  const back = e.target.closest("[data-back]");
  if (back && back.dataset.back === "record") {
    const evidence = new URLSearchParams(location.hash.split("?")[1] || "").get("e") || new URLSearchParams(location.hash.split("?")[1] || "").get("rec");
    if (evidence) S.focus = `[data-ev="${CSS.escape(evidence)}"], [data-rec="${CSS.escape(evidence)}"]`;
  } else if (back && back.dataset.back === "finding-list") {
    const f = new URLSearchParams(location.hash.split("?")[1] || "").get("f");
    if (f) S.focus = `[data-finding="${CSS.escape(f)}"]`;
  } else {
    const opener = e.target.closest("[data-finding], [data-ev], [data-rec]");
    if (opener && opener.tagName === "A") S.focus = opener.dataset.finding ? "#inspector-title" : "#record-title";
  }
});
render();
