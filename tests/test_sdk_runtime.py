import tempfile
import unittest
from pathlib import Path

import ebaseline_sdk as sdk


class LatestContextFileTest(unittest.TestCase):
    def _make(self, root: Path, relative: str) -> Path:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}", encoding="utf-8")
        return path

    def test_finds_attempt_layout(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            latest = self._make(
                root, "ebase_runs/group_7/batch_1/attempt_2/70001.json")
            self._make(
                root, "ebase_runs/group_7/batch_1/attempt_2/70001.qa.json")
            self.assertEqual(sdk._latest_context_file(root, 7), latest)

    def test_finds_legacy_layout(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            legacy = self._make(root, "ebase_runs/group_7/batch_1/70001.json")
            self.assertEqual(sdk._latest_context_file(root, 7), legacy)

    def test_ignores_qa_records(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._make(root, "ebase_runs/group_7/batch_1/70001.qa.json")
            self.assertIsNone(sdk._latest_context_file(root, 7))

    def test_missing_group(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(sdk._latest_context_file(Path(tmp), 7))


if __name__ == "__main__":
    unittest.main()
