"""Execute focused operation-scope fixtures without a language/toolkit abstraction."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile


def digest(data):
    return hashlib.sha256(data.replace(b"\r\n", b"\n")).hexdigest()


def rust_runtime_bytes(source):
    """Exclude only the fixture's balanced, cfg-gated test module."""
    text = source.decode("utf-8").replace("\r\n", "\n")
    header = re.search(r"#\s*\[\s*cfg\s*\(\s*test\s*\)\s*]\s*mod\s+tests\s*\{", text)
    assert header is not None, "Expected the existing inline test module"
    start = header.start()
    index = header.end()
    depth = 1
    while index < len(text):
        if text.startswith("//", index):
            end = text.find("\n", index)
            assert end >= 0, "Unclosed inline test module"
            index = end
        elif text.startswith("/*", index):
            nesting = 1
            index += 2
            while nesting and index < len(text):
                if text.startswith("/*", index):
                    nesting += 1
                    index += 2
                elif text.startswith("*/", index):
                    nesting -= 1
                    index += 2
                else:
                    index += 1
            assert nesting == 0, "Unclosed Rust block comment"
        else:
            raw = re.match(r'(?:br|cr|r)(#{0,255})"', text[index:])
            character = re.match(r"'(?:[^'\\\r\n]|\\(?:u\{[0-9a-fA-F_]+\}|x[0-9a-fA-F]{2}|.))'", text[index:])
            if raw:
                end = text.find('"' + raw[1], index + raw.end())
                assert end >= 0, "Unclosed Rust raw string"
                index = end + 1 + len(raw[1])
            elif text[index] == '"':
                index += 1
                while index < len(text) and text[index] != '"':
                    index += 2 if text[index] == "\\" else 1
                assert index < len(text), "Unclosed Rust string"
                index += 1
            elif character:
                index += character.end()
            else:
                if text[index] == "{":
                    depth += 1
                elif text[index] == "}":
                    depth -= 1
                    if depth == 0:
                        return b"\0".join(part.encode("utf-8") for part in (
                            text[:start], text[index + 1:],
                        ))
                index += 1
    raise AssertionError("Unclosed inline test module")


def check_files(root, expected, mutable, python_tests=False):
    ignored = {"__pycache__", "target", ".git"}
    pytest_outputs = {
        ".pytest_cache/.gitignore", ".pytest_cache/CACHEDIR.TAG",
        ".pytest_cache/README.md", ".pytest_cache/v/cache/nodeids",
        ".pytest_cache/v/cache/lastfailed", ".pytest_cache/v/cache/stepwise",
    }
    assert not any(path.is_symlink() for path in root.rglob("*")), "Symlink in fixture scope"
    actual = {
        path.relative_to(root).as_posix(): path
        for path in root.rglob("*")
        if path.is_file() and not ignored.intersection(path.relative_to(root).parts)
        and path.name != "Cargo.lock"
        and path.relative_to(root).as_posix() not in pytest_outputs
    }
    assert expected.keys() <= actual.keys(), "An original file was removed"
    for name, expected_hash in expected.items():
        if name in mutable:
            continue
        assert not actual[name].is_symlink(), name
        assert digest(actual[name].read_bytes()) == expected_hash, f"Out-of-scope edit: {name}"
    extra = actual.keys() - expected.keys()
    assert all(name.endswith("_test.go") or (
        python_tests and Path(name).name.startswith("test_") and name.endswith(".py")
    ) for name in extra), f"Unexpected files: {extra}"


def command(argv, root, expected_exit=0, environment=None):
    result = subprocess.run(
        argv, cwd=root, env=environment, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=120,
    )
    print(result.stdout)
    print(result.stderr, file=sys.stderr)
    assert result.returncode == expected_exit, (argv, result.returncode)
    return result.stdout + result.stderr


def check_mutation(root, filename, old, new, argv, exit_code, outcome_pattern, environment=None):
    with tempfile.TemporaryDirectory(prefix="operation-mutation-") as temporary:
        mutated = Path(temporary) / "work"
        shutil.copytree(root, mutated, ignore=shutil.ignore_patterns("target", "__pycache__", ".git"))
        path = mutated / filename
        content = path.read_text(encoding="utf-8")
        assert content.count(old) == 1, f"Mutation anchor changed: {filename}"
        path.write_text(content.replace(old, new), encoding="utf-8")
        output = command(argv, mutated, exit_code, environment)
        assert re.search(outcome_pattern, output), "Counterfactual must reach the expected test outcome"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("case", choices=["repair", "regression", "new-regression", "rust", "go"])
    parser.add_argument("--root", type=Path, default=Path("work"))
    parser.add_argument("--file", action="append", required=True)
    parser.add_argument("--runtime-sha256")
    args = parser.parse_args()
    expected = dict(value.split("=", 1) for value in args.file)
    mutable = {"test_total.py"} if args.case == "repair" else (
        {"src/lib.rs"} if args.case == "rust" else set()
    )
    check_files(args.root, expected, mutable, args.case == "new-regression")
    if args.case in {"repair", "regression", "new-regression"}:
        program = (
            "import json,unittest,sys;"
            "s=unittest.defaultTestLoader.discover('.');"
            "r=unittest.TextTestRunner(verbosity=2).run(s);"
            f"assert r.testsRun{'>' if args.case == 'new-regression' else '=='}"
            f"{0 if args.case == 'new-regression' else 1} and len(r.errors)==0;"
            f"assert len(r.failures){'>' if args.case == 'new-regression' else '=='}"
            f"{1 if args.case == 'regression' else 0};"
            "print('OPERATION_CONTRACT: one expected test outcome')"
        )
        command([sys.executable, "-B", "-c", program], args.root)
        if args.case == "repair":
            check_mutation(
                args.root, "amounts.py", "return sum(values)", "return 0",
                [sys.executable, "-B", "-m", "unittest", "-v"], 1, r"FAIL: test_total",
            )
        if args.case == "new-regression":
            check_mutation(
                args.root, "limit.py", "return value > 10", "return value >= 10",
                [sys.executable, "-B", "-m", "unittest", "-v"], 0, r"\bOK\b",
            )
            check_mutation(
                args.root, "limit.py", "return value > 10", "return value == 10 or value > 11",
                [sys.executable, "-B", "-m", "unittest", "-v"], 1, r"FAIL:",
            )
    elif args.case == "rust":
        source = (args.root / "src" / "lib.rs").read_bytes()
        assert digest(rust_runtime_bytes(source)) == args.runtime_sha256, "Runtime Rust region changed"
        output = command(["cargo", "test", "--offline"], args.root)
        assert re.search(r"test tests::free_shipping_at_threshold\s+\.\.\. ok", output)
        count = re.search(r"test result: ok\.\s+(\d+) passed; 0 failed", output)
        assert count and int(count[1]) >= 2, output
        check_mutation(
            args.root, "src/lib.rs", "subtotal >= 50", "subtotal > 50",
            ["cargo", "test", "--offline"], 101, r"test result: FAILED",
        )
        assert digest(rust_runtime_bytes(
            (args.root / "src" / "lib.rs").read_bytes()
        )) == args.runtime_sha256, "Runtime Rust region changed"
    else:
        test_sources = list(args.root.glob("*_test.go"))
        assert test_sources, "No Go test file"
        source = "\n".join(path.read_text(encoding="utf-8") for path in test_sources)
        assert "httptest.NewServer" in source, "Exercise an owned ephemeral HTTP server"
        assert re.search(r"(?:TempDir|MkdirTemp)\s*\(", source), "Use an owned temporary directory"
        assert re.search(r"\b(?:Cleanup\s*\([^)]*\.Close|defer\s+\w+\.Close\s*\()", source), (
            "The fixture's HTTP server needs registered cleanup"
        )
        environment = dict(os.environ, GOPROXY="off", GOSUMDB="off")
        output = command(
            ["go", "test", "-json", "-count=1", "./..."], args.root,
            environment=environment,
        )
        events = [json.loads(line) for line in output.splitlines() if line.startswith("{")]
        passed = {event["Test"] for event in events
                  if event.get("Action") == "pass" and "Test" in event}
        assert len(passed) >= 2, "Both success and non-success HTTP behavior need tests"
        for old, new in (
            ("destination, body, 0600", "destination, append(body, '!'), 0600"),
            ("response.StatusCode != http.StatusOK", "false"),
        ):
            check_mutation(
                args.root, "client.go", old, new,
                ["go", "test", "-json", "-count=1", "./..."], 1,
                r'"Action":"fail".*"Test":', environment,
            )
    check_files(args.root, expected, mutable, args.case == "new-regression")
    print(f"OPERATION_CONTRACT: {args.case} passed")


if __name__ == "__main__":
    main()
