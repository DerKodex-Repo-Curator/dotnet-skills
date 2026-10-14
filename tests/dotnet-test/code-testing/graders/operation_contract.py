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


def is_linked_path(path):
    return path.is_symlink() or getattr(path, "is_junction", lambda: False)()


def generated_path(relative, case):
    if relative.parts[0] == ".git":
        return True
    if case == "rust":
        return relative.parts[0] == "target" or relative.as_posix() == "Cargo.lock"
    pytest_outputs = {
        ".pytest_cache/.gitignore", ".pytest_cache/CACHEDIR.TAG",
        ".pytest_cache/README.md", ".pytest_cache/v/cache/nodeids",
        ".pytest_cache/v/cache/lastfailed", ".pytest_cache/v/cache/stepwise",
    }
    return case in {"repair", "regression", "new-regression"} and (
        relative.as_posix() in pytest_outputs
        or (relative.suffix == ".pyc" and "__pycache__" in relative.parts)
    )


def check_files(root, expected, case):
    assert case in {"repair", "regression", "new-regression", "rust", "go"}, f"Unknown operation: {case}"
    assert not is_linked_path(root), f"Symlink or junction in fixture scope: {root}"
    paths = list(root.rglob("*"))
    assert not any(is_linked_path(path) for path in paths), (
        f"Symlink or junction in fixture scope: {root}"
    )
    actual = {
        path.relative_to(root).as_posix(): path
        for path in paths
        if path.is_file() and not generated_path(path.relative_to(root), case)
    }
    assert expected.keys() <= actual.keys(), "An original file was removed"
    for name, expected_hash in expected.items():
        if case == "rust" and name == "src/lib.rs":
            continue
        assert not actual[name].is_symlink(), name
        content = actual[name].read_bytes()
        if case == "repair" and name == "test_total.py":
            content = content.replace(b"from amounts import total", b"from amount import total")
        assert digest(content) == expected_hash, f"Out-of-scope edit: {name}"
    extra = actual.keys() - expected.keys()
    assert all(
        not {"__pycache__", ".pytest_cache"}.intersection(Path(name).parts)
        and ((case == "go" and name.endswith("_test.go")) or (
            case == "new-regression" and Path(name).name.startswith("test_") and name.endswith(".py")
        )) for name in extra
    ), f"Unexpected files: {extra}"


def command(argv, root, expected_exit=0, environment=None):
    result = subprocess.run(
        argv, cwd=root, env=environment, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=120,
    )
    print(result.stdout)
    print(result.stderr, file=sys.stderr)
    assert result.returncode == expected_exit, (argv, result.returncode)
    return result.stdout + result.stderr


def check_mutation(root, filename, old, new, argv, exit_code, outcome_pattern, environment=None, *, case):
    with tempfile.TemporaryDirectory(prefix="operation-mutation-") as temporary:
        mutated = Path(temporary) / "work"
        shutil.copytree(root, mutated, ignore=lambda directory, names: [
            name for name in names if generated_path(Path(directory).relative_to(root) / name, case)
        ])
        path = mutated / filename
        content = path.read_text(encoding="utf-8")
        assert content.count(old) == 1, f"Mutation anchor changed: {filename}"
        path.write_text(content.replace(old, new), encoding="utf-8")
        output = command(argv, mutated, exit_code, environment)
        assert re.search(outcome_pattern, output), "Counterfactual must reach the expected test outcome"


def has_go_cleanup(source):
    source = re.sub(
        r'//[^\r\n]*|/\*.*?\*/|`[^`]*`|"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'',
        lambda match: " " * len(match[0]), source, flags=re.S,
    )
    if re.search(r"\b(?:Cleanup\s*\(\s*\w+\.Close\s*\)|defer\s+\w+\.Close\s*\(\s*\))", source):
        return True
    for match in re.finditer(r"\b(?:Cleanup\s*\(\s*|defer\s+)func\s*\([^)]*\)\s*\{", source):
        depth = 1
        end = match.end()
        while end < len(source) and depth:
            depth += (source[end] == "{") - (source[end] == "}")
            end += 1
        if depth == 0 and re.search(r"\b\w+\.Close\s*\(\s*\)", source[match.end():end - 1]):
            return True
    return False


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("case", choices=["repair", "regression", "new-regression", "rust", "go"])
    parser.add_argument("--root", type=Path, default=Path("work"))
    parser.add_argument("--file", action="append", required=True)
    parser.add_argument("--runtime-sha256")
    args = parser.parse_args()
    expected = dict(value.split("=", 1) for value in args.file)
    check_files(args.root, expected, args.case)
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
                case=args.case,
            )
        if args.case == "new-regression":
            check_mutation(
                args.root, "limit.py", "return value > 10", "return value >= 10",
                [sys.executable, "-B", "-m", "unittest", "-v"], 0, r"\bOK\b",
                case=args.case,
            )
            check_mutation(
                args.root, "limit.py", "return value > 10", "return value == 10 or value > 11",
                [sys.executable, "-B", "-m", "unittest", "-v"], 1, r"FAIL:",
                case=args.case,
            )
    elif args.case == "rust":
        source = (args.root / "src" / "lib.rs").read_bytes()
        assert digest(rust_runtime_bytes(source)) == args.runtime_sha256, "Runtime Rust region changed"
        output = command(["cargo", "test", "--offline"], args.root)
        assert re.search(r"test tests::free_shipping_at_threshold\s+\.\.\. ok", output)
        count = re.search(r"test result: ok\.\s+(\d+) passed; 0 failed", output)
        assert count and int(count[1]) >= 2, output
        for cutoff in ("subtotal > 50", "subtotal >= 49"):
            check_mutation(
                args.root, "src/lib.rs", "subtotal >= 50", cutoff,
                ["cargo", "test", "--offline"], 101, r"test result: FAILED",
                case=args.case,
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
        assert has_go_cleanup(source), (
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
            ('return fmt.Errorf("unexpected status %d", response.StatusCode)',
             '_ = os.WriteFile(destination, []byte("unexpected error body"), 0600)\n'
             '\t\treturn fmt.Errorf("unexpected status %d", response.StatusCode)'),
        ):
            check_mutation(
                args.root, "client.go", old, new,
                ["go", "test", "-json", "-count=1", "./..."], 1,
                r'"Action":"fail".*"Test":', environment,
                case=args.case,
            )
    check_files(args.root, expected, args.case)
    print(f"OPERATION_CONTRACT: {args.case} passed")


if __name__ == "__main__":
    main()
