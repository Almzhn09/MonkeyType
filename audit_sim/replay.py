"""Check recorded actions and observations against the live audit contract."""

from dataclasses import asdict
from itertools import groupby

from .environment import AuditEnv, STEP_LIMIT
from .wire import MAX_TRACE_ROW, encode_json, fingerprint, parse_json


def replay_key(row):
    try:
        episode = row["episode_id"]
        policy = row.get("policy", "recorded")
    except (AttributeError, KeyError, TypeError):
        raise ValueError("Each trajectory row needs an episode_id") from None
    if not all(isinstance(value, str) and value for value in (episode, policy)):
        raise ValueError("Trajectory episode_id and policy must be nonempty strings")
    return episode, policy


def read_trajectory(path):
    with path.open("rb") as stream:
        number = 0
        while line := stream.readline(MAX_TRACE_ROW + 1):
            number += 1
            if len(line) > MAX_TRACE_ROW:
                raise ValueError(f"Trajectory line {number} exceeds the size limit")
            if not line.strip():
                continue
            try:
                entry = parse_json(line)
                replay_key(entry)
            except ValueError as error:
                raise ValueError(f"Trajectory line {number}: {error}") from error
            yield entry


def read_reports(path):
    if path is None:
        return {}
    entries = parse_json(path.read_bytes())
    if not isinstance(entries, list):
        raise ValueError("Reports must contain a JSON array")
    reports = {}
    for entry in entries:
        key = replay_key(entry)
        if key[1] == "oracle":
            continue
        if key in reports:
            raise ValueError("Duplicate report for an episode and policy")
        reports[key] = entry
    return reports


def step_result(observation, reward, terminated, truncated):
    return {
        "observation": observation, "reward": reward,
        "terminated": terminated, "truncated": truncated,
    }


def same_value(expected, recorded):
    if type(expected) is float:
        return type(recorded) in (int, float) and recorded == expected
    # Compare JSON representations so integers and booleans remain distinct.
    return encode_json(expected, sort_keys=True) == encode_json(recorded, sort_keys=True)


def apply_recorded_step(env, entry, index, label):
    if index > STEP_LIMIT or type(entry.get("step")) is not int or entry["step"] != index:
        raise ValueError(f"{label}: invalid step order at {index}")
    if env.terminated or env.truncated or "action" not in entry:
        raise ValueError(f"{label}: unexpected action at step {index}")
    observed = step_result(*env.step(entry["action"])[:4])
    for key, value in observed.items():
        if key not in entry or not same_value(value, entry[key]):
            raise ValueError(f"{label}: {key} differs at step {index}")


def replay_session(location, key, entries, mode, report, tolerances):
    episode_id, policy = key
    label = f"{episode_id}/{policy}"
    if report is not None:
        report_mode = report.get("mode")
        if mode is None:
            mode = report_mode
        elif report_mode != mode:
            raise ValueError(f"{label}: report mode differs from requested mode")
        if "tolerances" in report:
            if tolerances is None:
                tolerances = report["tolerances"]
            elif not same_value(tolerances, report["tolerances"]):
                raise ValueError(f"{label}: report tolerances differ from requested tolerances")
    if mode is None:
        raise ValueError("Specify --mode or provide reports with the recorded mode")
    env = AuditEnv(mode=mode, tolerances=tolerances, clock=lambda: 0.0)
    directory, private = location
    initial, _ = env.reset(config={"episode": directory, "private": private})
    if env.episode_id != episode_id:
        raise ValueError(f"{label}: dataset episode ID differs")
    if report is not None and "initial_observation_sha256" in report:
        if fingerprint(initial) != report["initial_observation_sha256"]:
            raise ValueError(f"{label}: initial observation differs from recorded fingerprint")

    for index, entry in enumerate(entries, 1):
        apply_recorded_step(env, entry, index, label)
    if env.score is None:
        reason = report.get("reason") if report is not None else None
        if reason not in ("timeout", "input_closed", "input_limit", "output_closed"):
            raise ValueError(f"{label}: missing terminal transition")
        env.truncate(reason)

    result = {
        "episode_id": episode_id, "policy": policy, "mode": mode,
        "steps": env.steps, "inspection_steps": env.inspection_steps,
        "reason": env.reason, "terminated": env.terminated, "truncated": env.truncated,
        **asdict(env.score),
    }
    if report is not None:
        for key, value in result.items():
            if key not in report or not same_value(value, report[key]):
                raise ValueError(f"{label}: evaluator report differs for {key}")
        if "submitted_findings" in report:
            findings = [] if env.submission is None else env.submission["findings"]
            if not same_value(findings, report["submitted_findings"]):
                raise ValueError(f"{label}: evaluator report differs for submitted_findings")
    return result


def replay(locations, trajectory_path, *, mode=None, reports_path=None, tolerances=None):
    reports = read_reports(reports_path)
    checked = []
    seen = set()
    for key, entries in groupby(read_trajectory(trajectory_path), key=replay_key):
        if key in seen:
            raise ValueError("Episode and policy rows must be contiguous")
        if key[0] not in locations:
            raise ValueError(f"Episode {key[0]} is absent from the dataset")
        if reports_path is not None and key not in reports:
            raise ValueError("Trajectory lacks its evaluator report")
        seen.add(key)
        checked.append(replay_session(locations[key[0]], key, entries, mode, reports.get(key), tolerances))
    for key in sorted(reports.keys() - seen):
        if reports[key].get("steps") != 0:
            raise ValueError("Evaluator report has missing trajectory rows")
        if key[0] not in locations:
            raise ValueError(f"Episode {key[0]} is absent from the dataset")
        checked.append(replay_session(locations[key[0]], key, (), mode, reports[key], tolerances))
    if not checked:
        raise ValueError("No recorded episodes to replay")
    return {"sessions": len(checked), "steps": sum(row["steps"] for row in checked), "reports": checked}
