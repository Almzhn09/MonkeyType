"""Gymnasium-style public actions and trusted evaluator state."""

from copy import deepcopy
from time import monotonic

from .calculator import calculate
from .data import load_episode
from .schema import TABLES, is_id
from .scoring import DEFAULT_TOLERANCES, score_submission

TASK = "Audit the financial records. Submit supported discrepancies with evidence IDs."
PAGE_SIZE = 50
STEP_LIMIT = 100


class AuditEnv:
    """The trusted runner owns this object; workers receive its JSON responses only."""

    def __init__(self, loader=load_episode, *, mode="closed", tolerances=None,
                 timeout=60.0, clock=monotonic):
        if mode not in ("train", "closed"):
            raise ValueError("Mode must be train or closed")
        self.loader = loader
        self.mode = mode
        self.tolerances = DEFAULT_TOLERANCES | (tolerances or {})
        if self.tolerances.keys() != DEFAULT_TOLERANCES.keys() or not all(
            type(value) is int and value >= 0 for value in self.tolerances.values()
        ):
            raise ValueError("Invalid amount tolerances")
        if not 0 < timeout <= 60:
            raise ValueError("Timeout must be above zero and at most 60 seconds")
        self.timeout = timeout
        self.clock = clock
        self.episode = None

    def reset(self, seed=None, config=None):
        self.episode = self.loader(seed, config or {})
        self.episode_id = self.episode.manifest["episode_id"]
        self.deadline = self.clock() + self.timeout
        self.steps = 0
        self.inspection_steps = 0
        self.terminated = False
        self.truncated = False
        self.score = None
        self.reason = None
        self.submission = None
        return deepcopy({
            "task": TASK,
            "manifest": self.episode.manifest,
            "policy": self.episode.policy,
            "steps_remaining": STEP_LIMIT,
        }), {}

    def _transition(self, observation, reward=0.0):
        return (
            deepcopy(observation | {"steps_remaining": STEP_LIMIT - self.steps}),
            reward, self.terminated, self.truncated, {},
        )

    def _finish(self, payload, reason, *, truncated=False):
        self.truncated = truncated
        self.terminated = not truncated
        self.reason = reason
        self.score = score_submission(self.episode, payload, self.inspection_steps, self.tolerances)
        if self.score.invalid_submission:
            self.reason = "invalid_submission"
        self.submission = deepcopy(payload) if not truncated and not self.score.invalid_submission else None
        reward = self.score.reward if self.mode == "train" else 0.0
        return self._transition({"result": "episode_finished"}, reward)

    def truncate(self, reason):
        """Trusted runner ends an episode when a worker misses the watchdog deadline."""
        if self.terminated or self.truncated:
            return self._transition({"error": "episode_finished"})
        return self._finish({"episode_id": self.episode_id, "findings": []}, reason, truncated=True)

    def step(self, action):
        if self.episode is None:
            raise RuntimeError("Reset the environment before stepping")
        if self.terminated or self.truncated:
            return self._transition({"error": "episode_finished"})
        if self.clock() >= self.deadline:
            return self.truncate("timeout")
        self.steps += 1
        name = action.get("action") if isinstance(action, dict) else None
        if name == "submit":
            return self._finish(action.get("findings"), "submitted")
        self.inspection_steps += 1
        if self.steps == STEP_LIMIT:
            return self.truncate("step_limit")
        return self._transition(self._inspect(name, action))

    def _inspect(self, name, action):
        if name in ("list_records", "get_record"):
            table = action.get("table")
            if table not in TABLES:
                return {"error": "unknown_table"}
            if name == "list_records":
                return self._page(table, action.get("cursor"), action.get("limit", PAGE_SIZE))
            key = action.get("id")
            if not is_id(key) or key not in self.episode.records[table]:
                return {"error": "unknown_id"}
            return {"result": self.episode.records[table][key]}
        if name == "get_document":
            key = action.get("doc_id")
            if not is_id(key) or key not in self.episode.documents:
                return {"error": "unknown_id"}
            return {"result": self.episode.documents[key]}
        if name == "calculator":
            try:
                return {"result": calculate(action.get("expression"))}
            except ValueError:
                return {"error": "invalid_expression"}
        return {"error": "unknown_action"}

    def _page(self, table, cursor, limit):
        if type(limit) is not int or not 1 <= limit <= PAGE_SIZE:
            return {"error": "invalid_limit"}
        rows = self.episode.tables[table]
        start = 0
        if cursor is not None:
            prefix = table + ":"
            if not isinstance(cursor, str) or len(cursor) > 64 or not cursor.startswith(prefix):
                return {"error": "invalid_cursor"}
            position = cursor[len(prefix):]
            if not position.isascii() or not position.isdecimal():
                return {"error": "invalid_cursor"}
            start = int(position)
            if start <= 0 or start >= len(rows):
                return {"error": "invalid_cursor"}
        end = start + limit
        return {"result": rows[start:end], "next_cursor": f"{table}:{end}" if end < len(rows) else None}
