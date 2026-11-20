"""One episode over JSON lines, for manual inspection or an external judge."""

import os
from time import monotonic

from .environment import AuditEnv
from .fixtures import dump_json, dump_jsonl
from .runner import evaluation_report
from .wire import MessageError, MessageReader, MessageTooLarge, send_message


def run_session(input_fd, output_fd, *, config, seed=None, mode="closed", timeout=60.0,
                tolerances=None, output=None):
    start = monotonic()
    env = AuditEnv(mode=mode, timeout=timeout, tolerances=tolerances)
    observation, info = env.reset(seed=seed, config=config)
    trace = []
    reader = MessageReader(input_fd)
    blocking = os.get_blocking(output_fd)
    os.set_blocking(output_fd, False)
    try:
        send_message(output_fd, {"observation": observation, "info": info}, env.deadline)
        while not (env.terminated or env.truncated):
            try:
                action = reader.read(env.deadline)
            except TimeoutError:
                send_result(output_fd, env.truncate("timeout"), monotonic() + 0.1)
                break
            except EOFError:
                send_result(output_fd, env.truncate("input_closed"), env.deadline)
                break
            except MessageTooLarge:
                send_result(output_fd, env.truncate("input_limit"), env.deadline)
                break
            except MessageError:
                action = None

            before = env.steps
            result = env.step(action)
            if env.steps != before:
                trace.append({
                    "episode_id": env.episode_id, "policy": "interactive",
                    "step": env.steps, "action": action, **public_result(result),
                })
            send_result(output_fd, result, monotonic() + 0.1 if env.reason == "timeout" else env.deadline)
    except TimeoutError:
        env.truncate("timeout")
    except BrokenPipeError:
        env.truncate("output_closed")
    finally:
        os.set_blocking(output_fd, blocking)

    if output is not None:
        output.mkdir(parents=True, exist_ok=True)
        report = evaluation_report(env, "interactive", monotonic() - start, observation)
        dump_json(output / "reports.json", [report])
        dump_jsonl(output / "trajectories.jsonl", trace)


def public_result(result):
    observation, reward, terminated, truncated, _ = result
    return {
        "observation": observation, "reward": reward,
        "terminated": terminated, "truncated": truncated,
    }


def send_result(fd, result, deadline):
    send_message(fd, {**public_result(result), "info": result[-1]}, deadline)
