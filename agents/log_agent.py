"""
Log Analysis Agent
==================
Third stage in the CloudOps AI pipeline.

Responsibilities
----------------
1. Consume IncidentContext (enriched with MetricsReport).
2. Query CloudWatch Logs for every affected resource.
3. Detect error patterns, identify recurring failures, and build
   a log-level timeline.
4. Correlate log findings with the metrics anomalies already discovered.
5. Call Amazon Bedrock to synthesise a root-cause hypothesis from
   the combined log evidence.
6. Append a LogReport to the shared context.
"""

import json
import logging
import re
from collections import Counter, defaultdict
from datetime import datetime, timezone
from typing import Optional
import boto3
from botocore.exceptions import ClientError
from agents.model_adapter import get_adapter

from tools.cloudwatch_logs import CloudWatchLogsTool, CloudWatchLogsError

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────
# Pattern Library
# ─────────────────────────────────────────────

class LogPatternLibrary:
    """
    Catalogued patterns for recognising common AWS failure modes in logs.
    Each entry is (name, compiled_regex, severity, category).
    """

    PATTERNS = [
        # Lambda
        ("Lambda Timeout",
         re.compile(r"Task timed out after", re.IGNORECASE),
         "CRITICAL", "TIMEOUT"),
        ("Lambda OOM",
         re.compile(r"Runtime exited with error.*signal: killed|out of memory", re.IGNORECASE),
         "CRITICAL", "RESOURCE"),
        ("Lambda Init Error",
         re.compile(r"Init phase failed|init_error", re.IGNORECASE),
         "HIGH", "STARTUP"),
        # General
        ("Connection Refused",
         re.compile(r"connection refused|ECONNREFUSED", re.IGNORECASE),
         "CRITICAL", "CONNECTIVITY"),
        ("Connection Timeout",
         re.compile(r"connection timed out|SocketTimeoutException", re.IGNORECASE),
         "HIGH", "CONNECTIVITY"),
        ("Database Error",
         re.compile(r"(mysql|postgres|dynamo).*(error|exception|failed)", re.IGNORECASE),
         "HIGH", "DATABASE"),
        ("HTTP 5xx",
         re.compile(r"\b5\d{2}\b.*(error|status)", re.IGNORECASE),
         "HIGH", "HTTP"),
        ("HTTP 4xx Spike",
         re.compile(r"\b4\d{2}\b.*(forbidden|unauthorized|not found)", re.IGNORECASE),
         "MEDIUM", "HTTP"),
        ("Permission Denied",
         re.compile(r"AccessDenied|not authorized|permission denied", re.IGNORECASE),
         "HIGH", "SECURITY"),
        ("Throttling",
         re.compile(r"ThrottlingException|Rate exceeded|Throttled", re.IGNORECASE),
         "HIGH", "THROTTLING"),
        ("OOM Killer",
         re.compile(r"Killed|OOM|Out Of Memory", re.IGNORECASE),
         "CRITICAL", "RESOURCE"),
        ("Unhandled Exception",
         re.compile(r"Unhandled exception|UnhandledPromiseRejection|panic:", re.IGNORECASE),
         "HIGH", "EXCEPTION"),
        ("Stack Overflow",
         re.compile(r"StackOverflow|stack overflow|maximum call stack", re.IGNORECASE),
         "HIGH", "EXCEPTION"),
        ("Disk Full",
         re.compile(r"no space left on device|disk full|ENOSPC", re.IGNORECASE),
         "CRITICAL", "RESOURCE"),
        ("SSL/TLS Error",
         re.compile(r"SSL|TLS|certificate.*error|CERT_", re.IGNORECASE),
         "HIGH", "SECURITY"),
    ]

    def match(self, message: str) -> list[dict]:
        """Return all patterns that match this log message."""
        results = []
        for name, regex, severity, category in self.PATTERNS:
            if regex.search(message):
                results.append({
                    "pattern_name": name,
                    "severity":     severity,
                    "category":     category,
                })
        return results


# ─────────────────────────────────────────────
# Log Correlation Engine
# ─────────────────────────────────────────────

class LogCorrelationEngine:
    """
    Correlates log findings with metric anomalies to surface
    the most probable cause-and-effect chains.
    """

    # Map log categories to metric signals
    CATEGORY_METRIC_MAP = {
        "TIMEOUT":      ["lambda_duration", "rds_read_latency", "apigw_latency"],
        "RESOURCE":     ["ec2_cpu", "ecs_memory", "lambda_duration"],
        "CONNECTIVITY": ["rds_connections", "ec2_network_in"],
        "THROTTLING":   ["lambda_throttles", "dynamo_read_throttle"],
        "DATABASE":     ["rds_cpu", "rds_connections", "rds_read_latency"],
        "HTTP":         ["apigw_5xx", "apigw_4xx", "apigw_latency"],
    }

    def correlate(
        self,
        log_patterns: list[dict],
        metric_anomalies: list[dict],
    ) -> list[dict]:
        """
        Produce a ranked list of correlated findings.

        Returns
        -------
        list of dicts with keys:
            log_category, log_pattern, related_metric_anomalies,
            correlation_strength (0.0–1.0), hypothesis
        """
        correlations = []
        metric_anom_by_type = defaultdict(list)
        for anomaly in metric_anomalies:
            metric_anom_by_type[anomaly.get("metric", "")].append(anomaly)

        seen_patterns: set = set()
        for pattern in log_patterns:
            category = pattern.get("category", "")
            key      = (pattern["pattern_name"], category)
            if key in seen_patterns:
                continue
            seen_patterns.add(key)

            related_metrics = self.CATEGORY_METRIC_MAP.get(category, [])
            related_anomalies = []
            for m in related_metrics:
                related_anomalies.extend(metric_anom_by_type.get(m, []))

            strength = 0.3
            if related_anomalies:
                strength = min(1.0, 0.5 + 0.25 * len(related_anomalies))

            hypothesis = self._build_hypothesis(pattern, related_anomalies)
            correlations.append({
                "log_category":             category,
                "log_pattern":              pattern["pattern_name"],
                "log_severity":             pattern["severity"],
                "related_metric_anomalies": related_anomalies[:3],
                "correlation_strength":     round(strength, 2),
                "hypothesis":               hypothesis,
            })

        # Sort by strength descending
        correlations.sort(key=lambda c: c["correlation_strength"], reverse=True)
        return correlations

    @staticmethod
    def _build_hypothesis(pattern: dict, related_anomalies: list) -> str:
        name     = pattern["pattern_name"]
        category = pattern["category"]
        if not related_anomalies:
            return f"Log pattern '{name}' in category {category} detected; no corroborating metric anomaly found."
        anom_descs = "; ".join(
            a.get("description", a.get("metric", "?")) for a in related_anomalies[:2]
        )
        return (
            f"'{name}' log pattern correlates with metric anomalies: {anom_descs}. "
            f"Likely root cause in the {category} subsystem."
        )


# ─────────────────────────────────────────────
# Log Agent
# ─────────────────────────────────────────────

class LogAgent:
    """
    Fetches and analyses CloudWatch Logs for all affected resources,
    then calls Bedrock for a root-cause hypothesis.

    Parameters
    ----------
    logs_tool        : CloudWatchLogsTool instance (injected)
    bedrock_model_id : Bedrock Claude model for log analysis
    region_name      : AWS region
    """

    DEFAULT_MODEL   = "anthropic.claude-3-sonnet-20240229-v1:0"
    DEFAULT_REGION  = "us-east-1"

    # Map resource service names → CloudWatch log group prefix pattern
    LOG_GROUP_TEMPLATES = {
        "lambda": "/aws/lambda/{name}",
        "rds":    "/aws/rds/instance/{name}/error",
        "ecs":    "/ecs/{name}",
        "ec2":    "/ec2/{name}",
        "apigw":  "API-Gateway-Execution-Logs_{name}",
    }

    def __init__(
        self,
        logs_tool: Optional[CloudWatchLogsTool] = None,
        bedrock_model_id: str = DEFAULT_MODEL,
        region_name: str = DEFAULT_REGION,
    ):
        self.logs_tool     = logs_tool or CloudWatchLogsTool(region_name=region_name)
        self.model_id      = bedrock_model_id
        self.region        = region_name
        self.pattern_lib   = LogPatternLibrary()
        self.correlator    = LogCorrelationEngine()
        self._adapter      = get_adapter(region_name)
        logger.info("LogAgent ready")

    # ── public ────────────────────────────────

    def analyze(self, incident_context: dict) -> dict:
        """
        Analyse logs for all affected resources and enrich context.

        Parameters
        ----------
        incident_context : dict (IncidentContext + MetricsReport)

        Returns
        -------
        dict – context with 'log_report' appended
        """
        logger.info("LogAgent.analyze() — incident %s", incident_context.get("incident_id"))

        resources        = incident_context.get("affected_resources", {})
        lookback         = incident_context.get("lookback_minutes", 60)
        metric_anomalies = (
            incident_context.get("metrics_report", {}).get("anomalies", [])
        )

        all_log_stats   : dict[str, dict]  = {}
        all_error_events: list[dict]        = []
        all_patterns    : list[dict]        = []

        # ── collect logs per resource ─────────
        for service, names in resources.items():
            template = self.LOG_GROUP_TEMPLATES.get(service)
            if not template:
                continue
            for name in names:
                log_group = template.format(name=name)
                resource_key = f"{service}:{name}"
                try:
                    stats = self.logs_tool.get_log_statistics(log_group, minutes=lookback)
                    all_log_stats[resource_key] = stats

                    errors = self.logs_tool.get_error_logs(log_group, minutes=lookback)
                    for evt in errors.get("events", [])[:50]:   # cap at 50 per resource
                        evt["resource"] = resource_key
                        all_error_events.append(evt)
                        patterns = self.pattern_lib.match(evt["message"])
                        for p in patterns:
                            p["resource"] = resource_key
                            p["timestamp"] = evt["timestamp"]
                            all_patterns.append(p)

                except CloudWatchLogsError as exc:
                    logger.warning("Log fetch failed for %s/%s: %s", service, name, exc)
                    all_log_stats[resource_key] = {"error": str(exc)}

        # ── pattern aggregation ───────────────
        pattern_counts = Counter(
            (p["pattern_name"], p["resource"]) for p in all_patterns
        )
        top_patterns: list[dict] = []
        for (pname, resource), count in pattern_counts.most_common(10):
            pat_meta = next(
                (p for p in all_patterns
                 if p["pattern_name"] == pname and p["resource"] == resource),
                {}
            )
            top_patterns.append({
                "pattern_name": pname,
                "resource":     resource,
                "count":        count,
                "severity":     pat_meta.get("severity", "UNKNOWN"),
                "category":     pat_meta.get("category", "UNKNOWN"),
                "last_seen":    pat_meta.get("timestamp", ""),
            })

        # ── error timeline ────────────────────
        error_timeline = self._build_error_timeline(all_error_events)

        # ── correlate with metrics ────────────
        correlations = self.correlator.correlate(all_patterns, metric_anomalies)

        # ── Bedrock root-cause hypothesis ─────
        hypothesis = self._bedrock_hypothesis(
            incident_context, all_log_stats, top_patterns, correlations
        )

        # ── health roll-up ────────────────────
        critical_groups = [
            k for k, v in all_log_stats.items()
            if isinstance(v, dict) and v.get("health_status") in ("CRITICAL", "DEGRADED")
        ]

        log_report = {
            "resources_analyzed":   len(all_log_stats),
            "total_error_events":   len(all_error_events),
            "unique_patterns":      len(set(p["pattern_name"] for p in all_patterns)),
            "log_statistics":       all_log_stats,
            "top_error_patterns":   top_patterns,
            "error_timeline":       error_timeline,
            "correlations":         correlations[:5],
            "root_cause_hypothesis": hypothesis,
            "critical_log_groups":  critical_groups,
            "summary": (
                f"Log analysis complete. {len(all_log_stats)} log groups scanned. "
                f"{len(all_error_events)} error events. "
                f"{len(top_patterns)} distinct error patterns. "
                f"{len(critical_groups)} critical log groups."
            ),
        }

        enriched = dict(incident_context)
        enriched["log_report"] = log_report
        logger.info(
            "LogAgent done — %d log groups, %d errors, %d patterns",
            len(all_log_stats), len(all_error_events), len(top_patterns),
        )
        return enriched

    # ── private helpers ───────────────────────

    @staticmethod
    def _build_error_timeline(error_events: list[dict]) -> list[dict]:
        """
        Bucket error events by minute to show volume-over-time.
        Returns the 20 busiest minute-buckets.
        """
        buckets: Counter = Counter()
        for evt in error_events:
            try:
                dt  = datetime.fromisoformat(evt["timestamp"])
                key = dt.strftime("%Y-%m-%dT%H:%M")
                buckets[key] += 1
            except (ValueError, KeyError):
                pass
        return [
            {"minute": ts, "error_count": cnt}
            for ts, cnt in sorted(buckets.most_common(20), key=lambda x: x[0])
        ]

    def _bedrock_hypothesis(
        self,
        incident_context: dict,
        log_stats: dict,
        top_patterns: list[dict],
        correlations: list[dict],
    ) -> str:
        """Call Bedrock to produce a root-cause hypothesis from log evidence."""
        stats_summary = {
            k: {
                "health_status":      v.get("health_status", "UNKNOWN"),
                "error_rate_percent": v.get("error_rate_percent", 0),
                "total_events":       v.get("total_events", 0),
            }
            for k, v in log_stats.items()
            if isinstance(v, dict) and "error" not in v
        }

        prompt = f"""You are a senior SRE performing root-cause analysis.

INCIDENT
--------
Severity    : {incident_context.get('severity', 'UNKNOWN')}
Description : {incident_context.get('raw_description', 'N/A')[:400]}
Initial Triage: {incident_context.get('initial_triage', '')[:400]}

LOG HEALTH SUMMARY
------------------
{json.dumps(stats_summary, indent=2)}

TOP ERROR PATTERNS
------------------
{json.dumps(top_patterns[:5], indent=2)}

LOG-METRIC CORRELATIONS
------------------------
{json.dumps([{k: v for k, v in c.items() if k != 'related_metric_anomalies'} for c in correlations[:3]], indent=2)}

Return a JSON object (no markdown fences) with:
  "root_cause_hypothesis"  – 2-3 sentences identifying the most likely root cause
  "evidence_chain"         – ordered list of 3-5 evidence items that support the hypothesis
  "confidence_level"       – LOW / MEDIUM / HIGH
  "next_investigation"     – what additional data would confirm/deny the hypothesis
"""
        try:
            result = self._adapter.invoke_json(
                model_id   = self.model_id,
                prompt     = prompt,
                max_tokens = 768,
            )
            return json.dumps(result, indent=2)
        except Exception as exc:
            logger.warning("Bedrock hypothesis failed: %s", exc)
            top_pattern_names = [p["pattern_name"] for p in top_patterns[:3]]
            return json.dumps({
                "root_cause_hypothesis": (
                    f"[Fallback] Dominant log patterns: {', '.join(top_pattern_names) or 'none'}. "
                    f"Correlations found: {len(correlations)}."
                ),
                "evidence_chain":        [c["hypothesis"] for c in correlations[:3]],
                "confidence_level":      "LOW",
                "next_investigation":    "Review full stack traces and recent deployment history.",
            }, indent=2)
