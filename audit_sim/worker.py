"""Private entry point for a policy worker. JSON lines are its only channel."""

import json
from pathlib import Path
import sys

# -I prevents environment and user site-packages from changing policy imports.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from audit_sim.policies import WORKER_POLICIES
from audit_sim.sandbox import enter_worker_sandbox


def send(message):
    print(json.dumps(message, ensure_ascii=True, allow_nan=False), flush=True)


def receive():
    return json.loads(input())


def request(action):
    send(action)
    return receive()


def main():
    enter_worker_sandbox()
    send({"ready": True})
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
