"""Real broken-workspace mutations for the scoped migration eval contracts."""

from pathlib import Path
import copy
import re
import subprocess
import shutil
import tempfile
import unittest

import yaml

from replay import materialize, run_graders


ROOT = Path(__file__).resolve().parents[1]


class ReplayTests(unittest.TestCase):
    def compiler_mutation(self, suite, prefix, project, source, before, after,
                          mutation="replacement", baseline=False):
        spec = ROOT / suite / "eval.yaml"
        stimulus = next(case for case in yaml.safe_load(
            spec.read_text(encoding="utf-8")
        )["stimuli"] if case["name"].startswith(prefix))
        with tempfile.TemporaryDirectory(prefix="compiler-mutation-") as temporary:
            workspace = Path(temporary)
            materialize(spec, stimulus, workspace)
            path = workspace / source
            original = path.read_bytes()
            replacement = path.parent / "Replacement.cs"
            if mutation != "missing":
                text = path.read_text(encoding="utf-8")
                self.assertIn(before, text)
                replacement.write_text(
                    "internal class ExtraFixtureSource {}" if mutation == "extra"
                    else text.replace(before, after), encoding="utf-8"
                )
            project_path = workspace / project
            checkpoint = project_path.parents[1] if "checkpoints/" in project else workspace
            condition = f"'$(MSBuildProjectName)' == '{project_path.stem}'"
            remove = f'<Compile Remove="{path.name}" />'
            if mutation in {"target-time", "restored"}:
                items = (
                    '<ItemGroup><Compile Remove="Replacement.cs" /></ItemGroup>'
                    f'<Target Name="ReplaceFixture" BeforeTargets="CoreCompile" Condition="{condition}">'
                    f'<ItemGroup>{remove}<Compile Include="Replacement.cs" /></ItemGroup></Target>'
                )
                if mutation == "restored":
                    items += (
                        f'<Target Name="RestoreFixtureItems" AfterTargets="CoreCompile" Condition="{condition}">'
                        '<ItemGroup><Compile Remove="Replacement.cs" />'
                        f'<Compile Include="{path.name}" /></ItemGroup></Target>'
                    )
            else:
                items = f'<ItemGroup Condition="{condition}">{remove if mutation != "extra" else ""}</ItemGroup>'
            (checkpoint / "Directory.Build.targets").write_text(
                f"<Project>{items}</Project>", encoding="utf-8"
            )
            if baseline:
                without_contract = copy.deepcopy(stimulus)
                for grader in without_contract["graders"]:
                    if grader["type"] == "run-command":
                        grader["config"]["command"] = re.sub(
                            r" --compile-source \S+", "", grader["config"]["command"]
                        )
                run_graders(without_contract, workspace)
                self.assertEqual(original, path.read_bytes())
            with self.assertRaisesRegex(AssertionError, "Compiler input contract") as rejected:
                run_graders(stimulus, workspace)
            diagnostic = rejected.exception.args[0][-1]
            if mutation == "restored":
                self.assertIn("disagrees with actual Csc inputs", diagnostic)
                self.assertIn("Replacement.cs", diagnostic)
            else:
                if mutation != "extra":
                    self.assertRegex(diagnostic, r"'missing': \[[^\]]*" + re.escape(path.name))
                if mutation != "missing":
                    self.assertRegex(diagnostic, r"'uncontracted': \[[^\]]*Replacement\.cs")
            self.assertEqual(original, path.read_bytes())

    def test_each_staged_compilation_requires_the_authenticated_source(self):
        for stage in ("checkpoints/v3/", "checkpoints/v4/", ""):
            with self.subTest(stage=stage or "final"):
                self.compiler_mutation(
                    "agent.test-migration", "Deliver staged",
                    stage + "OrderTests/OrderTests.csproj",
                    stage + "OrderTests/OrderServiceTests.cs",
                    'Assert.AreEqual("item-1", order.ItemId)', 'Assert.AreEqual("item-1", "item-1")',
                )

    def test_upgrade_rejects_excluded_missing_extra_and_target_time_sources(self):
        for mutation in ("replacement", "missing", "extra", "target-time", "restored"):
            with self.subTest(mutation=mutation):
                self.compiler_mutation(
                    "migrate-mstest-v3-to-v4", "Verified MSTest",
                    "TestProject.csproj", "OrderServiceTests.cs",
                    "Assert.AreEqual(42, orderId)", "Assert.AreEqual(orderId, orderId)",
                    mutation=mutation,
                )

    def test_native_and_bridge_reject_replacements_in_every_project(self):
        for prefix in ("Deliver native", "Keep the explicit"):
            for project, source, before, after in (
                ("Alpha.Tests/Alpha.Tests.csproj", "Alpha.Tests/AdditionTests.cs",
                 "Assert.AreEqual(expected, Arithmetic.Add(left, right))",
                 "Assert.AreEqual(expected, expected)"),
                ("Beta.Tests/Beta.Tests.csproj", "Beta.Tests/BoundaryTests.cs",
                 "Assert.AreEqual(0, Arithmetic.Add(0, 0))", "Assert.IsTrue(true)"),
                ("Library/Library.csproj", "Library/Arithmetic.cs",
                 "checked(left + right)", "left + right"),
            ):
                with self.subTest(runner=prefix, project=project):
                    self.compiler_mutation(
                        "migrate-vstest-to-mtp", prefix, project, source, before, after,
                        baseline=(prefix == "Deliver native" and project.startswith("Alpha.")
                                  or prefix == "Keep the explicit" and project.startswith("Beta.")),
                    )

    def test_native_and_bridge_reject_missing_and_extra_sources(self):
        for prefix in ("Deliver native", "Keep the explicit"):
            for mutation in ("missing", "extra", "target-time"):
                with self.subTest(runner=prefix, mutation=mutation):
                    self.compiler_mutation(
                        "migrate-vstest-to-mtp", prefix,
                        "Alpha.Tests/Alpha.Tests.csproj", "Alpha.Tests/AdditionTests.cs",
                        "Assert.AreEqual(expected, Arithmetic.Add(left, right))",
                        "Assert.AreEqual(expected, expected)", mutation=mutation,
                    )

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
            "Source invariant changed",
        )

    def test_upgrade_cannot_turn_an_assertion_into_a_tautology(self):
        self.broken_golden(
            "migrate-mstest-v3-to-v4",
            "Verified MSTest v3 to v4 package update with build and test",
            "OrderServiceTests.cs", "Assert.AreEqual(42, orderId)",
            "Assert.AreEqual(orderId, orderId)", "Source invariant changed",
        )

    def test_native_solution_cannot_drop_a_module(self):
        self.broken_golden(
            "migrate-vstest-to-mtp",
            "Deliver native MTP migration with fresh reports from both projects",
            "Suite.slnx", '  <Project Path="Beta.Tests/Beta.Tests.csproj" />\n', "",
            "Expected 2 fresh reports",
        )

    def test_secondary_project_cannot_change_package_versions(self):
        self.broken_golden(
            "migrate-vstest-to-mtp",
            "Deliver native MTP migration with fresh reports from both projects",
            "Beta.Tests/Beta.Tests.csproj",
            'Include="MSTest.TestFramework" Version="4.4.1"',
            'Include="MSTest.TestFramework" Version="4.5.1"',
            "Beta.Tests.*MSTest.TestFramework",
        )

    def test_secondary_project_cannot_stay_on_vstest_in_bridge_solution(self):
        self.broken_golden(
            "migrate-vstest-to-mtp",
            "Keep the explicit SDK 10 compatibility bridge while enabling MTP",
            "Beta.Tests/Beta.Tests.csproj",
            '<Project Sdk="Microsoft.NET.Sdk">',
            '<Project Sdk="Microsoft.NET.Sdk">\n<PropertyGroup>'
            '<EnableMSTestRunner>false</EnableMSTestRunner>'
            '<TestingPlatformDotnetTestSupport>false</TestingPlatformDotnetTestSupport>'
            '</PropertyGroup>',
            "EnableMSTestRunner",
        )

    def test_platform_migration_cannot_weaken_test_assertions(self):
        for name in (
            "Deliver native MTP migration with fresh reports from both projects",
            "Keep the explicit SDK 10 compatibility bridge while enabling MTP",
        ):
            for filename, before, after in (
                ("Alpha.Tests/AdditionTests.cs",
                 "Assert.AreEqual(expected, Arithmetic.Add(left, right))",
                 "Assert.AreEqual(expected, expected)"),
                ("Beta.Tests/BoundaryTests.cs",
                 "Assert.AreEqual(0, Arithmetic.Add(0, 0))",
                 "Assert.IsTrue(true)"),
            ):
                with self.subTest(case=name, file=filename):
                    self.broken_golden("migrate-vstest-to-mtp", name, filename,
                                       before, after, "Preserved fixture changed")

    def test_shared_fixture_must_be_disposed(self):
        self.broken_golden(
            "migrate-xunit-to-mstest",
            "Preserve an ordinary shared collection without a disable-parallelization flag",
            "CollectionTests.cs", "            resource.Value.Dispose();",
            "            _ = resource.Value;", "disposed",
        )

    def test_central_runner_properties_cannot_pollute_production(self):
        self.broken_golden(
            "migrate-vstest-to-mtp",
            "Deliver native MTP migration with fresh reports from both projects",
            "Directory.Build.props",
            ' Condition="$(MSBuildProjectName.EndsWith(\'.Tests\'))"', "",
            "OutputType",
        )

    def test_platform_migration_preserves_production_nullable_configuration(self):
        for name in (
            "Deliver native MTP migration with fresh reports from both projects",
            "Keep the explicit SDK 10 compatibility bridge while enabling MTP",
        ):
            for replacement in ("", "<Nullable>disable</Nullable>"):
                with self.subTest(case=name, replacement=replacement):
                    self.broken_golden(
                        "migrate-vstest-to-mtp", name, "Directory.Build.props",
                        "<Nullable>enable</Nullable>", replacement, "Nullable",
                    )

    def test_execution_cannot_rewrite_preserved_production_or_test_sources(self):
        for name in (
            "Deliver native MTP migration with fresh reports from both projects",
            "Keep the explicit SDK 10 compatibility bridge while enabling MTP",
        ):
            for filename in ("../Library/Arithmetic.cs", "AdditionTests.cs"):
                with self.subTest(case=name, file=filename):
                    self.broken_golden(
                        "migrate-vstest-to-mtp", name, "Alpha.Tests/Alpha.Tests.csproj",
                        "</Project>",
                        '<Target Name="RewritePreservedSource" AfterTargets="CoreCompile">'
                        f'<WriteLinesToFile File="$(MSBuildProjectDirectory)/{filename}" '
                        'Lines="// overwritten after compilation" Overwrite="true" />'
                        '</Target></Project>',
                        "Preserved fixture changed",
                    )

    def test_staged_delivery_cannot_drop_a_parameterized_case(self):
        self.broken_golden(
            "agent.test-migration",
            "Deliver staged MSTest v2 to v4 and native MTP migration",
            "OrderTests/OrderServiceTests.cs",
            '    [DataRow(-1, DisplayName = "invalid-negative")]\n', "",
            "Source invariant changed",
        )

    def test_every_stage_preserves_assertions_not_only_case_names(self):
        for prefix in ("checkpoints/v3/", "checkpoints/v4/", ""):
            with self.subTest(stage=prefix or "final"):
                self.broken_golden(
                    "agent.test-migration",
                    "Deliver staged MSTest v2 to v4 and native MTP migration",
                    prefix + "OrderTests/OrderServiceTests.cs",
                    '        Assert.AreEqual("item-1", order.ItemId);\n', "",
                    "Source invariant changed",
                )

    def test_checkpoint_cannot_link_final_workspace_source(self):
        self.broken_golden(
            "agent.test-migration",
            "Deliver staged MSTest v2 to v4 and native MTP migration",
            "checkpoints/v3/OrderTests/OrderTests.csproj",
            "</Project>",
            '<ItemGroup><Compile Remove="OrderServiceTests.cs" />'
            '<Compile Include="../../../OrderTests/OrderServiceTests.cs" /></ItemGroup></Project>',
            "External checkpoint reference",
        )

    def test_final_only_workspace_cannot_claim_validated_checkpoints(self):
        spec = ROOT / "agent.test-migration" / "eval.yaml"
        stimulus = next(case for case in yaml.safe_load(
            spec.read_text(encoding="utf-8")
        )["stimuli"] if case["name"] == "Deliver staged MSTest v2 to v4 and native MTP migration")
        with tempfile.TemporaryDirectory(prefix="missing-migration-checkpoint-") as temporary:
            workspace = Path(temporary)
            materialize(spec, stimulus, workspace)
            shutil.rmtree(workspace / "checkpoints")
            with self.assertRaisesRegex(AssertionError, "Missing replayable migration checkpoint"):
                run_graders(stimulus, workspace)

    def test_each_intermediate_checkpoint_must_preserve_parameterized_cases(self):
        for directory in ("v3", "v4"):
            with self.subTest(stage=directory):
                self.broken_golden(
                    "agent.test-migration",
                    "Deliver staged MSTest v2 to v4 and native MTP migration",
                    f"checkpoints/{directory}/OrderTests/OrderServiceTests.cs",
                    '    [DataRow(-1, DisplayName = "invalid-negative")]\n', "",
                    "Source invariant changed",
                )

    def test_v3_checkpoint_cannot_be_a_copy_of_final_native_output(self):
        spec = ROOT / "agent.test-migration" / "eval.yaml"
        stimulus = next(case for case in yaml.safe_load(
            spec.read_text(encoding="utf-8")
        )["stimuli"] if case["name"] == "Deliver staged MSTest v2 to v4 and native MTP migration")
        with tempfile.TemporaryDirectory(prefix="wrong-migration-checkpoint-") as temporary:
            workspace = Path(temporary)
            materialize(spec, stimulus, workspace)
            target = workspace / "checkpoints" / "v3"
            shutil.copytree(workspace / "OrderTests", target / "OrderTests", dirs_exist_ok=True)
            shutil.copyfile(workspace / "global.json", target / "global.json")
            with self.assertRaises(AssertionError):
                run_graders(stimulus, workspace)

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
