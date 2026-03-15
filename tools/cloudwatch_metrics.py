"""
CloudWatch Metrics Tool
=======================
Production-grade wrapper for AWS CloudWatch Metrics API.
Retrieves infrastructure performance data (CPU, memory, error rates,
latency, etc.) and computes health summaries for the AI agents.
"""

import boto3
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional
from botocore.exceptions import ClientError, BotoCoreError
from statistics import mean, stdev

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────
# Exceptions
# ─────────────────────────────────────────────

class CloudWatchMetricsError(Exception):
    """Raised when a CloudWatch Metrics operation fails."""


# ─────────────────────────────────────────────
# Metric Definitions
# ─────────────────────────────────────────────

METRIC_DEFINITIONS = {
    # Lambda
    "lambda_errors":      {"namespace": "AWS/Lambda",      "metric": "Errors",          "stat": "Sum"},
    "lambda_duration":    {"namespace": "AWS/Lambda",      "metric": "Duration",         "stat": "Average"},
    "lambda_throttles":   {"namespace": "AWS/Lambda",      "metric": "Throttles",        "stat": "Sum"},
    "lambda_invocations": {"namespace": "AWS/Lambda",      "metric": "Invocations",      "stat": "Sum"},
    # EC2
    "ec2_cpu":            {"namespace": "AWS/EC2",         "metric": "CPUUtilization",   "stat": "Average"},
    "ec2_network_in":     {"namespace": "AWS/EC2",         "metric": "NetworkIn",        "stat": "Sum"},
    "ec2_network_out":    {"namespace": "AWS/EC2",         "metric": "NetworkOut",       "stat": "Sum"},
    # RDS
    "rds_cpu":            {"namespace": "AWS/RDS",         "metric": "CPUUtilization",   "stat": "Average"},
    "rds_connections":    {"namespace": "AWS/RDS",         "metric": "DatabaseConnections", "stat": "Average"},
    "rds_free_storage":   {"namespace": "AWS/RDS",         "metric": "FreeStorageSpace", "stat": "Average"},
    "rds_read_latency":   {"namespace": "AWS/RDS",         "metric": "ReadLatency",      "stat": "Average"},
    "rds_write_latency":  {"namespace": "AWS/RDS",         "metric": "WriteLatency",     "stat": "Average"},
    # API Gateway
    "apigw_4xx":          {"namespace": "AWS/ApiGateway",  "metric": "4XXError",         "stat": "Sum"},
    "apigw_5xx":          {"namespace": "AWS/ApiGateway",  "metric": "5XXError",         "stat": "Sum"},
    "apigw_latency":      {"namespace": "AWS/ApiGateway",  "metric": "Latency",          "stat": "Average"},
    # ECS
    "ecs_cpu":            {"namespace": "AWS/ECS",         "metric": "CPUUtilization",   "stat": "Average"},
    "ecs_memory":         {"namespace": "AWS/ECS",         "metric": "MemoryUtilization","stat": "Average"},
    # DynamoDB
    "dynamo_read_throttle":  {"namespace": "AWS/DynamoDB", "metric": "ReadThrottledRequests",  "stat": "Sum"},
    "dynamo_write_throttle": {"namespace": "AWS/DynamoDB", "metric": "WriteThrottledRequests", "stat": "Sum"},
    "dynamo_latency":        {"namespace": "AWS/DynamoDB", "metric": "SuccessfulRequestLatency","stat": "Average"},
}

# Thresholds for health classification
THRESHOLDS = {
    "ec2_cpu":          {"warning": 70, "critical": 90},
    "rds_cpu":          {"warning": 70, "critical": 85},
    "ecs_cpu":          {"warning": 75, "critical": 90},
    "ecs_memory":       {"warning": 80, "critical": 95},
    "lambda_errors":    {"warning": 5,  "critical": 20},   # count
    "lambda_throttles": {"warning": 1,  "critical": 10},   # count
    "apigw_5xx":        {"warning": 5,  "critical": 20},   # count
}


# ─────────────────────────────────────────────
# Tool Class
# ─────────────────────────────────────────────

class CloudWatchMetricsTool:
    """
    Encapsulates all CloudWatch Metrics interactions for the AI agents.

    Usage
    -----
    tool = CloudWatchMetricsTool(region_name="us-east-1")

    # Lambda health
    health = tool.get_lambda_health("my-function", minutes=60)

    # EC2 health
    health = tool.get_ec2_health("i-0123456789abcdef0", minutes=60)

    # Raw metric datapoints
    data = tool.get_metric_data("AWS/EC2", "CPUUtilization",
                                [{"Name": "InstanceId", "Value": "i-xxx"}],
                                period_seconds=300, minutes=60)

    # Full infrastructure overview
    summary = tool.get_infrastructure_health(resource_ids={...}, minutes=60)
    """

    def __init__(
        self,
        region_name: str = "us-east-1",
        default_period_seconds: int = 300,
        default_lookback_minutes: int = 60,
    ):
        self.region_name = region_name
        self.default_period = default_period_seconds
        self.default_lookback = default_lookback_minutes
        self._client = boto3.client("cloudwatch", region_name=region_name)
        logger.info("CloudWatchMetricsTool initialized (region=%s)", region_name)

    # ── helpers ───────────────────────────────

    def _time_range(self, minutes: Optional[int]) -> tuple[datetime, datetime]:
        end   = datetime.now(timezone.utc)
        start = end - timedelta(minutes=minutes or self.default_lookback)
        return start, end

    @staticmethod
    def _classify_health(metric_name: str, value: float) -> str:
        thresholds = THRESHOLDS.get(metric_name)
        if thresholds is None:
            return "UNKNOWN"
        if value >= thresholds["critical"]:
            return "CRITICAL"
        if value >= thresholds["warning"]:
            return "WARNING"
        return "HEALTHY"

    @staticmethod
    def _stats(values: list[float]) -> dict:
        if not values:
            return {"min": 0, "max": 0, "avg": 0, "stddev": 0, "latest": 0, "datapoints": 0}
        return {
            "min":        round(min(values), 4),
            "max":        round(max(values), 4),
            "avg":        round(mean(values), 4),
            "stddev":     round(stdev(values), 4) if len(values) > 1 else 0,
            "latest":     round(values[-1], 4),
            "datapoints": len(values),
        }

    # ── core metric fetch ─────────────────────

    def get_metric_data(
        self,
        namespace: str,
        metric_name: str,
        dimensions: list[dict],
        stat: str = "Average",
        period_seconds: Optional[int] = None,
        minutes: Optional[int] = None,
    ) -> dict:
        """
        Fetch raw CloudWatch metric datapoints.

        Returns
        -------
        dict with keys: namespace, metric, dimensions, stat,
                        values (list[float]), timestamps (list[str]),
                        statistics (computed summary), unit
        """
        start, end = self._time_range(minutes)
        period = period_seconds or self.default_period

        query = {
            "Id":         "m1",
            "MetricStat": {
                "Metric": {
                    "Namespace":  namespace,
                    "MetricName": metric_name,
                    "Dimensions": dimensions,
                },
                "Period": period,
                "Stat":   stat,
            },
            "ReturnData": True,
        }

        try:
            response = self._client.get_metric_data(
                MetricDataQueries=[query],
                StartTime=start,
                EndTime=end,
            )
        except ClientError as exc:
            raise CloudWatchMetricsError(
                f"Failed to fetch {namespace}/{metric_name}: {exc}"
            ) from exc
        except BotoCoreError as exc:
            raise CloudWatchMetricsError(f"BotoCore error: {exc}") from exc

        result_list = response.get("MetricDataResults", [{}])
        result      = result_list[0] if result_list else {}

        # Pair timestamps + values, sort chronologically
        pairs = sorted(
            zip(result.get("Timestamps", []), result.get("Values", [])),
            key=lambda p: p[0],
        )
        timestamps = [ts.isoformat() for ts, _ in pairs]
        values     = [v for _, v in pairs]

        return {
            "namespace":  namespace,
            "metric":     metric_name,
            "dimensions": dimensions,
            "stat":       stat,
            "unit":       result.get("Label", ""),
            "values":     values,
            "timestamps": timestamps,
            "statistics": self._stats(values),
        }

    # ── service-level health helpers ──────────

    def get_lambda_health(
        self,
        function_name: str,
        minutes: Optional[int] = None,
    ) -> dict:
        """Return a comprehensive health summary for an AWS Lambda function."""
        dims = [{"Name": "FunctionName", "Value": function_name}]
        lookback = minutes or self.default_lookback

        errors      = self.get_metric_data("AWS/Lambda", "Errors",      dims, stat="Sum",     minutes=lookback)
        invocations = self.get_metric_data("AWS/Lambda", "Invocations",  dims, stat="Sum",     minutes=lookback)
        duration    = self.get_metric_data("AWS/Lambda", "Duration",     dims, stat="Average", minutes=lookback)
        throttles   = self.get_metric_data("AWS/Lambda", "Throttles",    dims, stat="Sum",     minutes=lookback)

        total_errors = sum(errors["values"])
        total_invs   = sum(invocations["values"])
        error_rate   = round((total_errors / total_invs * 100), 2) if total_invs > 0 else 0.0

        issues = []
        if total_errors > THRESHOLDS["lambda_errors"]["critical"]:
            issues.append(f"CRITICAL: {total_errors} errors in {lookback}m")
        elif total_errors > THRESHOLDS["lambda_errors"]["warning"]:
            issues.append(f"WARNING: {total_errors} errors in {lookback}m")

        avg_dur = duration["statistics"]["avg"]
        if avg_dur > 10_000:
            issues.append(f"CRITICAL: avg duration {avg_dur:.0f}ms (near timeout)")
        elif avg_dur > 5_000:
            issues.append(f"WARNING: avg duration {avg_dur:.0f}ms (high latency)")

        total_throttles = sum(throttles["values"])
        if total_throttles > 0:
            issues.append(f"Throttles detected: {total_throttles} events")

        # Overall health
        if any("CRITICAL" in i for i in issues):
            overall = "CRITICAL"
        elif any("WARNING" in i for i in issues):
            overall = "WARNING"
        elif issues:
            overall = "DEGRADED"
        else:
            overall = "HEALTHY"

        return {
            "resource_type":   "Lambda",
            "function_name":   function_name,
            "time_range_minutes": lookback,
            "overall_health":  overall,
            "metrics": {
                "total_invocations": int(total_invs),
                "total_errors":      int(total_errors),
                "error_rate_percent": error_rate,
                "avg_duration_ms":   avg_dur,
                "max_duration_ms":   duration["statistics"]["max"],
                "total_throttles":   int(total_throttles),
            },
            "issues":  issues,
            "summary": (
                f"Lambda '{function_name}' — {overall}. "
                f"Invocations: {int(total_invs)}, Errors: {int(total_errors)} "
                f"({error_rate}%), Avg Duration: {avg_dur:.0f}ms."
            ),
        }

    def get_ec2_health(
        self,
        instance_id: str,
        minutes: Optional[int] = None,
    ) -> dict:
        """Return a health summary for an EC2 instance."""
        dims    = [{"Name": "InstanceId", "Value": instance_id}]
        lookback = minutes or self.default_lookback

        cpu_data = self.get_metric_data("AWS/EC2", "CPUUtilization", dims, minutes=lookback)
        net_in   = self.get_metric_data("AWS/EC2", "NetworkIn",  dims, stat="Sum", minutes=lookback)
        net_out  = self.get_metric_data("AWS/EC2", "NetworkOut", dims, stat="Sum", minutes=lookback)

        avg_cpu = cpu_data["statistics"]["avg"]
        max_cpu = cpu_data["statistics"]["max"]
        cpu_health = self._classify_health("ec2_cpu", avg_cpu)

        issues = []
        if cpu_health == "CRITICAL":
            issues.append(f"CRITICAL: avg CPU {avg_cpu:.1f}% (max {max_cpu:.1f}%)")
        elif cpu_health == "WARNING":
            issues.append(f"WARNING: avg CPU {avg_cpu:.1f}%")

        return {
            "resource_type": "EC2",
            "instance_id":   instance_id,
            "time_range_minutes": lookback,
            "overall_health": cpu_health,
            "metrics": {
                "avg_cpu_percent":  avg_cpu,
                "max_cpu_percent":  max_cpu,
                "total_network_in_bytes":  sum(net_in["values"]),
                "total_network_out_bytes": sum(net_out["values"]),
            },
            "issues": issues,
            "summary": (
                f"EC2 '{instance_id}' — {cpu_health}. "
                f"CPU avg: {avg_cpu:.1f}%, max: {max_cpu:.1f}%."
            ),
        }

    def get_rds_health(
        self,
        db_instance_id: str,
        minutes: Optional[int] = None,
    ) -> dict:
        """Return a health summary for an RDS instance."""
        dims     = [{"Name": "DBInstanceIdentifier", "Value": db_instance_id}]
        lookback = minutes or self.default_lookback

        cpu_data   = self.get_metric_data("AWS/RDS", "CPUUtilization",     dims, minutes=lookback)
        conn_data  = self.get_metric_data("AWS/RDS", "DatabaseConnections", dims, minutes=lookback)
        read_lat   = self.get_metric_data("AWS/RDS", "ReadLatency",         dims, minutes=lookback)
        write_lat  = self.get_metric_data("AWS/RDS", "WriteLatency",        dims, minutes=lookback)
        free_stor  = self.get_metric_data("AWS/RDS", "FreeStorageSpace",    dims, minutes=lookback)

        avg_cpu = cpu_data["statistics"]["avg"]
        cpu_health = self._classify_health("rds_cpu", avg_cpu)

        free_gb = (free_stor["statistics"]["latest"] or 0) / (1024 ** 3)
        issues  = []
        if cpu_health in ("CRITICAL", "WARNING"):
            issues.append(f"{cpu_health}: RDS CPU at {avg_cpu:.1f}%")
        if free_gb < 5:
            issues.append(f"CRITICAL: Only {free_gb:.2f} GB free storage remaining")

        return {
            "resource_type":  "RDS",
            "db_instance_id": db_instance_id,
            "time_range_minutes": lookback,
            "overall_health": cpu_health,
            "metrics": {
                "avg_cpu_percent":        avg_cpu,
                "avg_connections":        conn_data["statistics"]["avg"],
                "avg_read_latency_ms":    round(read_lat["statistics"]["avg"] * 1000, 3),
                "avg_write_latency_ms":   round(write_lat["statistics"]["avg"] * 1000, 3),
                "free_storage_gb":        round(free_gb, 2),
            },
            "issues": issues,
            "summary": (
                f"RDS '{db_instance_id}' — {cpu_health}. "
                f"CPU: {avg_cpu:.1f}%, Free storage: {free_gb:.2f} GB."
            ),
        }

    def get_infrastructure_health(
        self,
        resource_ids: Optional[dict] = None,
        minutes: Optional[int] = None,
    ) -> dict:
        """
        Aggregate health across Lambda, EC2, and RDS resources.

        Parameters
        ----------
        resource_ids : dict, e.g.
            {
                "lambda":  ["fn-api", "fn-worker"],
                "ec2":     ["i-0abc123"],
                "rds":     ["prod-db-1"],
            }

        Returns
        -------
        dict with per-service health and an overall_health roll-up
        """
        resource_ids = resource_ids or {}
        lookback = minutes or self.default_lookback
        results: dict = {"time_range_minutes": lookback, "services": {}}

        statuses: list[str] = []

        for fn_name in resource_ids.get("lambda", []):
            h = self.get_lambda_health(fn_name, minutes=lookback)
            results["services"].setdefault("lambda", {})[fn_name] = h
            statuses.append(h["overall_health"])

        for inst_id in resource_ids.get("ec2", []):
            h = self.get_ec2_health(inst_id, minutes=lookback)
            results["services"].setdefault("ec2", {})[inst_id] = h
            statuses.append(h["overall_health"])

        for db_id in resource_ids.get("rds", []):
            h = self.get_rds_health(db_id, minutes=lookback)
            results["services"].setdefault("rds", {})[db_id] = h
            statuses.append(h["overall_health"])

        # Roll-up: take worst status
        priority = {"CRITICAL": 3, "WARNING": 2, "DEGRADED": 1, "HEALTHY": 0, "UNKNOWN": -1}
        overall = max(statuses, key=lambda s: priority.get(s, -1)) if statuses else "UNKNOWN"
        results["overall_health"] = overall
        results["summary"] = (
            f"Infrastructure check across {len(statuses)} resources — Overall: {overall}"
        )
        return results
