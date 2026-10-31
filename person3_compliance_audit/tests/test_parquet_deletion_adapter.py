
import json
import os
import shutil
import tempfile
import unittest

from pathlib import Path

import duckdb

from person3_compliance_audit.deletion.parquet_deletion_adapter import (
    ParquetDeletionAdapter,
)


class TestParquetDeletionAdapter(unittest.TestCase):

    CUSTOMER_REF = "trace-synthetic-202611-0006"

    def setUp(self):
        """Create an isolated temporary copy for every test."""

        self.project_root = Path(__file__).resolve().parents[2]

        self.original_batches = (
            self.project_root
            / "person2_query_interface"
            / "data"
            / "monthly_batches"
        )

        self.temp_dir = tempfile.TemporaryDirectory()

        self.test_batches = (
            Path(self.temp_dir.name) / "monthly_batches"
        )

        shutil.copytree(
            self.original_batches,
            self.test_batches,
        )

        self.adapter = ParquetDeletionAdapter(
            self.test_batches
        )

    def tearDown(self):
        """Remove temporary test data."""

        self.temp_dir.cleanup()

    # -----------------------------------------------------
    # Helper methods
    # -----------------------------------------------------

    def get_record_count(self, batch, table):

        parquet_path = (
            self.test_batches
            / batch
            / f"{table}.parquet"
        )

        connection = duckdb.connect(
            database=":memory:"
        )

        try:
            return connection.execute(
                "SELECT COUNT(*) FROM read_parquet(?)",
                [str(parquet_path)],
            ).fetchone()[0]

        finally:
            connection.close()

    def read_manifest(self, batch):

        manifest_path = (
            self.test_batches
            / batch
            / "batch_manifest.json"
        )

        with manifest_path.open(
            "r", encoding="utf-8"
        ) as file:
            return json.load(file)

    def directory_file_hashes(self, batch):

        batch_dir = self.test_batches / batch

        return {
            path.name: path.read_bytes()
            for path in batch_dir.iterdir()
            if path.is_file()
        }

    # -----------------------------------------------------
    # Test 1: Preview identifies correct records
    # -----------------------------------------------------

    def test_preview_identifies_customer_records(self):

        result = self.adapter.preview_deletion(
            self.CUSTOMER_REF
        )

        self.assertEqual(
            result["customer_ref"],
            self.CUSTOMER_REF,
        )

        self.assertEqual(
            result["prediction_count"],
            1,
        )

        self.assertEqual(
            len(result["prediction_ids"]),
            1,
        )

        self.assertEqual(
            len(result["input_snapshot_ids"]),
            1,
        )

        self.assertEqual(
            len(result["explanation_ids"]),
            1,
        )

    # -----------------------------------------------------
    # Test 2: Delete prediction and linked records
    # -----------------------------------------------------

    def test_deletion_removes_customer_records(self):

        result = self.adapter.delete_customer_records(
            self.CUSTOMER_REF
        )

        self.assertEqual(
            result["status"],
            "VERIFIED",
        )

        self.assertEqual(
            result["deleted_predictions"],
            1,
        )

        self.assertEqual(
            result["deleted_input_snapshots"],
            1,
        )

        self.assertEqual(
            result["deleted_explanations"],
            1,
        )

        preview = self.adapter.preview_deletion(
            self.CUSTOMER_REF
        )

        self.assertEqual(
            preview["prediction_count"],
            0,
        )

    # -----------------------------------------------------
    # Test 3: Other customers' records are preserved
    # -----------------------------------------------------

    def test_other_customer_records_are_preserved(self):

        before = (
            self.get_record_count(
                "2026-11",
                "prediction_events",
            )
        )

        self.adapter.delete_customer_records(
            self.CUSTOMER_REF
        )

        after = (
            self.get_record_count(
                "2026-11",
                "prediction_events",
            )
        )

        self.assertEqual(before - after, 1)

        self.assertEqual(after, 499)

    # -----------------------------------------------------
    # Test 4: Only affected monthly batch is modified
    # -----------------------------------------------------

    def test_only_target_batch_is_modified(self):

        october_before = self.directory_file_hashes(
            "2026-10"
        )

        december_before = self.directory_file_hashes(
            "2026-12"
        )

        result = self.adapter.delete_customer_records(
            self.CUSTOMER_REF
        )

        october_after = self.directory_file_hashes(
            "2026-10"
        )

        december_after = self.directory_file_hashes(
            "2026-12"
        )

        self.assertEqual(
            october_before,
            october_after,
        )

        self.assertEqual(
            december_before,
            december_after,
        )

        self.assertEqual(
            result["modified_batches"],
            ["2026-11"],
        )

    # -----------------------------------------------------
    # Test 5: Model registry records are preserved
    # -----------------------------------------------------

    def test_model_registry_is_preserved(self):

        before = {}

        for batch in (
            "2026-10",
            "2026-11",
            "2026-12",
        ):

            before[batch] = self.get_record_count(
                batch,
                "model_registry",
            )

        self.adapter.delete_customer_records(
            self.CUSTOMER_REF
        )

        for batch in (
            "2026-10",
            "2026-11",
            "2026-12",
        ):

            after = self.get_record_count(
                batch,
                "model_registry",
            )

            self.assertEqual(
                before[batch],
                after,
            )

    # -----------------------------------------------------
    # Test 6: Nonexistent customer returns NOT_FOUND
    # -----------------------------------------------------

    def test_nonexistent_customer_returns_not_found(self):

        result = self.adapter.delete_customer_records(
            "trace-does-not-exist"
        )

        self.assertEqual(
            result["status"],
            "NOT_FOUND",
        )

        self.assertEqual(
            result["deleted_predictions"],
            0,
        )

        self.assertEqual(
            result["modified_batches"],
            [],
        )

    # -----------------------------------------------------
    # Test 7: Manifest counts match Parquet files
    # -----------------------------------------------------

    def test_manifest_counts_match_parquet_files(self):

        self.adapter.delete_customer_records(
            self.CUSTOMER_REF
        )

        for batch in (
            "2026-10",
            "2026-11",
            "2026-12",
        ):

            manifest = self.read_manifest(batch)

            for entry in manifest["entries"]:

                actual_count = self.get_record_count(
                    batch,
                    entry["table_name"],
                )

                self.assertEqual(
                    entry["record_count"],
                    actual_count,
                    msg=(
                        f"Count mismatch in {batch}: "
                        f"{entry['table_name']}"
                    ),
                )

            prediction_count = self.get_record_count(
                batch,
                "prediction_events",
            )

            self.assertEqual(
                manifest["record_count"],
                prediction_count,
            )

    # -----------------------------------------------------
    # Test 8: Empty customer reference is rejected
    # -----------------------------------------------------

    def test_empty_customer_reference_is_rejected(self):

        with self.assertRaises(ValueError):

            self.adapter.preview_deletion("")


if __name__ == "__main__":
    unittest.main()
