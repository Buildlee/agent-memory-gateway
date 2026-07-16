import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agent_memory_gateway.metadata_migrations import (
    MetadataSchemaReport,
    REQUIRED_METADATA_COLUMNS,
    REQUIRED_METADATA_TABLES,
    BASE_SCHEMA_VERSION,
    expected_checksums,
    migration_specs,
    read_schema,
    schema_directory,
)
from agent_memory_gateway import metadata_migrations


class MetadataSchemaContractTests(unittest.TestCase):
    def test_bundled_schema_matches_the_checkout_and_is_used_without_a_checkout_root(self):
        root = Path(__file__).resolve().parents[1]
        bundled = root / "src" / "agent_memory_gateway" / "_schema"
        checkout_files = sorted((root / "schema").rglob("*.sql"))
        bundled_files = sorted(bundled.rglob("*.sql"))
        self.assertEqual(
            [path.relative_to(root / "schema") for path in checkout_files],
            [path.relative_to(bundled) for path in bundled_files],
        )
        for checkout in checkout_files:
            packaged = bundled / checkout.relative_to(root / "schema")
            self.assertEqual(checkout.read_bytes(), packaged.read_bytes())

        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            metadata_migrations, "repository_root", return_value=Path(directory)
        ):
            self.assertEqual(schema_directory(), bundled)
            self.assertEqual(len(expected_checksums()), 7)

    def test_schema_declares_every_required_metadata_table(self):
        sql = "\n".join(read_schema(spec.path) for spec in migration_specs())
        for table in REQUIRED_METADATA_TABLES:
            self.assertIn(f"CREATE TABLE IF NOT EXISTS {table}", sql)

    def test_schema_is_transactional_and_has_no_destructive_statement(self):
        sql = "\n".join(
            line for line in read_schema(migration_specs()[0].path).splitlines() if not line.lstrip().startswith("--")
        ).upper()
        self.assertIn("BEGIN;", sql)
        self.assertTrue(sql.rstrip().endswith("COMMIT;"))
        for statement in ("DROP ", "DELETE ", "TRUNCATE ", "ALTER TABLE"):
            self.assertNotIn(statement, sql)

    def test_runtime_role_has_only_required_database_privileges(self):
        sql = read_schema(migration_specs()[0].path).upper()
        self.assertIn("GRANT SELECT, INSERT, UPDATE ON ALL TABLES", sql)
        self.assertIn("GRANT USAGE, SELECT ON ALL SEQUENCES", sql)
        self.assertNotIn("GRANT DELETE", sql)
        self.assertNotIn("GRANT CREATE", sql)

    def test_schema_report_requires_matching_migration_checksum(self):
        expected = expected_checksums()
        compatible = MetadataSchemaReport(
            database="memory_gateway",
            tables=REQUIRED_METADATA_TABLES,
            columns=REQUIRED_METADATA_COLUMNS,
            migration_checksums=expected,
            expected_checksums=expected,
        )
        stale = MetadataSchemaReport(
            database="memory_gateway",
            tables=REQUIRED_METADATA_TABLES,
            columns=REQUIRED_METADATA_COLUMNS,
            migration_checksums={BASE_SCHEMA_VERSION: "stale"},
            expected_checksums=expected,
        )
        self.assertEqual(BASE_SCHEMA_VERSION, "2026-07-11.1")
        self.assertTrue(compatible.compatible)
        self.assertFalse(stale.compatible)
        self.assertFalse(stale.checksum_matches)

    def test_scope_migration_refuses_to_guess_historical_scope(self):
        sql = read_schema(migration_specs()[1].path)
        self.assertIn("必须先人工映射 scope", sql)
        self.assertIn("gateway_events_scope_check", sql)

    def test_refresh_replay_migration_only_adds_complete_ciphertext_fields(self):
        sql = read_schema(migration_specs()[2].path)
        self.assertIn("replacement_ciphertext BYTEA", sql)
        self.assertIn("replacement_nonce BYTEA", sql)
        self.assertIn("replacement_key_version TEXT", sql)
        self.assertIn("refresh_credentials_replacement_ciphertext_complete", sql)
        normalized = sql.upper()
        for statement in ("DROP ", "DELETE ", "TRUNCATE "):
            self.assertNotIn(statement, normalized)

    def test_instruction_like_migration_is_fail_closed_for_historical_events(self):
        sql = read_schema(migration_specs()[3].path)
        self.assertIn("instruction_like BOOLEAN NOT NULL DEFAULT true", sql)
        self.assertIn("instruction_like = false", sql)
        normalized = sql.upper()
        for statement in ("DROP ", "DELETE ", "TRUNCATE "):
            self.assertNotIn(statement, normalized)

    def test_sync_protocol_migration_adds_epochs_and_contiguous_sequence(self):
        sql = read_schema(migration_specs()[4].path)
        self.assertIn("last_contiguous_event_seq BIGINT", sql)
        self.assertIn("device_auth_epoch BIGINT", sql)
        self.assertIn("agent_auth_epoch BIGINT", sql)
        self.assertIn("policy_version TEXT", sql)
        self.assertIn("VALUES ('sync_epoch'", sql)
        normalized = sql.upper()
        for statement in ("DROP ", "DELETE ", "TRUNCATE "):
            self.assertNotIn(statement, normalized)

    def test_review_lifecycle_migration_is_append_only_and_preserves_history(self):
        sql = read_schema(migration_specs()[5].path)
        self.assertIn("CREATE TABLE IF NOT EXISTS review_operations", sql)
        self.assertIn("CREATE TABLE IF NOT EXISTS memory_lifecycle", sql)
        self.assertIn("CREATE TABLE IF NOT EXISTS memory_lifecycle_history", sql)
        self.assertIn("ON CONFLICT (backend_ref) DO NOTHING", sql)
        normalized = sql.upper()
        for statement in ("DROP ", "DELETE ", "TRUNCATE "):
            self.assertNotIn(statement, normalized)

    def test_crystal_state_migration_only_tracks_references_and_status(self):
        sql = read_schema(migration_specs()[6].path)
        self.assertIn("CREATE TABLE IF NOT EXISTS memory_crystals", sql)
        self.assertIn("scope_binding_hash", sql)
        self.assertIn("source_refs JSONB", sql)
        normalized = sql.upper()
        for statement in ("DROP ", "DELETE ", "TRUNCATE "):
            self.assertNotIn(statement, normalized)


if __name__ == "__main__":
    unittest.main()
