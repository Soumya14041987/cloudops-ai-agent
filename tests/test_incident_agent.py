"""
Tests for IncidentAgent (Stage 1).
All Bedrock calls are mocked — no real AWS credentials needed.
"""

import json
import pytest
from unittest.mock import MagicMock, patch

from agents.incident_agent import IncidentAgent


pytestmark = pytest.mark.unit


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def make_agent(bedrock_response: dict | None = None) -> IncidentAgent:
    """Return an IncidentAgent whose Bedrock client is mocked."""
    agent = IncidentAgent.__new__(IncidentAgent)
    agent.model_id   = "anthropic.claude-3-sonnet-20240229-v1:0"
    agent.region     = "us-east-1"
    agent.max_tokens = 1024

    mock_client = MagicMock()
    if bedrock_response is not None:
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


# ─────────────────────────────────────────────────────────────────────────────
# Severity detection
# ─────────────────────────────────────────────────────────────────────────────

class TestSeverityExtraction:
    def test_detects_critical(self):
        agent = make_agent()
        assert agent._extract_severity("CRITICAL outage on payment service") == "CRITICAL"

    def test_detects_high_latency(self):
        agent = make_agent()
        assert agent._extract_severity("high latency observed on API") == "HIGH"

    def test_detects_medium(self):
        agent = make_agent()
        assert agent._extract_severity("intermittent errors on auth service") == "MEDIUM"

    def test_defaults_to_medium(self):
        agent = make_agent()
        assert agent._extract_severity("something is off") == "MEDIUM"

    def test_pager_levels(self):
        agent = make_agent()
        assert agent._extract_severity("P0 incident declared") == "CRITICAL"
        assert agent._extract_severity("SEV2 issue detected")  == "HIGH"


# ─────────────────────────────────────────────────────────────────────────────
# Resource extraction
# ─────────────────────────────────────────────────────────────────────────────

class TestResourceExtraction:
    def test_detects_lambda(self):
        agent = make_agent()
        resources = agent._extract_resources(
            "Lambda function payments-processor has errors", {}
        )
        assert "lambda" in resources

    def test_detects_rds(self):
        agent = make_agent()
        resources = agent._extract_resources("RDS database is at 95% CPU", {})
        assert "rds" in resources

    def test_merges_hints(self):
        agent = make_agent()
        resources = agent._extract_resources(
            "Lambda errors", {"lambda": ["payments-processor"], "rds": ["prod-db"]}
        )
        assert "payments-processor" in resources.get("lambda", [])
        assert "prod-db" in resources.get("rds", [])

    def test_deduplicates_resources(self):
        agent = make_agent()
        resources = agent._extract_resources(
            "Lambda errors",
            {"lambda": ["fn1", "fn1", "fn2"]},
        )
        assert resources["lambda"].count("fn1") == 1


# ─────────────────────────────────────────────────────────────────────────────
# Symptom extraction
# ─────────────────────────────────────────────────────────────────────────────

class TestSymptomExtraction:
    def test_extracts_high_error_rate(self):
        agent = make_agent()
        symptoms = agent._extract_symptoms("high error rate on the API endpoint")
        assert any("error" in s.lower() for s in symptoms)

    def test_extracts_timeout(self):
        agent = make_agent()
        symptoms = agent._extract_symptoms("function timeout detected in payments")
        assert any("timeout" in s.lower() for s in symptoms)

    def test_returns_empty_for_clean_description(self):
        agent = make_agent()
        symptoms = agent._extract_symptoms("everything is nominal")
        assert isinstance(symptoms, list)


# ─────────────────────────────────────────────────────────────────────────────
# Lookback calculation
# ─────────────────────────────────────────────────────────────────────────────

class TestLookback:
    def test_critical_lookback(self):
        agent = make_agent()
        assert agent._suggest_lookback("CRITICAL") == 30

    def test_high_lookback(self):
        agent = make_agent()
        assert agent._suggest_lookback("HIGH") == 60

    def test_low_lookback(self):
        agent = make_agent()
        assert agent._suggest_lookback("LOW") == 240


# ─────────────────────────────────────────────────────────────────────────────
# Full investigate() flow
# ─────────────────────────────────────────────────────────────────────────────

class TestInvestigate:
    def test_returns_incident_context(self):
        agent = make_agent({
            "triage_summary":      "High error rate.",
            "investigation_plan":  ["Check metrics"],
            "key_questions":       ["When did it start?"],
            "risk_assessment":     "Customer-facing impact.",
        })
        ctx = agent.investigate(
            "CRITICAL: Lambda payments-processor has 40% error rate",
            resource_hints={"lambda": ["payments-processor"]},
        )
        assert "incident_id" in ctx
        assert ctx["severity"] == "CRITICAL"
        assert "lambda" in ctx["affected_resources"]
        assert "initial_triage" in ctx

    def test_incident_id_format(self):
        agent = make_agent()
        ctx = agent.investigate("Lambda has errors")
        assert ctx["incident_id"].startswith("INC-")

    def test_raises_on_empty_description(self):
        agent = make_agent()
        with pytest.raises(ValueError):
            agent.investigate("")

    def test_override_severity(self):
        agent = make_agent()
        ctx = agent.investigate("minor blip", override_severity="CRITICAL")
        assert ctx["severity"] == "CRITICAL"

    def test_fallback_when_bedrock_fails(self):
        agent = make_agent(bedrock_response=None)   # Bedrock raises
        ctx = agent.investigate("Lambda has errors")
        # Should still return a context — fallback triage kicks in
        assert "initial_triage" in ctx
        triage = json.loads(ctx["initial_triage"])
        assert "triage_summary" in triage
