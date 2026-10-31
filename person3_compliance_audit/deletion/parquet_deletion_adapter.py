
"""Safe deletion adapter for Person 2's monthly DuckDB/Parquet batches.

This adapter operates on a configurable monthly batch directory.

Important:
- Use a temporary dataset copy during testing.
- This deletes records from current Parquet files only.
- It does not erase historical Iceberg snapshots or backups.
- Shared input and explanation records are preserved.
- Model registry records are never deleted.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import uuid

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import duckdb


class ParquetDeletionError(Exception):
    """Raised when a Parquet deletion operation fails."""


class ParquetDeletionAdapter:

    REQUIRED_TABLES = {
        "prediction_events",
        "input_snapshots",
        "explanation_records",
        "model_registry",
    }

    def __init__(self, batches_dir: str | Path):
        self.batches_dir = Path(batches_dir).resolve()

        if not self.batches_dir.exists():
            raise FileNotFoundError(
                f"Batch directory does not exist: {self.batches_dir}"
            )

        if not self.batches_dir.is_dir():
            raise NotADirectoryError(self.batches_dir)

        self._validate_manifests()

    # ---------------------------------------------------------
    # Utility methods
    # ---------------------------------------------------------

    @staticmethod
    def _sql_string(value: str | Path) -> str:
        """Safely quote a SQL string literal."""
        return "'" + str(value).replace("'", "''") + "'"

    @staticmethod
    def _sql_values(values: set[str]) -> str:
        """Convert identifiers into a SQL IN-list."""
        return ", ".join(
            ParquetDeletionAdapter._sql_string(value)
            for value in sorted(values)
        )

    @staticmethod
    def _now() -> str:
        return (
            datetime.now(timezone.utc)
            .isoformat()
            .replace("+00:00", "Z")
        )

    def _batch_directories(self) -> list[Path]:
        return sorted(
            path
            for path in self.batches_dir.iterdir()
            if path.is_dir()
            and (path / "batch_manifest.json").is_file()
        )

    def _manifest_path(self, batch_dir: Path) -> Path:
        return batch_dir / "batch_manifest.json"

    def _read_manifest(self, batch_dir: Path) -> dict[str, Any]:
        with self._manifest_path(batch_dir).open(
            "r", encoding="utf-8"
        ) as file:
            return json.load(file)

    # ---------------------------------------------------------
    # Manifest validation
    # ---------------------------------------------------------

    def _validate_manifests(self) -> None:
        batch_dirs = self._batch_directories()

        if not batch_dirs:
            raise ParquetDeletionError(
                "No monthly batch manifests were found."
            )

        for batch_dir in batch_dirs:

            manifest = self._read_manifest(batch_dir)
            entries = manifest.get("entries", [])

            tables = {
                entry.get("table_name")
                for entry in entries
            }

            missing = self.REQUIRED_TABLES - tables

            if missing:
                raise ParquetDeletionError(
                    f"{batch_dir.name}: missing tables: "
                    f"{sorted(missing)}"
                )

            for entry in entries:

                if "path" not in entry:
                    raise ParquetDeletionError(
                        "Manifest entry is missing its path."
                    )

                path = (
                    batch_dir / entry["path"]
                ).resolve()

                # Prevent paths from escaping the batch directory.
                if batch_dir.resolve() not in path.parents:
                    raise ParquetDeletionError(
                        f"Unsafe manifest path: {entry['path']}"
                    )

                if not path.is_file():
                    raise FileNotFoundError(
                        f"Parquet file not found: {path}"
                    )

    def _entries(self) -> list[dict[str, Any]]:
        result = []

        for batch_dir in self._batch_directories():

            manifest = self._read_manifest(batch_dir)

            for entry in manifest["entries"]:

                result.append({
                    "batch_dir": batch_dir,
                    "manifest": manifest,
                    "entry": entry,
                    "path": (
                        batch_dir / entry["path"]
                    ).resolve(),
                })

        return result

    # ---------------------------------------------------------
    # Parquet reading
    # ---------------------------------------------------------

    @staticmethod
    def _rows(path: Path) -> list[dict[str, Any]]:

        connection = duckdb.connect(database=":memory:")

        try:

            result = connection.execute(
                f"SELECT * FROM read_parquet("
                f"{ParquetDeletionAdapter._sql_string(path)})"
            )

            columns = [
                item[0]
                for item in result.description
            ]

            return [
                dict(zip(columns, row))
                for row in result.fetchall()
            ]

        finally:
            connection.close()

    def _all_prediction_rows(self) -> list[dict[str, Any]]:

        rows = []

        for item in self._entries():

            if (
                item["entry"]["table_name"]
                == "prediction_events"
            ):
                rows.extend(
                    self._rows(item["path"])
                )

        return rows

    # ---------------------------------------------------------
    # Deletion preview
    # ---------------------------------------------------------

    def preview_deletion(
        self,
        customer_ref: str
    ) -> dict[str, Any]:
        """Identify records without modifying any files."""

        if (
            not isinstance(customer_ref, str)
            or not customer_ref.strip()
        ):
            raise ValueError(
                "customer_ref must be a non-empty string."
            )

        customer_ref = customer_ref.strip()

        all_predictions = self._all_prediction_rows()

        target_predictions = [
            row
            for row in all_predictions
            if row.get("request_trace_id") == customer_ref
        ]

        prediction_ids = {
            str(row["prediction_id"])
            for row in target_predictions
            if row.get("prediction_id") is not None
        }

        input_ids = {
            str(row["input_snapshot_id"])
            for row in target_predictions
            if row.get("input_snapshot_id") is not None
        }

        explanation_ids = {
            str(row["explanation_id"])
            for row in target_predictions
            if row.get("explanation_id") is not None
        }

        # Find predictions that will remain after deletion.
        remaining_predictions = [
            row
            for row in all_predictions
            if str(row.get("prediction_id"))
            not in prediction_ids
        ]

        # Find child records still referenced by other predictions.
        referenced_input_ids = {
            str(row["input_snapshot_id"])
            for row in remaining_predictions
            if row.get("input_snapshot_id") is not None
        }

        referenced_explanation_ids = {
            str(row["explanation_id"])
            for row in remaining_predictions
            if row.get("explanation_id") is not None
        }

        removable_input_ids = (
            input_ids - referenced_input_ids
        )

        removable_explanation_ids = (
            explanation_ids - referenced_explanation_ids
        )

        return {
            "customer_ref": customer_ref,
            "prediction_count": len(prediction_ids),
            "prediction_ids": sorted(prediction_ids),

            "input_snapshot_ids": sorted(
                removable_input_ids
            ),

            "explanation_ids": sorted(
                removable_explanation_ids
            ),

            "input_records_preserved_as_shared": sorted(
                input_ids & referenced_input_ids
            ),

            "explanation_records_preserved_as_shared": sorted(
                explanation_ids & referenced_explanation_ids
            ),
        }

    # ---------------------------------------------------------
    # Parquet rewriting
    # ---------------------------------------------------------

    def _write_filtered_parquet(
        self,
        source_path: Path,
        destination_path: Path,
        column: str,
        values: set[str],
    ) -> None:

        if not values:
            raise ValueError(
                "At least one identifier is required."
            )

        if not column.replace("_", "").isalnum():
            raise ValueError(
                f"Invalid column name: {column}"
            )

        connection = duckdb.connect(database=":memory:")

        try:

            sql = (
                "COPY ("
                "SELECT * FROM read_parquet("
                f"{self._sql_string(source_path)}) "
                f'WHERE CAST("{column}" AS VARCHAR) '
                f"NOT IN ({self._sql_values(values)})"
                ") TO "
                f"{self._sql_string(destination_path)} "
                "(FORMAT PARQUET, COMPRESSION ZSTD)"
            )

            connection.execute(sql)

        finally:
            connection.close()

    # ---------------------------------------------------------
    # File statistics
    # ---------------------------------------------------------

    def _file_statistics(
        self,
        path: Path,
        table_name: str
    ) -> tuple[int, str | None, str | None]:

        timestamp_column = {
            "prediction_events": "decision_ts",
            "input_snapshots": "created_at",
            "explanation_records": "created_at",
        }.get(table_name)

        connection = duckdb.connect(database=":memory:")

        try:

            if timestamp_column:

                query = (
                    "SELECT COUNT(*), "
                    f'MIN("{timestamp_column}"), '
                    f'MAX("{timestamp_column}") '
                    "FROM read_parquet("
                    f"{self._sql_string(path)})"
                )

                count, minimum, maximum = (
                    connection.execute(query).fetchone()
                )

                return (
                    count,
                    minimum.isoformat() + "Z"
                    if minimum else None,
                    maximum.isoformat() + "Z"
                    if maximum else None,
                )

            count = connection.execute(
                "SELECT COUNT(*) FROM read_parquet("
                f"{self._sql_string(path)})"
            ).fetchone()[0]

            return count, None, None

        finally:
            connection.close()

    # ---------------------------------------------------------
    # Main deletion operation
    # ---------------------------------------------------------

    def delete_customer_records(
        self,
        customer_ref: str
    ) -> dict[str, Any]:
        """Delete a customer's current Parquet records.

        Files are staged before replacement. Backups are used to
        restore originals if replacement or verification fails.
        """

        preview = self.preview_deletion(customer_ref)

        if preview["prediction_count"] == 0:

            return {
                "status": "NOT_FOUND",
                "deleted_predictions": 0,
                "deleted_input_snapshots": 0,
                "deleted_explanations": 0,
                "customer_ref": customer_ref,
                "modified_batches": [],
            }

        deletion_sets = {

            "prediction_events": (
                "prediction_id",
                set(preview["prediction_ids"]),
            ),

            "input_snapshots": (
                "input_snapshot_id",
                set(preview["input_snapshot_ids"]),
            ),

            "explanation_records": (
                "explanation_id",
                set(preview["explanation_ids"]),
            ),
        }

        staged_files: dict[Path, Path] = {}
        staged_manifests: dict[Path, Path] = {}
        backups: dict[Path, Path] = {}

        modified_batches: set[Path] = set()

        try:

            # -------------------------------------------------
            # Stage only files containing target records.
            # -------------------------------------------------

            for item in self._entries():

                table_name = item["entry"]["table_name"]

                if table_name not in deletion_sets:
                    continue

                column, values = deletion_sets[table_name]

                if not values:
                    continue

                source = item["path"]
                batch_dir = item["batch_dir"]

                # IMPORTANT FIX:
                # Do not rewrite a batch if it has no matching IDs.
                existing_rows = self._rows(source)

                matching_rows = [
                    row
                    for row in existing_rows
                    if str(row.get(column)) in values
                ]

                if not matching_rows:
                    continue

                fd, temp_name = tempfile.mkstemp(
                    prefix=f".{source.stem}_",
                    suffix=".parquet",
                    dir=batch_dir,
                )

                os.close(fd)

                staged = Path(temp_name)

                try:

                    self._write_filtered_parquet(
                        source,
                        staged,
                        column,
                        values,
                    )

                except Exception:

                    staged.unlink(missing_ok=True)
                    raise

                staged_files[source] = staged
                modified_batches.add(batch_dir)

            if not staged_files:
                raise ParquetDeletionError(
                    "No matching Parquet files were staged."
                )

            # -------------------------------------------------
            # Stage updated manifests for affected batches.
            # -------------------------------------------------

            for batch_dir in modified_batches:

                manifest_path = self._manifest_path(batch_dir)

                manifest = self._read_manifest(batch_dir)

                new_snapshot_id = str(uuid.uuid4())
                written_at = self._now()

                prediction_count = None

                for entry in manifest["entries"]:

                    table_name = entry["table_name"]

                    parquet_path = (
                        batch_dir / entry["path"]
                    ).resolve()

                    effective_path = staged_files.get(
                        parquet_path,
                        parquet_path,
                    )

                    count, minimum, maximum = (
                        self._file_statistics(
                            effective_path,
                            table_name,
                        )
                    )

                    entry["record_count"] = count
                    entry["min_ts"] = minimum
                    entry["max_ts"] = maximum

                    entry["snapshot_id"] = new_snapshot_id
                    entry["written_at"] = written_at

                    if table_name == "prediction_events":
                        prediction_count = count

                manifest["record_count"] = prediction_count
                manifest["snapshot_id"] = new_snapshot_id
                manifest["written_at"] = written_at

                fd, temp_name = tempfile.mkstemp(
                    prefix=".batch_manifest_",
                    suffix=".json",
                    dir=batch_dir,
                )

                os.close(fd)

                staged_manifest = Path(temp_name)

                with staged_manifest.open(
                    "w", encoding="utf-8"
                ) as file:

                    json.dump(
                        manifest,
                        file,
                        indent=2,
                    )

                    file.write("\n")

                staged_manifests[manifest_path] = (
                    staged_manifest
                )

            # -------------------------------------------------
            # Create backups before replacing original files.
            # -------------------------------------------------

            for original in (
                list(staged_files.keys())
                + list(staged_manifests.keys())
            ):

                fd, backup_name = tempfile.mkstemp(
                    prefix=".deletion_backup_",
                    dir=original.parent,
                )

                os.close(fd)

                backup = Path(backup_name)

                shutil.copy2(original, backup)

                backups[original] = backup

            # -------------------------------------------------
            # Replace Parquet files and manifests.
            # -------------------------------------------------

            for original, staged in staged_files.items():
                os.replace(staged, original)

            for original, staged in staged_manifests.items():
                os.replace(staged, original)

            # -------------------------------------------------
            # Verify prediction deletion.
            # -------------------------------------------------

            remaining = [
                row
                for row in self._all_prediction_rows()
                if row.get("request_trace_id") == customer_ref
            ]

            if remaining:
                raise ParquetDeletionError(
                    "Verification failed: target predictions remain."
                )

            # -------------------------------------------------
            # Verify linked child record deletion.
            # -------------------------------------------------

            for item in self._entries():

                table_name = item["entry"]["table_name"]

                if table_name == "input_snapshots":

                    column = "input_snapshot_id"

                    values = set(
                        preview["input_snapshot_ids"]
                    )

                elif table_name == "explanation_records":

                    column = "explanation_id"

                    values = set(
                        preview["explanation_ids"]
                    )

                else:
                    continue

                if not values:
                    continue

                rows = self._rows(item["path"])

                if any(
                    str(row.get(column)) in values
                    for row in rows
                ):

                    raise ParquetDeletionError(
                        f"Verification failed in {table_name}."
                    )

            # -------------------------------------------------
            # Verify manifest counts.
            # -------------------------------------------------

            self._validate_manifests()

            for item in self._entries():

                count, _, _ = self._file_statistics(
                    item["path"],
                    item["entry"]["table_name"],
                )

                if count != item["entry"]["record_count"]:

                    raise ParquetDeletionError(
                        f"Manifest count mismatch: {item['path']}"
                    )

            # -------------------------------------------------
            # Return successful result.
            # -------------------------------------------------

            return {
                "status": "VERIFIED",

                "deleted_predictions": (
                    preview["prediction_count"]
                ),

                "deleted_input_snapshots": len(
                    preview["input_snapshot_ids"]
                ),

                "deleted_explanations": len(
                    preview["explanation_ids"]
                ),

                "preserved_shared_inputs": len(
                    preview[
                        "input_records_preserved_as_shared"
                    ]
                ),

                "preserved_shared_explanations": len(
                    preview[
                        "explanation_records_preserved_as_shared"
                    ]
                ),

                "modified_batches": sorted(
                    batch.name
                    for batch in modified_batches
                ),
            }

        except Exception as error:

            # -------------------------------------------------
            # Roll back replaced files if an operation fails.
            # -------------------------------------------------

            rollback_errors = []

            for original, backup in backups.items():

                try:

                    if backup.exists():
                        os.replace(backup, original)

                except Exception as rollback_error:

                    rollback_errors.append(
                        str(rollback_error)
                    )

            if rollback_errors:

                raise ParquetDeletionError(
                    f"Deletion failed: {error}. "
                    f"Rollback errors: {rollback_errors}"
                ) from error

            raise ParquetDeletionError(
                "Deletion failed and original files "
                f"were restored: {error}"
            ) from error

        finally:

            # Remove leftover staging and backup files.
            for path in staged_files.values():
                path.unlink(missing_ok=True)

            for path in staged_manifests.values():
                path.unlink(missing_ok=True)

            for path in backups.values():
                path.unlink(missing_ok=True)
