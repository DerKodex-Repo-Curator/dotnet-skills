"""Reject old xUnit configuration and newly imposed diagnostic deadlines."""

import argparse
from pathlib import Path
import re
import xml.etree.ElementTree as ET


def verify(root, forbid_timeouts=False):
    ignored = {".git", ".eval", ".agents", ".copilot", ".claude", "bin", "obj"}
    for path in root.rglob("*"):
        if not path.is_file() or any(part in ignored for part in path.relative_to(root).parts):
            continue
        assert path.name.lower() != "xunit.runner.json", f"Obsolete xUnit runner config: {path}"
        if path.suffix in {".csproj", ".props", ".targets", ".runsettings"}:
            for node in ET.parse(path).getroot().iter():
                tag = node.tag.rsplit("}", 1)[-1]
                if tag in {"PackageReference", "PackageVersion"}:
                    package = node.get("Include", node.get("Update", "")).lower()
                    assert package != "xunit" and not package.startswith("xunit."), (
                        f"Retained xUnit package: {package} in {path}"
                    )
                if tag == "Using":
                    namespace = node.get("Include", "").removeprefix("global::")
                    assert namespace != "Xunit" and not namespace.startswith("Xunit."), (
                        f"Retained xUnit import: {namespace} in {path}"
                    )
                if forbid_timeouts and tag in {"TestTimeout", "TestSessionTimeout"}:
                    assert int((node.text or "").strip()) == 0, (
                        f"Diagnostic warning became enforced {tag}: {path}"
                    )
        elif path.suffix == ".cs":
            text = path.read_text(encoding="utf-8")
            text = re.sub(
                r'(?P<raw>"{3,})[\s\S]*?(?P=raw)|@"(?:[^"]|"")*"|'
                r'"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'|//[^\n]*|/\*[\s\S]*?\*/',
                lambda match: " " + "\n" * match[0].count("\n"),
                text,
            )
            assert not re.search(
                r"\b(?:global\s+)?using\s+(?:(?:static|unsafe)\s+)?"
                r"(?:\w+\s*=\s*)?(?:global\s*::\s*)?Xunit\b",
                text,
            ), f"Retained xUnit using directive: {path}"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--forbid-timeouts", action="store_true")
    args = parser.parse_args()
    verify(Path.cwd(), args.forbid_timeouts)


if __name__ == "__main__":
    main()
