from pathlib import Path
import tempfile
import unittest

from operation_contract import check_files, digest


class ScopeTests(unittest.TestCase):
    def test_standard_pytest_cache_is_not_a_source_edit(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "limit.py"
            source.write_bytes(b"unchanged")
            expected = {"limit.py": digest(source.read_bytes())}
            cache = root / ".pytest_cache" / "v" / "cache"
            cache.mkdir(parents=True)
            (cache / "nodeids").write_text("[]")
            check_files(root, expected, set(), python_tests=True)
            (cache / "hidden.py").write_text("unauthorized source")
            with self.assertRaises(AssertionError):
                check_files(root, expected, set(), python_tests=True)

    def test_new_test_does_not_authorize_existing_test_or_product_edits(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for name in ("limit.py", "test_existing.py"):
                (root / name).write_bytes(b"original")
            expected = {name: digest((root / name).read_bytes())
                        for name in ("limit.py", "test_existing.py")}
            (root / "test_boundary.py").write_text("new test")
            check_files(root, expected, set(), python_tests=True)
            for name in expected:
                with self.subTest(path=name):
                    (root / name).write_bytes(b"changed")
                    with self.assertRaises(AssertionError):
                        check_files(root, expected, set(), python_tests=True)
                    (root / name).write_bytes(b"original")


if __name__ == "__main__":
    unittest.main()
