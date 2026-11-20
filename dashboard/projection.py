"""Turn trusted-side evaluation outputs into a sanitized bundle the browser may read.

Inputs (reports, release exports, episode roots) are read on the trusted side. Only the
fields listed here are written to the served directory. Answer keys, provenance, ID maps,
Oracle submissions, private paths and raw archives never leave this module.

Bundle layout under ``<out>/bundles/<bundle-id>/``::

    bundle.json                                   source, runs, cohort facts, import notes
    episodes/<split>/<episode>.json               vetted public records, or why they are absent
    activity/<split>/<episode>/<policy>__<mode>.json   one run's recorded actions
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone

from fincheck.load import SchemaError, load_public

ROOT = Path(__file__).resolve().parents[1]
BUNDLE_FORMAT = 1
SAFE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{0,47}$")
MAX_SAFE_INT = 2**53 - 1

# Splits whose data is synthetic and may carry evaluator metrics in a reviewer export.
REVIEWER_SPLITS = ("demo", "train", "dev", "pairs")
POLICY_ROLES = {
    "reconcile": "Reference policy",
    "random": "Baseline",
    "mark_all": "Negative control",
    "oracle": "Evaluator control",
}
ISSUE_TYPES = ("duplicate_invoice", "payment_allocation", "fx_conversion", "wrong_period")

RUN_FIELDS = ("episode_id", "policy", "mode", "steps", "inspection_steps", "duration_seconds",
              "terminated", "truncated", "reason")
METRIC_FIELDS = ("tp", "fp", "fn", "precision", "recall", "f1", "reward")
FINDING_FIELDS = ("type", "affected_ids", "claimed_amount_minor", "currency", "evidence_ids", "explanation")
MANIFEST_FIELDS = ("schema_version", "episode_id", "company", "base_currency", "currency_exponents",
                   "timezone", "utc_offset", "period_start", "period_end", "extract_start",
                   "extract_end", "counts", "rounding")
LEDGER_FIELDS = ("row_id", "entry_id", "timestamp", "account", "debit_minor", "credit_minor",
                 "currency", "entity_id", "invoice_id", "payment_id")
INVOICE_FIELDS = ("doc_id", "invoice_id", "entity_id", "issued_at", "due_at", "currency",
                  "total_minor", "line_items")
LINE_FIELDS = ("line_id", "description", "quantity", "unit_price_minor", "amount_minor")
PAYMENT_FIELDS = ("doc_id", "payment_id", "entity_id", "timestamp", "currency", "amount_minor",
                  "invoice_refs")
REF_FIELDS = ("invoice_id", "amount_minor")
FX_FIELDS = ("date", "source_currency", "target_currency", "rate")
ACTION_FIELDS = ("action", "table", "cursor", "limit", "id", "doc_id", "expression", "findings")

# Demo episodes come from a temporary copy; this is where their public records live.
DEMO_EPISODES = {
    "EP-CHK-A08": ROOT / "fincheck" / "fixtures" / "a08" / "public",
    "EP-QRS8HS6811": ROOT / "episodes" / "public" / "dev" / "EP-QRS8HS6811",
}


class PrepareError(ValueError):
    pass


# ----------------------------------------------------------------------------- helpers

def _exact(value):
    """Integers beyond IEEE-754 precision travel as strings so the browser keeps every digit."""
    if type(value) is int and abs(value) > MAX_SAFE_INT:
        return str(value)
    return value


def _pick(record, fields):
    return {name: record[name] for name in fields if name in record}


def _read_json(path, label):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise PrepareError(f"{label} not found") from None
    except (ValueError, UnicodeDecodeError) as error:
        raise PrepareError(f"{label} is not valid JSON: {error}") from None


def _read_jsonl(path, label, notes):
    rows = []
    try:
        handle = Path(path).open(encoding="utf-8")
    except FileNotFoundError:
        return None
    with handle:
        for number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
                if not isinstance(row, dict):
                    raise ValueError("not an object")
            except ValueError as error:
                notes.append(f"{label} line {number} skipped: {error}")
                continue
            rows.append(row)
    return rows


def _status(report):
    if report.get("invalid_submission") or report.get("reason") == "invalid_submission":
        return "invalid_submission"
    return {"submitted": "submitted", "timeout": "timeout", "step_limit": "step_limit"}.get(
        report.get("reason"), "unknown")


def _valid_report(row):
    """Return a problem description, or None when the row has the evaluator report shape."""
    if not isinstance(row, dict):
        return "not an object"
    for name in ("episode_id", "policy", "mode"):
        if not isinstance(row.get(name), str) or not SAFE.match(row[name]):
            return f"missing or unsafe {name}"
    for name in ("steps", "inspection_steps"):
        if name in row and (type(row[name]) is not int or row[name] < 0):
            return f"invalid {name}"
    for name in ("tp", "fp", "fn"):
        if name in row and (type(row[name]) is not int or row[name] < 0):
            return f"invalid {name}"
    for name in ("precision", "recall", "f1", "reward", "duration_seconds"):
        if name in row and not isinstance(row[name], (int, float)):
            return f"invalid {name}"
    findings = row.get("submitted_findings")
    if findings is not None and (not isinstance(findings, list) or not all(isinstance(f, dict) for f in findings)):
        return "invalid submitted_findings"
    return None


def _finding(item):
    finding = _pick(item, FINDING_FIELDS)
    if "claimed_amount_minor" in finding:
        finding["claimed_amount_minor"] = _exact(finding["claimed_amount_minor"])
    for name in ("affected_ids", "evidence_ids"):
        if not isinstance(finding.get(name), list):
            finding[name] = []
        finding[name] = [str(value) for value in finding[name]]
    if not isinstance(finding.get("explanation"), str):
        finding["explanation"] = ""
    return finding


# ----------------------------------------------------------------------------- records

def project_episode(public_dir):
    """Vetted public records for one episode, or a status explaining their absence."""
    if public_dir is None or not (Path(public_dir) / "manifest.json").is_file():
        return {"status": "missing", "message": "Public records for this episode were not supplied with the source."}
    try:
        manifest = json.loads((Path(public_dir) / "manifest.json").read_text(encoding="utf-8"))
        version = manifest.get("schema_version") if isinstance(manifest, dict) else None
    except (ValueError, UnicodeDecodeError):
        return {"status": "invalid", "message": "manifest.json is not valid JSON."}
    if str(version) != "1.0":
        return {"status": "unsupported", "schema_version": str(version),
                "message": f"Records use schema {version!r}; the inspector supports contract v1.0 only."}
    try:
        public = load_public(str(public_dir))
    except SchemaError as error:
        root = str(Path(public_dir).resolve())
        problems = [problem.replace(root, "<episode>") for problem in error.errors[:8]]
        return {"status": "invalid", "message": "Public records failed contract v1.0 validation.",
                "problems": problems}

    def money(record, names):
        for name in names:
            if name in record:
                record[name] = _exact(record[name])
        return record

    invoices = []
    for item in public.invoices:
        invoice = money(_pick(item, INVOICE_FIELDS), ("total_minor",))
        invoice["line_items"] = [money(_pick(line, LINE_FIELDS), ("unit_price_minor", "amount_minor"))
                                 for line in item.get("line_items") or []]
        invoices.append(invoice)
    payments = []
    for item in public.payments:
        payment = money(_pick(item, PAYMENT_FIELDS), ("amount_minor",))
        refs = item.get("invoice_refs")
        payment["invoice_refs"] = None if refs is None else [money(_pick(ref, REF_FIELDS), ("amount_minor",)) for ref in refs]
        payments.append(payment)
    return {
        "status": "ok",
        "manifest": _pick(public.manifest, MANIFEST_FIELDS),
        "policy": public.policy,
        "ledger": [money(_pick(row, LEDGER_FIELDS), ("debit_minor", "credit_minor")) for row in public.ledger],
        "invoices": invoices,
        "payments": payments,
        "fx": [{name: str(row[name]) for name in FX_FIELDS} for row in public.fx_rows],
    }


# ----------------------------------------------------------------------------- runs

def _activity_rows(rows, reviewer):
    out = []
    for row in rows:
        if type(row.get("step")) is not int:
            continue
        action = row.get("action")
        action = _pick(action, ACTION_FIELDS) if isinstance(action, dict) else {"action": None}
        if isinstance(action.get("findings"), dict):
            findings = action["findings"]
            action["findings"] = {
                "episode_id": findings.get("episode_id"),
                "findings": [_finding(item) for item in findings.get("findings") or [] if isinstance(item, dict)],
            }
        entry = {
            "step": row["step"],
            "action": action,
            "observation": row.get("observation") if isinstance(row.get("observation"), dict) else {},
            "terminated": bool(row.get("terminated")),
            "truncated": bool(row.get("truncated")),
        }
        if reviewer and isinstance(row.get("reward"), (int, float)):
            entry["reward"] = row["reward"]
        out.append(entry)
    out.sort(key=lambda item: item["step"])
    return out


def build_bundle(*, bundle_id, label, kind, audience, reports, episode_dirs, trajectories,
                 trajectory_note=None, validation=None, notes=None):
    """``reports``: list of (report, split). ``episode_dirs``: {(split, episode): public dir or None}.
    ``trajectories``: list of rows (each with policy/episode_id/step) or None when no log exists."""
    if audience not in ("reviewer", "public"):
        raise PrepareError("Audience must be reviewer or public")
    if not SLUG.match(bundle_id):
        raise PrepareError("Bundle name must be lowercase letters, digits and dashes (max 48)")
    reviewer = audience == "reviewer"
    notes = list(notes or [])
    keys = {}
    for report, split in reports:
        key = (split, report["episode_id"], report["policy"], report["mode"])
        keys.setdefault(key, []).append(report)
    duplicates = sorted("/".join(key) for key, items in keys.items() if len(items) > 1)
    if duplicates:
        notes.append(f"{len(duplicates)} run key(s) appeared more than once and were excluded; "
                     f"import reruns as separate sources: {', '.join(duplicates[:5])}")

    grouped = {}
    if trajectories is not None:
        for row in trajectories:
            policy, episode = row.get("policy"), row.get("episode_id")
            if isinstance(policy, str) and isinstance(episode, str):
                grouped.setdefault((episode, policy), []).append(row)

    runs, activity_files = [], {}
    for (split, episode, policy, mode), items in sorted(keys.items()):
        if len(items) > 1:
            continue
        report = items[0]
        if reviewer and split not in REVIEWER_SPLITS:
            notes.append(f"{episode}/{policy}: split is {split!r}, so evaluator metrics may not be shown; run excluded "
                         f"from this reviewer export (use --audience public).")
            continue
        if not reviewer and policy == "oracle":
            continue
        run = {name: report[name] for name in RUN_FIELDS if name in report}
        run["split"] = split
        run["key"] = "~".join((split, episode, policy, mode))
        run["status"] = _status(report)
        run["role"] = POLICY_ROLES.get(policy)
        if reviewer:
            run["metrics"] = {name: report[name] for name in METRIC_FIELDS if name in report}
        if policy == "oracle":
            run["findings_state"] = "oracle"
        elif run["status"] != "submitted":
            run["findings_state"] = run["status"]
        elif isinstance(report.get("submitted_findings"), list):
            run["findings_state"] = "available"
            run["findings"] = [_finding(item) for item in report["submitted_findings"]]
        else:
            run["findings_state"] = "missing"
        if reviewer and run["status"] == "submitted" and run.get("findings") == [] and all(
            report.get(name) == 0 for name in ("tp", "fp", "fn")
        ):
            run["clean_established"] = True

        if policy == "oracle":
            run["activity_state"] = "oracle"
        elif trajectories is None:
            run["activity_state"] = trajectory_note or "missing"
        else:
            rows = _activity_rows(grouped.get((episode, policy), []), reviewer)
            steps = [row["step"] for row in rows]
            complete = (steps == list(range(1, len(steps) + 1)) and rows
                        and (rows[-1]["terminated"] or rows[-1]["truncated"])
                        and len(rows) == report.get("steps", len(rows)))
            if not rows:
                run["activity_state"] = "missing"
            else:
                run["activity_state"] = "available" if complete else "partial"
                activity_files[(split, episode, policy, mode)] = rows
        run["records"] = "pending"
        runs.append(run)

    if not runs:
        raise PrepareError("No runs remain after validation. " + (notes[-1] if notes else ""))

    episodes = {}
    for run in runs:
        ref = (run["split"], run["episode_id"])
        if ref not in episodes:
            episodes[ref] = project_episode(episode_dirs.get(ref))
        run["records"] = episodes[ref]["status"]

    splits = sorted({run["split"] for run in runs})
    bundle = {
        "format": BUNDLE_FORMAT,
        "id": bundle_id,
        "label": label,
        "kind": kind,
        "audience": audience,
        "synthetic": all(split in REVIEWER_SPLITS for split in splits),
        "prepared_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "splits": splits,
        "modes": sorted({run["mode"] for run in runs}),
        "runs": runs,
        "notes": notes,
        "episodes": {f"{split}/{episode}": {"status": data["status"], "message": data.get("message"),
                                            "problems": data.get("problems"),
                                            "company": (data.get("manifest") or {}).get("company", {}).get("name")}
                     for (split, episode), data in episodes.items()},
    }
    if validation is not None and reviewer:
        bundle["validation"] = validation
    return bundle, episodes, activity_files


def write_bundle(out, bundle, episodes, activity_files):
    out = Path(out)
    target = out / "bundles" / bundle["id"]
    staging = Path(tempfile.mkdtemp(prefix=".staging-", dir=out.mkdir(parents=True, exist_ok=True) or out))
    try:
        def dump(relative, payload):
            path = staging / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                            encoding="utf-8", newline="\n")

        dump("bundle.json", bundle)
        for (split, episode), data in episodes.items():
            if data["status"] == "ok" and SAFE.match(split) and SAFE.match(episode):
                dump(f"episodes/{split}/{episode}.json", data)
        for (split, episode, policy, mode), rows in activity_files.items():
            dump(f"activity/{split}/{episode}/{policy}__{mode}.json", rows)
        if target.exists():
            shutil.rmtree(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        os.replace(staging, target)
    finally:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
    write_index(out)
    return target


def write_index(out):
    out = Path(out)
    entries = []
    for path in sorted((out / "bundles").glob("*/bundle.json")):
        try:
            bundle = json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            continue
        entries.append({name: bundle.get(name) for name in
                        ("id", "label", "kind", "audience", "synthetic", "prepared_at", "splits", "modes")}
                       | {"runs": len(bundle.get("runs", []))})
    (out / "index.json").write_text(json.dumps({"format": BUNDLE_FORMAT, "bundles": entries}, indent=1),
                                    encoding="utf-8", newline="\n")


# ----------------------------------------------------------------------------- sources

def _load_reports(path, label, notes):
    data = _read_json(path, label)
    if not isinstance(data, list):
        raise PrepareError(f"{label} must contain a list of run reports")
    valid = []
    for index, row in enumerate(data, 1):
        problem = _valid_report(row)
        if problem:
            notes.append(f"{label} row {index} skipped: {problem}")
        else:
            valid.append(row)
    if not valid:
        raise PrepareError(f"{label} has no valid run reports")
    return valid


def _split_for(episode_id, episodes_root, explicit):
    """Trusted split from the dataset layout, or the explicit CLI value, or 'unspecified'."""
    if episodes_root is not None:
        found = [path.parent.parent.name for path in (Path(episodes_root) / "public").glob(f"*/{episode_id}/manifest.json")]
        if explicit is not None:
            return (explicit, Path(episodes_root) / "public" / explicit / episode_id) if explicit in found else ("unspecified", None)
        if len(found) == 1:
            return found[0], Path(episodes_root) / "public" / found[0] / episode_id
        return "unspecified", None
    return (explicit or "unspecified"), None


def prepare_demo(out, name="demo"):
    """Run the documented demo with the existing CLI, then project it as a reviewer bundle."""
    with tempfile.TemporaryDirectory() as temp:
        result = subprocess.run([sys.executable, "-m", "audit_sim", "demo", "--output", temp],
                                cwd=ROOT, capture_output=True, text=True)
        if result.returncode != 0:
            raise PrepareError("audit_sim demo failed:\n" + (result.stderr or result.stdout).strip())
        notes = []
        reports = _load_reports(Path(temp) / "reports.json", "reports.json", notes)
        trajectories = _read_jsonl(Path(temp) / "trajectories.jsonl", "trajectories.jsonl", notes)
    unknown = sorted({row["episode_id"] for row in reports} - DEMO_EPISODES.keys())
    if unknown:
        raise PrepareError(f"Demo produced unexpected episodes: {unknown}")
    bundle = build_bundle(
        bundle_id=name, label="Demo: A08 control and a clean dev episode", kind="demo", audience="reviewer",
        reports=[(row, "demo") for row in reports],
        episode_dirs={("demo", episode): path for episode, path in DEMO_EPISODES.items()},
        trajectories=trajectories, notes=notes,
    )
    return write_bundle(out, *bundle)


def prepare_run(out, results, *, audience, name, episodes_root=None, split=None, label=None):
    """Import ``audit_sim run --output DIR``."""
    results = Path(results)
    notes = []
    reports = _load_reports(results / "reports.json", "reports.json", notes)
    trajectories = _read_jsonl(results / "trajectories.jsonl", "trajectories.jsonl", notes)
    if trajectories is None:
        notes.append("trajectories.jsonl was not found; activity is unavailable for every run.")
    resolved, dirs = [], {}
    for row in reports:
        row_split, public = _split_for(row["episode_id"], episodes_root, split)
        resolved.append((row, row_split))
        dirs[(row_split, row["episode_id"])] = public
    if any(item[1] == "unspecified" for item in resolved):
        notes.append("Some runs have an unverified split (no matching episode under the supplied root); "
                     "they are labelled Unspecified.")
    if episodes_root is None:
        notes.append("No episode root was supplied; record evidence is unavailable.")
    bundle = build_bundle(
        bundle_id=name, label=label or f"CLI run: {results.name}", kind="run", audience=audience,
        reports=resolved, episode_dirs=dirs, trajectories=trajectories, notes=notes,
    )
    return write_bundle(out, *bundle)


def prepare_release(out, release, *, audience, name, label=None):
    """Import ``audit_sim release --output DIR`` (or ``scripts/check.py --output DIR``)."""
    release = Path(release)
    notes = []
    reports = _load_reports(release / "dev-reports.json", "dev-reports.json", notes)
    validation = None
    if (release / "validation.json").is_file():
        raw = _read_json(release / "validation.json", "validation.json")
        if isinstance(raw, dict):
            validation = {name: raw[name] for name in (
                "train_episodes", "dev_episodes", "verified_pairs", "dev_runs", "recorded_train_steps",
                "max_dev_episode_seconds", "dev") if name in raw}
    else:
        notes.append("validation.json was not found; release validation context is unavailable.")
    train_rows = _read_jsonl(release / "trajectories.jsonl", "trajectories.jsonl", notes)
    if train_rows is not None:
        notes.append(f"The release log trajectories.jsonl holds {len(train_rows)} train-split steps; "
                     "it is not joined to dev runs.")
    resolved, dirs = [], {}
    for row in reports:
        row_split, public = _split_for(row["episode_id"], release / "episodes", "dev")
        resolved.append((row, row_split))
        dirs[(row_split, row["episode_id"])] = public
    bundle = build_bundle(
        bundle_id=name, label=label or f"Release export: {release.name}", kind="release", audience=audience,
        reports=resolved, episode_dirs=dirs, trajectories=None, trajectory_note="release_dev",
        validation=validation, notes=notes,
    )
    return write_bundle(out, *bundle)
