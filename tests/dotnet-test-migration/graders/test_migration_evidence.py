import importlib.util
from pathlib import Path
import tempfile
import unittest


SPEC = importlib.util.spec_from_file_location(
    "migration_evidence", Path(__file__).with_name("migration_evidence.py")
)
evidence = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(evidence)


class EvidenceTests(unittest.TestCase):
    def test_source_contract_normalizes_only_required_framework_rewrites(self):
        root = Path(__file__).resolve().parents[1]
        staged = (root / "agent.test-migration/fixtures/mstest-v2-vstest/OrderServiceTests.cs").read_text()
        expected = evidence.invariant_source(staged, "staged-orders")
        rewritten = staged.replace("ThrowsException", "ThrowsExactly").replace("[DataTestMethod]", "[TestMethod]")
        self.assertEqual(expected, evidence.invariant_source("// harmless comment\n" + rewritten, "staged-orders"))
        for before, after in (
            ('Assert.AreEqual("item-1", order.ItemId);', ""),
            ('"item-1"', '"item-2"'),
            ("quantity <= 0", "quantity < 0"),
        ):
            self.assertNotEqual(expected, evidence.invariant_source(rewritten.replace(before, after), "staged-orders"))
        upgrade = (root / "migrate-mstest-v3-to-v4/fixtures/v3-verified-migration/OrderServiceTests.cs").read_text()
        migrated = upgrade.replace("ClassCleanupBehavior.EndOfClass", "").replace("[ClassCleanup()]", "[ClassCleanup]").replace(
            "TestTimeout.Infinite", "int.MaxValue"
        )
        self.assertEqual(evidence.invariant_source(upgrade, "mstest-upgrade"),
                         evidence.invariant_source(migrated, "mstest-upgrade"))
        self.assertEqual(evidence.invariant_source(upgrade, "mstest-upgrade"),
                         evidence.invariant_source(migrated.replace("[ClassCleanup]", "[ClassCleanup()]"),
                                                   "mstest-upgrade"))
        self.assertNotEqual(evidence.invariant_source(upgrade, "mstest-upgrade"),
                            evidence.invariant_source(migrated.replace(
                                "Assert.AreEqual(42, orderId)", "Assert.AreEqual(orderId, orderId)"
                            ), "mstest-upgrade"))

    def test_checkpoint_rejects_external_and_dynamic_references(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = root / "Test.csproj"
            for item in (
                '<Compile Include="../outside.cs" />',
                '<ProjectReference Include="../Other/Other.csproj" />',
                '<Compile Include="C:\\outside\\file.cs" />',
                '<Import Project="../external.props" />',
                '<Compile Include="$(ExternalSource)" />',
            ):
                project.write_text(f'<Project Sdk="Microsoft.NET.Sdk"><ItemGroup>{item}</ItemGroup></Project>')
                with self.subTest(item=item), self.assertRaises(AssertionError):
                    evidence.validate_checkpoint(root)
            project.write_text('<Project Sdk="Microsoft.NET.Sdk"><ItemGroup><Compile Include="src/Test.cs" /></ItemGroup></Project>')
            evidence.validate_checkpoint(root)

    def test_checkpoint_rejects_symlinked_sources(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "checkpoint"
            root.mkdir()
            external = Path(temporary) / "External.cs"
            external.write_text("class External {}")
            (root / "Linked.cs").symlink_to(external)
            with self.assertRaisesRegex(AssertionError, "Checkpoint symlink"):
                evidence.validate_checkpoint(root)

    def test_framework_and_adapter_are_checked_for_each_project(self):
        assets = {"MSTest.TestFramework/4.4.1": {}, "MSTest.TestAdapter/4.4.1": {}}
        evidence.verify_mstest_packages(assets, "Beta.Tests.csproj", "4", "4.4.1")
        for package in ("MSTest.TestFramework", "MSTest.TestAdapter"):
            changed = dict(assets)
            changed[package + "/4.5.1"] = changed.pop(package + "/4.4.1")
            with self.subTest(package=package), self.assertRaises(AssertionError):
                evidence.verify_mstest_packages(changed, "Beta.Tests.csproj", "4", "4.4.1")

    def test_equal_totals_do_not_hide_changed_case_set(self):
        with self.assertRaises(AssertionError):
            evidence.verify_cases(
                {"row-replacement": {"outcome": "Passed", "message": ""}},
                {"row-original": "Passed"}, {},
            )

    def test_skip_reason_is_not_just_a_skipped_count(self):
        with self.assertRaises(AssertionError):
            evidence.verify_cases(
                {"deferred": {"outcome": "NotExecuted", "message": "unrelated"}},
                {"deferred": "NotExecuted"}, {"deferred": "tracked reason"},
            )

    def test_missing_case_is_rejected(self):
        with self.assertRaises(AssertionError):
            evidence.verify_cases({}, {"required": "Passed"}, {})

    def test_known_complete_outcome_is_accepted(self):
        evidence.verify_cases(
            {"row": {"outcome": "Passed", "message": ""},
             "skip": {"outcome": "NotExecuted", "message": "tracked reason"}},
            {"row": "Passed", "skip": "NotExecuted"}, {"skip": "tracked reason"},
        )

    def test_preservation_covers_added_deleted_and_changed_siblings(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = root / "project.xml"
            second = root / "test.cs"
            first.write_bytes(b"version 2.2.10\n")
            second.write_bytes(b"original test\n")
            expected = {path.name: evidence.sha256(path) for path in (first, second)}
            evidence.preserve(root, expected)
            second.write_bytes(b"silently rewritten test\n")
            with self.assertRaises(AssertionError):
                evidence.preserve(root, expected)
            second.unlink()
            with self.assertRaises(AssertionError):
                evidence.preserve(root, expected)
            second.write_bytes(b"original test\n")
            (root / "new.cs").write_bytes(b"new file")
            with self.assertRaises(AssertionError):
                evidence.preserve(root, expected)

    def test_duplicate_id_and_inconsistent_counters_are_rejected(self):
        template = (
            '<TestRun xmlns="http://microsoft.com/schemas/VisualStudio/TeamTest/2010">'
            '<Results>{results}</Results><ResultSummary><Counters total="{total}"/>'
            '</ResultSummary></TestRun>'
        )
        result = '<UnitTestResult testName="row" outcome="Passed"/>'
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "result.trx"
            for results, total in ((result * 2, 2), (result, 2)):
                path.write_text(template.format(results=results, total=total), encoding="utf-8")
                with self.assertRaises(AssertionError):
                    evidence.read_trx(path)


if __name__ == "__main__":
    unittest.main()
