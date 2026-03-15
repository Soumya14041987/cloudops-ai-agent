"""
Tests for MetricsAgent (Stage 2) and AnomalyDetector.
CloudWatch calls are mocked via the injected metrics_tool fixture.
"""

import json
import pytest
from unittest.mock import MagicMock

from agents.metrics_agent import MetricsAgent, AnomalyDetector


pytestmark = pytest.mark.unit


# ─────────────────────────────────────────────────────────────────────────────
# AnomalyDetector
# ─────────────────────────────────────────────────────────────────────────────

class TestAnomalyDetector:
    detector = AnomalyDetector()

    def test_no_anomaly_on_stable_series(self):
        values = [10.0, 11.0, 10.5, 10.8, 11.2, 10.3]
        result = self.detector.detect(values, "cpu")
        assert result["is_anomalous"] is False

    def test_detects_high_cpu_threshold(self):
        values = [30.0, 35.0, 32.0, 91.0]
        result = self.detector.detect(values, "ec2_cpu")
        assert result["is_anomalous"] is True
        assert result["anomaly_type"] == "THRESHOLD_BREACH"

    def test_detects_z_score_outlier(self):
        values = [5.0, 5.1, 5.2, 4.9, 5.0, 45.0]
        result = self.detector.detect(values, "latency")
        assert result["is_anomalous"] is True

    def test_handles_insufficient_data(self):
        result = self.detector.detect([], "cpu")
        assert result["is_anomalous"] is False
        assert result["datapoints"] == 0

    def test_anomaly_score_between_0_and_1(self):
        values = [10.0, 10.0, 10.0, 99.0]
        result = self.detector.detect(values, "ec2_cpu")
        assert 0.0 <= result["anomaly_score"] <= 1.0


# ─────────────────────────────────────────────────────────────────────────────
# MetricsAgent
# ─────────────────────────────────────────────────────────────────────────────

def make_metrics_agent(metrics_tool, bedrock_response: dict | None = None):
    agent = MetricsAgent.__new__(MetricsAgent)
    agent.metrics_tool = metrics_tool
    agent.model_id     = "anthropic.claude-3-sonnet-20240229-v1:0"
    agent.region       = "us-east-1"
    agent.detector     = AnomalyDetector()

    mock_client = MagicMock()
    if bedrock_response:
        body_bytes = json.dumps({
            "content": [{"type": "text", "text": json.dumps(bedrock_response)}],
        }).encode()
        mock_body = MagicMock()
        mock_body.read.return_value = body_bytes
        mock_client.invoke_model.return_value = {"body": mock_body}
    else:
        mock_client.invoke_model.side_effect = Exception("Bedrock unavailable")
    agent._bedrock = mock_client
    return agent


class TestMetricsAgent:
    def test_returns_enriched_context(
        self, sample_incident, mock_cloudwatch_metrics_tool
    ):
        agent = make_metrics_agent(mock_cloudwatch_metrics_tool)
        result = agent.analyze(sample_incident)
        assert "metrics_report" in result

    def test_metrics_report_has_required_keys(
        self, sample_incident, mock_cloudwatch_metrics_tool
    ):
        agent = make_metrics_agent(mock_cloudwatch_metrics_tool)
        result = agent.analyze(sample_incident)
        report = result["metrics_report"]
        for key in ("overall_health", "resources_checked", "anomalies", "summary"):
            assert key in report, f"Missing key: {key}"

    def test_detects_critical_lambda(
        self, sample_incident, mock_cloudwatch_metrics_tool
    ):
        agent = make_metrics_agent(mock_cloudwatch_metrics_tool)
        result = agent.analyze(sample_incident)
        assert result["metrics_report"]["overall_health"] == "CRITICAL"

    def test_anomalies_list_populated(
        self, sample_incident, mock_cloudwatch_metrics_tool
    ):
        agent = make_metrics_agent(mock_cloudwatch_metrics_tool)
        result = agent.analyze(sample_incident)
        assert len(result["metrics_report"]["anomalies"]) > 0

    def test_original_context_preserved(
        self, sample_incident, mock_cloudwatch_metrics_tool
    ):
        agent = make_metrics_agent(mock_cloudwatch_metrics_tool)
        result = agent.analyze(sample_incident)
        assert result["incident_id"] == sample_incident["incident_id"]
        assert result["severity"]    == sample_incident["severity"]

    def test_graceful_on_metrics_tool_error(
        self, sample_incident
    ):
        from tools.cloudwatch_metrics import CloudWatchMetricsError
        broken_tool = MagicMock()
        broken_tool.get_lambda_health.side_effect = CloudWatchMetricsError("timeout")
        broken_tool.get_ec2_health.side_effect    = CloudWatchMetricsError("timeout")
        broken_tool.get_rds_health.side_effect    = CloudWatchMetricsError("timeout")
        agent = make_metrics_agent(broken_tool)
        result = agent.analyze(sample_incident)
        # Should not raise; report should still exist
        assert "metrics_report" in result

    def test_bedrock_fallback_on_failure(
        self, sample_incident, mock_cloudwatch_metrics_tool
    ):
        agent = make_metrics_agent(mock_cloudwatch_metrics_tool, bedrock_response=None)
        result = agent.analyze(sample_incident)
        interp = json.loads(result["metrics_report"]["interpretation"])
        assert "confidence_level" in interp
