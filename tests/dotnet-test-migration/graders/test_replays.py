"""Real broken-workspace mutations for the scoped migration eval contracts."""

from pathlib import Path
import subprocess
import tempfile
import unittest

import yaml

from replay import materialize, run_graders


ROOT = Path(__file__).resolve().parents[1]


class ReplayTests(unittest.TestCase):
    def broken_golden(self, suite, name, filename, before, after, failure):
        spec = ROOT / suite / "eval.yaml"
        stimulus = next(case for case in yaml.safe_load(
            spec.read_text(encoding="utf-8")
        )["stimuli"] if case["name"] == name)
        with tempfile.TemporaryDirectory(prefix="migration-mutation-") as temporary:
            workspace = Path(temporary)
            materialize(spec, stimulus, workspace)
            path = workspace / filename
            content = path.read_text(encoding="utf-8")
            self.assertEqual(1, content.count(before), filename)
            path.write_text(content.replace(before, after), encoding="utf-8")
            with self.assertRaisesRegex(AssertionError, failure):
                run_graders(stimulus, workspace)

    def test_upgrade_cannot_drop_one_data_row(self):
        self.broken_golden(
            "migrate-mstest-v3-to-v4",
            "Verified MSTest v3 to v4 package update with build and test",
            "OrderServiceTests.cs",
            '    [DataRow(-2, 3, 1, DisplayName = "sum-negative")]\n', "",
            "sum-negative",
        )

    def test_native_solution_cannot_drop_a_module(self):
        self.broken_golden(
            "migrate-vstest-to-mtp",
            "Deliver native MTP migration with fresh reports from both projects",
            "Suite.slnx", '  <Project Path="Beta.Tests/Beta.Tests.csproj" />\n', "",
            "Expected 2 fresh reports",
        )

    def test_shared_fixture_must_be_disposed(self):
        self.broken_golden(
            "migrate-xunit-to-mstest",
            "Preserve an ordinary shared collection without a disable-parallelization flag",
            "CollectionTests.cs", "public static void Cleanup() => Resource.Dispose();",
            "public static void Cleanup() { }", "disposed",
        )

    def test_central_runner_properties_cannot_pollute_production(self):
        self.broken_golden(
            "migrate-vstest-to-mtp",
            "Deliver native MTP migration with fresh reports from both projects",
            "Directory.Build.props",
            ' Condition="$(MSBuildProjectName.EndsWith(\'.Tests\'))"', "",
            "OutputType",
        )

    def test_staged_delivery_cannot_drop_a_parameterized_case(self):
        self.broken_golden(
            "agent.test-migration",
            "Deliver staged MSTest v2 to v4 and native MTP migration",
            "OrderTests/OrderServiceTests.cs",
            '    [DataRow(-1, DisplayName = "invalid-negative")]\n', "",
            "invalid-negative",
        )

    def test_plan_cannot_edit_tests_while_retaining_old_package_string(self):
        self.broken_golden(
            "agent.test-migration",
            "Plan a staged MSTest v2 to v4 and MTP migration",
            "OrderTests/OrderServiceTests.cs", "Assert.AreEqual(2, order.Quantity);",
            "Assert.AreEqual(99, order.Quantity);", "expected",
        )

    def test_evaluator_cannot_be_replaced_with_a_success_script(self):
        self.broken_golden(
            "migrate-mstest-v3-to-v4",
            "Leave an already current MSTest project unchanged",
            ".eval/migration_evidence.py", '    main()\n',
            '    print("forged success")\n', "Evaluator code changed",
        )

    def test_zero_result_suppression_is_per_invocation(self):
        spec = ROOT / "migrate-vstest-to-mtp" / "eval.yaml"
        stimulus = next(case for case in yaml.safe_load(
            spec.read_text(encoding="utf-8")
        )["stimuli"] if case["name"].startswith("Deliver native MTP migration"))
        with tempfile.TemporaryDirectory(prefix="migration-empty-") as temporary:
            workspace = Path(temporary)
            materialize(spec, stimulus, workspace)
            import sys
            command = [
                sys.executable, "-B", str(ROOT / "graders" / "migration_evidence.py"),
                "run", "--project", "Alpha.Tests/Alpha.Tests.csproj",
                "--runner", "native", "--mstest-major", "4", "--allow-empty",
                "--runner-arg=--filter", "--runner-arg=FullyQualifiedName~NoSuchCase",
            ]
            for exit_code, extra in (
                (8, []),
                (0, ["--runner-arg=--ignore-exit-code", "--runner-arg=8"]),
                (8, []),
            ):
                result = subprocess.run(
                    command + ["--exit-code", str(exit_code)] + extra,
                    cwd=workspace, capture_output=True, text=True,
                    encoding="utf-8", timeout=180,
                )
                self.assertEqual(0, result.returncode, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
