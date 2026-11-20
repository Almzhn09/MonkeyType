from copy import deepcopy
import json
from pathlib import Path
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from audit_sim.scoring import oracle_submission
from playground.client import ApiError, Client, run
from playground.server import Playground, make_server

ROOT = Path(__file__).resolve().parents[1]


class TestPlayground(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = Playground(ROOT / "episodes")
        cls.dev = [item for item in cls.app.episodes if item["split"] == "dev"]
        cls.with_errors = next(
            item for item in cls.dev
            if json.loads((item["_private"] / "truth.json").read_text(encoding="utf-8"))["issues"]
        )

    def submit(self, token, findings):
        session = self.app._session(token)
        episode_id = session["env"].episode_id
        return self.app.step(token, {"action": "submit", "findings": {"episode_id": episode_id, "findings": findings}})

    def test_listing_hides_private_paths_and_answers(self):
        self.assertEqual(len(self.dev), 10)
        listing = json.dumps([{k: v for k, v in e.items() if not k.startswith("_")} for e in self.app.episodes])
        self.assertNotIn("private", listing)
        self.assertNotIn("issues", listing)

    def test_steps_cost_and_answer_key_locked_until_end(self):
        game = self.app.start(self.with_errors["id"])
        self.assertNotIn("issues", json.dumps(game))
        token = game["session"]
        page = self.app.step(token, {"action": "list_records", "table": "ledger", "cursor": None, "limit": 50})
        self.assertEqual(page["steps_used"], 1)
        self.assertEqual(len(page["observation"]["result"]), 50)
        with self.assertRaises(PermissionError):
            self.app.result(token)
        self.submit(token, [])
        result = self.app.result(token)
        self.assertEqual(result["inspection_steps"], 1)
        self.assertEqual(result["score"]["tp"], 0)
        self.assertTrue(all(issue["found_by"] is None for issue in result["review"]["issues"]))

    def test_review_agrees_with_grader(self):
        token = self.app.start(self.with_errors["id"])["session"]
        env = self.app._session(token)["env"]
        correct = oracle_submission(env.episode)["findings"]
        wrong_amount = dict(correct[0], claimed_amount_minor=correct[0]["claimed_amount_minor"] + 1000)
        bad_evidence = dict(correct[0], evidence_ids=["NOT-A-RECORD"])
        no_evidence = dict(correct[0], evidence_ids=[])
        findings = [deepcopy(correct[0]), deepcopy(correct[0]), wrong_amount, bad_evidence, no_evidence, *correct[1:]]
        self.submit(token, findings)
        result = self.app.result(token)
        verdicts = [item["verdict"]["status"] for item in result["review"]["findings"]]
        self.assertEqual(verdicts.count("correct"), result["score"]["tp"])
        self.assertEqual(len(verdicts) - verdicts.count("correct"), result["score"]["fp"])
        self.assertEqual(result["score"]["fn"], 0)
        self.assertEqual(verdicts[:5], ["correct", "duplicate", "wrong", "wrong", "wrong"])
        details = [item["verdict"].get("detail", "") for item in result["review"]["findings"][2:5]]
        self.assertIn("amount is wrong", details[0])
        self.assertIn("not found", details[1])
        self.assertIn("evidence is incomplete", details[2])
        self.assertEqual(result["baselines"]["oracle"]["f1"], 1)
        self.assertEqual(result["baselines"]["mark_all"]["reward"], -1)

    def test_http_endpoints(self):
        server = make_server(self.app, port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        base = f"http://127.0.0.1:{server.server_port}"

        def post(path, body):
            request = Request(base + path, json.dumps(body).encode(), {"Content-Type": "application/json"})
            with urlopen(request, timeout=10) as response:
                return json.loads(response.read())

        with urlopen(base + "/", timeout=10) as response:
            self.assertIn(b"Audit Playground", response.read())
        with urlopen(base + "/api/episodes", timeout=10) as response:
            self.assertEqual(len(json.loads(response.read())), len(self.app.episodes))
        token = post("/api/start", {"episode": self.dev[0]["id"]})["session"]
        with self.assertRaises(HTTPError) as early:
            post("/api/result", {"session": token})
        self.assertEqual(early.exception.code, 409)
        with self.assertRaises(HTTPError) as unknown:
            post("/api/step", {"session": "missing", "action": {"action": "submit"}})
        self.assertEqual(unknown.exception.code, 404)
        with self.assertRaises(HTTPError) as missing:
            post("/api/start", {"episode": "../../etc"})
        self.assertEqual(missing.exception.code, 404)

        client = Client(base)
        result = run(client, self.with_errors["id"])
        self.assertEqual(result["reason"], "submitted")
        self.assertEqual(result["score"]["tp"], 0)
        self.assertGreater(result["score"]["fn"], 0)
        self.assertEqual(result["inspection_steps"], client.result()["inspection_steps"])
        game = client.start(self.with_errors["id"])
        answer = oracle_submission(self.app._session(client.session)["env"].episode)["findings"]
        self.assertTrue(client.submit(answer)["finished"])
        self.assertEqual(client.result()["score"]["f1"], 1)
        self.assertEqual(game["observation"]["steps_remaining"], 100)
        after = client.step({"action": "list_records", "table": "ledger", "cursor": None, "limit": 50})
        self.assertEqual(after["observation"]["error"], "episode_finished")
        with self.assertRaises(ApiError):
            Client(base).result()


if __name__ == "__main__":
    unittest.main()
