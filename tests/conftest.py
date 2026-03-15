"""
conftest.py — Shared pytest fixtures and mock helpers.

All AWS API calls are mocked via moto so tests run without
real credentials. Each fixture is scoped to minimise setup overhead.
"""

import json
import os
import pytest
import boto3
from moto import mock_aws
from unittest.mock import MagicMock, patch

# Force boto3 to use us-east-1 and dummy creds in tests
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_ACCESS_KEY_ID",  "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
os.environ.setdefault("AWS_SECURITY_TOKEN", "testing")
os.environ.setdefault("AWS_SESSION_TOKEN",  "testing")
os.environ.setdefault("BEDROCK_MODEL_ID", "anthropic.claude-3-sonnet-20240229-v1:0")
os.environ.setdefault("DRY_RUN", "true")
os.environ.setdefault("AUTO_EXECUTE", "false")


# ─────────────────────────────────────────────────────────────────────────────
# Sample data
# ─────────────────────────────────────────────────────────────────────────────

SAMPLE_INCIDENT_DESCRIPTION = (
    "CRITICAL: Lambda function payments-processor has a 40% error rate "
    "over the last 30 minutes. Users are seeing failed transactions. "
    "Connection timeouts and 500 errors observed."
)

SAMPLE_RESOURCE_HINTS = {
    "lambda": ["payments-processor"],
    "rds":    ["prod-payments-db"],
    "ec2":    ["i-0abc123def456789"],
}

SAMPLE_BEDROCK_TRIAGE = {
    "triage_summary": "High error rate on Lambda function.",
    "investigation_plan": ["Check CloudWatch Metrics", "Review error logs"],
    "key_questions": ["When did errors start?", "Any recent deployments?"],
    "risk_assessment": "Customer-facing payment failures.",
}

SAMPLE_BEDROCK_METRICS = {
    "metrics_interpretation": "CPU and error metrics are elevated.",
    "likely_bottleneck": "lambda:payments-processor",
    "correlation_clues": ["High error rate", "Elevated duration"],
    "confidence_level": "HIGH",
}

SAMPLE_BEDROCK_HYPOTHESIS = {
    "root_cause_hypothesis": "Lambda timeout due to DB connection pool exhaustion.",
    "evidence_chain": ["High error rate", "Connection timeouts in logs"],
    "confidence_level": "HIGH",
    "next_investigation": "Check RDS connection count metrics.",
}

SAMPLE_BEDROCK_RECOMMENDATION = {
    "executive_summary": "Payments Lambda is failing due to DB connection exhaustion.",
    "recommended_action": "Deploy RDS Proxy to manage connection pooling.",
    "implementation_order": ["RDS-003", "LMB-001"],
    "post_incident_tasks": ["Add CloudWatch alarms", "Conduct post-mortem"],
    "estimated_resolution": "20-30 minutes",
    "confidence_overall": "HIGH",
}


# ─────────────────────────────────────────────────────────────────────────────
# Bedrock mock factory
# ─────────────────────────────────────────────────────────────────────────────

def make_bedrock_response(payload: dict) -> MagicMock:
    """Return a mock Bedrock runtime invoke_model response."""
    body_bytes = json.dumps({
        "content": [{"type": "text", "text": json.dumps(payload)}],
    }).encode()

    mock_body = MagicMock()
    mock_body.read.return_value = body_bytes

    mock_response = MagicMock()
    mock_response.__getitem__ = lambda self, key: mock_body if key == "body" else None
    mock_response.get = lambda key, default=None: mock_body if key == "body" else default
    return mock_response


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def sample_incident():
    """Plain dict matching IncidentAgent.investigate() output."""
    return {
        "incident_id":        "INC-20240315140000-TESTTEST",
        "raw_description":    SAMPLE_INCIDENT_DESCRIPTION,
        "severity":           "CRITICAL",
        "affected_resources": SAMPLE_RESOURCE_HINTS,
        "lookback_minutes":   30,
        "symptoms":           ["high error rate", "connection timeout"],
        "initial_triage":     json.dumps(SAMPLE_BEDROCK_TRIAGE),
        "start_time":         "2024-03-15T14:00:00+00:00",
    }


@pytest.fixture
def sample_incident_with_metrics(sample_incident):
    """IncidentContext enriched with a MetricsReport."""
    return {
        **sample_incident,
        "metrics_report": {
            "overall_health":     "CRITICAL",
            "resources_checked":  3,
            "anomalies_detected": 2,
            "critical_resources": 1,
            "warning_resources":  1,
            "resource_health": {
                "lambda:payments-processor": {
                    "overall_health": "CRITICAL",
                    "metrics": {
                        "total_invocations": 500,
                        "total_errors":      200,
                        "error_rate_percent": 40.0,
                        "avg_duration_ms":   8500,
                        "total_throttles":   0,
                    },
                    "issues": ["CRITICAL: 200 errors", "WARNING: avg duration 8500ms"],
                },
            },
            "anomalies": [
                {
                    "resource":     "lambda:payments-processor",
                    "metric":       "error_rate",
                    "value":        40.0,
                    "anomaly_type": "THRESHOLD_BREACH",
                    "severity":     "CRITICAL",
                    "description":  "Lambda payments-processor error rate is 40.0%",
                },
            ],
            "interpretation": json.dumps(SAMPLE_BEDROCK_METRICS),
            "summary": "Metrics analysis complete.",
        },
    }


@pytest.fixture
def sample_full_context(sample_incident_with_metrics):
    """Fully enriched context including log report."""
    return {
        **sample_incident_with_metrics,
        "log_report": {
            "resources_analyzed":    2,
            "total_error_events":    45,
            "unique_patterns":       3,
            "log_statistics": {
                "lambda:payments-processor": {
                    "health_status":      "CRITICAL",
                    "error_rate_percent": 38.0,
                    "total_events":       120,
                },
            },
            "top_error_patterns": [
                {
                    "pattern_name": "Connection Timeout",
                    "resource":     "lambda:payments-processor",
                    "count":        30,
                    "severity":     "HIGH",
                    "category":     "CONNECTIVITY",
                },
            ],
            "error_timeline": [
                {"minute": "2024-03-15T14:00", "error_count": 15},
                {"minute": "2024-03-15T14:01", "error_count": 30},
            ],
            "correlations": [],
            "root_cause_hypothesis": json.dumps(SAMPLE_BEDROCK_HYPOTHESIS),
            "critical_log_groups": ["lambda:payments-processor"],
            "summary": "Log analysis complete.",
        },
    }


@pytest.fixture
def mock_bedrock_client():
    """Patch boto3 Bedrock runtime client at the module level."""
    with patch("boto3.client") as mock_factory:
        mock_client = MagicMock()
        mock_factory.return_value = mock_client
        yield mock_client


@pytest.fixture
def mock_cloudwatch_logs_tool():
    """Return a fully mocked CloudWatchLogsTool."""
    tool = MagicMock()
    tool.get_recent_logs.return_value = {
        "log_group": "/aws/lambda/payments-processor",
        "time_range_minutes": 30,
        "total_events": 10,
        "error_count": 3,
        "warning_count": 1,
        "events": [
            {"timestamp": "2024-03-15T14:00:00+00:00",
             "message": "ERROR: Connection timed out",
             "severity": "ERROR", "stream": "stream-1"},
        ],
        "summary": "Retrieved 10 events.",
    }
    tool.get_error_logs.return_value = {
        "log_group": "/aws/lambda/payments-processor",
        "time_range_minutes": 30,
        "total_events": 3,
        "error_count": 3,
        "warning_count": 0,
        "events": [
            {"timestamp": "2024-03-15T14:00:00+00:00",
             "message": "ERROR: Connection timed out",
             "severity": "ERROR", "stream": "stream-1"},
        ],
        "summary": "3 error events found.",
    }
    tool.get_log_statistics.return_value = {
        "log_group": "/aws/lambda/payments-processor",
        "time_range_minutes": 30,
        "total_events": 10,
        "severity_breakdown": {"ERROR": 3, "WARNING": 1, "INFO": 6},
        "top_errors": [{"message": "Connection timed out", "count": 3}],
        "error_rate_percent": 30.0,
        "health_status": "DEGRADED",
        "summary": "DEGRADED — 30% error rate.",
    }
    return tool


@pytest.fixture
def mock_cloudwatch_metrics_tool():
    """Return a fully mocked CloudWatchMetricsTool."""
    tool = MagicMock()
    tool.get_lambda_health.return_value = {
        "resource_type":   "Lambda",
        "function_name":   "payments-processor",
        "time_range_minutes": 30,
        "overall_health":  "CRITICAL",
        "metrics": {
            "total_invocations":  500,
            "total_errors":       200,
            "error_rate_percent": 40.0,
            "avg_duration_ms":    8500.0,
            "max_duration_ms":    14000.0,
            "total_throttles":    0,
        },
        "issues": ["CRITICAL: 200 errors in 30m", "WARNING: avg duration 8500ms"],
        "summary": "Lambda 'payments-processor' — CRITICAL.",
    }
    tool.get_ec2_health.return_value = {
        "resource_type":  "EC2",
        "instance_id":    "i-0abc123def456789",
        "overall_health": "HEALTHY",
        "metrics": {"avg_cpu_percent": 35.0, "max_cpu_percent": 55.0},
        "issues": [],
        "summary": "EC2 'i-0abc123' — HEALTHY.",
    }
    tool.get_rds_health.return_value = {
        "resource_type":  "RDS",
        "db_instance_id": "prod-payments-db",
        "overall_health": "WARNING",
        "metrics": {
            "avg_cpu_percent":      68.0,
            "avg_connections":      490,
            "avg_read_latency_ms":  12.0,
            "avg_write_latency_ms": 15.0,
            "free_storage_gb":      42.0,
        },
        "issues": ["WARNING: RDS CPU at 68.0%"],
        "summary": "RDS 'prod-payments-db' — WARNING.",
    }
    return tool
