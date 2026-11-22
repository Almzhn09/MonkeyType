"""Private entry point for a policy worker. JSON lines are its only channel."""

import json
from pathlib import Path
import sys

# -I prevents environment and user site-packages from changing policy imports.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from audit_sim.policies import WORKER_POLICIES
from audit_sim.sandbox import enter_worker_sandbox


def send(message):
    # Binary streams keep the protocol identical on every platform (no CRLF translation).
    sys.stdout.buffer.write(json.dumps(message, ensure_ascii=True, allow_nan=False).encode() + b"\n")
    sys.stdout.buffer.flush()


def receive():
    line = sys.stdin.buffer.readline()
    if not line:
        raise SystemExit(0)
    return json.loads(line)


def request(action):
    send(action)
    return receive()


def main():
    isolation = enter_worker_sandbox()
    send({"ready": True, "isolation": isolation})
    initial = receive()
    observation = initial["observation"]
    policy = WORKER_POLICIES[initial["policy"]]
    findings = policy(request, observation, initial["policy_seed"])
    send({
        "action": "submit",
        "findings": {"episode_id": observation["manifest"]["episode_id"], "findings": findings},
    })


if __name__ == "__main__":
    main()
