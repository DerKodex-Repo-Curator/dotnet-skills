"""Replay selected migration goldens through their executable eval graders."""

import argparse
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

import yaml


def materialize(spec, stimulus, workspace):
    for item in stimulus.get("environment", {}).get("files", []):
        source = (spec.parent / item["src"]).resolve()
        target = workspace / item["dest"]
        if source.is_dir():
            shutil.copytree(source, target, dirs_exist_ok=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
    # Match the quality gate's portable text-fixture/golden materialization.
    for path in workspace.rglob("*"):
        if path.is_file():
            path.write_bytes(path.read_bytes().replace(b"\r\n", b"\n"))
    if "golden_patch" in stimulus:
        patch = spec.parent / stimulus["golden_patch"]["path"]
        subprocess.run(
            ["git", "apply", "--whitespace=nowarn", "-"], cwd=workspace,
            input=patch.read_bytes().replace(b"\r\n", b"\n"), check=True,
            env=dict(os.environ, GIT_CEILING_DIRECTORIES=str(workspace.parent.resolve())),
        )


def run_graders(stimulus, workspace, response=None):
    if response is None:
        steps = stimulus.get("golden_trajectory", {}).get("inline", {}).get("steps", [])
        response = "\n".join(step.get("message", "") for step in steps)
    for grader in stimulus["graders"]:
        kind = grader["type"]
        config = grader.get("config", {})
        if kind == "prompt":
            continue
        if kind == "exit-success":
            assert response.strip(), "Missing reference response"
            continue
        if kind.startswith("output-"):
            assert response, "Output grader has no response to grade"
            if "matches" in kind:
                matched = bool(re.search(config["pattern"], response, re.I))
            else:
                matched = config["substring"] in response
            assert matched == ("not" not in kind), (kind, config, response)
            continue
        if kind.startswith("file-"):
            paths = [path for path in workspace.glob(config["path"]) if path.is_file()]
            if "contains" in kind:
                assert paths, config
                matched = any(config["value"] in path.read_text(encoding="utf-8") for path in paths)
            else:
                matched = bool(paths)
            assert matched == ("not" not in kind), (kind, config)
            continue
        if kind == "program":
            with tempfile.TemporaryDirectory(prefix="grader-input-", dir=workspace) as directory:
                input_path = Path(directory) / "input.json"
                input_path.write_text(json.dumps({
                    "trajectory": {"workDir": str(workspace), "output": response},
                }), encoding="utf-8")
                result = subprocess.run(
                    [config["program"], *config.get("args", [])],
                    cwd=workspace, capture_output=True, text=True,
                    encoding="utf-8", errors="replace", timeout=360,
                    env=dict(os.environ, EVALUATE_WORKSPACE=str(workspace),
                             EVALUATE_GRADER_INPUT=str(input_path)),
                )
                assert result.returncode == 0, (result.stdout, result.stderr)
                assert result.stdout.strip(), "Program grader produced no verdict"
                verdict = json.loads(result.stdout)
                assert verdict["passed"], verdict
            continue
        assert kind == "run-command", f"Unsupported deterministic grader: {kind}"
        result = subprocess.run(
            config["command"], cwd=workspace, shell=True, capture_output=True,
            text=True, encoding="utf-8", errors="replace", timeout=360,
        )
        if result.returncode != config.get("expected_exit_code", 0):
            raise AssertionError((config["command"], result.stdout, result.stderr))
        if "stdout_matches" in config:
            assert re.search(config["stdout_matches"], result.stdout, re.I), result.stdout
        if "stdout_contains" in config:
            assert config["stdout_contains"] in result.stdout, result.stdout
        print(result.stdout.strip())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("spec", type=Path)
    parser.add_argument("--name", required=True, action="append")
    parser.add_argument("--results-dir", required=True, type=Path)
    args = parser.parse_args()
    spec = args.spec.resolve()
    doc = yaml.safe_load(spec.read_text(encoding="utf-8"))
    selected = [case for case in doc["stimuli"] if case["name"] in args.name]
    assert len(selected) == len(args.name), "Missing or duplicate requested stimulus"
    args.results_dir.mkdir(parents=True, exist_ok=True)
    for case in selected:
        workspace = Path(tempfile.mkdtemp(prefix="golden-", dir=args.results_dir.resolve()))
        materialize(spec, case, workspace)
        run_graders(case, workspace)
        print(f"GOLDEN PASSED: {case['name']} ({workspace})")


if __name__ == "__main__":
    main()
