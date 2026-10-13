"""Correlate the executed fixture's scheduling policy with the final report."""

import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys


def verified_bytes(path, expected):
    data = path.read_bytes()
    assert hashlib.sha256(data.replace(b"\r\n", b"\n")).hexdigest() == expected, (
        f"Evaluator resource changed: {path}"
    )
    return data


def validate_report(policy, response):
    assert policy in {"assembly-serial", "deferred-members", "shared-resource"}, policy
    assert isinstance(response, str) and response.strip(), "Missing final report"
    normalized = re.sub(r"[`*_]", "", response).replace("\u2019", "'")
    normalized = re.sub(r"\s+", " ", normalized)
    clauses = [
        clause.strip()
        for clause in re.split(r"[.!?;\n]+|\b(?:whereas|unlike)\b", normalized, flags=re.I)
        if clause.strip()
    ]
    target_clauses = [
        clause for clause in clauses
        if not re.match(r"^(?:in\s+)?xunit\b", clause, re.I)
    ]
    subject = r"\b(?:unrelated|other)\s+(?:(?:test\s+)?(?:classes|tests)|collections)\b"
    parallel = r"\b(?:parallel|concurrent(?:ly)?|overlap)\b"
    restrictive = [
        subject + r".{0,80}\b(?:cannot|can't|do not|don't|no longer)\b.{0,60}" + parallel,
        subject + r"(?:(?!\b(?:not|never)\b).){0,80}\b(?:serial(?:ized|ly)?|sequential(?:ly)?|nonparallel|blocked)\b",
        r"(?<!not )(?<!n't )(?<!never )\b(?:serializes?|serializing|blocks?)\b.{0,80}" + subject,
    ]
    permissive = [
        subject + r".{0,80}\b(?:can|may|remain|continue|allowed)\b.{0,60}" + parallel,
        r"\b(?:allows?|permits?)\b.{0,60}" + subject + r".{0,60}" + parallel,
        r"\b(?:does not|doesn't)\s+(?:serialize|block)\b.{0,60}" + subject,
        subject + r".{0,60}\b(?:not|never)\s+(?:serialized|blocked)\b",
    ]
    restricted = any(re.search(pattern, clause, re.I) for clause in target_clauses
                     for pattern in restrictive)
    permitted = any(re.search(pattern, clause, re.I) for clause in target_clauses
                    for pattern in permissive)
    if policy == "shared-resource":
        assert permitted and not restricted, (
            "Scheduling disclosure contradicts shared-resource policy: "
            "unrelated classes remain eligible for parallel execution"
        )
    else:
        assert restricted and not permitted, (
            f"Scheduling disclosure contradicts {policy}: "
            "unrelated tests cannot freely overlap these serialized members"
        )


def main():
    workspace = Path(os.environ["EVALUATE_WORKSPACE"]).resolve()
    grading = json.loads(Path(os.environ["EVALUATE_GRADER_INPUT"]).read_text(encoding="utf-8"))
    guard = verified_bytes(
        workspace / ".eval" / "CollectionSchedulingContract.txt",
        "a1d32b2ef667a7abb236d3c91884a7b8e13121d656618eab0ea6c446c01d87a7",
    )
    verified_bytes(
        workspace / ".eval" / "migration_evidence.py",
        "76b8749c16c1e220c476899af6978d50ac13de496b2c1fa908142ecae4f46446",
    )
    (workspace / "CollectionSchedulingContract.cs").write_bytes(guard)
    result = subprocess.run(
        [
            sys.executable, ".eval/migration_evidence.py", "run",
            "--project", "TestProject.csproj", "--runner", "vstest",
            "--mstest-major", "4", "--lifecycle",
            "--expect", "FirstCase=Passed", "--expect", "SecondCase=Passed",
            "--unchanged-file",
            "bbadd2d5430f817f830d9a34899bc1bc9d99aae2dcd20815a4d1615b28c2f7b5:LifecycleProbe.cs",
        ],
        cwd=workspace, capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=210,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    evidence = json.loads(result.stdout)
    reports = list(evidence["reports"])
    assert len(reports) == 1, reports
    policy_path = Path(reports[0]).parent / "lifecycle.scheduling.json"
    policy = policy_path.read_text(encoding="utf-8")
    validate_report(policy, grading["trajectory"]["output"])
    print(json.dumps({
        "name": "collection-policy-report", "kind": "code",
        "passed": True, "score": 1,
        "evidence": f"Runtime lifecycle and report match scheduling policy: {policy}",
    }))


if __name__ == "__main__":
    main()
