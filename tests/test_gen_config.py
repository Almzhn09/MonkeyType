"""A03/A14: config validation, determinism, sizes, variation across seeds,
CLI, and the injection hook API (with a test-only injector)."""

import contextlib
import csv
import hashlib
import io
import json
import os
import re
import unittest
from unittest import mock

from fingen import inject
from fingen.__main__ import main
from fingen.config import ConfigError, config_sha256, resolve_config
from fingen.export import LEDGER_COLUMNS
from fingen.generate import build_episode, generate_episode
from fingen.history import invoice_lines
from fingen.reader import load_private, load_public
from tests.gen_support import EXAMPLE, episode, read_tree, tmpdir


class ConfigValidationTest(unittest.TestCase):
    def test_defaults_resolved_and_hash_stable(self):
        a = resolve_config({"seed": 5})
        b = resolve_config({"seed": 5})
        self.assertEqual(a, b)
        self.assertEqual(config_sha256(a), config_sha256(b))
        for k in ("timezone", "base_currency", "foreign_currencies", "rounding_mode"):
            self.assertIsNotNone(a[k])
        self.assertNotIn(a["base_currency"], a["foreign_currencies"])
        explicit = resolve_config({"seed": 5, "timezone": "Asia/Dubai"})
        self.assertEqual(explicit["timezone"], "Asia/Dubai")
        self.assertEqual(explicit["base_currency"], a["base_currency"])

    def test_invalid_configs(self):
        bad = [
            {},
            {"seed": -1},
            {"seed": True},
            {"seed": 1, "bogus": 1},
            {"seed": 1, "timezone": "Europe/Berlin"},
            {"seed": 1, "base_currency": "RUB"},
            {"seed": 1, "base_currency": "KZT", "foreign_currencies": ["KZT"]},
            {"seed": 1, "foreign_currencies": ["EUR", "EUR"]},
            {"seed": 1, "rounding_mode": "floor"},
            {"seed": 1, "size": {"invoices": 5, "background_entries": 10}},
            {"seed": 1, "size": {"invoices": 400, "background_entries": 10}},
            {"seed": 1, "size": {"invoices": 40, "background_entries": 5000}},
            {"seed": 1, "period": {"start_month": "2026-13", "months": 1}},
            {"seed": 1, "period": {"start_month": "2026-03", "months": 0}},
            {"seed": 1, "errors": {"duplicate_invoice": 13}},
            {"seed": 1, "errors": {"not_a_type": 1}},
            {"seed": 1, "size": {"invoices": 20, "background_entries": 10},
             "errors": {"duplicate_invoice": 4, "wrong_period": 3}},  # > invoices//3
            {"seed": 1, "combos": {"duplicate_plus_period": 7}},
            {"seed": 1, "distractors": {"refunds": 2, "advances": 1}},
            {"seed": 1, "base_currency": "KZT", "foreign_currencies": [],
             "errors": {"fx_conversion": 1}},
            {"seed": 1, "schema_version": "2.0"},
            {"seed": 1, "split": "Train Set"},
            {"seed": 1, "transform": 3},
        ]
        for cfg in bad:
            with self.subTest(cfg=cfg), self.assertRaises(ConfigError):
                resolve_config(cfg)

    def test_example_config_valid(self):
        with open(EXAMPLE, encoding="utf-8") as fh:
            cfg = resolve_config(json.load(fh))
        self.assertEqual(sum(cfg["errors"].values()), 0)


class DeterminismTest(unittest.TestCase):
    def test_same_config_byte_identical(self):
        cfg = {"seed": 2024, "distractors": {"split_payments": 3, "refunds": 1,
                                             "repeated_amounts": 2, "utc_boundary": 3,
                                             "advances": 2}}
        d1, d2 = tmpdir(), tmpdir()
        generate_episode(cfg, d1)
        generate_episode(dict(cfg), d2)
        t1, t2 = read_tree(d1), read_tree(d2)
        self.assertEqual(len(t1), 8)
        self.assertEqual(t1, t2)

    def test_cli_twice_identical(self):
        d1, d2 = tmpdir(), tmpdir()
        for d in (d1, d2):
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main(["generate", "--config", EXAMPLE, "--out", d]), 0)
        self.assertEqual(read_tree(d1), read_tree(d2))
        for content in read_tree(d1).values():
            self.assertNotIn(d1.encode(), content)

    def test_different_seeds_differ(self):
        a = build_episode({"seed": 11, "timezone": "Asia/Almaty", "base_currency": "KZT",
                           "foreign_currencies": ["EUR"]})
        b = build_episode({"seed": 12, "timezone": "Asia/Almaty", "base_currency": "KZT",
                           "foreign_currencies": ["EUR"]})
        self.assertNotEqual(a.episode_id, b.episode_id)
        for name in a.public:
            self.assertNotEqual(a.public[name], b.public[name], name)

    def test_config_file_forms_and_index(self):
        d = tmpdir()
        base = {"seed": 31, "size": {"invoices": 14, "background_entries": 20}}
        forms = {"single.json": base,
                 "list.json": [base, dict(base, seed=32)],
                 "episodes.json": {"episodes": [base, dict(base, seed=32), dict(base, seed=33)]}}
        for fname, content in forms.items():
            with open(os.path.join(d, fname), "w", encoding="utf-8") as fh:
                json.dump(content, fh)
        out = tmpdir()
        with contextlib.redirect_stdout(io.StringIO()) as buf:
            self.assertEqual(main(["generate", "--config", os.path.join(d, "episodes.json"),
                                   "--out", out]), 0)
        self.assertEqual(len(os.listdir(os.path.join(out, "public", "train"))), 3)
        self.assertEqual(len(buf.getvalue().strip().splitlines()), 3)
        out2 = tmpdir()
        with contextlib.redirect_stdout(io.StringIO()):
            main(["generate", "--config", os.path.join(d, "list.json"), "--index", "1",
                  "--out", out2])
        (ep_id,) = os.listdir(os.path.join(out2, "public", "train"))
        prov = load_private(os.path.join(out2, "private", "train", ep_id))["provenance"]
        self.assertEqual(prov["seed"], 32)
        out3 = tmpdir()
        with contextlib.redirect_stdout(io.StringIO()):
            main(["generate", "--config", os.path.join(d, "single.json"), "--out", out3])
        self.assertEqual(len(os.listdir(os.path.join(out3, "public", "train"))), 1)
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(main(["generate", "--config", os.path.join(d, "list.json"),
                                   "--index", "5", "--out", tmpdir()]), 2)


class SizeTest(unittest.TestCase):
    def test_small_and_large_within_targets(self):
        for name in ("small", "large"):
            m = episode(name)["public"]["manifest"]
            with self.subTest(config=name):
                self.assertTrue(20 <= m["counts"]["documents"] <= 200, m["counts"])
                self.assertTrue(200 <= m["counts"]["ledger_rows"] <= 2000, m["counts"])
        self.assertGreater(episode("large")["public"]["manifest"]["counts"]["documents"], 150)
        self.assertGreater(episode("large")["public"]["manifest"]["counts"]["ledger_rows"], 1000)

    def test_many_seeds_within_targets(self):
        for seed in range(20):
            for size in ({"invoices": 12, "background_entries": 0},
                         {"invoices": 95, "background_entries": 500}):
                ep = build_episode({"seed": seed, "size": size,
                                    "distractors": {"split_payments": 2, "refunds": 1,
                                                    "repeated_amounts": 2, "utc_boundary": 2,
                                                    "advances": 1}})
                h = ep.history
                self.assertTrue(20 <= len(h.invoices) + len(h.payments) <= 200)
                self.assertTrue(200 <= len(h.rows) <= 2000)


class VariationTest(unittest.TestCase):
    """A14: names, IDs, currencies, time zones and ordering vary across seeds."""

    @classmethod
    def setUpClass(cls):
        cls.eps = [build_episode({"seed": s, "size": {"invoices": 14, "background_entries": 10}})
                   for s in range(100, 112)]

    def test_varies(self):
        hs = [e.history for e in self.eps]
        self.assertGreaterEqual(len({h.company_name for h in hs}), 11)
        self.assertGreaterEqual(len({h.accounts["ar"] for h in hs}), 10)
        self.assertGreaterEqual(len({h.timezone for h in hs}), 3)
        self.assertGreaterEqual(len({h.base_currency for h in hs}), 2)
        self.assertGreaterEqual(len({tuple(h.foreign_currencies) for h in hs}), 4)

        def prefix(value):
            return re.match(r"^[A-Z]+", value).group(0)

        for kind in ("row_id", "entry_id"):
            prefixes = {prefix(e.history.sorted_rows()[0].to_public()[kind]) for e in self.eps}
            self.assertGreaterEqual(len(prefixes), 3, kind)
        doc_prefixes = {prefix(next(iter(e.history.invoices))) for e in self.eps}
        self.assertGreaterEqual(len(doc_prefixes), 3)
        inv_prefixes = {prefix(i.invoice_id) for e in self.eps for i in e.history.invoices.values()}
        self.assertGreaterEqual(len(inv_prefixes), 3)

    def test_ids_opaque_and_unique(self):
        for e in self.eps:
            h = e.history
            all_ids = [*h.rows, *h.entries, *h.invoices, *h.payments,
                       *(i.invoice_id for i in h.invoices.values()),
                       *(p.payment_id for p in h.payments.values()),
                       *h.entities]
            self.assertEqual(len(all_ids), len(set(all_ids)))
            for i in all_ids:
                self.assertRegex(i, r"^[A-Z0-9_-]+$")
                self.assertFalse(i.startswith("FX"))
            # ledger order (time) is not the row_id order; invoice numbers are
            # not assigned in issue-date order
            ledger_ids = [r.row_id for r in h.sorted_rows()]
            self.assertNotEqual(ledger_ids, sorted(ledger_ids))
            by_date = [i.invoice_id for i in sorted(h.invoices.values(),
                                                    key=lambda i: (i.issued_at, i.doc_id))]
            self.assertNotEqual(by_date, sorted(by_date))


def _test_duplicate_injector(ctx, count):
    """Test-only injector exercising the hook API (not the real A09)."""
    issues = []
    for inv in ctx.candidates("invoice")[:count]:
        h = ctx.history
        issue_id = ctx.new_issue_id()
        entry = ctx.new_entry("invoice", inv.doc_id)
        added = [ctx.add_row(entry, inv.issued_at, role, d, c, issue_id=issue_id,
                             cause="test: invoice posted twice", entity_id=inv.entity_id,
                             invoice_id=invoice_id, payment_id=payment_id)
                 for role, d, c, invoice_id, payment_id in invoice_lines(h, inv)]
        original = [r for r in h.doc_rows(inv.doc_id, "ar") if r.entry_id != entry.entry_id][0]
        ctx.claim(inv.doc_id, issue_id)
        issues.append(ctx.make_issue(issue_id, "duplicate_invoice", inv.doc_id,
                                     added[0].debit_minor,
                                     [[inv.doc_id, original.row_id, added[0].row_id]]))
    return issues


class InjectionHookTest(unittest.TestCase):
    def test_unregistered_type_raises(self):
        with mock.patch.dict(inject.REGISTRY, clear=True), \
                mock.patch.object(inject, "load_injectors", lambda: None):
            with self.assertRaises(NotImplementedError):
                build_episode({"seed": 3, "errors": {"wrong_period": 1}})

    def test_registered_injector_records_reversible_changes(self):
        with mock.patch.dict(inject.REGISTRY, clear=True), \
                mock.patch.object(inject, "load_injectors", lambda: None):
            inject.register("duplicate_invoice")(_test_duplicate_injector)
            out = tmpdir()
            ep, pub_dir, priv_dir = generate_episode(
                {"seed": 3, "errors": {"duplicate_invoice": 2}}, out)
        pub, priv = load_public(pub_dir), load_private(priv_dir)
        truth, prov = priv["truth"], priv["provenance"]
        self.assertEqual(len(truth["issues"]), 2)
        self.assertEqual([i["issue_id"] for i in truth["issues"]],
                         sorted(i["issue_id"] for i in truth["issues"]))
        for issue in truth["issues"]:
            self.assertEqual(issue["type"], "duplicate_invoice")
            self.assertEqual(issue["currency"], pub["manifest"]["base_currency"])
            self.assertEqual(issue["tolerance_minor"], 0)
            row_ids = {r["row_id"] for r in pub["ledger"]}
            for ev in issue["evidence_sets"][0][1:]:
                self.assertIn(ev, row_ids)
        self.assertEqual(len(prov["changes"]), 4)
        self.assertEqual([c["order"] for c in prov["changes"]], [0, 1, 2, 3])
        for c in prov["changes"]:
            self.assertEqual(c["op"], "add_row")
            self.assertIsNone(c["before"])
            self.assertEqual(set(c["after"]), set(LEDGER_COLUMNS))
        self.assertEqual(pub["manifest"]["counts"]["ledger_rows"],
                         len(pub["ledger"]))
        # Reverse the changes (reverse order) and recover the baseline ledger.
        with open(os.path.join(pub_dir, "ledger.csv"), encoding="utf-8", newline="") as fh:
            records = list(csv.reader(fh))
        header, body = records[0], records[1:]
        for c in sorted(prov["changes"], key=lambda c: -c["order"]):
            if c["op"] == "add_row":
                body = [r for r in body if r[0] != c["target"]]
        buf = io.StringIO()
        w = csv.writer(buf, lineterminator="\n")
        w.writerow(header)
        w.writerows(body)
        self.assertEqual(hashlib.sha256(buf.getvalue().encode()).hexdigest(),
                         prov["baseline_sha256"]["ledger.csv"])
        self.assertNotEqual(prov["baseline_sha256"]["ledger.csv"],
                            pub["manifest"]["files"]["ledger.csv"])
        # Public files still carry no trace of the injection.
        with open(os.path.join(pub_dir, "ledger.csv"), encoding="utf-8") as fh:
            blob = fh.read()
        self.assertNotIn("test:", blob)


if __name__ == "__main__":
    unittest.main()
