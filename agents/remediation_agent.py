"""
Remediation Agent
=================
Final stage in the CloudOps AI pipeline.

Responsibilities
----------------
1. Consume the fully enriched IncidentContext (incident + metrics + logs).
2. Apply a rule-based remediation catalogue to generate candidate actions.
3. Score and rank actions by: confidence, risk, estimated time-to-resolve.
4. Call Amazon Bedrock to produce a final, human-readable remediation plan.
5. Optionally execute safe, low-risk AWS remediation actions automatically.
6. Return the completed pipeline result as a structured RemediationReport.
"""

import json
import logging
from dataclasses import dataclass, field, asdict
from typing import Optional
import boto3
from botocore.exceptions import ClientError
from agents.model_adapter import get_adapter

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────
# Data Classes
# ─────────────────────────────────────────────

@dataclass
class RemediationAction:
    action_id:           str
    title:               str
    description:         str
    category:            str          # SCALING / RESTART / CONFIG / ALERT / ROLLBACK / INFRA
    risk_level:          str          # LOW / MEDIUM / HIGH / CRITICAL
    estimated_ttr_mins:  int          # estimated time-to-resolve in minutes
    confidence:          float        # 0.0 – 1.0
    aws_service:         str          # affected AWS service
    automated:           bool         # can this be safely auto-executed?
    prerequisites:       list[str] = field(default_factory=list)
    steps:               list[str] = field(default_factory=list)
    rollback_plan:       str = ""
    priority:            int  = 99    # lower = higher priority (filled at scoring time)


# ─────────────────────────────────────────────
# Remediation Catalogue
# ─────────────────────────────────────────────

class RemediationCatalogue:
    """
    Rule-based catalogue mapping {pattern_category, metric_anomaly_type} →
    list[RemediationAction].

    Each rule is evaluated against the enriched incident context;
    matching rules produce candidate actions.
    """

    def get_candidates(self, incident_context: dict) -> list[RemediationAction]:
        """Evaluate all rules and return applicable remediation actions."""
        candidates: list[RemediationAction] = []

        metrics_report = incident_context.get("metrics_report", {})
        log_report     = incident_context.get("log_report", {})
        severity       = incident_context.get("severity", "MEDIUM")
        resources      = incident_context.get("affected_resources", {})
        anomalies      = metrics_report.get("anomalies", [])
        log_patterns   = log_report.get("top_error_patterns", [])

        # ── categorise signals ────────────────
        anomaly_types     = {a.get("anomaly_type", "") for a in anomalies}
        anomaly_metrics   = {a.get("metric", "") for a in anomalies}
        log_categories    = {p.get("category", "") for p in log_patterns}
        has_lambda        = bool(resources.get("lambda"))
        has_ec2           = bool(resources.get("ec2"))
        has_rds           = bool(resources.get("rds"))

        # ─── Lambda rules ─────────────────────
        if has_lambda:
            if "error_rate" in anomaly_metrics or "EXCEPTION" in log_categories:
                candidates.append(RemediationAction(
                    action_id     = "LMB-001",
                    title         = "Increase Lambda Memory / Timeout",
                    description   = "High error rates may stem from OOM or timeouts. Increase memory (and CPU share) and timeout value.",
                    category      = "CONFIG",
                    risk_level    = "LOW",
                    estimated_ttr_mins = 5,
                    confidence    = 0.75,
                    aws_service   = "Lambda",
                    automated     = False,
                    steps         = [
                        "Open Lambda console → Configuration → General configuration.",
                        "Increase Memory from current to next tier (e.g. 512 MB → 1024 MB).",
                        "Increase Timeout (e.g. 30 s → 60 s).",
                        "Save and re-invoke to verify error rate drops.",
                    ],
                    rollback_plan = "Revert memory and timeout to original values.",
                ))
            if "THROTTLING" in log_categories or "lambda_throttles" in anomaly_metrics:
                candidates.append(RemediationAction(
                    action_id     = "LMB-002",
                    title         = "Increase Lambda Concurrency Limit",
                    description   = "Throttling detected. Raise reserved or provisioned concurrency.",
                    category      = "SCALING",
                    risk_level    = "MEDIUM",
                    estimated_ttr_mins = 2,
                    confidence    = 0.88,
                    aws_service   = "Lambda",
                    automated     = True,
                    steps         = [
                        "Open Lambda console → Configuration → Concurrency.",
                        "Increase Reserved Concurrency (e.g. 100 → 500).",
                        "Optionally enable Provisioned Concurrency to eliminate cold starts.",
                    ],
                    rollback_plan = "Reduce concurrency to previous value.",
                ))
            if "TIMEOUT" in log_categories:
                candidates.append(RemediationAction(
                    action_id     = "LMB-003",
                    title         = "Enable Lambda SnapStart / Optimise Initialisation",
                    description   = "Repeated timeouts suggest slow init. Enable SnapStart (Java) or reduce import overhead.",
                    category      = "CONFIG",
                    risk_level    = "LOW",
                    estimated_ttr_mins = 15,
                    confidence    = 0.60,
                    aws_service   = "Lambda",
                    automated     = False,
                    steps         = [
                        "Audit cold-start duration in X-Ray traces.",
                        "Reduce dependency bundle size.",
                        "For Java: enable SnapStart on new version.",
                        "Move heavy init outside handler to module scope.",
                    ],
                    rollback_plan = "Disable SnapStart; revert code changes.",
                ))

        # ─── EC2 rules ────────────────────────
        if has_ec2:
            critical_cpu = any(
                a.get("metric") == "cpu_utilization" and a.get("severity") == "CRITICAL"
                for a in anomalies
            )
            if critical_cpu or severity in ("CRITICAL", "HIGH"):
                candidates.append(RemediationAction(
                    action_id     = "EC2-001",
                    title         = "Scale-Out EC2 Auto Scaling Group",
                    description   = "CPU critically elevated. Add instances to distribute load.",
                    category      = "SCALING",
                    risk_level    = "LOW",
                    estimated_ttr_mins = 3,
                    confidence    = 0.82,
                    aws_service   = "EC2",
                    automated     = True,
                    steps         = [
                        "Identify the Auto Scaling Group (ASG) for the affected instance.",
                        "Increase Desired Capacity by 2–4 instances.",
                        "Monitor CloudWatch — CPU should normalise in ~3 minutes.",
                        "If using ELB, verify new instances become healthy.",
                    ],
                    rollback_plan = "Scale-in: reduce Desired Capacity after traffic drops.",
                ))
                candidates.append(RemediationAction(
                    action_id     = "EC2-002",
                    title         = "Investigate & Kill CPU-Hungry Processes",
                    description   = "SSH to instance and identify runaway processes consuming CPU.",
                    category      = "INFRA",
                    risk_level    = "MEDIUM",
                    estimated_ttr_mins = 10,
                    confidence    = 0.65,
                    aws_service   = "EC2",
                    automated     = False,
                    steps         = [
                        "Connect via SSM Session Manager (no SSH key needed).",
                        "Run: top -b -n 1 | head -20",
                        "Identify PID of high-CPU process.",
                        "Decide: kill -9 <PID> or gracefully restart the service.",
                    ],
                    rollback_plan = "Restart the killed service if it is required for operation.",
                ))

        # ─── RDS rules ────────────────────────
        if has_rds:
            if "rds_cpu" in anomaly_metrics or "DATABASE" in log_categories:
                candidates.append(RemediationAction(
                    action_id     = "RDS-001",
                    title         = "Enable RDS Read Replicas to Offload Reads",
                    description   = "High DB CPU; create a read replica and redirect read traffic.",
                    category      = "SCALING",
                    risk_level    = "LOW",
                    estimated_ttr_mins = 20,
                    confidence    = 0.70,
                    aws_service   = "RDS",
                    automated     = False,
                    steps         = [
                        "Create a read replica via RDS console or CLI.",
                        "Update application DB connection string to use replica for SELECTs.",
                        "Monitor primary CPU — should drop within minutes.",
                        "Optionally promote replica if primary is failing.",
                    ],
                    rollback_plan = "Delete replica and revert connection string.",
                ))
                candidates.append(RemediationAction(
                    action_id     = "RDS-002",
                    title         = "Identify & Kill Long-Running DB Queries",
                    description   = "Long queries may be holding locks and causing CPU spikes.",
                    category      = "INFRA",
                    risk_level    = "MEDIUM",
                    estimated_ttr_mins = 5,
                    confidence    = 0.78,
                    aws_service   = "RDS",
                    automated     = False,
                    steps         = [
                        "Enable Performance Insights in RDS console.",
                        "Identify queries with high Average Active Sessions.",
                        "Use: SELECT pid, query, now()-query_start AS duration FROM pg_stat_activity WHERE state='active' ORDER BY duration DESC;",
                        "Terminate with: SELECT pg_terminate_backend(<pid>);",
                    ],
                    rollback_plan = "N/A — no state changes needed.",
                ))
            if "rds_connections" in anomaly_metrics:
                candidates.append(RemediationAction(
                    action_id     = "RDS-003",
                    title         = "Deploy RDS Proxy to Manage Connection Pooling",
                    description   = "Too many DB connections; RDS Proxy pools and multiplexes them.",
                    category      = "CONFIG",
                    risk_level    = "LOW",
                    estimated_ttr_mins = 30,
                    confidence    = 0.72,
                    aws_service   = "RDS",
                    automated     = False,
                    steps         = [
                        "Create an RDS Proxy in the RDS console for the DB.",
                        "Update Lambda/app to connect to Proxy endpoint instead of DB directly.",
                        "Set max_connections_percent = 80% in proxy settings.",
                        "Verify connection count drops in RDS metrics.",
                    ],
                    rollback_plan = "Revert DB endpoint in app; delete proxy.",
                ))

        # ─── Connectivity rules ───────────────
        if "CONNECTIVITY" in log_categories:
            candidates.append(RemediationAction(
                action_id     = "NET-001",
                title         = "Check Security Groups & VPC Network ACLs",
                description   = "Connection failures may be caused by misconfigured SGs or NACLs.",
                category      = "CONFIG",
                risk_level    = "LOW",
                estimated_ttr_mins = 5,
                confidence    = 0.65,
                aws_service   = "VPC",
                automated     = False,
                steps         = [
                    "Open VPC console → Security Groups for affected resources.",
                    "Verify inbound rules allow traffic on the required port.",
                    "Check NACL deny rules that might block traffic.",
                    "Use VPC Flow Logs to trace rejected packets.",
                    "Check Route Tables for correct routes.",
                ],
                rollback_plan = "Revert SG rule changes.",
            ))

        # ─── Alerting rule (always) ────────────
        if severity in ("CRITICAL", "HIGH"):
            candidates.append(RemediationAction(
                action_id     = "ALT-001",
                title         = "Create CloudWatch Alarm for Proactive Detection",
                description   = "Set up alarms so similar incidents trigger PagerDuty / SNS instantly.",
                category      = "ALERT",
                risk_level    = "LOW",
                estimated_ttr_mins = 10,
                confidence    = 0.95,
                aws_service   = "CloudWatch",
                automated     = False,
                steps         = [
                    "Open CloudWatch → Alarms → Create alarm.",
                    "Select the metric that triggered this incident.",
                    "Set threshold to 80% of the current anomalous value.",
                    "Add SNS topic or PagerDuty as notification action.",
                    "Add a dashboard widget for continuous visibility.",
                ],
                rollback_plan = "Delete alarm if it produces excessive noise.",
            ))

        return candidates


# ─────────────────────────────────────────────
# Remediation Agent
# ─────────────────────────────────────────────

class RemediationAgent:
    """
    Generates a ranked, actionable remediation plan for the incident.

    Parameters
    ----------
    bedrock_model_id : Bedrock Claude model
    region_name      : AWS region
    auto_execute     : If True, attempt to execute LOW-risk automated actions
    dry_run          : If True, never actually call AWS APIs
    """

    DEFAULT_MODEL  = "anthropic.claude-3-sonnet-20240229-v1:0"
    DEFAULT_REGION = "us-east-1"

    def __init__(
        self,
        bedrock_model_id: str = DEFAULT_MODEL,
        region_name: str = DEFAULT_REGION,
        auto_execute: bool = False,
        dry_run: bool = True,
    ):
        self.model_id     = bedrock_model_id
        self.region       = region_name
        self.auto_execute = auto_execute
        self.dry_run      = dry_run
        self.catalogue    = RemediationCatalogue()
        self._adapter     = get_adapter(region_name)
        logger.info(
            "RemediationAgent ready (auto_execute=%s, dry_run=%s)",
            auto_execute, dry_run,
        )

    # ── public ────────────────────────────────

    def remediate(self, incident_context: dict) -> dict:
        """
        Produce a ranked remediation plan and optionally execute safe actions.

        Parameters
        ----------
        incident_context : fully enriched dict from LogAgent

        Returns
        -------
        dict – final pipeline result with 'remediation_report' appended
        """
        logger.info(
            "RemediationAgent.remediate() — incident %s",
            incident_context.get("incident_id"),
        )

        # Step 1 – get candidate actions from rule catalogue
        candidates = self.catalogue.get_candidates(incident_context)

        # Step 2 – score and rank
        ranked = self._rank_actions(candidates, incident_context)

        # Step 3 – identify auto-executable actions
        auto_actions = [a for a in ranked if a.automated and a.risk_level == "LOW"]
        executed     = []
        if self.auto_execute and not self.dry_run:
            executed = self._execute_safe_actions(auto_actions, incident_context)

        # Step 4 – Bedrock final recommendation
        final_recommendation = self._bedrock_final_recommendation(
            incident_context, ranked[:5]
        )

        # Step 5 – assemble report
        severity  = incident_context.get("severity", "MEDIUM")
        immediate = [a for a in ranked if a.risk_level in ("LOW",) and a.priority <= 2]
        short_term = [a for a in ranked if a not in immediate][:3]

        remediation_report = {
            "total_actions_identified": len(ranked),
            "auto_executed_count":      len(executed),
            "top_actions": [asdict(a) for a in ranked[:5]],
            "immediate_actions":  [asdict(a) for a in immediate[:2]],
            "short_term_actions": [asdict(a) for a in short_term[:3]],
            "auto_executed":      executed,
            "final_recommendation": final_recommendation,
            "estimated_total_ttr_mins": sum(a.estimated_ttr_mins for a in ranked[:3]),
            "summary": (
                f"Remediation plan generated: {len(ranked)} actions identified. "
                f"{len(immediate)} immediate, {len(short_term)} short-term. "
                f"Estimated TTR: ~{sum(a.estimated_ttr_mins for a in ranked[:3])} mins."
            ),
        }

        enriched = dict(incident_context)
        enriched["remediation_report"] = remediation_report
        logger.info(
            "RemediationAgent done — %d actions, estimated TTR=%d mins",
            len(ranked),
            remediation_report["estimated_total_ttr_mins"],
        )
        return enriched

    # ── private helpers ───────────────────────

    @staticmethod
    def _rank_actions(
        actions: list[RemediationAction],
        incident_context: dict,
    ) -> list[RemediationAction]:
        """
        Score each action by composite metric:
            score = confidence * 0.4
                  + risk_penalty * 0.3
                  + speed_score * 0.3
        where lower score = lower priority number = executed first.
        """
        severity = incident_context.get("severity", "MEDIUM")
        severity_multiplier = {"CRITICAL": 1.5, "HIGH": 1.2, "MEDIUM": 1.0, "LOW": 0.8}.get(
            severity, 1.0
        )
        risk_penalty_map = {"LOW": 0, "MEDIUM": 0.2, "HIGH": 0.5, "CRITICAL": 1.0}

        scored: list[tuple[float, RemediationAction]] = []
        for action in actions:
            risk_penalty  = risk_penalty_map.get(action.risk_level, 0.5)
            speed_score   = max(0, 1.0 - action.estimated_ttr_mins / 60.0)
            raw_score     = (
                action.confidence    * 0.4 +
                (1 - risk_penalty)   * 0.3 +
                speed_score          * 0.3
            ) * severity_multiplier
            scored.append((raw_score, action))

        scored.sort(key=lambda x: x[0], reverse=True)
        ranked = []
        for i, (_, action) in enumerate(scored):
            action.priority = i + 1
            ranked.append(action)
        return ranked

    def _execute_safe_actions(
        self,
        actions: list[RemediationAction],
        incident_context: dict,
    ) -> list[dict]:
        """
        Execute AWS remediation actions that are LOW-risk and marked automated=True.
        Currently supports: Lambda concurrency increase.
        """
        executed: list[dict] = []
        lambda_client = boto3.client("lambda", region_name=self.region)
        for action in actions:
            try:
                if action.action_id == "LMB-002":
                    for fn in incident_context.get("affected_resources", {}).get("lambda", []):
                        lambda_client.put_function_concurrency(
                            FunctionName         = fn,
                            ReservedConcurrentExecutions = 500,
                        )
                        executed.append({
                            "action_id": action.action_id,
                            "resource":  f"lambda:{fn}",
                            "result":    "SUCCESS",
                            "details":   f"Reserved concurrency set to 500 for {fn}",
                        })
            except ClientError as exc:
                executed.append({
                    "action_id": action.action_id,
                    "result":    "FAILED",
                    "details":   str(exc),
                })
        return executed

    def _bedrock_final_recommendation(
        self,
        incident_context: dict,
        top_actions: list[RemediationAction],
    ) -> str:
        """Synthesise a final, executive-level recommendation from all pipeline findings."""
        triage     = incident_context.get("initial_triage", "")
        hypothesis = incident_context.get("log_report", {}).get("root_cause_hypothesis", "")
        actions_summary = [
            {"id": a.action_id, "title": a.title, "confidence": a.confidence,
             "risk": a.risk_level, "ttr_mins": a.estimated_ttr_mins}
            for a in top_actions
        ]

        prompt = f"""You are the Lead SRE summarising a production incident investigation.

INCIDENT
--------
ID       : {incident_context.get('incident_id', 'N/A')}
Severity : {incident_context.get('severity', 'UNKNOWN')}
Started  : {incident_context.get('start_time', 'N/A')}
Description: {incident_context.get('raw_description', 'N/A')[:400]}

ROOT CAUSE HYPOTHESIS (from log analysis)
-----------------------------------------
{hypothesis[:600]}

INITIAL AI TRIAGE
-----------------
{triage[:400]}

TOP REMEDIATION ACTIONS
-----------------------
{json.dumps(actions_summary, indent=2)}

Produce a JSON object (no markdown fences) with:
  "executive_summary"    – 3-4 sentence summary for leadership
  "recommended_action"   – single most important action to take RIGHT NOW
  "implementation_order" – ordered list of action IDs to execute
  "post_incident_tasks"  – list of 3 follow-up tasks to prevent recurrence
  "estimated_resolution" – plain-English time estimate (e.g. "10-20 minutes")
  "confidence_overall"   – LOW / MEDIUM / HIGH
"""
        try:
            result = self._adapter.invoke_json(
                model_id   = self.model_id,
                prompt     = prompt,
                max_tokens = 1024,
            )
            return json.dumps(result, indent=2)
        except Exception as exc:
            logger.warning("Bedrock final recommendation failed: %s", exc)
            action_ids = [a.action_id for a in top_actions]
            return json.dumps({
                "executive_summary":    "[Fallback] Incident analysis complete. See remediation actions.",
                "recommended_action":   top_actions[0].title if top_actions else "Review logs manually",
                "implementation_order": action_ids,
                "post_incident_tasks":  [
                    "Add CloudWatch alarms for all anomalous metrics.",
                    "Conduct post-mortem within 48 hours.",
                    "Update runbook with new remediation steps.",
                ],
                "estimated_resolution": f"{sum(a.estimated_ttr_mins for a in top_actions[:2])} minutes",
                "confidence_overall":   "LOW",
            }, indent=2)
