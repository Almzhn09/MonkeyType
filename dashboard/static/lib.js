// Pure formatting and aggregation helpers. No DOM access, so they can be tested in Node.

export const POLICY_ORDER = ["reconcile", "random", "mark_all", "oracle"];
export const POLICY_ROLES = {
  reconcile: "Reference policy", random: "Baseline", mark_all: "Negative control", oracle: "Evaluator control",
};
export const TYPE_LABELS = {
  duplicate_invoice: "Duplicate invoice",
  payment_allocation: "Payment allocation",
  fx_conversion: "FX conversion",
  wrong_period: "Wrong reporting period",
};
export const STATUS_LABELS = {
  submitted: "Submitted", timeout: "Timed out", step_limit: "Step limit",
  invalid_submission: "Invalid submission", unknown: "Unknown",
};

export function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, (c) => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
}

/** Exact minor-unit formatting from an integer (or digit string) and the currency exponent. */
export function money(minor, exponent) {
  if (minor === null || minor === undefined || minor === "") return "";
  const text = String(minor).trim();
  if (!/^-?\d+$/.test(text)) return text;
  const negative = text.startsWith("-");
  const exp = Number.isInteger(exponent) && exponent >= 0 ? exponent : 2;
  const digits = text.replace("-", "").padStart(exp + 1, "0");
  const whole = digits.slice(0, digits.length - exp).replace(/^0+(?=\d)/, "").replace(/\B(?=(\d{3})+(?!\d))/g, ",");
  const frac = exp ? "." + digits.slice(digits.length - exp) : "";
  return (negative && /[1-9]/.test(digits) ? "−" : "") + whole + frac;
}

/** Company-local timestamp as recorded, without browser time-zone conversion. */
export function timestamp(value) {
  const match = /^(\d{4}-\d{2}-\d{2})T(\d{2}:\d{2}:\d{2})(\.\d+)?(Z|[+-]\d{2}:\d{2})$/.exec(String(value ?? ""));
  if (!match) return String(value ?? "");
  return `${match[1]} ${match[2]}${match[3] || ""} ${match[4] === "Z" ? "+00:00" : match[4]}`;
}

export function metric(value, full = false) {
  if (value === null || value === undefined || Number.isNaN(value)) return "—";
  if (full) return String(value);
  const text = Number(value).toFixed(3);
  return text.replace("-", "−") === "−0.000" ? "0.000" : text.replace("-", "−");
}

export function elapsed(seconds) {
  if (typeof seconds !== "number" || Number.isNaN(seconds)) return "—";
  return seconds < 1 ? `${(seconds * 1000).toFixed(1)} ms` : `${seconds.toFixed(2)} s`;
}

export function mean(values) {
  const list = values.filter((v) => typeof v === "number" && !Number.isNaN(v));
  return list.length ? list.reduce((a, b) => a + b, 0) / list.length : null;
}

export function policyOrder(names) {
  return [...new Set(names)].sort((a, b) => {
    const ia = POLICY_ORDER.indexOf(a), ib = POLICY_ORDER.indexOf(b);
    return (ia < 0 ? 99 : ia) - (ib < 0 ? 99 : ib) || a.localeCompare(b);
  });
}

/** Episode coverage per policy and the common cohort every policy ran on. */
export function cohort(runs) {
  const coverage = new Map();
  for (const run of runs) {
    if (!coverage.has(run.policy)) coverage.set(run.policy, new Set());
    coverage.get(run.policy).add(run.episode_id);
  }
  const policies = policyOrder([...coverage.keys()]);
  let common = null;
  for (const policy of policies) {
    const episodes = coverage.get(policy);
    common = common === null ? new Set(episodes) : new Set([...common].filter((e) => episodes.has(e)));
  }
  common = common || new Set();
  const equal = policies.every((p) => coverage.get(p).size === common.size);
  return {policies, coverage, common, equal};
}

/** Per-policy arithmetic means over the visible report rows (one row per episode). */
export function benchmark(runs, {useCommon = true} = {}) {
  const c = cohort(runs);
  return c.policies.map((policy) => {
    const rows = runs.filter((r) => r.policy === policy && (!useCommon || c.common.has(r.episode_id)));
    const m = (name) => mean(rows.map((r) => r.metrics ? r.metrics[name] : undefined));
    return {
      policy, role: POLICY_ROLES[policy] || "", episodes: rows.length,
      f1: m("f1"), reward: m("reward"), precision: m("precision"), recall: m("recall"),
      steps: mean(rows.map((r) => r.steps)), seconds: mean(rows.map((r) => r.duration_seconds)),
      timeout: rows.filter((r) => r.status === "timeout").length,
      step_limit: rows.filter((r) => r.status === "step_limit").length,
      invalid: rows.filter((r) => r.status === "invalid_submission").length,
    };
  });
}

/** Evidence IDs resolve by doc_id, ledger row_id or canonical FX key, within one episode. */
export function indexEpisode(data) {
  const byDoc = new Map(), byRow = new Map(), byFx = new Map();
  const invoiceById = new Map(), paymentById = new Map();
  const ledgerByInvoice = new Map(), ledgerByPayment = new Map(), ledgerByEntry = new Map(), paymentsByInvoice = new Map();
  const push = (map, key, value) => { if (key === null || key === undefined) return; if (!map.has(key)) map.set(key, []); map.get(key).push(value); };
  for (const inv of data.invoices) { byDoc.set(inv.doc_id, {kind: "invoice", record: inv}); invoiceById.set(inv.invoice_id, inv); }
  for (const pay of data.payments) {
    byDoc.set(pay.doc_id, {kind: "payment", record: pay});
    paymentById.set(pay.payment_id, pay);
    (pay.invoice_refs || []).forEach((ref, order) => push(paymentsByInvoice, ref.invoice_id, {payment: pay, ref, order}));
  }
  for (const row of data.ledger) {
    byRow.set(row.row_id, {kind: "ledger", record: row});
    push(ledgerByInvoice, row.invoice_id, row); push(ledgerByPayment, row.payment_id, row); push(ledgerByEntry, row.entry_id, row);
  }
  for (const fx of data.fx) byFx.set(`FX:${fx.date}:${fx.source_currency}:${fx.target_currency}`, {kind: "fx", record: fx});
  const roles = new Map();
  for (const [role, code] of Object.entries((data.policy && data.policy.accounts) || {})) {
    for (const c of [].concat(code)) roles.set(c, role);
  }
  return {
    data, roles, invoiceById, paymentById, ledgerByInvoice, ledgerByPayment, ledgerByEntry, paymentsByInvoice,
    resolve(id) {
      const key = String(id ?? "").trim();
      return byDoc.get(key) || byRow.get(key) || byFx.get(key) || byFx.get(key.toUpperCase()) || null;
    },
    exponent(currency) {
      const exps = (data.manifest && data.manifest.currency_exponents) || {};
      return Number.isInteger(exps[currency]) ? exps[currency] : 2;
    },
  };
}

export function recordId(kind, record) {
  if (kind === "ledger") return record.row_id;
  if (kind === "fx") return `FX:${record.date}:${record.source_currency}:${record.target_currency}`;
  return record.doc_id;
}
