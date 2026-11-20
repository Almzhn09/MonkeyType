from contextlib import closing, redirect_stderr
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
import json
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
import unittest
from unittest.mock import patch

from audit_sim.admin import AdminHandler, Workspace, company_config
from audit_sim.replay import replay
from fincheck.verify import discover


COMPANY = {
    "name": "Склад Север", "invoices": 40, "currency": "KZT", "start_month": "2026-03",
    "months": 1, "seed": "910203", "scenarios": ["duplicate_invoice"],
}


class TestLocalAdmin(unittest.TestCase):
    def test_queue_creates_replayable_training_data_and_survives_restart(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = Workspace(root)
            try:
                with self.assertRaisesRegex(ValueError, "уже открыта"):
                    Workspace(root)
                identifier = workspace.add(COMPANY)
                workspace.worker.submit(lambda: None).result(timeout=20)
                company = workspace.companies()[0]
                self.assertEqual((company["id"], company["status"]), (identifier, "done"))
                self.assertEqual(company["result"]["tp"], 1)
                folder = root / "runs" / identifier
                locations, errors = discover(str(folder / "episodes"))
                self.assertFalse(errors)
                (public, private), = locations
                episode = company["result"]["episode_id"]
                result = replay({episode: (Path(public), Path(private))}, folder / "trajectories.jsonl",
                                reports_path=folder / "reports.json")
                self.assertEqual(result["reports"][0]["mode"], "train")
                self.assertEqual(result["steps"], company["result"]["steps"])
                with self.assertRaisesRegex(ValueError, "уже есть"):
                    workspace.add(dict(COMPANY, name="Второе имя"))
                with workspace.database() as db:
                    db.execute("UPDATE companies SET status='running' WHERE id=?", (identifier,))
            finally:
                workspace.close()
            reopened = Workspace(root)
            try:
                reopened.worker.submit(lambda: None).result(timeout=20)
                self.assertEqual(reopened.companies(), [company])
                self.assertEqual(reopened.result_paths(), [folder / "trajectories.jsonl"])
            finally:
                reopened.close()

    def test_local_http_session_protects_data_and_publishes_completed_export(self):
        with TemporaryDirectory() as directory:
            workspace = Workspace(Path(directory))
            server = ThreadingHTTPServer(("127.0.0.1", 0), AdminHandler)
            server.workspace = workspace
            server.token = "test-session"
            thread = Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                base = f"http://127.0.0.1:{server.server_port}"
                with closing(HTTPConnection("127.0.0.1", server.server_port, timeout=5)) as client:
                    def request(method, path, headers=None, body=None):
                        client.request(method, path, body, headers or {})
                        response = client.getresponse()
                        return response.status, dict(response.getheaders()), response.read()

                    self.assertEqual(request("GET", "/api/companies")[0], 401)
                    self.assertEqual(request("GET", "/", {"Host": "other.example"})[0], 403)
                    status, headers, _ = request("GET", "/?open=test-session")
                    self.assertEqual(status, 303)
                    cookie = headers["Set-Cookie"].partition(";")[0]
                    same_origin = {"Cookie": cookie, "Origin": base, "Content-Type": "application/json"}
                    foreign = dict(same_origin, Origin="http://unrelated.example")
                    payload = json.dumps(COMPANY)
                    self.assertEqual(request("POST", "/api/companies", foreign, payload)[0], 403)
                    self.assertEqual(request("POST", "/api/companies", same_origin, "{}+")[0], 400)
                    status, _, result = request("POST", "/api/companies", same_origin, payload)
                    self.assertEqual(status, 202)
                    identifier = json.loads(result)["id"]
                    workspace.worker.submit(lambda: None).result(timeout=20)
                    self.assertEqual(request("GET", f"/runs/{identifier}/private/truth.json", same_origin)[0], 404)
                    self.assertEqual(request("GET", f"/runs/{identifier}/report.html", same_origin)[0], 200)
                    status, _, data = request("GET", "/export/trajectories.jsonl", same_origin)
                    self.assertEqual(status, 200)
                    rows = [json.loads(line) for line in data.splitlines()]
                    self.assertNotIn(b"evidence_sets", data)
                    self.assertNotIn(b"provenance", data)
                    self.assertTrue(rows[-1]["terminated"])
                    self.assertGreater(rows[-1]["reward"], 0)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)
                workspace.close()

    def test_form_rejects_incompatible_and_non_synthetic_inputs(self):
        with self.assertRaisesRegex(ValueError, "Выберите сценарии"):
            company_config(dict(COMPANY, scenarios=["unknown"]))
        with self.assertRaisesRegex(ValueError, "хотя бы 24 счетов"):
            company_config(dict(COMPANY, invoices=12, scenarios=[
                "duplicate_invoice", "payment_allocation", "fx_conversion", "wrong_period",
                "duplicate_plus_period", "fx_partial_payment_boundary",
            ]))
        with self.assertRaisesRegex(ValueError, "настройки вымышленной компании"):
            company_config(dict(COMPANY, ledger="real data"))

    def test_failed_run_can_retry_without_creating_another_company(self):
        with TemporaryDirectory() as directory:
            workspace = Workspace(Path(directory))
            try:
                with patch.object(workspace, "generate_result", side_effect=OSError("write failed")), redirect_stderr(StringIO()):
                    identifier = workspace.add(COMPANY)
                    workspace.worker.submit(lambda: None).result(timeout=20)
                self.assertEqual(workspace.companies()[0]["status"], "failed")
                self.assertTrue(workspace.retry(identifier))
                workspace.worker.submit(lambda: None).result(timeout=20)
                (company,) = workspace.companies()
                self.assertEqual((company["id"], company["status"]), (identifier, "done"))
                self.assertFalse(workspace.retry(identifier))
            finally:
                workspace.close()
