"""The repair no-op must preserve every supplied file, not merely pass tests."""

from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest

import yaml


SPEC = Path(__file__).with_name("eval.yaml")
DOCUMENT = yaml.safe_load(SPEC.read_text(encoding="utf-8"))
CASE = next(
    case for case in DOCUMENT["stimuli"]
    if case["name"] == "Leave already-correct tests unchanged during a repair request"
)
FILES = DOCUMENT["environment"]["files"] + CASE["environment"]["files"]


class RepairNoopTests(unittest.TestCase):
    def test_healthy_golden_and_every_file_mutation(self):
        with tempfile.TemporaryDirectory(prefix="repair-noop-") as directory:
            root = Path(directory)
            for item in FILES:
                target = root / item["dest"]
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes((SPEC.parent / item["src"]).read_bytes())
            command = shlex.split(CASE["graders"][0]["config"]["command"])
            command[0] = sys.executable

            def run_guard():
                return subprocess.run(command, cwd=root, capture_output=True, text=True, timeout=30)

            result = run_guard()
            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            self.assertIn("Ran 2 tests", result.stderr)
            subprocess.run(
                [sys.executable, "-m", "unittest", "-v"], cwd=root / "NoopRepair",
                check=True, capture_output=True, text=True, timeout=30,
            )
            result = run_guard()
            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            for item in FILES:
                path = root / item["dest"]
                original = path.read_bytes()
                for remove in (False, True):
                    with self.subTest(path=item["dest"], remove=remove):
                        if remove:
                            path.unlink()
                        else:
                            path.write_bytes(original + b"\n# unnecessary rewrite\n")
                        self.assertNotEqual(0, run_guard().returncode)
                        path.write_bytes(original)
            for name in (
                "NoopRepair/amount.py", "NoopRepair/__pycache__/hidden.py",
                "NoopRepair/nested/extra.txt", "amount.py", "unrelated/source.py",
                ".eval/extra.py",
            ):
                with self.subTest(addition=name):
                    addition = root / name
                    addition.parent.mkdir(parents=True, exist_ok=True)
                    addition.write_text("from amounts import total\n", encoding="utf-8")
                    self.assertNotEqual(0, run_guard().returncode)
                    addition.unlink()


if __name__ == "__main__":
    unittest.main()
