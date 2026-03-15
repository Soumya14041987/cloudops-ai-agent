"""
Tests for LogAgent (Stage 3), LogPatternLibrary, and LogCorrelationEngine.
CloudWatch Logs calls are mocked via the injected logs_tool fixture.
"""

import json
import pytest
from unittest.mock import MagicMock

from agents.log_agent import LogAgent, LogPatternLibrary, LogCorrelationEngine


pytestmark = pytest.mark.unit


# ─────────────────────────────────────────────────────────────────────────────
# LogPatternLibrary
# ─────────────────────────────────────────────────────────────────────────────

class TestLogPatternLibrary:
    lib = LogPatternLibrary()

    def test_matches_lambda_timeout(self):
        matches = self.lib.match("Task timed out after 15.00 seconds")
        names = [m["pattern_name"] for m in matches]
        assert "Lambda Timeout" in names

    def test_matches_connection_refused(self):
        matches = self.lib.match("Connection refused to 10.0.0.5:5432")
        names = [m["pattern_name"] for m in matches]
        assert "Connection Refused" in names

    def test_matches_throttling(self):
        matches = self.lib.match("ThrottlingException: Rate exceeded")
        names = [m["pattern_name"] for m in matches]
        assert "Throttling" in names

    def test_matches_permission_denied(self):
        matches = self.lib.match("AccessDenied: Not authorized to perform action")
        names = [m["pattern_name"] for m in matches]
        assert "Permission Denied" in names

    def test_returns_empty_for_clean_log(self):
        matches = self.lib.match("INFO: Request completed successfully in 120ms")
        assert matches == []

    def test_returns_severity_and_category(self):
        matches = self.lib.match("Task timed out after 15.00 seconds")
        assert matches[0]["severity"] == "CRITICAL"
        assert matches[0]["category"] == "TIMEOUT"


# ─────────────────────────────────────────────────────────────────────────────
# LogCorrelationEngine
# ─────────────────────────────────────────────────────────────────────────────

class TestLogCorrelationEngine:
    engine = LogCorrelationEngine()

    def test_produces_correlations(self):
        patterns = [{"pattern_name": "Lambda Timeout", "category": "TIMEOUT",
                     "severity": "CRITICAL", "resource": "lambda:fn"}]
        anomalies = [{"metric": "lambda_duration", "description": "Duration spike",
                      "severity": "WARNING"}]
        results = self.engine.correlate(patterns, anomalies)
        assert len(results) >= 1
        assert results[0]["correlation_strength"] > 0

    def test_higher_strength_with_matching_anomaly(self):
        patterns  = [{"pattern_name": "Throttling", "category": "THROTTLING",
                      "severity": "HIGH", "resource": "lambda:fn"}]
        anomalies = [{"metric": "lambda_throttles", "description": "Throttle spike",
                      "severity": "HIGH"}]
        results = self.engine.correlate(patterns, anomalies)
        assert results[0]["correlation_strength"] > 0.5

    def test_deduplicates_same_pattern(self):
        patterns = [
            {"pattern_name": "Lambda Timeout", "category": "TIMEOUT",
             "severity": "CRITICAL", "resource": "lambda:fn"},
            {"pattern_name": "Lambda Timeout", "category": "TIMEOUT",
             "severity": "CRITICAL", "resource": "lambda:fn"},
        ]
        results = self.engine.correlate(patterns, [])
        assert len(results) == 1


# ─────────────────────────────────────────────────────────────────────────────
# LogAgent
# ─────────────────────────────────────────────────────────────────────────────

def make_log_agent(logs_tool, bedrock_response: dict | None = None):
    agent = LogAgent.__new__(LogAgent)
    agent.logs_tool   = logs_tool
    agent.model_id    = "anthropic.claude-3-sonnet-20240229-v1:0"
    agent.region      = "us-east-1"
    agent.pattern_lib = LogPatternLibrary()
    agent.correlator  = LogCorrelationEngine()

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


class TestLogAgent:
    def test_returns_enriched_context(
        self, sample_incident_with_metrics, mock_cloudwatch_logs_tool
    ):
        agent = make_log_agent(mock_cloudwatch_logs_tool)
        result = agent.analyze(sample_incident_with_metrics)
        assert "log_report" in result

    def test_log_report_required_keys(
        self, sample_incident_with_metrics, mock_cloudwatch_logs_tool
    ):
        agent = make_log_agent(mock_cloudwatch_logs_tool)
        result = agent.analyze(sample_incident_with_metrics)
        report = result["log_report"]
        for key in ("resources_analyzed", "total_error_events", "top_error_patterns",
                    "root_cause_hypothesis", "summary"):
            assert key in report, f"Missing key: {key}"

    def test_error_timeline_is_sorted(
        self, sample_incident_with_metrics, mock_cloudwatch_logs_tool
    ):
        agent = make_log_agent(mock_cloudwatch_logs_tool)
        result = agent.analyze(sample_incident_with_metrics)
        timeline = result["log_report"]["error_timeline"]
        if len(timeline) > 1:
            timestamps = [t["minute"] for t in timeline]
            assert timestamps == sorted(timestamps)

    def test_original_context_preserved(
        self, sample_incident_with_metrics, mock_cloudwatch_logs_tool
    ):
        agent = make_log_agent(mock_cloudwatch_logs_tool)
        result = agent.analyze(sample_incident_with_metrics)
        assert result["incident_id"] == sample_incident_with_metrics["incident_id"]

    def test_graceful_on_logs_tool_error(self, sample_incident_with_metrics):
        from tools.cloudwatch_logs import CloudWatchLogsError
        broken_tool = MagicMock()
        broken_tool.get_log_statistics.side_effect = CloudWatchLogsError("timeout")
        broken_tool.get_error_logs.side_effect     = CloudWatchLogsError("timeout")
        agent = make_log_agent(broken_tool)
        result = agent.analyze(sample_incident_with_metrics)
        assert "log_report" in result

    def test_bedrock_fallback_on_failure(
        self, sample_incident_with_metrics, mock_cloudwatch_logs_tool
    ):
        agent = make_log_agent(mock_cloudwatch_logs_tool, bedrock_response=None)
        result = agent.analyze(sample_incident_with_metrics)
        hyp = json.loads(result["log_report"]["root_cause_hypothesis"])
        assert "confidence_level" in hyp
