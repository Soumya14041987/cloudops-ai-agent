"""
End-to-end tests for CloudOpsOrchestrator and the Lambda handler.
All agent stages and Bedrock/CloudWatch calls are mocked so this
tests the wiring logic without any real AWS dependencies.
"""

import json
import pytest
from unittest.mock import MagicMock, patch

from app import CloudOpsOrchestrator, lambda_handler


pytestmark = pytest.mark.unit


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def make_orchestrator(
    mock_logs_tool,
    mock_metrics_tool,
    bedrock_triage,
    bedrock_metrics,
    bedrock_hypothesis,
    bedrock_recommendation,
):
    """Build a CloudOpsOrchestrator with fully mocked agents."""
    with patch("app.CloudWatchLogsTool",    return_value=mock_logs_tool), \
         patch("app.CloudWatchMetricsTool", return_value=mock_metrics_tool), \
         patch("boto3.client") as mock_boto:

        def make_bedrock_client(*args, **kwargs):
            mock_client = MagicMock()
            responses = iter([
                bedrock_triage, bedrock_metrics,
                bedrock_hypothesis, bedrock_recommendation,
            ])

            def invoke(*a, **kw):
                payload = next(responses, bedrock_triage)
                body_bytes = json.dumps({
                    "content": [{"type": "text", "text": json.dumps(payload)}],
                }).encode()
                mock_body = MagicMock()
                mock_body.read.return_value = body_bytes
                return {"body": mock_body}

            mock_client.invoke_model.side_effect = invoke
            return mock_client

        mock_boto.side_effect = make_bedrock_client
        return CloudOpsOrchestrator(region_name="us-east-1")


# ─────────────────────────────────────────────────────────────────────────────
# CloudOpsOrchestrator.run()
# ─────────────────────────────────────────────────────────────────────────────

class TestOrchestratorRun:
    def test_returns_success_status(
        self,
        mock_cloudwatch_logs_tool,
        mock_cloudwatch_metrics_tool,
        sample_full_context,
    ):
        orch = make_orchestrator(
            mock_cloudwatch_logs_tool,
            mock_cloudwatch_metrics_tool,
            {"triage_summary": "test", "investigation_plan": [], "key_questions": [],
             "risk_assessment": "low"},
            {"metrics_interpretation": "ok", "likely_bottleneck": "lambda:fn",
             "correlation_clues": [], "confidence_level": "HIGH"},
            {"root_cause_hypothesis": "timeout", "evidence_chain": [],
             "confidence_level": "HIGH", "next_investigation": "none"},
            {"executive_summary": "resolved", "recommended_action": "scale",
             "implementation_order": [], "post_incident_tasks": [],
             "estimated_resolution": "5 min", "confidence_overall": "HIGH"},
        )
        result = orch.run(
            incident_description = "Lambda has 40% error rate",
            resource_hints       = {"lambda": ["payments-processor"]},
        )
        assert result["status"] == "SUCCESS"

    def test_result_has_all_top_level_keys(
        self, mock_cloudwatch_logs_tool, mock_cloudwatch_metrics_tool
    ):
        orch = make_orchestrator(
            mock_cloudwatch_logs_tool, mock_cloudwatch_metrics_tool,
            {"triage_summary": "t", "investigation_plan": [], "key_questions": [],
             "risk_assessment": "low"},
            {"metrics_interpretation": "ok", "likely_bottleneck": "fn",
             "correlation_clues": [], "confidence_level": "MEDIUM"},
            {"root_cause_hypothesis": "h", "evidence_chain": [],
             "confidence_level": "MEDIUM", "next_investigation": "none"},
            {"executive_summary": "s", "recommended_action": "a",
             "implementation_order": [], "post_incident_tasks": [],
             "estimated_resolution": "5 min", "confidence_overall": "MEDIUM"},
        )
        result = orch.run("Lambda errors")
        for key in ("status", "incident_id", "severity",
                    "pipeline_result", "agent_reports", "performance"):
            assert key in result, f"Missing key: {key}"

    def test_performance_timings_recorded(
        self, mock_cloudwatch_logs_tool, mock_cloudwatch_metrics_tool
    ):
        orch = make_orchestrator(
            mock_cloudwatch_logs_tool, mock_cloudwatch_metrics_tool,
            {"triage_summary": "t", "investigation_plan": [], "key_questions": [],
             "risk_assessment": "low"},
            {"metrics_interpretation": "ok", "likely_bottleneck": "fn",
             "correlation_clues": [], "confidence_level": "LOW"},
            {"root_cause_hypothesis": "h", "evidence_chain": [],
             "confidence_level": "LOW", "next_investigation": "none"},
            {"executive_summary": "s", "recommended_action": "a",
             "implementation_order": [], "post_incident_tasks": [],
             "estimated_resolution": "5 min", "confidence_overall": "LOW"},
        )
        result = orch.run("Lambda errors")
        perf = result["performance"]
        assert "total_elapsed_seconds" in perf
        assert "stage_timings" in perf
        for stage in ("incident_agent", "metrics_agent", "log_agent",
                      "remediation_agent"):
            assert stage in perf["stage_timings"]


# ─────────────────────────────────────────────────────────────────────────────
# lambda_handler
# ─────────────────────────────────────────────────────────────────────────────

class TestLambdaHandler:
    def test_returns_200_on_success(
        self, mock_cloudwatch_logs_tool, mock_cloudwatch_metrics_tool
    ):
        with patch("app._get_orchestrator") as mock_orch:
            mock_orch.return_value.run.return_value = {
                "status": "SUCCESS", "incident_id": "INC-001",
                "severity": "HIGH", "pipeline_result": {},
                "agent_reports": {}, "performance": {},
            }
            event = {"body": json.dumps({
                "incident_description": "Lambda has errors",
            })}
            response = lambda_handler(event, MagicMock())
        assert response["statusCode"] == 200

    def test_returns_400_on_missing_description(self):
        event = {"body": json.dumps({})}
        response = lambda_handler(event, MagicMock())
        assert response["statusCode"] == 400
        body = json.loads(response["body"])
        assert "error" in body

    def test_returns_400_on_invalid_json(self):
        event = {"body": "not-json"}
        response = lambda_handler(event, MagicMock())
        assert response["statusCode"] == 400

    def test_handles_direct_invocation(self):
        with patch("app._get_orchestrator") as mock_orch:
            mock_orch.return_value.run.return_value = {
                "status": "SUCCESS", "incident_id": "INC-001",
                "severity": "HIGH", "pipeline_result": {},
                "agent_reports": {}, "performance": {},
            }
            # Direct Lambda invocation (no 'body' key wrapping)
            event = {"incident_description": "Lambda has errors"}
            response = lambda_handler(event, MagicMock())
        assert response["statusCode"] == 200

    def test_cors_header_present(self):
        with patch("app._get_orchestrator") as mock_orch:
            mock_orch.return_value.run.return_value = {
                "status": "SUCCESS", "incident_id": "INC-001",
                "severity": "HIGH", "pipeline_result": {},
                "agent_reports": {}, "performance": {},
            }
            event = {"body": json.dumps({"incident_description": "test"})}
            response = lambda_handler(event, MagicMock())
        assert "Access-Control-Allow-Origin" in response["headers"]

    def test_returns_500_on_orchestrator_error(self):
        with patch("app._get_orchestrator") as mock_orch:
            mock_orch.return_value.run.side_effect = RuntimeError("boom")
            event = {"body": json.dumps({"incident_description": "test"})}
            response = lambda_handler(event, MagicMock())
        assert response["statusCode"] == 500
