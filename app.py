"""
CloudOps AI Agent — Main Orchestrator (app.py)
===============================================
This module wires together the four specialised agents into a single
end-to-end pipeline and exposes two entry-points:

  1. AWS Lambda handler  – ``lambda_handler(event, context)``
     Invoked by API Gateway or EventBridge. Accepts JSON body.

  2. Local CLI runner    – ``run_local(description, resource_hints)``
     For development and integration testing without deploying to AWS.

Pipeline flow
-------------
  User Request
       │
  [1] IncidentAgent   → parse & triage the incident description
       │
  [2] MetricsAgent    → query CloudWatch Metrics + detect anomalies
       │
  [3] LogAgent        → query CloudWatch Logs  + correlate patterns
       │
  [4] RemediationAgent→ rank actions + Bedrock final recommendation
       │
  Final JSON response (pipeline_result)
"""

import json
import logging
import os
import time
import traceback
from typing import Optional

# ── agent imports ─────────────────────────────
from agents.incident_agent    import IncidentAgent
from agents.metrics_agent     import MetricsAgent
from agents.log_agent         import LogAgent
from agents.remediation_agent import RemediationAgent

# ── tool imports ──────────────────────────────
from tools.cloudwatch_logs    import CloudWatchLogsTool
from tools.cloudwatch_metrics import CloudWatchMetricsTool

# ─────────────────────────────────────────────
# Logging configuration
# ─────────────────────────────────────────────
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level   = getattr(logging, LOG_LEVEL, logging.INFO),
    format  = "%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    datefmt = "%Y-%m-%dT%H:%M:%SZ",
)
logger = logging.getLogger("cloudops-ai-agent")

# ─────────────────────────────────────────────
# Environment / Configuration
# ─────────────────────────────────────────────

AWS_REGION        = os.getenv("AWS_REGION",         "us-east-1")
BEDROCK_MODEL_ID  = os.getenv("BEDROCK_MODEL_ID",   "anthropic.claude-3-sonnet-20240229-v1:0")
AUTO_EXECUTE      = os.getenv("AUTO_EXECUTE",        "false").lower() == "true"
DRY_RUN           = os.getenv("DRY_RUN",             "true").lower()  == "true"
MAX_LOG_LOOKBACK  = int(os.getenv("MAX_LOG_LOOKBACK",  "240"))   # minutes

# ─────────────────────────────────────────────
# Orchestrator
# ─────────────────────────────────────────────

class CloudOpsOrchestrator:
    """
    Coordinates the four agents in the correct sequence and
    produces the final structured pipeline result.

    Designed for *dependency injection*: tools and agents are created
    once in the constructor and reused across invocations (important
    when running in AWS Lambda with warm containers).
    """

    def __init__(
        self,
        region_name:      str  = AWS_REGION,
        bedrock_model_id: str  = BEDROCK_MODEL_ID,
        auto_execute:     bool = AUTO_EXECUTE,
        dry_run:          bool = DRY_RUN,
    ):
        logger.info(
            "Initialising CloudOpsOrchestrator (region=%s, model=%s, auto_execute=%s, dry_run=%s)",
            region_name, bedrock_model_id, auto_execute, dry_run,
        )

        # ── shared AWS tools ──────────────────
        self._logs_tool    = CloudWatchLogsTool(region_name=region_name)
        self._metrics_tool = CloudWatchMetricsTool(region_name=region_name)

        # ── agents (injected with shared tools) ─
        self._incident_agent    = IncidentAgent(
            bedrock_model_id = bedrock_model_id,
            region_name      = region_name,
        )
        self._metrics_agent = MetricsAgent(
            metrics_tool     = self._metrics_tool,
            bedrock_model_id = bedrock_model_id,
            region_name      = region_name,
        )
        self._log_agent = LogAgent(
            logs_tool        = self._logs_tool,
            bedrock_model_id = bedrock_model_id,
            region_name      = region_name,
        )
        self._remediation_agent = RemediationAgent(
            bedrock_model_id = bedrock_model_id,
            region_name      = region_name,
            auto_execute     = auto_execute,
            dry_run          = dry_run,
        )

        logger.info("CloudOpsOrchestrator ready — all agents initialised")

    # ── public ────────────────────────────────

    def run(
        self,
        incident_description: str,
        resource_hints:       Optional[dict] = None,
        override_severity:    Optional[str]  = None,
    ) -> dict:
        """
        Execute the full four-stage pipeline.

        Parameters
        ----------
        incident_description : str
            Free-form description of the production issue.
        resource_hints : dict, optional
            Pre-known resource IDs:
            {"lambda": ["fn-name"], "ec2": ["i-xxx"], "rds": ["db-id"]}
        override_severity : str, optional
            Force a severity level (CRITICAL / HIGH / MEDIUM / LOW).

        Returns
        -------
        dict – complete pipeline result with all agent reports
        """
        pipeline_start = time.perf_counter()
        logger.info("=== Pipeline START ===")

        stage_timings: dict[str, float] = {}
        context: dict = {}

        try:
            # ── Stage 1: Incident Analysis ────
            t0 = time.perf_counter()
            context = self._incident_agent.investigate(
                incident_description,
                resource_hints    = resource_hints,
                override_severity = override_severity,
            )
            stage_timings["incident_agent"] = round(time.perf_counter() - t0, 3)
            logger.info("Stage 1 done in %.2fs — incident %s",
                        stage_timings["incident_agent"], context.get("incident_id"))

            # ── Stage 2: Metrics Analysis ─────
            t0 = time.perf_counter()
            context = self._metrics_agent.analyze(context)
            stage_timings["metrics_agent"] = round(time.perf_counter() - t0, 3)
            logger.info("Stage 2 done in %.2fs — overall_health=%s",
                        stage_timings["metrics_agent"],
                        context.get("metrics_report", {}).get("overall_health"))

            # ── Stage 3: Log Analysis ─────────
            t0 = time.perf_counter()
            context = self._log_agent.analyze(context)
            stage_timings["log_agent"] = round(time.perf_counter() - t0, 3)
            logger.info("Stage 3 done in %.2fs — %d error patterns",
                        stage_timings["log_agent"],
                        len(context.get("log_report", {}).get("top_error_patterns", [])))

            # ── Stage 4: Remediation ──────────
            t0 = time.perf_counter()
            context = self._remediation_agent.remediate(context)
            stage_timings["remediation_agent"] = round(time.perf_counter() - t0, 3)
            logger.info("Stage 4 done in %.2fs — %d actions",
                        stage_timings["remediation_agent"],
                        context.get("remediation_report", {}).get("total_actions_identified", 0))

            total_elapsed = round(time.perf_counter() - pipeline_start, 3)
            logger.info("=== Pipeline COMPLETE in %.2fs ===", total_elapsed)

            return self._build_success_response(context, stage_timings, total_elapsed)

        except Exception as exc:
            total_elapsed = round(time.perf_counter() - pipeline_start, 3)
            logger.error("Pipeline FAILED after %.2fs: %s", total_elapsed, exc, exc_info=True)
            return self._build_error_response(
                exc, context, stage_timings, total_elapsed
            )

    # ── private helpers ───────────────────────

    @staticmethod
    def _build_success_response(
        context: dict,
        timings: dict,
        elapsed: float,
    ) -> dict:
        """Shape the final response payload."""
        remediation = context.get("remediation_report", {})
        metrics     = context.get("metrics_report",     {})
        logs        = context.get("log_report",          {})

        return {
            "status":      "SUCCESS",
            "incident_id": context.get("incident_id"),
            "severity":    context.get("severity"),
            "pipeline_result": {
                "incident_summary":      context.get("initial_triage"),
                "metrics_summary":       metrics.get("summary"),
                "log_summary":           logs.get("summary"),
                "remediation_summary":   remediation.get("summary"),
                "final_recommendation":  remediation.get("final_recommendation"),
                "estimated_ttr_minutes": remediation.get("estimated_total_ttr_mins"),
                "overall_health":        metrics.get("overall_health"),
            },
            "agent_reports": {
                "incident":    {
                    "incident_id":        context.get("incident_id"),
                    "severity":           context.get("severity"),
                    "affected_resources": context.get("affected_resources"),
                    "initial_triage":     context.get("initial_triage"),
                },
                "metrics":   metrics,
                "logs":      logs,
                "remediation": remediation,
            },
            "performance": {
                "total_elapsed_seconds": elapsed,
                "stage_timings":         timings,
            },
        }

    @staticmethod
    def _build_error_response(
        exc: Exception,
        partial_context: dict,
        timings: dict,
        elapsed: float,
    ) -> dict:
        return {
            "status":      "ERROR",
            "incident_id": partial_context.get("incident_id", "UNKNOWN"),
            "error": {
                "type":      type(exc).__name__,
                "message":   str(exc),
                "traceback": traceback.format_exc(),
            },
            "partial_context": partial_context,
            "performance": {
                "total_elapsed_seconds": elapsed,
                "stage_timings":         timings,
            },
        }


# ─────────────────────────────────────────────
# Lazy-loaded global orchestrator (Lambda warm-start optimisation)
# ─────────────────────────────────────────────
_orchestrator: Optional[CloudOpsOrchestrator] = None


def _get_orchestrator() -> CloudOpsOrchestrator:
    global _orchestrator
    if _orchestrator is None:
        _orchestrator = CloudOpsOrchestrator()
    return _orchestrator


# ─────────────────────────────────────────────
# AWS Lambda handler
# ─────────────────────────────────────────────

def lambda_handler(event: dict, context) -> dict:
    """
    AWS Lambda entry-point.

    Expected event body (JSON):
    {
        "incident_description": "Lambda payments-processor has 40% error rate since 14:00 UTC",
        "resource_hints": {
            "lambda": ["payments-processor"],
            "rds":    ["prod-payments-db"]
        },
        "override_severity": "HIGH"   // optional
    }

    Returns an API Gateway-compatible response.
    """
    logger.info("lambda_handler invoked — request_id=%s", getattr(context, "aws_request_id", "local"))

    try:
        # Parse body (API Gateway wraps the body as a JSON string)
        body: dict = {}
        if isinstance(event.get("body"), str):
            body = json.loads(event["body"])
        elif isinstance(event.get("body"), dict):
            body = event["body"]
        else:
            # Direct Lambda invocation (not via API Gateway)
            body = event

        incident_description = body.get("incident_description", "")
        if not incident_description:
            return _api_response(400, {"error": "incident_description is required"})

        resource_hints    = body.get("resource_hints", {})
        override_severity = body.get("override_severity")

        orchestrator = _get_orchestrator()
        result = orchestrator.run(
            incident_description = incident_description,
            resource_hints       = resource_hints,
            override_severity    = override_severity,
        )

        status_code = 200 if result["status"] == "SUCCESS" else 500
        return _api_response(status_code, result)

    except json.JSONDecodeError as exc:
        logger.error("Invalid JSON body: %s", exc)
        return _api_response(400, {"error": f"Invalid JSON: {exc}"})
    except Exception as exc:
        logger.error("Unhandled error in lambda_handler: %s", exc, exc_info=True)
        return _api_response(500, {"error": str(exc), "traceback": traceback.format_exc()})


def _api_response(status_code: int, body: dict) -> dict:
    """Return an API Gateway-compatible HTTP response."""
    return {
        "statusCode": status_code,
        "headers": {
            "Content-Type":                "application/json",
            "Access-Control-Allow-Origin": "*",
        },
        "body": json.dumps(body, default=str),
    }


# ─────────────────────────────────────────────
# Local CLI runner
# ─────────────────────────────────────────────

def run_local(
    incident_description: str,
    resource_hints: Optional[dict] = None,
    override_severity: Optional[str] = None,
    pretty_print: bool = True,
) -> dict:
    """
    Run the pipeline locally (no Lambda / API Gateway needed).

    Example
    -------
    >>> from app import run_local
    >>> result = run_local(
    ...     "Lambda payments-processor has 40% error rate for the last 30 minutes",
    ...     resource_hints={"lambda": ["payments-processor"], "rds": ["prod-db"]},
    ... )
    >>> print(result["pipeline_result"]["final_recommendation"])
    """
    orchestrator = _get_orchestrator()
    result = orchestrator.run(
        incident_description = incident_description,
        resource_hints       = resource_hints or {},
        override_severity    = override_severity,
    )
    if pretty_print:
        print(json.dumps(result, indent=2, default=str))
    return result


# ─────────────────────────────────────────────
# __main__ — quick smoke-test
# ─────────────────────────────────────────────

if __name__ == "__main__":
    print("=" * 70)
    print(" CloudOps AI Agent — local smoke-test")
    print("=" * 70)

    sample_incident = (
        "CRITICAL: Lambda function 'payments-processor' has an elevated error rate "
        "of 45% over the last 30 minutes. Users are reporting failed transactions. "
        "Some requests are also timing out. RDS database connections appear elevated."
    )

    sample_hints = {
        "lambda": ["payments-processor", "notifications-sender"],
        "rds":    ["prod-payments-db"],
        "ec2":    ["i-0abc123def456789"],
    }

    result = run_local(
        incident_description = sample_incident,
        resource_hints       = sample_hints,
        override_severity    = "CRITICAL",
        pretty_print         = True,
    )

    print("\n" + "=" * 70)
    print(f"Pipeline status : {result['status']}")
    print(f"Incident ID     : {result.get('incident_id')}")
    print(f"Total time      : {result['performance']['total_elapsed_seconds']}s")
    print("=" * 70)
