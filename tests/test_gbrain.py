import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agent_memory_gateway.gbrain import (
    GBrainSchemaReport,
    REQUIRED_COLUMNS,
    REQUIRED_EXTENSIONS,
    REQUIRED_TABLES,
)


class GBrainSchemaTests(unittest.TestCase):
    def test_report_is_compatible_when_all_required_objects_exist(self):
        report = GBrainSchemaReport("gbrain", REQUIRED_EXTENSIONS, REQUIRED_TABLES, REQUIRED_COLUMNS)
        self.assertTrue(report.compatible)
        self.assertEqual(report.missing_extensions, [])
        self.assertEqual(report.missing_tables, [])
        self.assertEqual(report.missing_columns, [])
        self.assertTrue(report.schema_version.startswith("gbrain-"))

    def test_report_lists_missing_objects(self):
        report = GBrainSchemaReport("gbrain", frozenset(), frozenset({"facts"}), frozenset())
        self.assertFalse(report.compatible)
        self.assertIn("vector", report.missing_extensions)
        self.assertIn("pages", report.missing_tables)
        self.assertIn("facts.fact", report.missing_columns)


if __name__ == "__main__":
    unittest.main()
