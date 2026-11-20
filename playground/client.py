"""Minimal client for driving the playground HTTP API from your own agent.

Start the server first (``python3 -m playground --no-browser``), then either import ``Client``
into your agent or run the example: ``python3 -m playground.client --episode EP-0CMTEHMK3Z``.
The example only reads every invoice and payment and submits no findings; replace
``choose_findings`` with your agent's logic.
"""

import argparse
import json
import sys
from urllib.error import HTTPError
from urllib.request import Request, urlopen


class ApiError(RuntimeError):
    pass


class Client:
    def __init__(self, url="http://127.0.0.1:8765", timeout=30):
        self.url = url.rstrip("/")
        self.timeout = timeout
        self.session = None
        self.episode_id = None

    def _call(self, path, body=None):
        data = None if body is None else json.dumps(body).encode("utf-8")
        request = Request(self.url + path, data, {"Content-Type": "application/json"})
        try:
            with urlopen(request, timeout=self.timeout) as response:
                return json.loads(response.read())
        except HTTPError as error:
            raise ApiError(json.loads(error.read() or b"{}").get("error", str(error))) from None

    def episodes(self):
        """Companies you can play: id, split, company, base_currency, period, sizes."""
        return self._call("/api/episodes")

    def start(self, episode_id):
        """Begin an episode. Returns the initial observation: task, manifest, policy, steps_remaining."""
        game = self._call("/api/start", {"episode": episode_id})
        self.session = game["session"]
        self.episode_id = game["observation"]["manifest"]["episode_id"]
        return game

    def step(self, action):
        """One environment action. Returns {"observation", "steps_used", "finished"}."""
        return self._call("/api/step", {"session": self.session, "action": action})

    def list_records(self, table, cursor=None, limit=50):
        return self.step({"action": "list_records", "table": table, "cursor": cursor, "limit": limit})

    def get_record(self, table, record_id):
        return self.step({"action": "get_record", "table": table, "id": record_id})

    def get_document(self, doc_id):
        return self.step({"action": "get_document", "doc_id": doc_id})

    def calculator(self, expression):
        return self.step({"action": "calculator", "expression": expression})

    def submit(self, findings):
        return self.step({"action": "submit", "findings": {"episode_id": self.episode_id, "findings": findings}})

    def result(self):
        """Score, per-finding review, answer key and baselines. Only after the episode has ended."""
        return self._call("/api/result", {"session": self.session})

    def read_table(self, table):
        """Read a whole table page by page. Costs one step per page of 50 rows."""
        rows, cursor = [], None
        while True:
            page = self.list_records(table, cursor)
            observation = page["observation"]
            if "error" in observation or page["finished"]:
                raise ApiError(f"Could not read {table}: {observation}")
            rows.extend(observation["result"])
            cursor = observation["next_cursor"]
            if cursor is None:
                return rows


def choose_findings(game, invoices, payments):
    """Replace with your agent. Each finding is a dict with: type, affected_ids,
    claimed_amount_minor, currency, evidence_ids, explanation."""
    return []


def run(client, episode_id):
    game = client.start(episode_id)
    invoices = client.read_table("invoices")
    payments = client.read_table("payments")
    client.submit(choose_findings(game, invoices, payments))
    return client.result()


def main():
    parser = argparse.ArgumentParser(prog="python3 -m playground.client", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--url", default="http://127.0.0.1:8765")
    parser.add_argument("--episode", help="Episode ID; omit to list the available episodes")
    args = parser.parse_args()
    client = Client(args.url)
    try:
        if not args.episode:
            for item in client.episodes():
                print(f"{item['id']}  {item['split']:5}  {item['company']}")
            return 0
        result = run(client, args.episode)
    except (ApiError, OSError) as error:
        print(f"playground.client: {error}", file=sys.stderr)
        return 1
    summary = {"reason": result["reason"], "steps": result["steps"], **result["score"],
               "baselines": {name: score["reward"] for name, score in result["baselines"].items()}}
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
