#!/usr/bin/env python3
"""Offline-only raw re-evaluation. Does not alter or regrade earlier experiments."""
import argparse
import json
from pathlib import Path

from order_rate import PLAN_VERSION, RUNS, SEED, assess, decide, read_json, schedule


def recompute(directory):
    plan = read_json(directory / "plan.json")
    runs = plan["runs_per_arm"]
    if (type(runs) is not int or not 1 <= runs <= RUNS or plan["plan_version"] != PLAN_VERSION
            or plan["seed"] != SEED or plan["order"] != [list(pair) for pair in schedule(runs)]):
        raise ValueError("plan does not match this pre-registered driver")
    execution = read_json(directory / "execution.json") if (directory / "execution.json").exists() else []
    rows = []
    for row in execution:
        number, arm = row["round"], row["arm"]
        if type(number) is not int or not 1 <= number <= runs or arm not in ("F", "P"):
            raise ValueError("invalid run identity")
        label = f"{arm}-{number:02d}"
        if row["label"] != label:
            raise ValueError("label mismatch")
        child = read_json(directory / (label + ".child.json"))
        if child != row["child"] or row["returncode"] != child["exit_code"]:
            raise ValueError("conflicting child lifecycle evidence")
        rows.append({"round": number, "arm": arm, **assess(directory / (label + ".json"), arm, child)})
    aborted = read_json(directory / "abort.json")["reason"] if (directory / "abort.json").exists() else None
    result = decide(rows, runs, aborted)
    result.update(smoke=runs != RUNS, aborted=aborted)
    return {"summary": result, "matches_remote": result == read_json(directory / "summary.json"),
            "validity": [{"round": row["round"], "arm": row["arm"], **row["validity"]} for row in rows]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = recompute(args.directory)
    with args.output.open("x") as stream:
        stream.write(json.dumps(result, indent=2, sort_keys=True) + "\n")
    print(json.dumps({"verdict": result["summary"]["verdict"], "matches_remote": result["matches_remote"]}))
    return 0 if result["matches_remote"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
