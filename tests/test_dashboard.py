import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import urlopen

from audit_sim.policies import POLICIES
from dashboard.projection import PrepareError, prepare_release, prepare_run, prepare_runs, project_episode
from dashboard.server import make_server

ROOT = Path(__file__).resolve().parents[1]
DENIED = ("evidence_sets", "expected_amount_minor", "issue_id", "provenance", "truth.json", "id_map",
          "baseline_sha256", "config_sha256")


def files(directory):
    return [path for path in Path(directory).rglob("*") if path.is_file()]


class TestDashboardProjection(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = Path(tempfile.mkdtemp(prefix="dashboard-tests-"))
        cls.addClassCleanup(shutil.rmtree, cls.temp, True)
        cls.results = cls.temp / "results"
        subprocess.run([sys.executable, "-m", "audit_sim", "run", str(ROOT / "episodes"), "--split", "dev",
                        "--output", str(cls.results)], cwd=ROOT, check=True, capture_output=True)
        cls.reports = json.loads((cls.results / "reports.json").read_text(encoding="utf-8"))

    def out(self, name):
        path = self.temp / name
        shutil.rmtree(path, ignore_errors=True)
        return path

    def copy_results(self, name, reports=None, trajectories=True):
        path = self.temp / name
        shutil.rmtree(path, ignore_errors=True)
        path.mkdir()
        (path / "reports.json").write_text(json.dumps(self.reports if reports is None else reports), encoding="utf-8")
        if trajectories is True:
            shutil.copy(self.results / "trajectories.jsonl", path / "trajectories.jsonl")
        elif trajectories:
            (path / "trajectories.jsonl").write_text(trajectories, encoding="utf-8")
        return path

    def bundle(self, out, name):
        return json.loads((out / "bundles" / name / "bundle.json").read_text(encoding="utf-8"))

    def assert_no_leaks(self, out):
        for path in files(out):
            text = path.read_text(encoding="utf-8")
            for word in DENIED:
                self.assertNotIn(word, text, f"{word} leaked into {path.name}")
            self.assertNotIn(str(ROOT), text)
            self.assertNotIn(str(self.temp), text)

    def test_reviewer_run_projection_and_joins(self):
        out = self.out("reviewer")
        prepare_run(out, self.results, audience="reviewer", name="dev", episodes_root=ROOT / "episodes")
        bundle = self.bundle(out, "dev")
        self.assertEqual(len(bundle["runs"]), 10 * len(POLICIES))
        self.assertEqual(bundle["splits"], ["dev"])
        by_key = {run["key"]: run for run in bundle["runs"]}
        for report in self.reports:
            run = by_key[f"dev~{report['episode_id']}~{report['policy']}~closed"]
            self.assertEqual(run["metrics"]["reward"], report["reward"])
            self.assertEqual(run["metrics"]["f1"], report["f1"])
            if report["policy"] == "oracle":
                self.assertNotIn("findings", run)
                self.assertEqual(run["activity_state"], "oracle")
            else:
                self.assertEqual(len(run["findings"]), len(report["submitted_findings"]))
                self.assertEqual(run["activity_state"], "available")
                rows = json.loads((out / "bundles" / "dev" / "activity" / "dev" / report["episode_id"]
                                   / f"{report['policy']}__closed.json").read_text(encoding="utf-8"))
                self.assertEqual([row["step"] for row in rows], list(range(1, report["steps"] + 1)))
                self.assertTrue(all(row["reward"] == 0 for row in rows))
        clean = [run for run in bundle["runs"] if run.get("clean_established")]
        self.assertTrue(clean and all(run["policy"] != "mark_all" for run in clean))
        records = json.loads((out / "bundles" / "dev" / "episodes" / "dev" / "EP-0CMTEHMK3Z.json").read_text(encoding="utf-8"))
        self.assertEqual(records["status"], "ok")
        self.assertTrue(all(isinstance(row["rate"], str) for row in records["fx"]))
        self.assertNotIn("files", records["manifest"])
        self.assert_no_leaks(out)

    def test_public_projection_withholds_evaluator_data(self):
        out = self.out("public")
        prepare_run(out, self.results, audience="public", name="dev-public", episodes_root=ROOT / "episodes")
        bundle = self.bundle(out, "dev-public")
        self.assertEqual(len(bundle["runs"]), 10 * (len(POLICIES) - 1))
        self.assertFalse(any(run["policy"] == "oracle" for run in bundle["runs"]))
        text = "".join(path.read_text(encoding="utf-8") for path in files(out))
        for word in ('"metrics"', '"reward"', '"tp"', '"f1"', "clean_established", "oracle"):
            self.assertNotIn(word, text)
        self.assert_no_leaks(out)

    def test_duplicates_malformed_and_partial_inputs(self):
        out = self.out("edge")
        reports = self.reports + [self.reports[1], {"episode_id": "../x", "policy": "p", "mode": "closed"}]
        lines = (self.results / "trajectories.jsonl").read_text(encoding="utf-8").splitlines()
        first = json.loads(lines[0])
        kept = [line for line in lines if not (json.loads(line)["episode_id"] == first["episode_id"]
                                                and json.loads(line)["policy"] == first["policy"]
                                                and json.loads(line)["step"] == 2)]
        source = self.copy_results("edge-src", reports, "\n".join(kept) + "\n{not json\n")
        prepare_run(out, source, audience="reviewer", name="edge", episodes_root=ROOT / "episodes")
        bundle = self.bundle(out, "edge")
        notes = " ".join(bundle["notes"])
        self.assertIn("more than once", notes)
        self.assertIn(f"row {len(self.reports) + 2} skipped", notes)
        self.assertIn("line", notes)
        self.assertEqual(len(bundle["runs"]), len(self.reports) - 1)
        partial = next(run for run in bundle["runs"] if run["episode_id"] == first["episode_id"] and run["policy"] == first["policy"])
        self.assertEqual(partial["activity_state"], "partial")

        broken = self.copy_results("broken-src")
        (broken / "reports.json").write_text("[{", encoding="utf-8")
        with self.assertRaises(PrepareError):
            prepare_run(self.out("broken"), broken, audience="reviewer", name="broken", episodes_root=ROOT / "episodes")

        no_log = self.copy_results("nolog-src", trajectories=False)
        prepare_run(self.out("nolog"), no_log, audience="reviewer", name="nolog", episodes_root=ROOT / "episodes")
        runs = self.bundle(self.temp / "nolog", "nolog")["runs"]
        self.assertTrue(all(run["activity_state"] in ("missing", "oracle") for run in runs))

    def test_split_verification_and_reviewer_gate(self):
        with self.assertRaises(PrepareError):
            prepare_run(self.out("nosplit"), self.results, audience="reviewer", name="nosplit")
        out = self.out("unspecified")
        prepare_run(out, self.results, audience="public", name="unspecified")
        bundle = self.bundle(out, "unspecified")
        self.assertEqual(bundle["splits"], ["unspecified"])
        self.assertTrue(all(run["records"] == "missing" for run in bundle["runs"]))
        wrong = self.out("wrong-split")
        prepare_run(wrong, self.results, audience="public", name="wrong", episodes_root=ROOT / "episodes", split="train")
        self.assertEqual(self.bundle(wrong, "wrong")["splits"], ["unspecified"])

    def test_simple_baseline_is_weaker_than_reference(self):
        by_policy = {}
        for report in self.reports:
            by_policy.setdefault(report["policy"], []).append(report)
        mean = lambda policy: sum(r["f1"] for r in by_policy[policy]) / len(by_policy[policy])
        self.assertLess(mean("simple"), mean("reconcile"))
        self.assertGreater(mean("simple"), mean("mark_all"))
        kinds = {f["type"] for r in by_policy["simple"] for f in r["submitted_findings"]}
        self.assertTrue(kinds <= {"duplicate_invoice", "payment_allocation"}, kinds)

    def test_merged_modes_keep_their_own_activity(self):
        train_mode = self.temp / "results-train-mode"
        if not train_mode.exists():
            subprocess.run([sys.executable, "-m", "audit_sim", "run", str(ROOT / "episodes"), "--split", "dev",
                            "--mode", "train", "--policy", "reconcile", "--output", str(train_mode)],
                           cwd=ROOT, check=True, capture_output=True)
        out = self.out("modes")
        prepare_runs(out, [self.results, train_mode], audience="reviewer", name="modes",
                     episodes_root=ROOT / "episodes", label="Modes")
        bundle = self.bundle(out, "modes")
        self.assertEqual(bundle["modes"], ["closed", "train"])
        episode = self.reports[0]["episode_id"]
        logs = {}
        for mode in ("closed", "train"):
            run = next(r for r in bundle["runs"] if r["key"] == f"dev~{episode}~reconcile~{mode}")
            self.assertEqual(run["activity_state"], "available")
            logs[mode] = json.loads((out / "bundles" / "modes" / "activity" / "dev" / episode
                                     / f"reconcile__{mode}.json").read_text(encoding="utf-8"))
        self.assertEqual(logs["closed"][-1]["reward"], 0)
        self.assertNotEqual(logs["train"][-1]["reward"], 0)

    def test_record_status_unsupported_and_invalid(self):
        source = ROOT / "episodes" / "public" / "dev" / "EP-0CMTEHMK3Z"
        old = self.temp / "old-episode"
        shutil.rmtree(old, ignore_errors=True)
        shutil.copytree(source, old)
        manifest = json.loads((old / "manifest.json").read_text(encoding="utf-8"))
        (old / "manifest.json").write_text(json.dumps(manifest | {"schema_version": "0.9"}), encoding="utf-8")
        self.assertEqual(project_episode(old)["status"], "unsupported")
        (old / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        (old / "ledger.csv").write_text("row_id\nbroken\n", encoding="utf-8")
        result = project_episode(old)
        self.assertEqual(result["status"], "invalid")
        self.assertTrue(result["problems"])
        self.assertNotIn(str(old), json.dumps(result))
        self.assertEqual(project_episode(self.temp / "absent")["status"], "missing")

    def test_release_export(self):
        release = self.temp / "release"
        if not release.exists():
            subprocess.run([sys.executable, "-m", "audit_sim", "release", "--output", str(release)],
                           cwd=ROOT, check=True, capture_output=True)
        out = self.out("release-out")
        prepare_release(out, release, audience="reviewer", name="release")
        bundle = self.bundle(out, "release")
        self.assertEqual(len(bundle["runs"]), 10 * len(POLICIES))
        self.assertEqual(bundle["splits"], ["dev"])
        self.assertEqual(bundle["validation"]["dev_runs"], 10 * len(POLICIES))
        self.assertTrue(all(run["activity_state"] in ("release_dev", "oracle") for run in bundle["runs"]))
        self.assertFalse((out / "bundles" / "release" / "activity").exists())
        self.assert_no_leaks(out)

        server = make_server(out, port=0)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        base = f"http://127.0.0.1:{server.server_port}"
        with urlopen(base + "/data/bundles/release/bundle.json", timeout=10) as response:
            self.assertEqual(json.loads(response.read())["id"], "release")
        for path in ("/data/bundles/release/../../../README.md", "/data/bundles/", "/dashboard/projection.py",
                     "/data/bundles/release/episodes/dev/EP-0CMTEHMK3Z.csv", "/data/x/truth.json"):
            with self.subTest(path=path), self.assertRaises(HTTPError) as denied:
                urlopen(base + path, timeout=10)
            self.assertEqual(denied.exception.code, 404)


class TestLegacyConsoleEncoding(unittest.TestCase):
    def test_demo_survives_windows_code_page(self):
        """Windows pipes default to cp1252, which cannot encode the demo's explanations (e.g. Σ)."""
        with tempfile.TemporaryDirectory() as temp:
            env = dict(os.environ, PYTHONIOENCODING="cp1252")
            result = subprocess.run([sys.executable, "-m", "audit_sim", "demo", "--output", temp], cwd=ROOT,
                                    capture_output=True, env=env)
            self.assertEqual(result.returncode, 0, result.stderr.decode("cp1252", "replace"))
            self.assertIn(b"EP-CHK-A08", result.stdout)


@unittest.skipUnless(shutil.which("node"), "Node.js is not installed")
class TestDashboardBrowserLibrary(unittest.TestCase):
    def test_formatting_and_aggregation(self):
        script = """
import {money, timestamp, metric, elapsed, cohort, benchmark, indexEpisode} from "./dashboard/static/lib.js";
const check = (actual, expected, label) => { if (actual !== expected) { console.error(label, JSON.stringify(actual), "!=", JSON.stringify(expected)); process.exit(1); } };
check(money(2500000, 2), "25,000.00", "KZT");
check(money(1234, 0), "1,234", "JPY");
check(money(1234567, 3), "1,234.567", "KWD");
check(money(-5, 2), "−0.05", "negative");
check(money(0, 2), "0.00", "zero");
check(money("90071992547409930", 2), "900,719,925,474,099.30", "beyond 2^53");
check(timestamp("2026-03-04T11:00:00+05:00"), "2026-03-04 11:00:00 +05:00", "timestamp");
check(timestamp("2026-03-04T11:00:00Z"), "2026-03-04 11:00:00 +00:00", "utc");
check(metric(0.9919999), "0.992", "metric");
check(metric(-1), "−1.000", "negative metric");
check(metric(0.9919999, true), "0.9919999", "full precision");
check(elapsed(0.0421), "42.1 ms", "ms");
check(elapsed(2.5), "2.50 s", "s");
const run = (p, e, f1, reward) => ({policy: p, episode_id: e, steps: 3, duration_seconds: 0.1, status: "submitted", metrics: {f1, reward}});
const runs = [run("reconcile", "A", 1, 0.9), run("reconcile", "B", 0, -1), run("mark_all", "A", 0, -1)];
const c = cohort(runs);
check(c.equal, false, "unequal"); check([...c.common].join(), "A", "common");
const common = benchmark(runs);
check(common[0].episodes, 1, "common n"); check(common[0].f1, 1, "common mean");
const all = benchmark(runs, {useCommon: false});
check(all[0].episodes, 2, "all n"); check(all[0].f1, 0.5, "all mean");
const ep = indexEpisode({manifest: {currency_exponents: {KZT: 2}}, policy: {accounts: {ar: "1210", expense: ["6010"]}},
  invoices: [{doc_id: "D1", invoice_id: "INV-1", currency: "KZT"}],
  payments: [{doc_id: "P1", payment_id: "PAY-1", currency: "KZT", invoice_refs: [{invoice_id: "INV-1", amount_minor: 5}]}],
  ledger: [{row_id: "R1", entry_id: "E1", invoice_id: "INV-1", payment_id: null, account: "1210"}],
  fx: [{date: "2026-01-02", source_currency: "EUR", target_currency: "KZT", rate: "501.000000"}]});
check(ep.resolve("D1").kind, "invoice", "doc"); check(ep.resolve("R1").kind, "ledger", "row");
check(ep.resolve("FX:2026-01-02:EUR:KZT").record.rate, "501.000000", "fx key");
check(ep.resolve("INV-1"), null, "business id is not evidence");
check(ep.paymentsByInvoice.get("INV-1")[0].order, 0, "allocation order");
check(ep.roles.get("6010"), "expense", "expense role");
console.log("ok");
"""
        result = subprocess.run(["node", "--input-type=module", "-e", script], cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)


if __name__ == "__main__":
    unittest.main()
