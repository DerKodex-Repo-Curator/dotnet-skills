"""Calibrate complete xUnit removal and diagnostic deadline configuration."""

from pathlib import Path
import tempfile
import unittest

import yaml

import replay


SPEC = Path(__file__).resolve().parent.parent / "migrate-xunit-to-mstest" / "eval.yaml"
NAMES = (
    "Keep long-running runner diagnostics separate from enforced timeouts",
    "Preserve an ordinary shared collection without a disable-parallelization flag",
)
CASES = [
    case for case in yaml.safe_load(SPEC.read_text(encoding="utf-8"))["stimuli"]
    if case["name"] in NAMES
]


def check(case, root):
    graders = [
        grader for grader in case["graders"]
        if "migration_cleanliness.py" in grader.get("config", {}).get("command", "")
    ]
    assert len(graders) == 1
    replay.run_graders(dict(case, graders=graders), root)


class MigrationCleanlinessTests(unittest.TestCase):
    def test_both_goldens_reject_retained_packages(self):
        self.assertEqual(2, len(CASES))
        for case in CASES:
            with self.subTest(case=case["name"]), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                replay.materialize(SPEC, case, root)
                check(case, root)
                project = root / "TestProject.csproj"
                original = project.read_text(encoding="utf-8")
                for package, version in (
                    ("xunit", "2.9.3"), ("xunit.runner.visualstudio", "2.8.2"), ("xunit.v3", "3.0.0")
                ):
                    with self.subTest(package=package):
                        project.write_text(original.replace(
                            "</Project>",
                            f'<ItemGroup><PackageReference Include="{package}" Version="{version}" />'
                            "</ItemGroup></Project>",
                        ), encoding="utf-8")
                        with self.assertRaisesRegex(AssertionError, "Retained xUnit package"):
                            check(case, root)
                project.write_text(original.replace(
                    "</Project>", '<ItemGroup><PackageReference Include="xunit" Version="2.9.3" />'
                    '<PackageReference Include="xunit.runner.visualstudio" Version="2.8.2" />'
                    "</ItemGroup></Project>",
                ), encoding="utf-8")
                runtime_only = dict(case, graders=[
                    grader for grader in case["graders"]
                    if grader["type"] in {"program", "run-command"} and
                    "migration_cleanliness.py" not in grader["config"].get("command", "")
                ])
                replay.run_graders(runtime_only, root)
                with self.assertRaisesRegex(AssertionError, "Retained xUnit package"):
                    check(case, root)
                project.write_text(original, encoding="utf-8")
                props = root / "Directory.Packages.props"
                props.write_text(
                    '<Project><ItemGroup><PackageVersion Include="xunit" Version="2.9.3" />'
                    '</ItemGroup></Project>', encoding="utf-8",
                )
                with self.assertRaisesRegex(AssertionError, "Retained xUnit package"):
                    check(case, root)

    def test_both_goldens_reject_using_forms_and_obsolete_runner_json(self):
        for case in CASES:
            with self.subTest(case=case["name"]), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                replay.materialize(SPEC, case, root)
                extra = root / "Imports.cs"
                for directive in (
                    "using Xunit;", "global using Xunit;",
                    "using X = global::Xunit;", "using static Xunit.Assert;",
                    "using\nXunit;", "using/* old namespace */Xunit;",
                ):
                    with self.subTest(directive=directive):
                        extra.write_text(directive, encoding="utf-8")
                        with self.assertRaisesRegex(AssertionError, "Retained xUnit using"):
                            check(case, root)
                extra.write_text(
                    '// Previously: using Xunit;\nclass Notes { const string Old = "using Xunit;"; }\n',
                    encoding="utf-8",
                )
                check(case, root)
                (root / "xunit.runner.json").write_text("{}", encoding="utf-8")
                with self.assertRaisesRegex(AssertionError, "Obsolete xUnit runner config"):
                    check(case, root)

    def test_msbuild_using_items_are_also_imports(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            replay.materialize(SPEC, CASES[0], root)
            (root / "Directory.Build.props").write_text(
                '<Project><ItemGroup><Using Include="Xunit" /></ItemGroup></Project>',
                encoding="utf-8",
            )
            with self.assertRaisesRegex(AssertionError, "Retained xUnit import"):
                check(CASES[0], root)

    def test_configured_runsettings_cannot_turn_warning_into_deadline(self):
        case = next(case for case in CASES if case["name"] == NAMES[0])
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            replay.materialize(SPEC, case, root)
            project = root / "TestProject.csproj"
            project.write_text(project.read_text(encoding="utf-8").replace(
                "</Project>", "<PropertyGroup>"
                "<RunSettingsFilePath>$(MSBuildProjectDirectory)/timeouts.runsettings</RunSettingsFilePath>"
                "</PropertyGroup></Project>",
            ), encoding="utf-8")
            for container, name in (("MSTest", "TestTimeout"), ("RunConfiguration", "TestSessionTimeout")):
                with self.subTest(name=name):
                    settings = root / "timeouts.runsettings"
                    settings.write_text(
                        f"<RunSettings><{container}><{name}>30000</{name}>"
                        f"</{container}></RunSettings>", encoding="utf-8",
                    )
                    with self.assertRaisesRegex(AssertionError, "Diagnostic warning became enforced"):
                        check(case, root)
                    settings.write_text(settings.read_text().replace("30000", "0"), encoding="utf-8")
                    check(case, root)


if __name__ == "__main__":
    unittest.main()
