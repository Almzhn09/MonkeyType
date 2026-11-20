"use strict";

const policy = document.getElementById("policy");
const episode = document.getElementById("episode");
const counter = document.getElementById("visible-count");
const runs = Array.from(document.querySelectorAll(".session"));

function applyFilters() {
  const text = episode.value.trim().toLowerCase();
  let shown = 0;
  for (const run of runs) {
    const policyMatches = !policy.value || policy.value === run.dataset.policy;
    const episodeMatches = run.dataset.episode.toLowerCase().includes(text);
    run.hidden = !policyMatches || !episodeMatches;
    if (!run.hidden) shown += 1;
  }
  counter.textContent = `Показано ${shown} из ${runs.length}`;
}

policy.addEventListener("change", applyFilters);
episode.addEventListener("input", applyFilters);
