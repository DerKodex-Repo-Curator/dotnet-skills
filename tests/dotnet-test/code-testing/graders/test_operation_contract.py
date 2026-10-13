from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from operation_contract import check_files, digest, main, rust_runtime_bytes


class ScopeTests(unittest.TestCase):
    def test_cli_rejects_appended_runtime_item(self):
        fixture = Path(__file__).resolve().parents[1] / "fixtures" / "rust-inline"
        original = (fixture / "src" / "lib.rs").read_bytes()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "src").mkdir()
            (root / "Cargo.toml").write_bytes((fixture / "Cargo.toml").read_bytes())
            (root / "src" / "lib.rs").write_bytes(original + b"\npub fn unauthorized() {}\n")
            result = subprocess.run([
                sys.executable, "-B", str(Path(__file__).with_name("operation_contract.py")),
                "rust", "--root", str(root),
                "--file", "Cargo.toml=" + digest((root / "Cargo.toml").read_bytes()),
                "--file", "src/lib.rs=" + digest(original),
                "--runtime-sha256", digest(rust_runtime_bytes(original)),
            ], capture_output=True, text=True, timeout=30)
            self.assertNotEqual(0, result.returncode)
            self.assertIn("Runtime Rust region changed", result.stderr)

    def test_test_execution_cannot_leave_runtime_edits(self):
        fixture = Path(__file__).resolve().parents[1] / "fixtures" / "rust-inline"
        original = (fixture / "src" / "lib.rs").read_bytes()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "src").mkdir()
            (root / "Cargo.toml").write_bytes((fixture / "Cargo.toml").read_bytes())
            source = root / "src" / "lib.rs"
            source.write_bytes(original)

            def run_tests(*args):
                source.write_bytes(original + b"\npub fn unauthorized() {}\n")
                return ("test tests::free_shipping_at_threshold ... ok\n"
                        "test result: ok. 2 passed; 0 failed")

            with patch.object(sys, "argv", [
                "operation_contract.py", "rust", "--root", str(root),
                "--file", "Cargo.toml=" + digest((root / "Cargo.toml").read_bytes()),
                "--file", "src/lib.rs=" + digest(original),
                "--runtime-sha256", digest(rust_runtime_bytes(original)),
            ]), patch("operation_contract.command", side_effect=run_tests), patch(
                "operation_contract.check_mutation",
            ):
                with self.assertRaisesRegex(AssertionError, "Runtime Rust region changed"):
                    main()

    def test_rust_test_body_is_the_only_mutable_region(self):
        original = b'pub fn before() {}\n\n#[cfg(test)]\nmod tests {\n    #[test] fn check() {}\n}\n\npub fn after() {}\n'
        expected = digest(rust_runtime_bytes(original))
        replacement = b'''    #[test] fn check() {
        let a = "}";
        let b = r###" } /* "###;
        let c = '}';
        // } is not the end of this module
        /* nested /* } */ comment */
        assert_eq!(a, "}");
    }'''
        changed_test = original.replace(b"    #[test] fn check() {}", replacement)
        self.assertEqual(expected, digest(rust_runtime_bytes(changed_test)))
        self.assertEqual(expected, digest(rust_runtime_bytes(
            original.replace(b"#[cfg(test)]\nmod tests {", b"#[ cfg ( test ) ]\nmod tests\n{")
        )))
        for old, new in ((b"fn before", b"fn changed"), (b"fn after", b"fn changed"),
                         (b"pub fn after() {}", b"pub fn after() { panic!(); }")):
            self.assertNotEqual(expected, digest(rust_runtime_bytes(original.replace(old, new))))
        self.assertNotEqual(expected, digest(rust_runtime_bytes(original + b"\npub fn injected() {}\n")))
        moved = original.replace(b"pub fn before() {}\n", b"") + b"pub fn before() {}\n"
        self.assertNotEqual(expected, digest(rust_runtime_bytes(moved)))

    def test_nested_or_quoted_test_headers_do_not_expand_the_mutable_region(self):
        original = b'pub fn f() {}\n#[cfg(test)]\nmod tests { fn check() {} }\n'
        expected = digest(rust_runtime_bytes(original))
        for replacement in (
            b'fn check() { let _ = "#[cfg(test)] mod tests {"; }',
            b'fn check() { let _ = r#"#[cfg(test)] mod tests {"#; }',
            b'/* #[cfg(test)] mod tests { */ fn check() {}',
            b'// #[cfg(test)] mod tests {\nfn check() {}',
            b'#[cfg(test)] mod tests { fn check() {} }',
        ):
            with self.subTest(replacement=replacement):
                changed = original.replace(b"fn check() {}", replacement)
                self.assertEqual(expected, digest(rust_runtime_bytes(changed)))
                self.assertNotEqual(
                    expected, digest(rust_runtime_bytes(changed + b"pub fn injected() {}\n")),
                )
        self.assertNotEqual(
            expected,
            digest(rust_runtime_bytes(original + b"#[cfg(test)] mod tests {}\n")),
        )

    def test_rust_runtime_suffix_cannot_hide_behind_string_or_comment_braces(self):
        original = b'pub fn f() {}\n#[cfg(test)]\nmod tests { let_text!{r#"}"#} }\n'
        self.assertNotEqual(
            digest(rust_runtime_bytes(original)),
            digest(rust_runtime_bytes(original + b"pub const CHANGED: i32 = 1;\n")),
        )
        with self.assertRaises(AssertionError):
            rust_runtime_bytes(original.replace(b"mod tests {", b"mod renamed {"))

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
