from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from value_eval.value_to_scenario.fingerprint import _semantic_python_hash


class FingerprintTest(unittest.TestCase):
    def test_logging_only_change_does_not_invalidate_semantic_hash(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "sample.py"
            path.write_text(
                'def build(logger):\n    logger.info("old message")\n    return 1\n',
                encoding="utf-8",
            )
            before = _semantic_python_hash(path)
            path.write_text(
                'def build(logger):\n    logger.info("new message", 2)\n    return 1\n',
                encoding="utf-8",
            )
            self.assertEqual(before, _semantic_python_hash(path))

    def test_content_change_invalidates_semantic_hash(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "sample.py"
            path.write_text("def build():\n    return 1\n", encoding="utf-8")
            before = _semantic_python_hash(path)
            path.write_text("def build():\n    return 2\n", encoding="utf-8")
            self.assertNotEqual(before, _semantic_python_hash(path))


if __name__ == "__main__":
    unittest.main()
