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
