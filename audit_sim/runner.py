"""Run isolated policies and keep private evaluation separate from trajectories."""

from dataclasses import asdict
import os
from pathlib import Path
import subprocess
import sys
from time import monotonic

from .environment import AuditEnv
from .policies import POLICIES
from .scoring import oracle_submission
from .wire import MessageError, MessageReader, fingerprint, send_message

WORKER = Path(__file__).with_name("worker.py").resolve()


class WorkerProtocolError(RuntimeError):
    pass


def run_policy(directory, policy, *, private_directory=None, mode="closed", seed=None, policy_seed=0,
               timeout=60.0, tolerances=None):
    env = AuditEnv(mode=mode, timeout=timeout, tolerances=tolerances)
    config = {"episode": directory, "private": private_directory}
    return run_episode(env, config, policy, seed, policy_seed)


def run_loaded_policy(episode, policy, *, mode="closed", policy_seed=0, timeout=60.0, tolerances=None):
    env = AuditEnv(
        loader=lambda seed, config: episode, mode=mode, timeout=timeout, tolerances=tolerances,
    )
    return run_episode(env, {}, policy, None, policy_seed)


def run_episode(env, config, policy, seed, policy_seed):
    if policy not in POLICIES:
        raise ValueError("Unknown policy")
    start = monotonic()
    initial, _ = env.reset(seed, config)
    trajectory = []
    if policy == "oracle":
        env.step({"action": "submit", "findings": oracle_submission(env.episode)})
    else:
        _run_worker(env, policy, policy_seed, initial, trajectory)
    return evaluation_report(env, policy, monotonic() - start, initial), trajectory


def evaluation_report(env, policy, elapsed, initial):
    report = {
        "episode_id": env.episode_id,
        "policy": policy,
        "mode": env.mode,
        "initial_observation_sha256": fingerprint(initial),
        "tolerances": env.tolerances,
        "steps": env.steps,
        "inspection_steps": env.inspection_steps,
        "duration_seconds": round(elapsed, 6),
        "terminated": env.terminated,
        "truncated": env.truncated,
        "reason": env.reason,
        "submitted_findings": [] if env.submission is None else env.submission["findings"],
        **asdict(env.score),
    }
    # Oracle submissions and evidence stay entirely on the evaluator side.
    if policy == "oracle":
        del report["submitted_findings"]
    return report


def _run_worker(env, policy, policy_seed, initial, trajectory):
    if sys.platform != "linux":
        raise WorkerProtocolError(
            "Policy isolation requires Linux. On macOS, use Docker Desktop or open demo-report.html."
        )
    with subprocess.Popen(
        [sys.executable, "-I", "-B", "-u", str(WORKER)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        cwd="/", env={"LC_ALL": "C"}, close_fds=True,
    ) as process:
        input_fd, output_fd = process.stdin.fileno(), process.stdout.fileno()
        os.set_blocking(input_fd, False)
        reader = MessageReader(output_fd)
        try:
            if reader.read(env.deadline) != {"ready": True}:
                raise WorkerProtocolError("Unexpected policy handshake")
            send_message(input_fd, {
                "policy": policy, "policy_seed": policy_seed, "observation": initial,
            }, env.deadline)
            while not (env.terminated or env.truncated):
                action = reader.read(env.deadline)
                previous_steps = env.steps
                observation, reward, terminated, truncated, info = env.step(action)
                if env.steps == previous_steps:
                    break
                outcome = {
                    "observation": observation,
                    "reward": reward,
                    "terminated": terminated,
                    "truncated": truncated,
                    "info": info,
                }
                trajectory.append({
                    "episode_id": env.episode_id,
                    "step": env.steps,
                    "action": action,
                    **{name: value for name, value in outcome.items() if name != "info"},
                })
                send_message(input_fd, outcome, env.deadline)
            if env.terminated or env.reason == "step_limit":
                wait_time = max(0.0, env.deadline - monotonic())
                if process.wait(timeout=wait_time) != 0:
                    raise WorkerProtocolError("Policy failed after the terminal transition")
        except (TimeoutError, subprocess.TimeoutExpired):
            env.truncate("timeout")
        except (WorkerProtocolError, EOFError, MessageError, BrokenPipeError) as error:
            # Infrastructure failures must reach the verification command, not score as silence.
            raise WorkerProtocolError(
                f"{policy}: {error}. Verify the Linux policy sandbox and worker."
            ) from error
        finally:
            if process.poll() is None:
                process.kill()
