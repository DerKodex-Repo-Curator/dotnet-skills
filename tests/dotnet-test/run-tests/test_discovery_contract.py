"""Replay discovery answers and reject commands that execute tests or rebuild."""

import importlib.util
from itertools import product
from pathlib import Path
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[3]
EVAL = Path(__file__).with_name("eval.yaml")
SPEC = yaml.safe_load(EVAL.read_text(encoding="utf-8"))
STIMULUS = next(
    stimulus for stimulus in SPEC["stimuli"]
    if stimulus["name"] == "List existing solution tests across VSTest and both MTP modes"
)
GOLDEN = STIMULUS["golden_trajectory"]["inline"]["steps"][-1]["message"]
COMMANDS = [line for line in GOLDEN.splitlines() if line.startswith("dotnet test")]
checker_spec = importlib.util.spec_from_file_location(
    "eval_quality", ROOT / "eng" / "eval-quality" / "check_eval_quality.py"
)
checker = importlib.util.module_from_spec(checker_spec)
checker_spec.loader.exec_module(checker)


class DiscoveryContractTests(unittest.TestCase):
    def failed_graders(self, response):
        failed = []
        for grader in STIMULUS["graders"]:
            kind = grader["type"]
            if kind == "prompt":
                continue
            self.assertIn(kind, ("output-matches", "output-not-matches"))
            found = checker.vally_regex_found(grader["config"]["pattern"], response)
            if found != (kind == "output-matches"):
                failed.append(kind)
        return failed

    def test_golden_passes_every_deterministic_grader(self):
        self.assertEqual(3, len(COMMANDS))
        self.assertEqual([], self.failed_graders(GOLDEN))

    def test_valid_quoting_option_order_prompts_and_native_separator(self):
        for quote, prefix, reversed_options, native_separator in product(
            ("", "'", '"'), ("", "$ ", "> "), (False, True), (False, True)
        ):
            with self.subTest(
                quote=quote, prefix=prefix, reversed_options=reversed_options,
                native_separator=native_separator,
            ):
                vstest_options = (
                    "--list-tests --no-build" if reversed_options
                    else "--no-build --list-tests"
                )
                native_options = (
                    f"--no-build --solution {quote}Native.slnx{quote}"
                    if reversed_options
                    else f"--solution {quote}Native.slnx{quote} --no-build"
                )
                native_options += " -- --list-tests" if native_separator else " --list-tests"
                commands = [
                    f"dotnet test {quote}VSTest.slnx{quote} {vstest_options}",
                    f"dotnet test {quote}Bridge.slnx{quote} --no-build -- --list-tests",
                    f"dotnet test {native_options}",
                ]
                response = "\n".join(f"  {prefix}{command}" for command in commands)
                self.assertEqual([], self.failed_graders(response))

    def test_extra_execution_is_rejected_with_all_discovery_commands_present(self):
        for command, prefix, position in product(
            (
                "dotnet test Native.slnx --no-build",
                "dotnet test --solution Native.slnx --no-build",
                "dotnet test VSTest.slnx --no-build",
                "dotnet test Bridge.slnx --no-build",
                "dotnet test",
                "dotnet test Native.slnx --no-build --list-tests-extra",
                "dotnet test Native.slnx --no-build --list-tests=false",
                "dotnet test Native.slnx --no-build\r\n",
                "dotnet test Native.slnx --no-build\n--list-tests",
            ),
            ("", "  ", "$ ", "> ", "\t"),
            range(4),
        ):
            with self.subTest(command=command, prefix=prefix, position=position):
                commands = list(COMMANDS)
                commands.insert(position, prefix + command)
                failures = self.failed_graders("\n".join(commands))
                self.assertNotIn("output-matches", failures)
                self.assertIn("output-not-matches", failures)

    def test_existing_negative_guards_still_reject_extra_commands(self):
        for command in (
            "dotnet build Native.slnx",
            "dotnet run --project Tests.csproj -- --list-tests",
            "dotnet watch test",
            "dotnet test Bridge.slnx -- --no-build --list-tests",
            "dotnet test --solution Native.slnx -- --list-tests --no-build",
        ):
            with self.subTest(command=command):
                failures = self.failed_graders(GOLDEN + "\n" + command)
                self.assertNotIn("output-matches", failures)
                self.assertIn("output-not-matches", failures)

    def test_compound_commands_cannot_borrow_discovery_from_another_segment(self):
        execution = "dotnet test Native.slnx --no-build"
        discovery = "dotnet test --solution Native.slnx --no-build --list-tests"
        for separator, prefix, position, reversed_commands in product(
            ("; ", " && ", " || ", " | ", " & ", ";", "&&", "||"),
            ("", "  ", "$ ", "> ", "\t"),
            range(4),
            (False, True),
        ):
            with self.subTest(
                separator=separator, prefix=prefix, position=position,
                reversed_commands=reversed_commands,
            ):
                segments = [execution, discovery]
                if reversed_commands:
                    segments.reverse()
                commands = list(COMMANDS)
                commands.insert(position, prefix + separator.join(segments))
                failures = self.failed_graders("\n".join(commands))
                self.assertNotIn("output-matches", failures)
                self.assertIn("output-not-matches", failures)

    def test_missing_discovery_command_still_fails(self):
        for command in COMMANDS:
            with self.subTest(command=command):
                self.assertIn(
                    "output-matches", self.failed_graders(GOLDEN.replace(command, ""))
                )

    def test_inline_command_mention_is_not_execution(self):
        for mention in (
            "Do not use `dotnet test Native.slnx --no-build` to discover tests.",
            "Do not chain `dotnet test Native.slnx --no-build; "
            "dotnet test --solution Native.slnx --no-build --list-tests`.",
        ):
            with self.subTest(mention=mention):
                self.assertEqual([], self.failed_graders(GOLDEN + "\n" + mention))


if __name__ == "__main__":
    unittest.main()
