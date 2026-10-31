
import tempfile
import unittest

from pathlib import Path

from custody.chain_of_custody import ChainOfCustody


class TestChainOfCustody(unittest.TestCase):

    def setUp(self):

        self.temp_dir = tempfile.TemporaryDirectory()

        db_path = Path(self.temp_dir.name) / "custody_test.db"

        self.custody = ChainOfCustody(db_path)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_record_single_event(self):

        event = self.custody.record_event(
            prediction_id="prediction_001",
            event_type="PREDICTION_CREATED",
            actor_id="service_001",
            actor_role="SYSTEM"
        )

        self.assertEqual(
            event["event_type"],
            "PREDICTION_CREATED"
        )

        self.assertEqual(
            len(event["record_hash"]),
            64
        )

    def test_record_complete_prediction_lifecycle(self):

        events = self.custody.record_prediction_lifecycle(
            prediction_id="prediction_001",
            actor_id="service_001",
            actor_role="SYSTEM",
            input_snapshot={
                "loan_amount": 50000,
                "credit_score": 720
            },
            model_version="model_v1",
            explanation={
                "reason": "Synthetic explanation"
            }
        )

        self.assertEqual(len(events), 5)

        self.assertEqual(
            events[0]["event_type"],
            "PREDICTION_CREATED"
        )

        self.assertEqual(
            events[-1]["event_type"],
            "PREDICTION_STORED"
        )

    def test_prediction_history_retrieval(self):

        self.custody.record_prediction_lifecycle(
            prediction_id="prediction_001",
            actor_id="service_001",
            actor_role="SYSTEM",
            input_snapshot={"feature": 10},
            model_version="model_v1",
            explanation={"reason": "Synthetic"}
        )

        history = self.custody.get_prediction_history(
            "prediction_001"
        )

        self.assertEqual(len(history), 5)

        self.assertEqual(
            history[2]["event_type"],
            "MODEL_APPLIED"
        )

    def test_hash_chain_verification(self):

        self.custody.record_prediction_lifecycle(
            prediction_id="prediction_001",
            actor_id="service_001",
            actor_role="SYSTEM",
            input_snapshot={"feature": 10},
            model_version="model_v1",
            explanation={"reason": "Synthetic"}
        )

        result = self.custody.verify_prediction_chain(
            "prediction_001"
        )

        self.assertTrue(result["valid"])
        self.assertEqual(result["checked_events"], 5)

    def test_different_predictions_have_separate_chains(self):

        self.custody.record_event(
            prediction_id="prediction_001",
            event_type="PREDICTION_CREATED",
            actor_id="service_001",
            actor_role="SYSTEM"
        )

        self.custody.record_event(
            prediction_id="prediction_002",
            event_type="PREDICTION_CREATED",
            actor_id="service_001",
            actor_role="SYSTEM"
        )

        history = self.custody.get_prediction_history(
            "prediction_002"
        )

        self.assertEqual(len(history), 1)

        self.assertEqual(
            history[0]["previous_hash"],
            "0" * 64
        )

    def test_artifact_hash_generation(self):

        digest = self.custody.calculate_artifact_hash(
            {"loan_amount": 50000}
        )

        self.assertEqual(len(digest), 64)

    def test_invalid_event_type_rejected(self):

        with self.assertRaises(ValueError):

            self.custody.record_event(
                prediction_id="prediction_001",
                event_type="UNKNOWN_EVENT",
                actor_id="service_001",
                actor_role="SYSTEM"
            )

    def test_missing_prediction_id_rejected(self):

        with self.assertRaises(ValueError):

            self.custody.record_event(
                prediction_id="",
                event_type="PREDICTION_CREATED",
                actor_id="service_001",
                actor_role="SYSTEM"
            )

    def test_tampering_is_detected(self):

        event = self.custody.record_event(
            prediction_id="prediction_001",
            event_type="PREDICTION_CREATED",
            actor_id="service_001",
            actor_role="SYSTEM"
        )

        with self.custody.get_connection() as conn:

            conn.execute("""
                UPDATE custody_events
                SET actor_id = ?
                WHERE event_id = ?
            """, (
                "modified_actor",
                event["event_id"]
            ))

        result = self.custody.verify_prediction_chain(
            "prediction_001"
        )

        self.assertFalse(result["valid"])
        self.assertEqual(
            result["error"],
            "Record hash mismatch"
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
