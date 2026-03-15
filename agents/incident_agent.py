"""
Incident Agent
==============
The entry-point agent of the CloudOps AI pipeline.

Responsibilities
----------------
1. Parse and validate the incoming user incident report.
2. Extract key investigation signals: affected resources, timeframe,
   observed symptoms, and severity level.
3. Build a structured IncidentContext that all downstream agents share.
4. Invoke Amazon Bedrock (Claude) to generate an initial triage summary
   and define what evidence the pipeline needs to collect.

This agent intentionally stays *narrow*: it does NOT call AWS APIs.
Its only job is to understand the problem and set the stage.
"""

import json
import logging
import re
import boto3
from datetime import datetime, timezone
from typing import Optional
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────
# Data Structures
# ─────────────────────────────────────────────

class IncidentContext(dict):
    """
    A plain dict subclass that holds structured incident state
    passed between all agents in the pipeline.

    Keys (all populated by IncidentAgent)
    ------
    incident_id         str      – unique ID for this investigation
    raw_description     str      – original user-supplied text
    severity            str      – CRITICAL / HIGH / MEDIUM / LOW
    affected_resources  dict     – {lambda: [...], ec2: [...], rds: [...]}
    lookback_minutes    int      – suggested metric/log window
    symptoms            list     – free-text symptom strings
    initial_triage      str      – Bedrock-generated analysis
    start_time          str      – ISO-8601 UTC
    """


# ─────────────────────────────────────────────
# Agent
# ─────────────────────────────────────────────

class IncidentAgent:
    """
    Parses an incident description and produces a structured IncidentContext.

    Parameters
    ----------
    bedrock_model_id : str
        Bedrock model to use for triage. Defaults to Claude 3 Sonnet.
    region_name : str
        AWS region for Bedrock runtime.
    max_tokens : int
        Max tokens for the triage response.
    """

    DEFAULT_MODEL   = "anthropic.claude-3-sonnet-20240229-v1:0"
    DEFAULT_REGION  = "us-east-1"
    MAX_TOKENS      = 1024

    # Simple heuristics for severity extraction
    SEVERITY_PATTERNS = {
        "CRITICAL": ["critical", "outage", "down", "unresponsive", "crash", "p0", "sev1"],
        "HIGH":     ["high", "degraded", "slow", "latency", "spike", "elevated", "p1", "sev2"],
        "MEDIUM":   ["medium", "intermittent", "occasional", "p2", "sev3"],
        "LOW":      ["low", "minor", "cosmetic", "p3", "sev4"],
    }

    # Resource-type hints in free-form text
    RESOURCE_PATTERNS = {
        "lambda":  r"\b(lambda|function|fn[-_]?\w+)\b",
        "ec2":     r"\b(ec2|instance|i-[0-9a-f]{8,17})\b",
        "rds":     r"\b(rds|database|db|aurora|postgres|mysql)\b",
        "ecs":     r"\b(ecs|container|task|service)\b",
        "apigw":   r"\b(api\s*gateway|apigateway|apigw|endpoint)\b",
        "dynamo":  r"\b(dynamodb|dynamo|ddb|table)\b",
    }

    def __init__(
        self,
        bedrock_model_id: str = DEFAULT_MODEL,
        region_name: str = DEFAULT_REGION,
        max_tokens: int = MAX_TOKENS,
    ):
        self.model_id   = bedrock_model_id
        self.region     = region_name
        self.max_tokens = max_tokens
        self._bedrock   = boto3.client("bedrock-runtime", region_name=region_name)
        logger.info("IncidentAgent ready (model=%s)", bedrock_model_id)

    # ── public ────────────────────────────────

    def investigate(
        self,
        incident_description: str,
        resource_hints: Optional[dict] = None,
        override_severity: Optional[str] = None,
    ) -> IncidentContext:
        """
        Main entry-point.  Parses the incident and returns an IncidentContext.

        Parameters
        ----------
        incident_description : str
            Free-form text from the operator, e.g. "Lambda function
            payments-processor has high error rates since 14:00 UTC".
        resource_hints : dict, optional
            Explicit resource IDs the caller already knows, e.g.
            {"lambda": ["payments-processor"], "rds": ["prod-db-1"]}.
        override_severity : str, optional
            If provided, bypasses heuristic severity detection.

        Returns
        -------
        IncidentContext  (dict subclass)
        """
        logger.info("IncidentAgent.investigate() called")

        if not incident_description or not incident_description.strip():
            raise ValueError("incident_description must be a non-empty string")

        # Step 1 – extract structured signals
        severity           = override_severity or self._extract_severity(incident_description)
        affected_resources = self._extract_resources(incident_description, resource_hints or {})
        symptoms           = self._extract_symptoms(incident_description)
        lookback_minutes   = self._suggest_lookback(severity)
        incident_id        = self._generate_incident_id()

        # Step 2 – Bedrock triage
        triage_text = self._bedrock_triage(
            incident_description, severity, affected_resources, symptoms
        )

        # Step 3 – assemble context
        ctx = IncidentContext(
            incident_id         = incident_id,
            raw_description     = incident_description.strip(),
            severity            = severity,
            affected_resources  = affected_resources,
            lookback_minutes    = lookback_minutes,
            symptoms            = symptoms,
            initial_triage      = triage_text,
            start_time          = datetime.now(timezone.utc).isoformat(),
        )

        logger.info(
            "Incident %s parsed — severity=%s, resources=%s",
            incident_id, severity, list(affected_resources.keys()),
        )
        return ctx

    # ── private helpers ───────────────────────

    def _extract_severity(self, text: str) -> str:
        text_lower = text.lower()
        for level, keywords in self.SEVERITY_PATTERNS.items():
            if any(kw in text_lower for kw in keywords):
                return level
        return "MEDIUM"   # safe default

    def _extract_resources(self, text: str, hints: dict) -> dict:
        """
        Build a resource map from regex heuristics + explicit hints.
        Returns e.g. {"lambda": ["payments-processor"], "ec2": [], ...}
        """
        resources: dict[str, list[str]] = {}
        text_lower = text.lower()

        for service, pattern in self.RESOURCE_PATTERNS.items():
            matches = re.findall(pattern, text_lower, re.IGNORECASE)
            if matches:
                resources[service] = list(dict.fromkeys(matches))  # deduplicate

        # Merge explicit hints (caller wins on specifics)
        for service, ids in hints.items():
            if ids:
                resources.setdefault(service, [])
                for rid in ids:
                    if rid not in resources[service]:
                        resources[service].append(rid)

        return resources

    def _extract_symptoms(self, text: str) -> list[str]:
        """
        Return a deduplicated list of operational symptom phrases found in the text.
        """
        symptom_patterns = [
            r"(high\s+(?:error\s+rate|latency|cpu|memory))",
            r"((?:function|service|endpoint)\s+(?:timeout|throttl\w+|crash\w*))",
            r"(5\d\d\s+errors?)",
            r"(elevated\s+\w+)",
            r"(spike\s+in\s+\w+)",
            r"(connection\s+(?:refused|reset|timeout))",
            r"(out\s+of\s+memory)",
            r"(disk\s+(?:full|space))",
            r"(cpu\s+utilization\s+(?:above|over)\s+\d+%)",
        ]
        symptoms = []
        for pattern in symptom_patterns:
            matches = re.findall(pattern, text, re.IGNORECASE)
            symptoms.extend(m.strip() for m in matches)
        return list(dict.fromkeys(symptoms))  # preserve order, deduplicate

    def _suggest_lookback(self, severity: str) -> int:
        """Map severity to a sensible metric/log lookback window in minutes."""
        return {"CRITICAL": 30, "HIGH": 60, "MEDIUM": 120, "LOW": 240}.get(severity, 60)

    @staticmethod
    def _generate_incident_id() -> str:
        from uuid import uuid4
        ts = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
        short_uuid = str(uuid4()).replace("-", "")[:8].upper()
        return f"INC-{ts}-{short_uuid}"

    def _bedrock_triage(
        self,
        description: str,
        severity: str,
        resources: dict,
        symptoms: list[str],
    ) -> str:
        """
        Call Amazon Bedrock (Claude) to produce a structured triage analysis.
        Falls back to a heuristic summary if Bedrock is unavailable.
        """
        prompt = f"""You are an expert AWS Site Reliability Engineer.
Analyse the following production incident report and provide a concise triage.

INCIDENT REPORT
---------------
{description}

EXTRACTED SIGNALS
-----------------
Severity  : {severity}
Resources : {json.dumps(resources, indent=2)}
Symptoms  : {', '.join(symptoms) if symptoms else 'None identified'}

Produce a JSON object (no markdown fences) with these exact keys:
  "triage_summary"  – 2-3 sentence explanation of the likely root-cause area
  "investigation_plan" – ordered list of 3-5 investigation steps
  "key_questions"   – list of 2-3 questions the data must answer
  "risk_assessment" – one sentence on blast radius / customer impact
"""
        try:
            body = json.dumps({
                "anthropic_version": "bedrock-2023-05-31",
                "max_tokens":        self.max_tokens,
                "messages":          [{"role": "user", "content": prompt}],
            })
            response = self._bedrock.invoke_model(
                modelId     = self.model_id,
                contentType = "application/json",
                accept      = "application/json",
                body        = body,
            )
            raw = json.loads(response["body"].read())
            content = raw.get("content", [{}])[0].get("text", "")
            # Validate it is JSON
            parsed = json.loads(content)
            return json.dumps(parsed, indent=2)

        except (ClientError, json.JSONDecodeError, KeyError) as exc:
            logger.warning("Bedrock triage failed (%s); using fallback.", exc)
            return self._fallback_triage(description, severity, resources, symptoms)

    @staticmethod
    def _fallback_triage(
        description: str,
        severity: str,
        resources: dict,
        symptoms: list[str],
    ) -> str:
        """Heuristic triage used when Bedrock is unavailable (e.g. local testing)."""
        resource_list = [f"{svc}: {ids}" for svc, ids in resources.items()]
        fallback = {
            "triage_summary": (
                f"[Heuristic] {severity} incident detected. "
                f"Affected resources: {', '.join(resource_list) or 'unknown'}. "
                f"Symptoms: {', '.join(symptoms) or 'not extracted'}."
            ),
            "investigation_plan": [
                "1. Check CloudWatch metrics for CPU/memory spikes.",
                "2. Review error logs for stack traces.",
                "3. Correlate timestamps between services.",
                "4. Compare against recent deployments.",
                "5. Check AWS Service Health Dashboard.",
            ],
            "key_questions": [
                "When did the error rate start rising?",
                "Which downstream services are affected?",
                "Were any deployments or config changes made recently?",
            ],
            "risk_assessment": (
                f"Severity {severity}: potential customer-facing impact on "
                f"{', '.join(resources.keys()) or 'unknown services'}."
            ),
        }
        return json.dumps(fallback, indent=2)
