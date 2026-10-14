import importlib.util
from contextlib import redirect_stdout
from io import StringIO
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import yaml

from operation_contract import check_files, check_mutation, digest, main, rust_runtime_bytes, verify_go_lifetime


class ScopeTests(unittest.TestCase):
    @staticmethod
    def go_case():
        suite = Path(__file__).resolve().parents[1]
        replay_spec = importlib.util.spec_from_file_location(
            "operation_replay", suite.parent.parent / "dotnet-test-migration" / "graders" / "replay.py"
        )
        replay = importlib.util.module_from_spec(replay_spec)
        replay_spec.loader.exec_module(replay)
        spec = suite / "eval.yaml"
        case = next(case for case in yaml.safe_load(spec.read_text(encoding="utf-8"))["stimuli"]
                    if case["name"].startswith("Test Go HTTP"))
        return replay, spec, case

    def test_go_requires_cleanup_for_each_server(self):
        replay, spec, case = self.go_case()
        for cleanup in ("t.Cleanup(server.Close)", "defer server.Close()"):
            with self.subTest(cleanup=cleanup), tempfile.TemporaryDirectory(
                prefix="go-missing-server-cleanup-"
            ) as temporary:
                root = Path(temporary)
                replay.materialize(spec, case, root)
                tests = root / "work" / "client_test.go"
                source = tests.read_text(encoding="utf-8")
                self.assertEqual(1, source.count(cleanup))
                tests.write_text(source.replace(cleanup, ""), encoding="utf-8")
                native = subprocess.run(
                    ["go", "test", "-json", "-count=1", "./..."], cwd=root / "work",
                    env=dict(os.environ, GOPROXY="off", GOSUMDB="off"),
                    capture_output=True, text=True, timeout=120,
                )
                self.assertEqual(0, native.returncode, native.stdout + native.stderr)
                with self.assertRaisesRegex(AssertionError, "GO_SERVER_LIFETIME: unclosed server"):
                    replay.run_graders(case, root)

    def test_go_cleanup_check_ignores_unregistered_calls_and_documentation(self):
        replay, spec, case = self.go_case()
        for replacement in (
            '// t.Cleanup(server.Close)\n\t_ = "server.Close()"',
            "t.Cleanup(func() { if false { server.Close() } })",
            "t.Cleanup(func() { server := httptest.NewServer(http.NotFoundHandler()); server.Close() })",
        ):
            with self.subTest(replacement=replacement), tempfile.TemporaryDirectory(
                prefix="go-false-cleanup-"
            ) as temporary:
                root = Path(temporary)
                replay.materialize(spec, case, root)
                tests = root / "work" / "client_test.go"
                source = tests.read_text(encoding="utf-8")
                self.assertEqual(1, source.count("t.Cleanup(server.Close)"))
                tests.write_text(source.replace("t.Cleanup(server.Close)", replacement), encoding="utf-8")
                before = {path: path.read_bytes() for path in (root / "work").iterdir()}
                output = StringIO()
                with redirect_stdout(output), self.assertRaises(AssertionError):
                    verify_go_lifetime(root / "work", dict(os.environ, GOPROXY="off", GOSUMDB="off"))
                self.assertIn("GO_SERVER_LIFETIME: unclosed server", output.getvalue())
                self.assertEqual(before, {path: path.read_bytes() for path in (root / "work").iterdir()})

    def test_go_cleanup_callbacks_pass_native_golden(self):
        replay, spec, case = self.go_case()
        for callback in (
            "t.Cleanup(func() { server.Close() })",
            "defer func() { server.Close() }()",
            "t.Cleanup(func() { if server != nil { server.Close() } })",
        ):
            with self.subTest(callback=callback), tempfile.TemporaryDirectory(
                prefix="go-cleanup-callback-"
            ) as temporary:
                root = Path(temporary)
                replay.materialize(spec, case, root)
                tests = root / "work" / "client_test.go"
                source = tests.read_text(encoding="utf-8")
                self.assertEqual(2, source.count("t.Cleanup(server.Close)") +
                                 source.count("defer server.Close()"))
                tests.write_text(source.replace(
                    "t.Cleanup(server.Close)", callback
                ).replace("defer server.Close()", callback), encoding="utf-8")
                replay.run_graders(case, root)

    def test_go_factory_and_cleanup_helpers_keep_test_ownership(self):
        replay, spec, case = self.go_case()
        for variant in ("helpers", "parallel-subtests"):
            with self.subTest(variant=variant), tempfile.TemporaryDirectory(
                prefix="go-owned-helpers-"
            ) as temporary:
                root = Path(temporary)
                replay.materialize(spec, case, root)
                tests = root / "work" / "client_test.go"
                source = tests.read_text(encoding="utf-8")
                if variant == "helpers":
                    source = source.replace("httptest.NewServer(", "makeServer(t, ").replace(
                        "t.Cleanup(server.Close)", "t.Cleanup(func() { closeServer(server) })"
                    ).replace("defer server.Close()", "defer closeServer(server)")
                    source += (
                        "\nfunc makeServer(owner testing.TB, handler http.Handler) *httptest.Server {\n"
                        "\treturn httptest.NewServer(handler)\n}\n"
                        "func closeServer(server *httptest.Server) { server.Close() }\n"
                    )
                    source = source.replace('"net/http/httptest"', 'webtest "net/http/httptest"').replace(
                        "httptest.", "webtest."
                    )
                else:
                    source, count = re.subn(
                        r"^(func Test\w+)\(t \*testing.T\) \{(.*?)^\}",
                        r'\1(parent *testing.T) { parent.Run("owned", func(t *testing.T) { t.Parallel();\2})}',
                        source, flags=re.M | re.S,
                    )
                    self.assertEqual(2, count)
                tests.write_text(source, encoding="utf-8")
                replay.run_graders(case, root)

    def test_go_lifetime_probe_is_authenticated(self):
        replay, spec, case = self.go_case()
        with tempfile.TemporaryDirectory(prefix="go-probe-authentication-") as temporary:
            root = Path(temporary)
            replay.materialize(spec, case, root)
            helper = root / ".eval" / "go_server_lifetime.go"
            helper.write_bytes(helper.read_bytes() + b"\n// changed\n")
            with self.assertRaisesRegex(AssertionError, "Go lifetime verifier changed"):
                replay.run_graders(case, root)

    def test_mutation_copy_preserves_other_languages_target_directories(self):
        for case in ("go", "new-regression"):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                (root / "source.txt").write_text("original")
                (root / "target").mkdir()
                (root / "target" / "test_input.py").write_text("must not be discarded")

                def run_tests(argv, copied, *args):
                    self.assertTrue((copied / "target" / "test_input.py").is_file())
                    return "expected result"

                with patch("operation_contract.command", side_effect=run_tests):
                    check_mutation(root, "source.txt", "original", "mutated",
                                   ["not-executed"], 0, "expected result", case=case)

    def test_scope_exemptions_and_additions_are_operation_specific(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "original.txt").write_bytes(b"unchanged")
            expected = {"original.txt": digest(b"unchanged")}
            python_cases = {"repair", "regression", "new-regression"}
            for case in ("repair", "regression", "new-regression", "rust", "go"):
                for name, allowed in (
                    ("target/helper.py", {"rust"}),
                    ("nested/target/helper.py", set()),
                    ("Cargo.lock", {"rust"}),
                    ("nested/Cargo.lock", set()),
                    ("unrequested_test.go", {"go"}),
                    ("test_boundary.py", {"new-regression"}),
                    ("__pycache__/module.cpython-314.pyc", python_cases),
                    (".pytest_cache/CACHEDIR.TAG", python_cases),
                ):
                    path = root / name
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(b"addition")
                    with self.subTest(case=case, path=name):
                        if case in allowed:
                            check_files(root, expected, case)
                        else:
                            with self.assertRaisesRegex(AssertionError, "Unexpected files"):
                                check_files(root, expected, case)
                    path.unlink()

    def test_repair_rejects_cross_operation_additions_after_execution(self):
        for name in ("target/helper.py", "unrequested_test.go"):
            with self.subTest(path=name), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary) / "work"
                arguments, _ = self.repair_fixture(root)

                def run_tests(*args):
                    path = root / name
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text("unauthorized addition")
                    return ""

                with patch.object(sys, "argv", arguments[2:]), patch(
                    "operation_contract.command", side_effect=run_tests,
                ), patch("operation_contract.check_mutation"):
                    with self.assertRaisesRegex(AssertionError, "Unexpected files"):
                        main()

    def test_missing_rust_boundary_and_go_absence_assertions_are_rejected(self):
        suite = Path(__file__).resolve().parents[1]
        replay_spec = importlib.util.spec_from_file_location(
            "operation_replay", suite.parent.parent / "dotnet-test-migration" / "graders" / "replay.py"
        )
        replay = importlib.util.module_from_spec(replay_spec)
        replay_spec.loader.exec_module(replay)
        spec = suite / "eval.yaml"
        cases = yaml.safe_load(spec.read_text(encoding="utf-8"))["stimuli"]
        for prefix, filename, before, after, evidence in (
            ("Repair and extend Rust", "work/src/lib.rs",
             "assert_eq!(shipping_cost(49), 10);", "assert_eq!(shipping_cost(50), 0);",
             "test result: ok"),
            ("Test Go HTTP", "work/client_test.go",
             '\tif _, err := os.Stat(destination); !os.IsNotExist(err) {\n'
             '\t\tt.Fatalf("error response created destination: %v", err)\n\t}\n',
             "", "TestDownloadRejectsErrorStatus"),
        ):
            with self.subTest(case=prefix), tempfile.TemporaryDirectory(
                prefix="operation-coverage-mutation-"
            ) as temporary:
                root = Path(temporary)
                case = next(case for case in cases if case["name"].startswith(prefix))
                replay.materialize(spec, case, root)
                target = root / filename
                text = target.read_text(encoding="utf-8")
                self.assertEqual(1, text.count(before))
                target.write_text(text.replace(before, after), encoding="utf-8")
                with self.assertRaises(AssertionError) as rejected:
                    replay.run_graders(case, root)
                self.assertIn(evidence, str(rejected.exception))

    def test_scope_rejects_linked_roots_and_descendants(self):
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary)
            external = parent / "external"
            external.mkdir()
            source = external / "limit.py"
            source.write_bytes(b"unchanged")
            expected = {"limit.py": digest(source.read_bytes())}
            check_files(external, expected, "regression")
            workspace = parent / "work"
            workspace.mkdir()
            link = workspace / "src"
            for kind in ("symlink", "junction") if os.name == "nt" else ("symlink",):
                if kind == "junction":
                    subprocess.run(
                        ["pwsh", "-NoProfile", "-NonInteractive", "-Command",
                         "New-Item -ItemType Junction -Path $env:OPERATION_LINK "
                         "-Target $env:OPERATION_TARGET | Out-Null"],
                        env=dict(os.environ, OPERATION_LINK=str(link), OPERATION_TARGET=str(external)),
                        check=True, capture_output=True, text=True, timeout=30,
                    )
                    self.assertFalse(link.is_symlink())
                    self.assertTrue(link.is_junction())
                else:
                    link.symlink_to(external, target_is_directory=True)
                try:
                    for root, files in (
                        (link, expected),
                        (workspace, {"src/limit.py": expected["limit.py"]}),
                    ):
                        with self.subTest(kind=kind, root=root), self.assertRaisesRegex(
                            AssertionError, "Symlink.*fixture scope"
                        ):
                            check_files(root, files, "regression")
                finally:
                    if kind == "junction":
                        link.rmdir()
                    else:
                        link.unlink()
                self.assertEqual(b"unchanged", source.read_bytes())

    def repair_fixture(self, root):
        fixture = Path(__file__).resolve().parents[1] / "fixtures" / "operation-repair"
        shutil.copytree(fixture, root)
        arguments = [
            sys.executable, "-B", str(Path(__file__).with_name("operation_contract.py")),
            "repair", "--root", str(root),
        ]
        for path in root.iterdir():
            arguments += ["--file", path.name + "=" + digest(path.read_bytes())]
        source = root / "test_total.py"
        source.write_bytes(source.read_bytes().replace(b"from amount import total", b"from amounts import total"))
        return arguments, source

    def test_import_only_repair_passes_but_weakened_assertion_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            arguments, source = self.repair_fixture(Path(temporary) / "work")
            result = subprocess.run(arguments, capture_output=True, text=True, timeout=30)
            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            source.write_bytes(source.read_bytes().replace(
                b"assertEqual(5, total([2, 3]))", b"assertNotEqual(0, total([2, 3]))",
            ))
            result = subprocess.run(arguments, capture_output=True, text=True, timeout=30)
            self.assertNotEqual(0, result.returncode, result.stdout + result.stderr)
            self.assertIn("Out-of-scope edit: test_total.py", result.stderr)

    def test_repair_rechecks_test_source_after_execution(self):
        with tempfile.TemporaryDirectory() as temporary:
            arguments, source = self.repair_fixture(Path(temporary) / "work")

            def run_tests(*args):
                source.write_bytes(source.read_bytes().replace(
                    b"assertEqual(5, total([2, 3]))", b"assertNotEqual(0, total([2, 3]))",
                ))
                return ""

            with patch.object(sys, "argv", arguments[2:]), patch(
                "operation_contract.command", side_effect=run_tests,
            ), patch("operation_contract.check_mutation"):
                with self.assertRaisesRegex(AssertionError, "Out-of-scope edit: test_total.py"):
                    main()

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
            check_files(root, expected, "new-regression")
            (cache / "hidden.py").write_text("unauthorized source")
            with self.assertRaises(AssertionError):
                check_files(root, expected, "new-regression")

    def test_python_cache_excludes_only_generated_bytecode(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "limit.py"
            source.write_bytes(b"unchanged")
            expected = {"limit.py": digest(source.read_bytes())}
            cache = root / "__pycache__"
            cache.mkdir()
            (cache / "limit.cpython-314.pyc").write_bytes(b"generated bytecode")
            for case in ("regression", "new-regression"):
                check_files(root, expected, case)
                for name in ("test_helper.py", "notes.txt"):
                    added = cache / name
                    added.write_bytes(b"unauthorized source")
                    with self.subTest(case=case, name=name), self.assertRaisesRegex(
                        AssertionError, "Unexpected files"
                    ):
                        check_files(root, expected, case)
                    added.unlink()

    def test_new_test_does_not_authorize_existing_test_or_product_edits(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for name in ("limit.py", "test_existing.py"):
                (root / name).write_bytes(b"original")
            expected = {name: digest((root / name).read_bytes())
                        for name in ("limit.py", "test_existing.py")}
            (root / "test_boundary.py").write_text("new test")
            check_files(root, expected, "new-regression")
            for name in expected:
                with self.subTest(path=name):
                    (root / name).write_bytes(b"changed")
                    with self.assertRaises(AssertionError):
                        check_files(root, expected, "new-regression")
                    (root / name).write_bytes(b"original")


if __name__ == "__main__":
    unittest.main()
