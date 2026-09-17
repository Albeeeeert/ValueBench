from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from value_eval.config import PACKAGE_ROOT


class PortabilityTest(unittest.TestCase):
    def test_project_has_no_source_machine_dependency(self) -> None:
        forbidden = "/home1/" + "chenzhiyuan/Study/valueagent"
        suffixes = {".py", ".yaml", ".json", ".md", ".sh"}
        for path in PACKAGE_ROOT.rglob("*"):
            if not path.is_file() or path.suffix not in suffixes:
                continue
            if "outputs" in path.parts or "logs" in path.parts:
                continue
            self.assertNotIn(forbidden, path.read_text(encoding="utf-8"), str(path))

    def test_validations_work_after_isolated_copy(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            copied = Path(temporary) / "Value_eval"
            shutil.copytree(
                PACKAGE_ROOT,
                copied,
                ignore=shutil.ignore_patterns("outputs", "logs", "__pycache__", "*.pyc", ".pytest_cache"),
            )
            environment = dict(os.environ)
            environment.pop("PYTHONPATH", None)
            commands = (
                [sys.executable, "-m", "value_eval", "validate-input", "--config", "configs/prepared_scenarios.yaml"],
                [sys.executable, "-m", "value_eval", "validate-excel", "--config", "configs/excel_to_benchmark.yaml"],
            )
            for command in commands:
                result = subprocess.run(
                    command,
                    cwd=copied,
                    env=environment,
                    text=True,
                    capture_output=True,
                    timeout=30,
                )
                self.assertEqual(result.returncode, 0, result.stderr or result.stdout)


if __name__ == "__main__":
    unittest.main()
