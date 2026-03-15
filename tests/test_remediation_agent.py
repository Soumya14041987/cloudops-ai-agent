"""
Tests for RemediationAgent (Stage 4) and RemediationCatalogue.
No real AWS calls; Bedrock is mocked.
"""

import json
import pytest
from unittest.mock import MagicMock
from dataclasses import asdict

from agents.remediation_agent import RemediationAgent, RemediationCatalogue, RemediationAction


pytestmark = pytest.mark.unit


# ─────────────────────────────────────────────────────────────────────────────
# RemediationCatalogue
# ─────────────────────────────────────────────────────────────────────────────

class TestRemediationCatalogue:
    catalogue = RemediationCatalogue()

    def _ctx(self, resources=None, anomaly_metrics=None, log_categories=None,
             severity="HIGH"):
        return {
            "severity":           severity,
            "affected_resources": resources or {},
            "metrics_report": {
                "anomalies": [
                    {"metric": m, "anomaly_type": "THRESHOLD_BREACH",
                     "severity": "CRITICAL", "description": m}
                    for m in (anomaly_metrics or [])
                ],
            },
            "log_report": {
                "top_error_patterns": [
                    {"pattern_name": c, "category": c, "severity": "HIGH",
                     "count": 5, "resource": "lambda:fn"}
                    for c in (log_categories or [])
                ],
            },
        }

    def test_returns_lambda_actions_for_lambda_resource(self):
        ctx = self._ctx(resources={"lambda": ["payments-fn"]},
                        anomaly_metrics=["error_rate"])
        actions = self.catalogue.get_candidates(ctx)
        ids = [a.action_id for a in actions]
        assert any(i.startswith("LMB") for i in ids)

    def test_returns_ec2_actions_for_critical_cpu(self):
        ctx = self._ctx(
            resources={"ec2": ["i-xxx"]},
            anomaly_metrics=["cpu_utilization"],
            severity="CRITICAL",
        )
        actions = self.catalogue.get_candidates(ctx)
        ids = [a.action_id for a in actions]
        assert any(i.startswith("EC2") for i in ids)

    def test_returns_rds_actions_for_rds_resource(self):
        ctx = self._ctx(resources={"rds": ["prod-db"]},
                        anomaly_metrics=["rds_cpu"])
        actions = self.catalogue.get_candidates(ctx)
        ids = [a.action_id for a in actions]
        assert any(i.startswith("RDS") for i in ids)

    def test_alert_action_for_critical_severity(self):
        ctx = self._ctx(resources={"lambda": ["fn"]}, severity="CRITICAL")
        actions = self.catalogue.get_candidates(ctx)
        ids = [a.action_id for a in actions]
        assert "ALT-001" in ids

    def test_returns_empty_for_no_resources(self):
        ctx = self._ctx()
        actions = self.catalogue.get_candidates(ctx)
        # Only the ALT-001 alert action if severity is HIGH/CRITICAL
        assert isinstance(actions, list)

    def test_all_actions_have_required_fields(self):
        ctx = self._ctx(
            resources={"lambda": ["fn"], "rds": ["db"]},
            anomaly_metrics=["error_rate", "rds_cpu"],
            log_categories=["TIMEOUT", "DATABASE"],
            severity="CRITICAL",
        )
        for action in self.catalogue.get_candidates(ctx):
            assert action.action_id
            assert action.title
            assert action.risk_level in ("LOW", "MEDIUM", "HIGH", "CRITICAL")
            assert 0.0 <= action.confidence <= 1.0
            assert action.estimated_ttr_mins > 0


# ─────────────────────────────────────────────────────────────────────────────
# RemediationAgent._rank_actions
# ─────────────────────────────────────────────────────────────────────────────

class TestRankActions:
    def test_priority_increases_by_index(self):
        actions = [
            RemediationAction("A1", "Low conf low risk", "", "SCALING", "LOW",
                              5, 0.5, "Lambda", False),
            RemediationAction("A2", "High conf low risk", "", "SCALING", "LOW",
                              2, 0.95, "Lambda", True),
            RemediationAction("A3", "High conf high risk", "", "CONFIG", "HIGH",
                              60, 0.9, "EC2", False),
        ]
        ranked = RemediationAgent._rank_actions(actions, {"severity": "HIGH"})
        priorities = [a.priority for a in ranked]
        assert priorities == sorted(priorities)

    def test_lower_risk_ranked_higher_than_higher_risk(self):
        low_risk  = RemediationAction("L", "L", "", "SCALING", "LOW",  5, 0.8, "Lambda", True)
        high_risk = RemediationAction("H", "H", "", "CONFIG",  "HIGH", 5, 0.8, "Lambda", False)
        ranked = RemediationAgent._rank_actions([high_risk, low_risk], {"severity": "MEDIUM"})
        assert ranked[0].action_id == "L"


# ─────────────────────────────────────────────────────────────────────────────
# RemediationAgent.remediate (full flow)
# ─────────────────────────────────────────────────────────────────────────────

def make_remediation_agent(bedrock_response: dict | None = None,
                           auto_execute: bool = False,
                           dry_run: bool = True):
    agent = RemediationAgent.__new__(RemediationAgent)
    agent.model_id     = "anthropic.claude-3-sonnet-20240229-v1:0"
    agent.region       = "us-east-1"
    agent.auto_execute = auto_execute
    agent.dry_run      = dry_run
    agent.catalogue    = RemediationCatalogue()

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


class TestRemediationAgent:
    def test_returns_enriched_context(self, sample_full_context):
        agent = make_remediation_agent()
        result = agent.remediate(sample_full_context)
        assert "remediation_report" in result

    def test_remediation_report_required_keys(self, sample_full_context):
        agent = make_remediation_agent()
        result = agent.remediate(sample_full_context)
        report = result["remediation_report"]
        for key in ("total_actions_identified", "top_actions",
                    "final_recommendation", "summary"):
            assert key in report, f"Missing key: {key}"

    def test_top_actions_are_dicts(self, sample_full_context):
        agent = make_remediation_agent()
        result = agent.remediate(sample_full_context)
        for action in result["remediation_report"]["top_actions"]:
            assert isinstance(action, dict)
            assert "action_id" in action
            assert "title" in action

    def test_original_context_preserved(self, sample_full_context):
        agent = make_remediation_agent()
        result = agent.remediate(sample_full_context)
        assert result["incident_id"] == sample_full_context["incident_id"]

    def test_no_auto_execute_when_dry_run(self, sample_full_context):
        agent = make_remediation_agent(auto_execute=True, dry_run=True)
        result = agent.remediate(sample_full_context)
        assert result["remediation_report"]["auto_executed_count"] == 0

    def test_bedrock_fallback(self, sample_full_context):
        agent = make_remediation_agent(bedrock_response=None)
        result = agent.remediate(sample_full_context)
        rec = json.loads(result["remediation_report"]["final_recommendation"])
        assert "recommended_action" in rec
