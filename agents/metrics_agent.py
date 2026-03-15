"""
Metrics Agent
=============
Second stage in the CloudOps AI pipeline.

Responsibilities
----------------
1. Consume the IncidentContext from IncidentAgent.
2. Query CloudWatch Metrics for every resource listed in the context.
3. Detect anomalies using statistical thresholds (z-score + IQR).
4. Invoke Amazon Bedrock to interpret raw numbers and surface
   meaningful operational insights.
5. Append a MetricsReport to the shared IncidentContext.
"""

import json
import logging
import statistics
from typing import Optional
import boto3
from botocore.exceptions import ClientError

from tools.cloudwatch_metrics import CloudWatchMetricsTool, CloudWatchMetricsError

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────
# Anomaly Detection
# ─────────────────────────────────────────────

class AnomalyDetector:
    """
    Lightweight statistical anomaly detector.

    Uses a combination of:
    - Z-score (|z| > threshold → anomalous)
    - IQR fence (value outside Q1-1.5*IQR / Q3+1.5*IQR)
    - Hard thresholds (CPU > 85 %, error count > 10, etc.)
    """

    Z_SCORE_THRESHOLD    = 2.5
    HIGH_CPU_THRESHOLD   = 85.0
    HIGH_ERROR_THRESHOLD = 10

    def detect(self, values: list[float], metric_name: str = "") -> dict:
        """
        Analyse a timeseries of metric values.

        Returns
        -------
        dict:
            is_anomalous   – bool
            anomaly_score  – 0.0 – 1.0
            anomaly_type   – str or None
            description    – human-readable finding
        """
        if not values or len(values) < 3:
            return self._no_data(metric_name)

        z_anomaly   = self._z_score_check(values)
        iqr_anomaly = self._iqr_check(values)
        hard_anom   = self._hard_threshold_check(values, metric_name)

        is_anomalous  = z_anomaly["flagged"] or iqr_anomaly["flagged"] or hard_anom["flagged"]
        anomaly_score = min(1.0, max(
            z_anomaly.get("score", 0),
            iqr_anomaly.get("score", 0),
            hard_anom.get("score", 0),
        ))
        anomaly_type = None
        description  = f"{metric_name}: No anomaly detected (latest={values[-1]:.2f})"

        if hard_anom["flagged"]:
            anomaly_type = "THRESHOLD_BREACH"
            description  = hard_anom["description"]
        elif z_anomaly["flagged"]:
            anomaly_type = "STATISTICAL_OUTLIER"
            description  = z_anomaly["description"]
        elif iqr_anomaly["flagged"]:
            anomaly_type = "IQR_OUTLIER"
            description  = iqr_anomaly["description"]

        return {
            "is_anomalous":  is_anomalous,
            "anomaly_score": round(anomaly_score, 3),
            "anomaly_type":  anomaly_type,
            "description":   description,
            "latest_value":  round(values[-1], 4),
            "mean_value":    round(statistics.mean(values), 4),
            "max_value":     round(max(values), 4),
        }

    def _z_score_check(self, values: list[float]) -> dict:
        if len(values) < 4:
            return {"flagged": False, "score": 0}
        mu, sigma = statistics.mean(values), statistics.stdev(values)
        if sigma == 0:
            return {"flagged": False, "score": 0}
        latest_z = abs((values[-1] - mu) / sigma)
        flagged  = latest_z > self.Z_SCORE_THRESHOLD
        return {
            "flagged":     flagged,
            "score":       min(1.0, latest_z / 5.0),
            "description": (
                f"Z-score={latest_z:.2f} (threshold={self.Z_SCORE_THRESHOLD}). "
                f"Latest={values[-1]:.2f}, mean={mu:.2f}."
            ),
        }

    @staticmethod
    def _iqr_check(values: list[float]) -> dict:
        sorted_vals = sorted(values)
        n = len(sorted_vals)
        q1  = sorted_vals[n // 4]
        q3  = sorted_vals[(3 * n) // 4]
        iqr = q3 - q1
        lower_fence = q1 - 1.5 * iqr
        upper_fence = q3 + 1.5 * iqr
        latest  = values[-1]
        flagged = latest < lower_fence or latest > upper_fence
        score   = 0.0
        if flagged and iqr > 0:
            deviation = max(latest - upper_fence, lower_fence - latest)
            score = min(1.0, deviation / (iqr or 1))
        return {
            "flagged":     flagged,
            "score":       round(score, 3),
            "description": (
                f"IQR fence [{lower_fence:.2f}, {upper_fence:.2f}]. "
                f"Latest={latest:.2f} {'outside' if flagged else 'inside'} fence."
            ),
        }

    def _hard_threshold_check(self, values: list[float], metric_name: str) -> dict:
        latest  = values[-1]
        flagged = False
        score   = 0.0
        description = ""
        metric_lower = metric_name.lower()

        if "cpu" in metric_lower and latest > self.HIGH_CPU_THRESHOLD:
            flagged = True
            score   = min(1.0, (latest - self.HIGH_CPU_THRESHOLD) / (100 - self.HIGH_CPU_THRESHOLD))
            description = f"CPU utilization {latest:.1f}% exceeds {self.HIGH_CPU_THRESHOLD}% threshold"

        elif "error" in metric_lower and latest > self.HIGH_ERROR_THRESHOLD:
            flagged = True
            score   = min(1.0, latest / 100)
            description = f"Error count {latest:.0f} exceeds {self.HIGH_ERROR_THRESHOLD} threshold"

        return {"flagged": flagged, "score": score, "description": description}

    @staticmethod
    def _no_data(metric_name: str) -> dict:
        return {
            "is_anomalous":  False,
            "anomaly_score": 0,
            "anomaly_type":  None,
            "description":   f"{metric_name}: insufficient data for anomaly detection",
            "latest_value":  0,
            "mean_value":    0,
            "max_value":     0,
        }


# ─────────────────────────────────────────────
# Metrics Agent
# ─────────────────────────────────────────────

class MetricsAgent:
    """
    Collects and analyses CloudWatch metrics for all affected resources
    identified by the IncidentAgent.

    Parameters
    ----------
    metrics_tool    : CloudWatchMetricsTool instance (injected)
    bedrock_model_id: Bedrock Claude model for metric interpretation
    region_name     : AWS region
    """

    DEFAULT_MODEL  = "anthropic.claude-3-sonnet-20240229-v1:0"
    DEFAULT_REGION = "us-east-1"

    def __init__(
        self,
        metrics_tool: Optional[CloudWatchMetricsTool] = None,
        bedrock_model_id: str = DEFAULT_MODEL,
        region_name: str = DEFAULT_REGION,
    ):
        self.metrics_tool = metrics_tool or CloudWatchMetricsTool(region_name=region_name)
        self.model_id     = bedrock_model_id
        self.region       = region_name
        self.detector     = AnomalyDetector()
        self._bedrock     = boto3.client("bedrock-runtime", region_name=region_name)
        logger.info("MetricsAgent ready")

    # ── public ────────────────────────────────

    def analyze(self, incident_context: dict) -> dict:
        """
        Analyse metrics for all resources in incident_context and
        return an enriched copy of the context.

        Parameters
        ----------
        incident_context : IncidentContext (or any dict with the same keys)

        Returns
        -------
        dict – original context augmented with 'metrics_report' key
        """
        logger.info("MetricsAgent.analyze() — incident %s", incident_context.get("incident_id"))

        resources  = incident_context.get("affected_resources", {})
        lookback   = incident_context.get("lookback_minutes", 60)
        raw_health = {}
        anomalies  = []

        # ── Lambda ────────────────────────────
        for fn_name in resources.get("lambda", []):
            try:
                health = self.metrics_tool.get_lambda_health(fn_name, minutes=lookback)
                raw_health[f"lambda:{fn_name}"] = health
                anomalies += self._detect_lambda_anomalies(fn_name, health)
            except CloudWatchMetricsError as exc:
                logger.warning("Lambda metrics failed for %s: %s", fn_name, exc)
                raw_health[f"lambda:{fn_name}"] = {"error": str(exc)}

        # ── EC2 ───────────────────────────────
        for inst_id in resources.get("ec2", []):
            try:
                health = self.metrics_tool.get_ec2_health(inst_id, minutes=lookback)
                raw_health[f"ec2:{inst_id}"] = health
                anomalies += self._detect_ec2_anomalies(inst_id, health)
            except CloudWatchMetricsError as exc:
                logger.warning("EC2 metrics failed for %s: %s", inst_id, exc)
                raw_health[f"ec2:{inst_id}"] = {"error": str(exc)}

        # ── RDS ───────────────────────────────
        for db_id in resources.get("rds", []):
            try:
                health = self.metrics_tool.get_rds_health(db_id, minutes=lookback)
                raw_health[f"rds:{db_id}"] = health
                anomalies += self._detect_rds_anomalies(db_id, health)
            except CloudWatchMetricsError as exc:
                logger.warning("RDS metrics failed for %s: %s", db_id, exc)
                raw_health[f"rds:{db_id}"] = {"error": str(exc)}

        # ── Bedrock interpretation ─────────────
        interpretation = self._bedrock_interpret(
            incident_context, raw_health, anomalies
        )

        # ── Overall roll-up ───────────────────
        critical_count = sum(
            1 for h in raw_health.values()
            if isinstance(h, dict) and h.get("overall_health") == "CRITICAL"
        )
        warning_count = sum(
            1 for h in raw_health.values()
            if isinstance(h, dict) and h.get("overall_health") == "WARNING"
        )
        overall = "CRITICAL" if critical_count > 0 else ("WARNING" if warning_count > 0 else "HEALTHY")

        metrics_report = {
            "overall_health":      overall,
            "resources_checked":   len(raw_health),
            "anomalies_detected":  len(anomalies),
            "critical_resources":  critical_count,
            "warning_resources":   warning_count,
            "resource_health":     raw_health,
            "anomalies":           anomalies,
            "interpretation":      interpretation,
            "summary": (
                f"Metrics analysis complete. {len(raw_health)} resources checked. "
                f"{len(anomalies)} anomalies detected. Overall: {overall}."
            ),
        }

        # Enrich and return context
        enriched = dict(incident_context)
        enriched["metrics_report"] = metrics_report
        logger.info(
            "MetricsAgent done — %d resources, %d anomalies, overall=%s",
            len(raw_health), len(anomalies), overall,
        )
        return enriched

    # ── anomaly helpers ───────────────────────

    def _detect_lambda_anomalies(self, fn_name: str, health: dict) -> list[dict]:
        anomalies = []
        metrics   = health.get("metrics", {})

        error_rate = metrics.get("error_rate_percent", 0)
        if error_rate > 5:
            anomalies.append({
                "resource":     f"lambda:{fn_name}",
                "metric":       "error_rate",
                "value":        error_rate,
                "anomaly_type": "THRESHOLD_BREACH",
                "severity":     "CRITICAL" if error_rate > 20 else "WARNING",
                "description":  f"Lambda {fn_name} error rate is {error_rate}%",
            })

        avg_dur = metrics.get("avg_duration_ms", 0)
        if avg_dur > 5000:
            anomalies.append({
                "resource":     f"lambda:{fn_name}",
                "metric":       "duration_ms",
                "value":        avg_dur,
                "anomaly_type": "LATENCY_SPIKE",
                "severity":     "CRITICAL" if avg_dur > 10_000 else "WARNING",
                "description":  f"Lambda {fn_name} avg duration {avg_dur:.0f}ms is elevated",
            })
        return anomalies

    def _detect_ec2_anomalies(self, inst_id: str, health: dict) -> list[dict]:
        anomalies = []
        avg_cpu   = health.get("metrics", {}).get("avg_cpu_percent", 0)
        result    = self.detector.detect([avg_cpu], metric_name="ec2_cpu")
        if result["is_anomalous"]:
            anomalies.append({
                "resource":     f"ec2:{inst_id}",
                "metric":       "cpu_utilization",
                "value":        avg_cpu,
                "anomaly_type": result["anomaly_type"],
                "severity":     "CRITICAL" if avg_cpu > 90 else "WARNING",
                "description":  f"EC2 {inst_id}: {result['description']}",
            })
        return anomalies

    def _detect_rds_anomalies(self, db_id: str, health: dict) -> list[dict]:
        anomalies = []
        metrics   = health.get("metrics", {})
        avg_cpu   = metrics.get("avg_cpu_percent", 0)
        free_gb   = metrics.get("free_storage_gb", 999)

        if avg_cpu > 70:
            anomalies.append({
                "resource":     f"rds:{db_id}",
                "metric":       "cpu_utilization",
                "value":        avg_cpu,
                "anomaly_type": "THRESHOLD_BREACH",
                "severity":     "CRITICAL" if avg_cpu > 85 else "WARNING",
                "description":  f"RDS {db_id} CPU at {avg_cpu:.1f}%",
            })
        if free_gb < 5:
            anomalies.append({
                "resource":     f"rds:{db_id}",
                "metric":       "free_storage_gb",
                "value":        free_gb,
                "anomaly_type": "THRESHOLD_BREACH",
                "severity":     "CRITICAL",
                "description":  f"RDS {db_id} only {free_gb:.2f} GB free storage",
            })
        return anomalies

    # ── Bedrock interpretation ────────────────

    def _bedrock_interpret(
        self,
        incident_context: dict,
        raw_health: dict,
        anomalies: list[dict],
    ) -> str:
        """Use Bedrock/Claude to generate a human-readable metrics interpretation."""
        health_summary = {
            k: {
                "overall_health": v.get("overall_health", "UNKNOWN"),
                "issues":         v.get("issues", []),
            }
            for k, v in raw_health.items()
            if isinstance(v, dict) and "error" not in v
        }

        prompt = f"""You are an AWS infrastructure monitoring expert.

INCIDENT CONTEXT
----------------
Severity    : {incident_context.get('severity', 'UNKNOWN')}
Description : {incident_context.get('raw_description', 'N/A')[:400]}

METRICS HEALTH SUMMARY
-----------------------
{json.dumps(health_summary, indent=2)}

ANOMALIES DETECTED
------------------
{json.dumps(anomalies, indent=2)}

Provide a concise JSON object (no markdown fences) with:
  "metrics_interpretation" – 2-3 sentences on what the metrics reveal
  "likely_bottleneck"      – the single most probable root-cause resource
  "correlation_clues"      – list of metric patterns that point to root cause
  "confidence_level"       – LOW / MEDIUM / HIGH based on data quality
"""
        try:
            body = json.dumps({
                "anthropic_version": "bedrock-2023-05-31",
                "max_tokens":        512,
                "messages":          [{"role": "user", "content": prompt}],
            })
            response = self._bedrock.invoke_model(
                modelId     = self.model_id,
                contentType = "application/json",
                accept      = "application/json",
                body        = body,
            )
            raw_text = json.loads(response["body"].read())
            content  = raw_text.get("content", [{}])[0].get("text", "")
            parsed   = json.loads(content)
            return json.dumps(parsed, indent=2)
        except Exception as exc:
            logger.warning("Bedrock metrics interpretation failed: %s", exc)
            anom_summary = "; ".join(a["description"] for a in anomalies[:3]) or "None"
            return json.dumps({
                "metrics_interpretation": (
                    f"[Fallback] Found {len(anomalies)} anomalies: {anom_summary}"
                ),
                "likely_bottleneck":  list(raw_health.keys())[0] if raw_health else "unknown",
                "correlation_clues":  [a["description"] for a in anomalies[:3]],
                "confidence_level":   "LOW",
            }, indent=2)
