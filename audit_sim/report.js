"use strict";

const policy = document.getElementById("policy");
const episode = document.getElementById("episode");
const metric = document.getElementById("metric");
const counter = document.getElementById("visible-count");
const chart = document.getElementById("policy-chart");
const axis = document.getElementById("comparison-axis");
const emptyChart = document.getElementById("comparison-empty");
const runs = Array.from(document.querySelectorAll(".session"));
const policyNames = new Map(Array.from(policy.options, option => [option.value, option.textContent]));
const number = new Intl.NumberFormat("ru", { maximumFractionDigits: 3 });

function element(tag, className, text = "") {
  const node = document.createElement(tag);
  node.className = className;
  node.textContent = text;
  return node;
}

function drawComparison(visible) {
  const key = metric.value;
  const minimum = key === "reward" ? -1 : 0;
  const maximum = key === "steps" ? Number(chart.dataset.stepLimit) : 1;
  const position = value => 100 * (value - minimum) / (maximum - minimum);
  const zero = position(0);
  const totals = new Map();
  for (const run of visible) {
    const name = run.dataset.policy;
    if (!totals.has(name)) totals.set(name, { sum: 0, count: 0 });
    const total = totals.get(name);
    total.sum += Number(run.dataset[key]);
    total.count += 1;
  }

  const bars = [];
  for (const [name, title] of policyNames) {
    if (!totals.has(name)) continue;
    const { sum, count } = totals.get(name);
    const value = sum / count;
    const end = position(value);
    const row = element("li", "comparison-row");
    const label = element("span", "policy-name", title);
    label.append(element("small", "policy-count", `${count} из ${visible.length}`));
    const track = element("span", "comparison-track");
    track.setAttribute("aria-hidden", "true");
    track.style.setProperty("--zero", `${zero}%`);
    const fill = element("span", value < 0 ? "comparison-fill negative" : "comparison-fill");
    fill.style.left = `${Math.min(end, zero)}%`;
    fill.style.width = `${Math.abs(end - zero)}%`;
    track.append(fill);
    row.append(label, track, element("strong", "comparison-value", number.format(Math.abs(value) < 0.0005 ? 0 : value)));
    bars.push(row);
  }
  chart.replaceChildren(...bars);
  const ticks = [minimum, (minimum + maximum) / 2, maximum];
  axis.replaceChildren(...ticks.map(value => element("span", "axis-tick", number.format(value))));
  axis.hidden = bars.length === 0;
  emptyChart.hidden = bars.length > 0;
}

function applyFilters() {
  const text = episode.value.trim().toLowerCase();
  const visible = [];
  for (const run of runs) {
    const policyMatches = !policy.value || policy.value === run.dataset.policy;
    const episodeMatches = run.dataset.episode.toLowerCase().includes(text);
    run.hidden = !policyMatches || !episodeMatches;
    if (!run.hidden) visible.push(run);
  }
  counter.textContent = `Показано ${visible.length} из ${runs.length}`;
  drawComparison(visible);
}

function drawTrace(canvas, points, current, series, cited) {
  const width = canvas.clientWidth;
  const height = canvas.clientHeight;
  if (!width || !height) return;
  const scale = window.devicePixelRatio || 1;
  canvas.width = Math.round(width * scale);
  canvas.height = Math.round(height * scale);
  const context = canvas.getContext("2d");
  if (!context) return;
  context.setTransform(scale, 0, 0, scale, 0, 0);

  const pad = { left: 40, top: 10, right: 12, bottom: 24 };
  const plotWidth = width - pad.left - pad.right;
  const plotHeight = height - pad.top - pad.bottom;
  const last = points.length - 1;
  const ceiling = Math.max(1, series === 1 ? cited : points[last][0]);
  const x = index => pad.left + index * plotWidth / last;
  const y = value => pad.top + plotHeight * (1 - value / ceiling);

  context.font = "11px system-ui, sans-serif";
  context.fillStyle = "#4e665d";
  context.textAlign = "right";
  context.textBaseline = "middle";
  const ticks = [...new Set([0, Math.floor(ceiling / 2), ceiling])];
  for (const tick of ticks) {
    context.strokeStyle = "#cfddd0";
    context.beginPath();
    context.moveTo(x(0), y(tick));
    context.lineTo(x(last), y(tick));
    context.stroke();
    context.fillText(number.format(tick), pad.left - 6, y(tick));
  }
  context.textAlign = "left";
  context.fillText("0", x(0), height - 9);
  context.textAlign = "right";
  context.fillText(`${last} шагов`, x(last), height - 9);

  function line(color, end) {
    context.beginPath();
    context.moveTo(x(0), y(0));
    for (let index = 1; index <= end; index += 1) {
      context.lineTo(x(index), y(points[index - 1][series]));
      context.lineTo(x(index), y(points[index][series]));
    }
    context.strokeStyle = color;
    context.lineWidth = 2;
    context.stroke();
  }

  const left = x(current);
  const top = y(points[current][series]);
  context.beginPath();
  context.moveTo(left, y(ceiling));
  context.lineTo(left, y(0));
  context.strokeStyle = "#aebdb1";
  context.stroke();
  line("#a6bcac", last);
  line("#24754e", current);
  context.beginPath();
  context.arc(left, top, 4, 0, Math.PI * 2);
  context.fillStyle = "#17583e";
  context.fill();
}

for (const figure of document.querySelectorAll(".investigation")) {
  const canvas = figure.querySelector(".trace-graph");
  const slider = figure.querySelector(".trace-step");
  const series = figure.querySelector(".trace-series");
  const stepOutput = figure.querySelector(".trace-number");
  const recordOutput = figure.querySelector(".trace-records");
  const proofOutput = figure.querySelector(".trace-proof");
  const timeline = Array.from(figure.querySelectorAll(".timeline li"));
  const points = JSON.parse(figure.dataset.progress);
  const cited = Number(figure.dataset.cited);
  let highlighted;
  const update = () => {
    const current = Number(slider.value);
    stepOutput.textContent = number.format(current);
    recordOutput.textContent = number.format(points[current][0]);
    if (proofOutput) proofOutput.textContent = `${points[current][1]} из ${cited}`;
    slider.setAttribute("aria-valuetext", `Шаг ${current}, записи ${points[current][0]}`);
    highlighted?.removeAttribute("aria-current");
    highlighted = timeline[current - 1];
    highlighted?.setAttribute("aria-current", "step");
    drawTrace(canvas, points, current, Number(series?.value ?? 0), cited);
  };
  slider.addEventListener("input", update);
  series?.addEventListener("change", update);
  if (window.ResizeObserver) new ResizeObserver(update).observe(canvas);
  else window.addEventListener("resize", update);
  figure.closest(".session").addEventListener("toggle", update);
  update();
}

policy.addEventListener("change", applyFilters);
episode.addEventListener("input", applyFilters);
metric.addEventListener("change", applyFilters);
applyFilters();
