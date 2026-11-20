"""HTTP server for the audit playground.

The page talks to the same ``AuditEnv`` the policies use: every page of records costs a step,
the 100-step limit applies and the submission is scored by the evaluator's grader. The only
difference is that a person gets no 60-second wall-clock limit. The answer key is revealed
only after the episode has ended.
"""

from dataclasses import asdict
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import secrets
import threading

from audit_sim.environment import STEP_LIMIT, AuditEnv
from audit_sim.runner import run_policy
from audit_sim.schema import ISSUE_TYPES, V1_ISSUE_TYPES
from audit_sim.scoring import candidate_graph, canonical_ids, key, validate_submission
from fincheck.verify import discover

STATIC = Path(__file__).with_name("static")
DEFAULT_EPISODES = Path(__file__).resolve().parents[1] / "episodes"
BASELINES = ("oracle", "reconcile", "random", "mark_all")
MAX_BODY = 1024 * 1024
MAX_SESSIONS = 200


def list_episodes(root):
    """Episodes under ``root/{public,private}/<split>/<id>``, without revealing their errors."""
    pairs, problems = discover(str(root))
    if problems:
        raise ValueError("\n".join(problems))
    episodes = []
    for public, private in pairs:
        public, private = Path(public), Path(private)
        manifest = json.loads((public / "manifest.json").read_text(encoding="utf-8"))
        counts = manifest.get("counts", {})
        episodes.append({
            "id": manifest["episode_id"],
            "split": public.parent.name,
            "company": manifest.get("company", {}).get("name", manifest["episode_id"]),
            "base_currency": manifest["base_currency"],
            "period_start": manifest["period_start"],
            "period_end": manifest["period_end"],
            "ledger_rows": counts.get("ledger_rows"),
            "documents": counts.get("documents"),
            "_public": public,
            "_private": private,
        })
    order = {"train": 0, "dev": 1, "pairs": 2}
    episodes.sort(key=lambda item: (order.get(item["split"], 9), item["company"]))
    return episodes


def public_view(episode):
    return {name: value for name, value in episode.items() if not name.startswith("_")}


def review(env, payload):
    """Explain the grader's decision for each finding. Mirrors ``score_submission``."""
    episode = env.episode
    issues = sorted(episode.issues, key=lambda issue: issue["issue_id"])
    if not validate_submission(payload, env.episode_id):
        return {"findings": [], "issues": [dict(issue, found_by=None) for issue in issues]}
    findings = payload["findings"]
    unique, positions, seen = [], [], {}
    verdicts = [None] * len(findings)
    for index, finding in enumerate(findings):
        signature = (key(finding), finding["claimed_amount_minor"], canonical_ids(finding["evidence_ids"]))
        if signature in seen:
            verdicts[index] = {"status": "duplicate", "detail": f"Same as finding {seen[signature] + 1}."}
            continue
        seen[signature] = index
        unique.append(finding)
        positions.append(index)

    graph = candidate_graph(unique, issues, episode.evidence_ids, {})
    owners = {}

    def assign(row, visited):
        for column in graph[row]:
            if column in visited:
                continue
            visited.add(column)
            if column not in owners or assign(owners[column], visited):
                owners[column] = row
                return True
        return False

    for row in range(len(graph)):
        assign(row, set())
    matched = {row: column for column, row in owners.items()}
    available = frozenset(canonical_ids(episode.evidence_ids))

    for row, finding in enumerate(unique):
        index = positions[row]
        if row in matched:
            verdicts[index] = {"status": "correct", "issue_id": issues[matched[row]]["issue_id"]}
            continue
        unknown = sorted(set(canonical_ids(finding["evidence_ids"])) - available)
        same_target = [issue for issue in issues if key(issue) == key(finding)]
        if unknown:
            detail = f"Evidence IDs not found in this company's records: {', '.join(unknown)}."
        elif not same_target:
            detail = "No error of this type on these documents in this currency."
        elif all(
            abs(finding["claimed_amount_minor"] - issue["expected_amount_minor"]) > issue.get("tolerance_minor", 0)
            for issue in same_target
        ):
            detail = "Right error and document, but the amount is wrong."
        elif graph[row]:
            detail = "This error was already credited to another of your findings."
        else:
            detail = "Right error, document and amount, but the evidence is incomplete."
        verdicts[index] = {"status": "wrong", "detail": detail}

    return {
        "findings": [dict(finding, verdict=verdicts[index]) for index, finding in enumerate(findings)],
        "issues": [
            dict(issue, found_by=positions[owners[column]] + 1 if column in owners else None)
            for column, issue in enumerate(issues)
        ],
    }


class Playground:
    def __init__(self, episodes_root=DEFAULT_EPISODES):
        self.episodes = list_episodes(episodes_root)
        if not self.episodes:
            raise ValueError(f"No episodes found under {episodes_root}")
        self.by_id = {item["id"]: item for item in self.episodes}
        self.sessions = {}
        self.lock = threading.Lock()

    def start(self, episode_id):
        episode = self.by_id.get(episode_id)
        if episode is None:
            raise LookupError("Unknown episode")
        # No wall-clock limit for people; the step limit and the grader are unchanged.
        env = AuditEnv(timeout=60.0, clock=lambda: 0.0)
        observation, _ = env.reset(None, {"episode": episode["_public"], "private": episode["_private"]})
        token = secrets.token_urlsafe(16)
        with self.lock:
            if len(self.sessions) >= MAX_SESSIONS:
                self.sessions.pop(next(iter(self.sessions)))
            self.sessions[token] = {"env": env, "episode": episode, "lock": threading.Lock(), "payload": None}
        v1 = str(observation["manifest"].get("schema_version")) == "1.0"
        return {
            "session": token,
            "episode": public_view(episode),
            "observation": observation,
            "issue_types": list(V1_ISSUE_TYPES if v1 else ISSUE_TYPES),
            "step_limit": STEP_LIMIT,
        }

    def _session(self, token):
        with self.lock:
            session = self.sessions.get(token)
        if session is None:
            raise LookupError("Unknown or expired session; start the episode again")
        return session

    def step(self, token, action):
        session = self._session(token)
        with session["lock"]:
            env = session["env"]
            if isinstance(action, dict) and action.get("action") == "submit" and not (env.terminated or env.truncated):
                session["payload"] = action.get("findings")
            observation, _, terminated, truncated, _ = env.step(action)
        return {"observation": observation, "steps_used": env.steps, "finished": terminated or truncated}

    def result(self, token):
        session = self._session(token)
        with session["lock"]:
            env = session["env"]
            if not (env.terminated or env.truncated):
                raise PermissionError("The answer key is shown only after the episode has ended")
            if "result" not in session:
                payload = session["payload"]
                if env.truncated:
                    payload = {"episode_id": env.episode_id, "findings": []}
                baselines = {}
                for policy in BASELINES:
                    report, _ = run_policy(
                        session["episode"]["_public"], policy, private_directory=session["episode"]["_private"],
                    )
                    baselines[policy] = {name: report[name] for name in ("tp", "fp", "fn", "f1", "reward", "steps")}
                session["result"] = {
                    "reason": env.reason,
                    "steps": env.steps,
                    "inspection_steps": env.inspection_steps,
                    "score": asdict(env.score),
                    "review": review(env, payload),
                    "baselines": baselines,
                }
            return session["result"]


def make_handler(app):
    class Handler(BaseHTTPRequestHandler):
        server_version = "MonkeyTypePlayground"

        def log_message(self, *_):
            pass

        def _send(self, status, body, content_type):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, status, payload):
            self._send(status, json.dumps(payload, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

        def do_GET(self):
            if self.path in ("/", "/index.html"):
                self._send(HTTPStatus.OK, (STATIC / "index.html").read_bytes(), "text/html; charset=utf-8")
            elif self.path == "/api/episodes":
                self._json(HTTPStatus.OK, [public_view(item) for item in app.episodes])
            else:
                self._json(HTTPStatus.NOT_FOUND, {"error": "Not found"})

        def do_POST(self):
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY:
                self._json(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {"error": "Request too large"})
                return
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
                if not isinstance(body, dict):
                    raise ValueError
            except ValueError:
                self._json(HTTPStatus.BAD_REQUEST, {"error": "Invalid JSON"})
                return
            try:
                if self.path == "/api/start":
                    payload = app.start(body.get("episode"))
                elif self.path == "/api/step":
                    payload = app.step(body.get("session"), body.get("action"))
                elif self.path == "/api/result":
                    payload = app.result(body.get("session"))
                else:
                    self._json(HTTPStatus.NOT_FOUND, {"error": "Not found"})
                    return
            except LookupError as error:
                self._json(HTTPStatus.NOT_FOUND, {"error": str(error)})
            except PermissionError as error:
                self._json(HTTPStatus.CONFLICT, {"error": str(error)})
            else:
                self._json(HTTPStatus.OK, payload)

    return Handler


def make_server(app, host="127.0.0.1", port=8765):
    return ThreadingHTTPServer((host, port), make_handler(app))
