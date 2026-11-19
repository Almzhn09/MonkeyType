"""Run isolated policies and keep private evaluation separate from trajectories."""

from dataclasses import asdict
import json
import os
from pathlib import Path
import selectors
import subprocess
import sys
from time import monotonic

from .environment import AuditEnv
from .policies import POLICIES
from .scoring import oracle_submission

MAX_MESSAGE = 1024 * 1024
WORKER = Path(__file__).with_name("worker.py").resolve()


class WorkerProtocolError(RuntimeError):
    pass


def read_message(fd, deadline):
    with selectors.DefaultSelector() as selector:
        selector.register(fd, selectors.EVENT_READ)
        data = bytearray()
        while b"\n" not in data:
            remaining = deadline - monotonic()
            if remaining <= 0 or not selector.select(remaining):
                raise TimeoutError("Policy exceeded its deadline")
            chunk = os.read(fd, 8192)
            if not chunk:
                raise WorkerProtocolError("Policy closed its output")
            data.extend(chunk)
            if len(data) > MAX_MESSAGE:
                raise WorkerProtocolError("Policy message exceeded its size limit")
    line, tail = data.split(b"\n", 1)
    if tail:
        raise WorkerProtocolError("Policy sent unsolicited output")
    try:
        return json.loads(line)
    except (ValueError, RecursionError) as error:
        raise WorkerProtocolError("Invalid policy JSON") from error


def send_message(fd, payload, deadline):
    data = memoryview((json.dumps(payload, ensure_ascii=True, allow_nan=False) + "\n").encode())
    if len(data) > MAX_MESSAGE:
        raise WorkerProtocolError("Public message exceeded its size limit")
    with selectors.DefaultSelector() as selector:
        selector.register(fd, selectors.EVENT_WRITE)
        while data:
            remaining = deadline - monotonic()
            if remaining <= 0 or not selector.select(remaining):
                raise TimeoutError("Policy exceeded its deadline")
            try:
                count = os.write(fd, data)
            except BlockingIOError:
                continue
            data = data[count:]


def run_policy(directory, policy, *, private_directory=None, mode="closed", seed=None, policy_seed=0,
               timeout=60.0, tolerances=None):
    if policy not in POLICIES:
        raise ValueError("Unknown policy")
    start = monotonic()
    env = AuditEnv(mode=mode, timeout=timeout, tolerances=tolerances)
    initial, _ = env.reset(seed, {"episode": directory, "private": private_directory})
    trajectory = []
    if policy == "oracle":
        env.step({"action": "submit", "findings": oracle_submission(env.episode)})
    else:
        _run_worker(env, policy, policy_seed, initial, trajectory)
    report = {
        "episode_id": env.episode_id,
        "policy": policy,
        "mode": mode,
        "steps": env.steps,
        "inspection_steps": env.inspection_steps,
        "duration_seconds": round(monotonic() - start, 6),
        "terminated": env.terminated,
        "truncated": env.truncated,
        "reason": env.reason,
        "submitted_findings": [] if env.submission is None else env.submission["findings"],
        **asdict(env.score),
    }
    # Oracle submissions and evidence stay entirely on the evaluator side.
    if policy == "oracle":
        del report["submitted_findings"]
    return report, trajectory


def _run_worker(env, policy, policy_seed, initial, trajectory):
    with subprocess.Popen(
        [sys.executable, "-I", "-B", "-u", str(WORKER)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        cwd="/", env={"LC_ALL": "C"}, close_fds=True,
    ) as process:
        input_fd, output_fd = process.stdin.fileno(), process.stdout.fileno()
        os.set_blocking(input_fd, False)
        try:
            if read_message(output_fd, env.deadline) != {"ready": True}:
                raise WorkerProtocolError("Unexpected policy handshake")
            send_message(input_fd, {
                "policy": policy, "policy_seed": policy_seed, "observation": initial,
            }, env.deadline)
            while not (env.terminated or env.truncated):
                action = read_message(output_fd, env.deadline)
                previous_steps = env.steps
                observation, reward, terminated, truncated, _ = env.step(action)
                if env.steps > previous_steps:
                    trajectory.append({
                        "episode_id": env.episode_id,
                        "step": env.steps,
                        "action": action,
                        "observation": observation,
                        "reward": reward,
                        "terminated": terminated,
                        "truncated": truncated,
                    })
                if not (terminated or truncated):
                    send_message(input_fd, observation, env.deadline)
        except TimeoutError:
            env.truncate("timeout")
        except (WorkerProtocolError, BrokenPipeError) as error:
            # Infrastructure failures must reach the verification command, not score as silence.
            raise WorkerProtocolError(
                f"{policy}: {error}. Verify the Linux policy sandbox and worker."
            ) from error
        finally:
            if process.poll() is None:
                process.kill()
