"""Fixture-owned SQLite connections must not leak into later CLI diagnostics."""

import sqlite3
import unittest
from unittest.mock import patch


class TestLifecycleFixtureCleanup(unittest.TestCase):
    def assert_cases_close_connections(self, names):
        for name in names:
            with self.subTest(case=name):
                connections = []
                connect = sqlite3.connect

                def track_connection(*args, **kwargs):
                    connection = connect(*args, **kwargs)
                    connections.append(connection)
                    return connection

                try:
                    suite = unittest.defaultTestLoader.loadTestsFromName(name)
                    result = unittest.TestResult()
                    with patch.object(sqlite3, "connect", side_effect=track_connection):
                        suite.run(result)
                    self.assertEqual(result.errors, [])
                    self.assertEqual(result.failures, [])
                    self.assertEqual(result.skipped, [])
                    self.assertGreater(len(connections), 0)
                    # Holding references makes this independent of Python's GC
                    # schedule and detects the leak even before 3.13 introduced
                    # sqlite3's unclosed-connection ResourceWarning.
                    open_connections = 0
                    for connection in connections:
                        try:
                            connection.execute("SELECT 1")
                        except sqlite3.ProgrammingError as error:
                            self.assertIn("closed", str(error))
                        else:
                            open_connections += 1
                    self.assertEqual(open_connections, 0, "fixture left owned SQLite connections open")
                finally:
                    # Keep a failed regression from leaking its deliberately
                    # retained references into unrelated tests.
                    for connection in connections:
                        connection.close()

    def test_shared_and_reopened_manager_fixtures_close_owned_connections(self):
        self.assert_cases_close_connections([
            "test_lifecycle_engine.TestLifecycleEngine.test_completed_transaction_id_retry_is_exact_and_conflicts_rejected",
            "test_lifecycle_engine.TestLifecycleEngine.test_distinct_skills_deduplicate_snapshot_not_version_or_identity",
            "test_lifecycle_recovery.TestLifecycleRecovery.test_second_move_step_failure_preserves_partial_evidence_and_can_resume",
            "test_lifecycle_recovery.TestLifecycleRecovery.test_real_process_death_at_durable_boundaries_and_explicit_resume",
            "test_lifecycle_recovery.TestLifecycleRecovery.test_real_verification_process_death_leaves_old_grant_requires_reapproval",
        ])

    def test_raw_mutation_helpers_commit_and_close_owned_connections(self):
        self.assert_cases_close_connections([
            "test_lifecycle_capture.TestLifecycleCapture.test_orphan_completion_event_cannot_be_replaced_by_a_new_observation",
            "test_lifecycle_repository.TestLifecycleRepository.test_invalid_json_checksum_and_revision_fail_closed",
            "test_lifecycle_model_sequences.TestLifecycleModelSequences.test_oracle_mutation_controls_detect_fact_loss_bad_binding_and_pointer_damage",
            "test_lifecycle_model_sequences.TestLifecycleModelSequences.test_oracle_fresh_observer_rejects_orphan_history_and_missing_completion_events",
        ])

    def test_schema_fixtures_close_owned_connections(self):
        self.assert_cases_close_connections([
            "test_lifecycle_schemas.TestLifecycleSchemas.test_real_core_records_and_every_operation_match_shipped_contracts",
            "test_lifecycle_schemas.TestLifecycleSchemas.test_malformed_shapes_fail_closed",
            "test_lifecycle_schemas.TestLifecycleSchemas.test_migration_and_interrupted_recovery_records_match_contracts",
            "test_lifecycle_schemas.TestLifecycleSchemas.test_timezone_qualified_imported_plan_matches_runtime",
        ])


if __name__ == "__main__":
    unittest.main()
