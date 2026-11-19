"""Paired episodes (A18): permute record order and rename every opaque ID.

``make_pair(public_dir, private_dir, out_root, split, transform_seed)`` reads one
episode (public + private files) and writes a new, self-consistent episode to
``out_root/{public,private}/<split>/<new_episode_id>`` where

* every opaque ID (row, entry, doc, invoice, payment, entity, line item, issue,
  episode) is replaced through one bijection by a fresh ID of a different
  style (other prefix, separator and token length); FX keys are not opaque
  and stay unchanged;
* every reference is rewritten: ledger columns, ``invoice_refs``, truth
  ``affected_ids``/``evidence_sets``, provenance ``target``/``before``/
  ``after``/``issue_id``, distractor records, ``episode_id`` everywhere, and
  IDs quoted inside free text (e.g. a change ``cause``);
* the rows of ``ledger.csv`` and ``fx.csv`` and the lines of
  ``invoices.jsonl``/``payments.jsonl`` are shuffled with ``transform_seed``
  (the order is guaranteed to differ from the source order);
  ``line_items`` and ``invoice_refs`` keep their order (``invoice_refs`` order
  is semantic: the rounding remainder goes to the last ref, CONTRACT §4);
* amounts, currencies and timestamps are untouched; manifest ``files``/
  ``counts`` and provenance ``baseline_sha256`` are recomputed
  (``ledger.csv`` baseline hash = canonical sorted form, CONTRACT §6);
* ``id_map.json`` (CONTRACT §6) is written to the new private directory; it is
  the only file that mentions source IDs (``provenance.config.transform``
  records ``{kind, seed, permute, rename}``).

The transform works on files only (it does not need the generator's
in-memory history), so it also applies to hand-written fixtures. Output is a
pure function of the input files and ``(transform_seed, permute, rename)``.
"""

from __future__ import annotations

import copy
import csv
import hashlib
import io
import json
import os
import random
import re
from datetime import datetime

from .config import canonical_json
from .export import dumps_csv, dumps_json, dumps_jsonl, sha256_hex, write_files
from .ids import ALPHABET, PREFIX_POOLS, TOKEN_LENGTHS

LEDGER_ID_COLUMNS = {"row_id": "row", "entry_id": "entry", "entity_id": "entity",
                     "invoice_id": "invoice", "payment_id": "payment"}
DATA_FILES = ("policy.json", "ledger.csv", "invoices.jsonl", "payments.jsonl", "fx.csv")

# Extra prefixes so that a different prefix is always available (e.g. for issues,
# whose generator pool is just "ISS").
EXTRA_PREFIXES: dict[str, tuple[str, ...]] = {
    "row": ("ROW", "LR", "GL"), "entry": ("JRN", "JNL", "BK"), "doc": ("DSRC", "DO", "SD"),
    "invoice": ("IV", "SB", "AR"), "payment": ("PM", "CR", "BNK"),
    "entity": ("CS", "PRT", "CLT"), "line": ("LNI", "ITM", "LIN"),
    "issue": ("CASE", "FND", "DSC", "ISX"),
}
SEPARATORS = ("-", "_", "")
KIND_ORDER = ("row", "entry", "doc", "invoice", "payment", "entity", "line", "issue")


class TransformError(ValueError):
    """The source episode cannot be paired (missing/garbled files)."""


# --------------------------------------------------------------------------- reading

def _read(path: str) -> bytes:
    try:
        with open(path, "rb") as fh:
            return fh.read()
    except OSError as exc:
        raise TransformError(f"cannot read {path}: {exc.__class__.__name__}") from None


def _csv_rows(data: bytes) -> tuple[list[str], list[dict]]:
    rows = list(csv.reader(io.StringIO(data.decode("utf-8"))))
    if not rows:
        raise TransformError("empty CSV file")
    header = rows[0]
    return header, [dict(zip(header, r)) for r in rows[1:]]


def _jsonl(data: bytes) -> list[dict]:
    return [json.loads(line) for line in data.decode("utf-8").split("\n") if line.strip()]


def _load(public_dir: str, private_dir: str) -> dict:
    src = {}
    for name in ("manifest.json", "policy.json"):
        src[name] = json.loads(_read(os.path.join(public_dir, name)))
    src["ledger_header"], src["ledger"] = _csv_rows(_read(os.path.join(public_dir, "ledger.csv")))
    src["fx_header"], src["fx"] = _csv_rows(_read(os.path.join(public_dir, "fx.csv")))
    src["invoices"] = _jsonl(_read(os.path.join(public_dir, "invoices.jsonl")))
    src["payments"] = _jsonl(_read(os.path.join(public_dir, "payments.jsonl")))
    for name in ("truth.json", "provenance.json"):
        src[name] = json.loads(_read(os.path.join(private_dir, name)))
    return src


# --------------------------------------------------------------------------- ID collection

def collect_ids(src: dict) -> dict[str, str]:
    """Every opaque ID of the episode -> its kind (first kind seen wins)."""
    kinds: dict[str, str] = {}

    def add(value, kind):
        if isinstance(value, str) and value and value not in kinds:
            kinds[value] = kind

    for r in src["ledger"]:
        for col, kind in LEDGER_ID_COLUMNS.items():
            add(r.get(col), kind)
    for d in src["invoices"]:
        add(d["doc_id"], "doc")
        add(d["invoice_id"], "invoice")
        add(d.get("entity_id"), "entity")
        for li in d.get("line_items") or []:
            add(li.get("line_id"), "line")
    for d in src["payments"]:
        add(d["doc_id"], "doc")
        add(d["payment_id"], "payment")
        add(d.get("entity_id"), "entity")
        for ref in d.get("invoice_refs") or []:
            add(ref.get("invoice_id"), "invoice")
    for iss in src["truth.json"]["issues"]:
        add(iss["issue_id"], "issue")
        for x in iss["affected_ids"]:
            add(x, "doc")
    for ch in src["provenance.json"]["changes"]:
        add(ch["issue_id"], "issue")
        for k in ("before", "after"):
            if ch[k] is not None:
                for col, kind in LEDGER_ID_COLUMNS.items():
                    add(ch[k].get(col), kind)
        add(ch["target"], "row")
    for iss in src["truth.json"]["issues"]:  # remaining evidence ids (rows); FX keys excluded
        for s in iss["evidence_sets"]:
            for x in s:
                if not x.startswith("FX:"):
                    add(x, "row")
    return kinds


# --------------------------------------------------------------------------- new IDs

def _common_prefix(values: list[str]) -> str:
    if not values:
        return ""
    lo, hi = min(values), max(values)
    n = 0
    while n < min(len(lo), len(hi)) and lo[n] == hi[n]:
        n += 1
    return lo[:n]


def _lead(values: list[str]) -> str:
    """Leading letters of the common prefix of a kind's IDs (its visible prefix)."""
    m = re.match(r"[A-Z]*", _common_prefix(values))
    return m.group(0) if m else ""


def _clashes(prefix: str, lead: str) -> bool:
    return bool(lead) and (prefix.startswith(lead) or lead.startswith(prefix))


class _Namer:
    """Fresh-ID factory whose per-kind prefixes differ from the source's."""

    def __init__(self, rng: random.Random, old_by_kind: dict[str, list[str]],
                 old_ids: set[str]) -> None:
        self._rng = rng
        self._old = old_ids
        self._old_re = _id_regex(old_ids, bounded=False)
        self._used: set[str] = set()
        leads = {k: _lead(v) for k, v in old_by_kind.items()}
        all_leads = [x for x in leads.values() if x]
        sep = rng.choice(SEPARATORS)
        self.styles: dict[str, dict] = {}
        taken: list[str] = []
        for kind in KIND_ORDER:
            pool = [p for p in PREFIX_POOLS[kind] + EXTRA_PREFIXES[kind] if p not in taken
                    and not any(_clashes(p, t) for t in taken)]
            strict = [p for p in pool if not any(_clashes(p, ld) for ld in all_leads)]
            same = [p for p in pool if not _clashes(p, leads.get(kind, ""))]
            prefix = rng.choice(strict or same or pool)
            taken.append(prefix)
            lo, hi = TOKEN_LENGTHS[kind]
            kind_sep = sep if sep else ("-" if kind == "issue" else "")
            self.styles[kind] = {"prefix": prefix, "sep": kind_sep,
                                 "length": rng.randint(lo, hi + 1)}

    def new(self, kind: str) -> str:
        st = self.styles[kind]
        while True:
            token = "".join(self._rng.choice(ALPHABET) for _ in range(st["length"]))
            value = st["prefix"] + st["sep"] + token
            if value in self._used or value in self._old or value.startswith("FX"):
                continue
            if self._old_re is not None and self._old_re.search(value):
                continue  # never embed an old ID inside a new one
            self._used.add(value)
            return value


def _id_regex(ids, bounded=True):
    if not ids:
        return None
    alt = "|".join(re.escape(i) for i in sorted(ids, key=lambda s: (-len(s), s)))
    if bounded:
        return re.compile(rf"(?<![A-Za-z0-9])(?:{alt})(?![A-Za-z0-9])")
    return re.compile(alt)


def new_episode_id(source_episode_id: str, transform_seed: int, permute: bool,
                   rename: bool) -> str:
    """``EP-`` + 12 base32 chars from the source episode and transform parameters."""
    key = f"pair:{source_episode_id}:{transform_seed}:{int(permute)}:{int(rename)}"
    n = int.from_bytes(hashlib.sha256(key.encode()).digest()[:8], "big")
    chars = []
    for _ in range(12):
        n, r = divmod(n, 32)
        chars.append(ALPHABET[r])
    return "EP-" + "".join(chars)


# --------------------------------------------------------------------------- rewriting

def _rewriter(mapping: dict[str, str]):
    """Return f(obj) that maps exact-ID strings and IDs quoted inside text."""
    rx = _id_regex(mapping)

    def sub(s: str) -> str:
        if s in mapping:
            return mapping[s]
        if rx is None or s.startswith("FX:"):
            return s
        return rx.sub(lambda m: mapping[m.group(0)], s)

    def walk(obj):
        if isinstance(obj, str):
            return sub(obj)
        if isinstance(obj, list):
            return [walk(v) for v in obj]
        if isinstance(obj, dict):
            return {k: walk(v) for k, v in obj.items()}
        return obj

    return walk


def _permute(items: list, rng: random.Random) -> list:
    """Shuffle; for >= 2 distinct items the result never equals the input order."""
    out = list(items)
    if len(out) < 2:
        return out
    for _ in range(64):
        rng.shuffle(out)
        if out != items:
            return out
    return items[1:] + items[:1]  # pragma: no cover (practically unreachable)


def _row_key(r: dict):
    return (datetime.fromisoformat(r["timestamp"]), r["row_id"])


def _row_str(row: dict, header: list[str]) -> dict:
    return {c: "" if row.get(c) is None else str(row[c]) for c in header}


def _baseline_ledger(rows: list[dict], changes: list[dict], header: list[str]) -> list[dict]:
    """Undo provenance changes (reverse ``order``) on string-valued ledger rows."""
    by_id = {r["row_id"]: r for r in rows}
    for ch in sorted(changes, key=lambda c: c["order"], reverse=True):
        if ch["after"] is not None:
            by_id.pop(ch["after"]["row_id"], None)
        if ch["before"] is not None:
            by_id[ch["before"]["row_id"]] = _row_str(ch["before"], header)
    return sorted(by_id.values(), key=_row_key)


def _ledger_bytes(header, rows) -> bytes:
    return dumps_csv(header, ([r[c] for c in header] for r in rows))


# --------------------------------------------------------------------------- main API

def build_pair(public_dir: str, private_dir: str, transform_seed: int, *, permute: bool = True,
               rename: bool = True) -> tuple[str, dict[str, bytes], dict[str, bytes]]:
    """In-memory pair: (new_episode_id, public files, private files)."""
    src = _load(public_dir, private_dir)
    old_eid = src["manifest.json"]["episode_id"]
    new_eid = new_episode_id(old_eid, transform_seed, permute, rename)
    seed_tag = f"{transform_seed}"

    kinds = collect_ids(src) if rename else {}
    kinds.pop(old_eid, None)
    mapping: dict[str, str] = {}
    if kinds:
        by_kind: dict[str, list[str]] = {}
        for v, k in kinds.items():
            by_kind.setdefault(k, []).append(v)
        namer = _Namer(random.Random(f"pair-ids:{seed_tag}"), by_kind,
                       set(kinds) | {old_eid})
        for kind in KIND_ORDER:  # deterministic: kinds in fixed order, IDs sorted
            for v in sorted(by_kind.get(kind, [])):
                mapping[v] = namer.new(kind)
    mapping[old_eid] = new_eid
    walk = _rewriter(mapping)

    # ---- public
    header = src["ledger_header"]
    ledger = [{c: mapping.get(r[c], r[c]) if c in LEDGER_ID_COLUMNS else r[c] for c in header}
              for r in src["ledger"]]
    invoices = walk(src["invoices"])
    payments = walk(src["payments"])
    fx = list(src["fx"])
    if permute:
        ledger = _permute(ledger, random.Random(f"pair-perm:{seed_tag}:ledger"))
        invoices = _permute(invoices, random.Random(f"pair-perm:{seed_tag}:invoices"))
        payments = _permute(payments, random.Random(f"pair-perm:{seed_tag}:payments"))
        fx = _permute(fx, random.Random(f"pair-perm:{seed_tag}:fx"))
    public = {
        "policy.json": dumps_json(walk(src["policy.json"])),
        "ledger.csv": _ledger_bytes(header, ledger),
        "invoices.jsonl": dumps_jsonl(invoices),
        "payments.jsonl": dumps_jsonl(payments),
        "fx.csv": dumps_csv(src["fx_header"], ([r[c] for c in src["fx_header"]] for r in fx)),
    }
    manifest = walk(src["manifest.json"])
    manifest["files"] = {name: sha256_hex(public[name]) for name in DATA_FILES
                         if name in src["manifest.json"].get("files", DATA_FILES)}
    public["manifest.json"] = dumps_json(manifest)

    # ---- private
    truth = walk(src["truth.json"])
    truth["issues"] = sorted(truth["issues"], key=lambda i: i["issue_id"])
    prov = walk(src["provenance.json"])
    config = prov.get("config")
    if isinstance(config, dict):
        config = dict(config)
        # The source episode is named only in id_map.json (no old ID anywhere else).
        config["transform"] = {"kind": "pair", "seed": transform_seed, "permute": permute,
                               "rename": rename}
        prov["config"] = config
        prov["config_sha256"] = hashlib.sha256(canonical_json(config)).hexdigest()
    base_rows = _baseline_ledger(ledger, prov["changes"], header)
    base_files = {name: public[name] for name in DATA_FILES}
    base_files["ledger.csv"] = _ledger_bytes(header, base_rows)
    base_manifest = copy.deepcopy(manifest)
    counts = base_manifest.get("counts")
    if isinstance(counts, dict):
        counts["ledger_rows"] = len(base_rows)
        counts["entries"] = len({r["entry_id"] for r in base_rows})
    base_manifest["files"] = {n: sha256_hex(base_files[n]) for n in manifest["files"]}
    base_files["manifest.json"] = dumps_json(base_manifest)
    prov["baseline_sha256"] = {n: sha256_hex(base_files[n])
                               for n in sorted(src["provenance.json"]["baseline_sha256"])
                               if n in base_files}
    id_map = {"source_episode_id": old_eid, "episode_id": new_eid,
              "ids": dict(sorted(mapping.items()))}
    private = {"truth.json": dumps_json(truth), "provenance.json": dumps_json(prov),
               "id_map.json": dumps_json(id_map)}
    return new_eid, public, private


def make_pair(public_dir: str, private_dir: str, out_root: str, split: str,
              transform_seed: int, *, permute: bool = True, rename: bool = True) -> str:
    """Write the paired episode under ``out_root`` (CONTRACT §1 layout) and return its
    episode_id. See the module docstring for what changes and what is kept."""
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", split or ""):
        raise TransformError("split must match [a-z0-9][a-z0-9_-]*")
    new_eid, public, private = build_pair(public_dir, private_dir, transform_seed,
                                          permute=permute, rename=rename)
    write_files(os.path.join(out_root, "public", split, new_eid), public)
    write_files(os.path.join(out_root, "private", split, new_eid), private)
    return new_eid


def pair_seed(episode_seed: int, episode_id: str) -> int:
    """Transform seed derived from an episode's generation seed (used by ``build --pairs``)."""
    digest = hashlib.sha256(f"{episode_seed}:{episode_id}:pair".encode()).digest()
    return int.from_bytes(digest[:4], "big")
