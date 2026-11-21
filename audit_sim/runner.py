"""Run isolated policies and keep private evaluation separate from trajectories."""

from dataclasses import asdict
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
from time import monotonic

from .environment import AuditEnv
from .policies import POLICIES
from .scoring import oracle_submission

MAX_MESSAGE = 1024 * 1024
WORKER = Path(__file__).with_name("worker.py").resolve()


class WorkerProtocolError(RuntimeError):
    pass


class _LineReader:
    """Reads newline-terminated messages from a pipe on a background thread.

    Pipes cannot be polled with select() on Windows, so a thread with a queue gives the
    same deadline semantics on every platform.
    """

    def __init__(self, stream):
        self._stream = stream
        self._queue = queue.Queue()
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        data = bytearray()
        try:
            while True:
                chunk = self._stream.read1(8192)
                if not chunk:
                    self._queue.put(WorkerProtocolError("Policy closed its output"))
                    return
                data.extend(chunk)
                if len(data) > MAX_MESSAGE:
                    self._queue.put(WorkerProtocolError("Policy message exceeded its size limit"))
                    return
                if b"\n" in data:
                    line, tail = bytes(data).split(b"\n", 1)
                    if tail:
                        self._queue.put(WorkerProtocolError("Policy sent unsolicited output"))
                        return
                    self._queue.put(line)
                    data.clear()
        except (OSError, ValueError):
            self._queue.put(WorkerProtocolError("Policy closed its output"))

    def read(self, deadline):
        try:
            item = self._queue.get(timeout=max(0.0, deadline - monotonic()))
        except queue.Empty:
            raise TimeoutError("Policy exceeded its deadline") from None
        if isinstance(item, Exception):
            raise item
        try:
            return json.loads(item)
        except (ValueError, RecursionError) as error:
            raise WorkerProtocolError("Invalid policy JSON") from error


def send_message(stream, payload, deadline):
    data = (json.dumps(payload, ensure_ascii=True, allow_nan=False) + "\n").encode()
    if len(data) > MAX_MESSAGE:
        raise WorkerProtocolError("Public message exceeded its size limit")
    failure = []

    def write():
        try:
            stream.write(data)
            stream.flush()
        except (OSError, ValueError) as error:
            failure.append(error)

    writer = threading.Thread(target=write, daemon=True)
    writer.start()
    writer.join(max(0.0, deadline - monotonic()))
    if writer.is_alive():
        # The caller kills the worker, which unblocks and ends the writer thread.
        raise TimeoutError("Policy exceeded its deadline")
    if failure:
        if isinstance(failure[0], OSError):
            raise BrokenPipeError("Policy closed its input") from failure[0]
        raise WorkerProtocolError("Policy closed its input") from failure[0]


def _worker_environment():
    env = {"LC_ALL": "C"}
    if sys.platform == "win32" and "SYSTEMROOT" in os.environ:
        # Windows needs SYSTEMROOT to start an interpreter reliably.
        env["SYSTEMROOT"] = os.environ["SYSTEMROOT"]
    return env


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
        cwd=WORKER.anchor, env=_worker_environment(), close_fds=True,
    ) as process:
        reader = _LineReader(process.stdout)
        try:
            if reader.read(env.deadline) != {"ready": True}:
                raise WorkerProtocolError("Unexpected policy handshake")
            send_message(process.stdin, {
                "policy": policy, "policy_seed": policy_seed, "observation": initial,
            }, env.deadline)
            while not (env.terminated or env.truncated):
                action = reader.read(env.deadline)
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
                    send_message(process.stdin, observation, env.deadline)
        except TimeoutError:
            env.truncate("timeout")
        except (WorkerProtocolError, BrokenPipeError) as error:
            # Infrastructure failures must reach the verification command, not score as silence.
            raise WorkerProtocolError(
                f"{policy}: {error}. Verify the policy sandbox and worker."
            ) from error
        finally:
            if process.poll() is None:
                process.kill()
