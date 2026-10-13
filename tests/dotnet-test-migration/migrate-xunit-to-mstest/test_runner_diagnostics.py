"""A runner warning must not become an enforced timeout after migration."""

import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import yaml


SPEC = Path(__file__).with_name("eval.yaml")
CASE = next(
    item for item in yaml.safe_load(SPEC.read_text(encoding="utf-8"))["stimuli"]
    if item["name"] == "Keep long-running runner diagnostics separate from enforced timeouts"
)
replay_spec = importlib.util.spec_from_file_location(
    "replay", SPEC.parent.parent / "graders" / "replay.py"
)
replay = importlib.util.module_from_spec(replay_spec)
replay_spec.loader.exec_module(replay)


class RunnerDiagnosticTests(unittest.TestCase):
    def test_source_setting_is_diagnostic_and_original_test_runs(self):
        settings = json.loads(
            (SPEC.parent / "fixtures" / "runner-diagnostics" / "xunit.runner.json").read_text()
        )
        self.assertEqual({"diagnosticMessages": True, "longRunningTestSeconds": 30}, settings)
        with tempfile.TemporaryDirectory(prefix="diagnostic-baseline-") as directory:
            root = Path(directory)
            baseline = dict(CASE)
            baseline.pop("golden_patch")
            replay.materialize(SPEC, baseline, root)
            result = subprocess.run(
                [
                    sys.executable, ".eval/migration_evidence.py", "run",
                    "--project", "TestProject.csproj", "--runner", "vstest",
                    "--expect", "DiagnosticsTests.CompletesNormally=Passed",
                ],
                cwd=root, capture_output=True, text=True, timeout=180,
            )
            self.assertEqual(0, result.returncode, result.stdout + result.stderr)

    def test_golden_passes_and_synthesized_timeout_is_rejected(self):
        with tempfile.TemporaryDirectory(prefix="diagnostic-golden-") as directory:
            root = Path(directory)
            replay.materialize(SPEC, CASE, root)
            replay.run_graders(CASE, root)
            source = root / "DiagnosticsTests.cs"
            golden = source.read_text(encoding="utf-8")
            source.write_text(
                golden.replace("[TestMethod]", "[TestMethod]\n    [Timeout(30000)]"),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(AssertionError, "Diagnostic timeout contract"):
                replay.run_graders(CASE, root)


if __name__ == "__main__":
    unittest.main()
