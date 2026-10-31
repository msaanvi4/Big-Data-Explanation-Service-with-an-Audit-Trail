
import tempfile
import unittest

from pathlib import Path
from types import SimpleNamespace

from audit.audit_logger import AuditLogger
from compliance.compliance_report import ComplianceReportGenerator


class FakeRepository:

    def list_prediction_ids(self):
        return ["prediction_001", "prediction_002"]

    def get_decision_bundle(self, prediction_id):

        return SimpleNamespace(
            prediction=SimpleNamespace(
                request_trace_id="trace_" + prediction_id
            )
        )

    def audit_completeness(self, request_trace_id):

        prediction_id = request_trace_id.replace("trace_", "")

        return [{
            "prediction_id": prediction_id,
            "input_status": "OK",
            "explanation_status": "OK",
            "model_status": "OK"
        }]


class FakeReproducibilityEngine:

    def consistency_report(self):

        return {
            "consistent": True,
            "violation_count": 0,
            "violations": []
        }

    def replay_prediction(self, prediction_id):

        return SimpleNamespace(
            replayed=True,
            score_match=True,
            decision_match=True,
            explanation_match=True,
            input_hash_verified=True,
            explanation_hash_verified=True
        )


class TestComplianceReport(unittest.TestCase):

    def setUp(self):

        self.temp_dir = tempfile.TemporaryDirectory()

        db_path = Path(self.temp_dir.name) / "compliance_test.db"

        self.logger = AuditLogger(db_path)

        self.repository = FakeRepository()

        self.engine = FakeReproducibilityEngine()

        self.generator = ComplianceReportGenerator(
            repository=self.repository,
            reproducibility_engine=self.engine,
            audit_logger=self.logger
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_consistency_check(self):

        result = self.generator.check_consistency()

        self.assertTrue(result["consistent"])
        self.assertEqual(result["violation_count"], 0)

    def test_reproducibility_check(self):

        result = self.generator.check_reproducibility()

        self.assertEqual(result["total_predictions"], 2)
        self.assertEqual(result["successful_replays"], 2)
        self.assertTrue(result["all_reproducible"])

    def test_audit_completeness_check(self):

        result = self.generator.check_audit_completeness()

        self.assertEqual(result["complete_predictions"], 2)
        self.assertTrue(result["all_complete"])

    def test_audit_integrity_check(self):

        result = self.generator.check_audit_integrity()

        self.assertTrue(result["valid"])

    def test_access_control_summary(self):

        self.logger.log_event(
            actor_id="customer_001",
            actor_role="CUSTOMER",
            action="VIEW_DECISION",
            purpose="View own decision",
            customer_ref="customer_001",
            status="SUCCESS"
        )

        self.logger.log_event(
            actor_id="customer_001",
            actor_role="CUSTOMER",
            action="VIEW_DECISION",
            purpose="Unauthorized access",
            customer_ref="customer_002",
            status="DENIED"
        )

        result = self.generator.access_control_summary()

        self.assertEqual(result["total_access_events"], 2)
        self.assertEqual(result["successful_access"], 1)
        self.assertEqual(result["denied_access"], 1)

    def test_complete_compliance_report(self):

        report = self.generator.generate_report()

        self.assertEqual(
            report["overall_status"],
            "COMPLIANT"
        )

        self.assertTrue(
            all(report["checks"].values())
        )

        self.assertIn(
            "decision_consistency",
            report["checks"]
        )

        self.assertIn(
            "audit_trail_integrity",
            report["checks"]
        )

    def test_empty_predictions_are_not_compliant(self):

        self.repository.list_prediction_ids = lambda: []

        report = self.generator.generate_report()

        self.assertEqual(
            report["overall_status"],
            "NON_COMPLIANT"
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
