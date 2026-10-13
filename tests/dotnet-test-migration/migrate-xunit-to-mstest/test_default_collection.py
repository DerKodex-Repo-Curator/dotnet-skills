"""Replay default-collection migration and reject broken scheduling/lifetime."""

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
    if item["name"] == "Preserve an ordinary shared collection without a disable-parallelization flag"
)
replay_spec = importlib.util.spec_from_file_location(
    "replay", SPEC.parent.parent / "graders" / "replay.py"
)
replay = importlib.util.module_from_spec(replay_spec)
replay_spec.loader.exec_module(replay)


class DefaultCollectionTests(unittest.TestCase):
    def workspace(self, parent):
        root = Path(tempfile.mkdtemp(prefix="default-collection-", dir=parent))
        replay.materialize(SPEC, CASE, root)
        return root

    def test_source_collection_has_no_disable_flag(self):
        source = SPEC.parent / "fixtures" / "default-collection" / "CollectionTests.cs"
        text = source.read_text(encoding="utf-8")
        self.assertIn('[CollectionDefinition("Shared")]', text)
        self.assertNotIn("DisableParallelization", text)

    def test_golden_and_broken_scheduling_and_disposal(self):
        with tempfile.TemporaryDirectory(prefix="collection-contract-") as parent:
            root = self.workspace(parent)
            replay.run_graders(CASE, root)
            source = root / "CollectionTests.cs"
            golden = source.read_text(encoding="utf-8")

            # Activating framework parallelism must not invalidate serialized members.
            parallel = golden.replace(
                "using Microsoft.VisualStudio.TestTools.UnitTesting;",
                "using Microsoft.VisualStudio.TestTools.UnitTesting;\n"
                "[assembly: Parallelize(Workers = 2, Scope = ExecutionScope.ClassLevel)]",
            )
            source.write_text(parallel, encoding="utf-8")
            replay.run_graders(CASE, root)

            source.write_text(parallel.replace("[DoNotParallelize]\n", "", 1), encoding="utf-8")
            replay.run_graders(CASE, root)

            resource_locks = parallel.replace(
                "[DoNotParallelize]", '[ResourceLock("Shared")]'
            )
            source.write_text(resource_locks, encoding="utf-8")
            replay.run_graders(CASE, root)

            source.write_text(resource_locks.replace(
                '[ResourceLock("Shared")]', '[ResourceLock("Other")]', 1
            ), encoding="utf-8")
            with self.assertRaisesRegex(AssertionError, "Collection scheduling contract"):
                replay.run_graders(CASE, root)

            source.write_text(parallel.replace("[DoNotParallelize]\n", ""), encoding="utf-8")
            with self.assertRaisesRegex(AssertionError, "Collection scheduling contract"):
                replay.run_graders(CASE, root)

            source.write_text(
                golden.replace(
                    "public static void Cleanup() => Resource.Dispose();",
                    "public static void Cleanup() { }",
                ), encoding="utf-8",
            )
            with self.assertRaises(AssertionError):
                replay.run_graders(CASE, root)

    def test_original_xunit_fixture_is_healthy_and_shares_one_resource(self):
        with tempfile.TemporaryDirectory(prefix="collection-baseline-") as parent:
            root = Path(parent)
            baseline = dict(CASE)
            baseline.pop("golden_patch")
            replay.materialize(SPEC, baseline, root)
            result = subprocess.run(
                [
                    sys.executable, ".eval/migration_evidence.py", "run",
                    "--project", "TestProject.csproj", "--runner", "vstest",
                    "--expect", "FirstMember.FirstCase=Passed",
                    "--expect", "SecondMember.SecondCase=Passed",
                ],
                cwd=root, capture_output=True, text=True, timeout=180,
            )
            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            events = [
                json.loads(line)
                for line in next(root.glob(".eval/results/*/lifecycle.jsonl")).read_text(
                    encoding="utf-8"
                ).splitlines()
            ]
            self.assertEqual(1, len({event["instance"] for event in events}))
            self.assertEqual(
                ["created", "entered", "exited", "entered", "exited", "disposed"],
                [event["event"] for event in events],
            )
            self.assertEqual(
                {"FirstCase", "SecondCase"},
                {event["case"] for event in events if event["event"] == "entered"},
            )

    def test_tradeoff_cannot_be_omitted_from_the_report(self):
        with tempfile.TemporaryDirectory(prefix="collection-report-") as parent:
            root = Path(parent)
            output_only = dict(CASE, graders=[
                grader for grader in CASE["graders"]
                if grader["type"].startswith("output-")
            ])
            replay.run_graders(output_only, root)
            replay.run_graders(
                output_only, root,
                "The shared resource is initialized once and disposed after all tests. "
                "Assembly lifecycle slightly widens its lifetime. MSTest's default "
                "scheduling also serializes unrelated classes, whereas xUnit permits "
                "unrelated collections to overlap.",
            )
            with self.assertRaises(AssertionError):
                replay.run_graders(
                    output_only, root, "Migrated and both tests pass with exact parity."
                )


if __name__ == "__main__":
    unittest.main()
