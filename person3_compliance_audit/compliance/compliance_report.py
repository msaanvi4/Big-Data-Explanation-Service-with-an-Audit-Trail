
"""
Person 3: Compliance Report Generator

Combines:
1. Person 2's decision consistency report.
2. Person 2's prediction replay results.
3. Person 2's audit completeness checks.
4. Person 3's audit hash-chain verification.
5. Person 3's access-control audit statistics.

This module uses dependency injection so it can work with
Person 2's existing repository and reproducibility engine.
"""

from datetime import datetime, timezone


class ComplianceReportGenerator:

    def __init__(
        self,
        repository,
        reproducibility_engine,
        audit_logger
    ):
        self.repository = repository
        self.engine = reproducibility_engine
        self.audit_logger = audit_logger

    # --------------------------------------------------
    # 1. CHECK DECISION CONSISTENCY
    # --------------------------------------------------

    def check_consistency(self):
        """
        Use Person 2's existing consistency_report().
        """

        result = self.engine.consistency_report()

        return {
            "consistent": result["consistent"],
            "violation_count": result["violation_count"],
            "violations": result["violations"]
        }

    # --------------------------------------------------
    # 2. CHECK PREDICTION REPRODUCIBILITY
    # --------------------------------------------------

    def check_reproducibility(self):
        """
        Replay every prediction using Person 2's engine.

        Checks:
        - Decision match
        - Score match
        - Explanation match
        - Input hash verification
        - Explanation hash verification
        """

        prediction_ids = self.repository.list_prediction_ids()

        results = []

        for prediction_id in prediction_ids:

            try:
                replay = self.engine.replay_prediction(prediction_id)

                results.append({
                    "prediction_id": prediction_id,
                    "replayed": replay.replayed,
                    "score_match": replay.score_match,
                    "decision_match": replay.decision_match,
                    "explanation_match": replay.explanation_match,
                    "input_hash_verified": replay.input_hash_verified,
                    "explanation_hash_verified":
                        replay.explanation_hash_verified,
                    "error": None
                })

            except Exception as exc:

                results.append({
                    "prediction_id": prediction_id,
                    "replayed": False,
                    "score_match": False,
                    "decision_match": False,
                    "explanation_match": False,
                    "input_hash_verified": False,
                    "explanation_hash_verified": False,
                    "error": str(exc)
                })

        successful = sum(
            1 for item in results if item["replayed"]
        )

        return {
            "total_predictions": len(results),
            "successful_replays": successful,
            "failed_replays": len(results) - successful,
            "all_reproducible": (
                len(results) > 0 and successful == len(results)
            ),
            "results": results
        }

    # --------------------------------------------------
    # 3. CHECK AUDIT COMPLETENESS
    # --------------------------------------------------

    def check_audit_completeness(self):
        """
        Use Person 2's existing audit_completeness().

        Every prediction must resolve to:
        - An input snapshot
        - An explanation
        - A model registry record
        """

        prediction_ids = self.repository.list_prediction_ids()

        results = []

        for prediction_id in prediction_ids:

            try:
                bundle = self.repository.get_decision_bundle(
                    prediction_id
                )

                request_trace_id = (
                    bundle.prediction.request_trace_id
                )

                completeness = self.repository.audit_completeness(
                    request_trace_id
                )

                if not completeness:
                    results.append({
                        "prediction_id": prediction_id,
                        "complete": False,
                        "details": [],
                        "error": "No completeness record returned"
                    })
                    continue

                matching = [
                    row for row in completeness
                    if row.get("prediction_id") == prediction_id
                ]

                if not matching:
                    results.append({
                        "prediction_id": prediction_id,
                        "complete": False,
                        "details": completeness,
                        "error": "Prediction missing from completeness result"
                    })
                    continue

                row = matching[0]

                required_fields = (
                    "input_status",
                    "explanation_status",
                    "model_status"
                )

                complete = all(
                    row.get(field) == "OK"
                    for field in required_fields
                )

                results.append({
                    "prediction_id": prediction_id,
                    "complete": complete,
                    "details": row,
                    "error": None
                })

            except Exception as exc:

                results.append({
                    "prediction_id": prediction_id,
                    "complete": False,
                    "details": {},
                    "error": str(exc)
                })

        complete_count = sum(
            1 for item in results if item["complete"]
        )

        return {
            "total_predictions": len(results),
            "complete_predictions": complete_count,
            "incomplete_predictions": len(results) - complete_count,
            "all_complete": (
                len(results) > 0 and complete_count == len(results)
            ),
            "results": results
        }

    # --------------------------------------------------
    # 4. CHECK AUDIT TRAIL INTEGRITY
    # --------------------------------------------------

    def check_audit_integrity(self):
        """
        Verify Person 3's SHA-256 audit hash chain.
        """

        return self.audit_logger.verify_chain()

    # --------------------------------------------------
    # 5. ACCESS CONTROL SUMMARY
    # --------------------------------------------------

    def access_control_summary(self):
        """
        Summarize successful and denied access attempts.

        Uses Person 3's audit database.
        """

        events = self.audit_logger.get_events()

        successful = sum(
            1 for event in events
            if event["status"] == "SUCCESS"
        )

        denied = sum(
            1 for event in events
            if event["status"] == "DENIED"
        )

        failed = sum(
            1 for event in events
            if event["status"] == "FAILED"
        )

        role_counts = {}

        for event in events:

            role = event["actor_role"]

            role_counts[role] = role_counts.get(role, 0) + 1

        return {
            "total_access_events": len(events),
            "successful_access": successful,
            "denied_access": denied,
            "failed_access": failed,
            "role_wise_events": role_counts
        }

    # --------------------------------------------------
    # 6. GENERATE COMPLETE COMPLIANCE REPORT
    # --------------------------------------------------

    def generate_report(self):
        """
        Generate the final compliance report.

        Overall status is COMPLIANT only when:
        - Decision consistency passes.
        - Every prediction is reproducible.
        - Every prediction has complete audit records.
        - Audit hash chain is valid.
        """

        consistency = self.check_consistency()

        reproducibility = self.check_reproducibility()

        completeness = self.check_audit_completeness()

        integrity = self.check_audit_integrity()

        access_summary = self.access_control_summary()

        checks = {
            "decision_consistency": consistency["consistent"],
            "prediction_reproducibility":
                reproducibility["all_reproducible"],
            "audit_completeness": completeness["all_complete"],
            "audit_trail_integrity": integrity["valid"]
        }

        overall_compliant = (
            len(reproducibility["results"]) > 0
            and all(checks.values())
        )

        recommendations = []

        if not consistency["consistent"]:
            recommendations.append(
                "Investigate inconsistent decisions for identical input hashes."
            )

        if not reproducibility["all_reproducible"]:
            recommendations.append(
                "Investigate failed prediction replays and hash mismatches."
            )

        if not completeness["all_complete"]:
            recommendations.append(
                "Resolve missing input snapshots, explanations or model records."
            )

        if not integrity["valid"]:
            recommendations.append(
                "Investigate audit trail integrity failure immediately."
            )

        if not recommendations and overall_compliant:
            recommendations.append(
                "All evaluated compliance checks passed."
            )

        return {
            "report_title": "Explanation Service Compliance Report",

            "generated_at": datetime.now(
                timezone.utc
            ).isoformat(),

            "overall_status": (
                "COMPLIANT"
                if overall_compliant
                else "NON_COMPLIANT"
            ),

            "checks": checks,

            "decision_consistency": consistency,

            "prediction_reproducibility": reproducibility,

            "audit_completeness": completeness,

            "audit_trail_integrity": integrity,

            "access_control_summary": access_summary,

            "recommendations": recommendations
        }
