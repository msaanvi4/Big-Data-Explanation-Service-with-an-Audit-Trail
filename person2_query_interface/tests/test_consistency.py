"""Person 2: decision-consistency tests for the compliance report.

Same customer + same inputs must always yield the same decision across model
versions and time. Q5 finds input hashes with more than one distinct
decision (violations).
"""

from __future__ import annotations

import duckdb


def test_consistency_query_flags_mixed_decisions_for_same_input():
    con = duckdb.connect(":memory:")
    con.execute(
        """
        CREATE TABLE prediction_events (
            prediction_id STRING, demo_decision STRING, input_snapshot_id STRING, model_version STRING
        );
        CREATE TABLE input_snapshots (
            input_snapshot_id STRING, input_hash STRING
        );
        INSERT INTO input_snapshots VALUES
            ('snap-1', 'hash-duplicate'), ('snap-2', 'hash-duplicate'), ('snap-3', 'hash-unique');
        INSERT INTO prediction_events VALUES
            ('evt-1', 'APPROVE', 'snap-1', 'loan-risk-demo-v1.0.0'),
            ('evt-2', 'DECLINE', 'snap-2', 'loan-risk-demo-v1.1.0'),  -- same inputs, different decision
            ('evt-3', 'APPROVE', 'snap-3', 'loan-risk-demo-v1.0.0');
        """
    )
    sql = """
    SELECT input_hash, COUNT(DISTINCT demo_decision) AS distinct_decisions
    FROM prediction_events p
    JOIN input_snapshots i ON i.input_snapshot_id = p.input_snapshot_id
    GROUP BY input_hash
    HAVING COUNT(DISTINCT demo_decision) > 1;
    """
    violations = con.execute(sql).fetchall()
    assert len(violations) == 1
    assert violations[0][0] == "hash-duplicate"
    assert violations[0][1] == 2


def test_consistency_query_passes_when_decisions_agree():
    con = duckdb.connect(":memory:")
    con.execute(
        """
        CREATE TABLE prediction_events (
            prediction_id STRING, demo_decision STRING, input_snapshot_id STRING, model_version STRING
        );
        CREATE TABLE input_snapshots (
            input_snapshot_id STRING, input_hash STRING
        );
        INSERT INTO input_snapshots VALUES ('snap-1', 'hash-a'), ('snap-2', 'hash-a');
        INSERT INTO prediction_events VALUES
            ('evt-1', 'APPROVE', 'snap-1', 'loan-risk-demo-v1.0.0'),
            ('evt-2', 'APPROVE', 'snap-2', 'loan-risk-demo-v1.1.0');
        """
    )
    sql = """
    SELECT COUNT(DISTINCT demo_decision) AS distinct_decisions
    FROM prediction_events p
    JOIN input_snapshots i ON i.input_snapshot_id = p.input_snapshot_id
    GROUP BY input_hash;
    """
    results = con.execute(sql).fetchall()
    assert results == [(1,)]
