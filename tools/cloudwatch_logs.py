"""
CloudWatch Logs Tool
====================
Production-grade wrapper for AWS CloudWatch Logs API.
Provides structured log retrieval, filtering, and pattern analysis
for use by the CloudOps AI Agent system.
"""

import boto3
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Optional
from botocore.exceptions import ClientError, BotoCoreError

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────
# Exceptions
# ─────────────────────────────────────────────

class CloudWatchLogsError(Exception):
    """Raised when a CloudWatch Logs operation fails."""


# ─────────────────────────────────────────────
# Tool Class
# ─────────────────────────────────────────────

class CloudWatchLogsTool:
    """
    Encapsulates all CloudWatch Logs interactions needed by the AI agents.

    Usage
    -----
    tool = CloudWatchLogsTool(region_name="us-east-1")
    entries = tool.get_recent_logs("/aws/lambda/my-function", minutes=30)
    errors  = tool.get_error_logs("/aws/lambda/my-function")
    results = tool.search_logs("/aws/lambda/my-function", "TimeoutError")
    stats   = tool.get_log_statistics("/aws/lambda/my-function")
    """

    # Severity keywords used for log classification
    ERROR_KEYWORDS   = ["ERROR", "Exception", "Traceback", "FATAL", "CRITICAL", "failed", "failure"]
    WARNING_KEYWORDS = ["WARN", "WARNING", "Timeout", "retry", "throttl"]
    INFO_KEYWORDS    = ["INFO", "Started", "Completed", "Success"]

    def __init__(
        self,
        region_name: str = "us-east-1",
        max_results: int = 1000,
        default_lookback_minutes: int = 60,
    ):
        self.region_name = region_name
        self.max_results = max_results
        self.default_lookback_minutes = default_lookback_minutes

        self._client = boto3.client("logs", region_name=region_name)
        logger.info("CloudWatchLogsTool initialized (region=%s)", region_name)

    # ── helpers ───────────────────────────────

    def _epoch_ms(self, dt: datetime) -> int:
        return int(dt.timestamp() * 1000)

    def _now_utc(self) -> datetime:
        return datetime.now(timezone.utc)

    def _time_range(self, minutes: Optional[int]) -> tuple[int, int]:
        lookback = minutes or self.default_lookback_minutes
        end   = self._now_utc()
        start = end - timedelta(minutes=lookback)
        return self._epoch_ms(start), self._epoch_ms(end)

    def _classify_severity(self, message: str) -> str:
        msg_upper = message.upper()
        for kw in self.ERROR_KEYWORDS:
            if kw.upper() in msg_upper:
                return "ERROR"
        for kw in self.WARNING_KEYWORDS:
            if kw.upper() in msg_upper:
                return "WARNING"
        return "INFO"

    # ── public API ────────────────────────────

    def get_recent_logs(
        self,
        log_group_name: str,
        minutes: Optional[int] = None,
        log_stream_name: Optional[str] = None,
        filter_pattern: str = "",
    ) -> dict:
        """
        Retrieve log events from a CloudWatch log group.

        Parameters
        ----------
        log_group_name   : CloudWatch log group name, e.g. "/aws/lambda/my-fn"
        minutes          : How far back to look (default: ``default_lookback_minutes``)
        log_stream_name  : Optional stream name filter
        filter_pattern   : Optional CloudWatch filter-pattern string

        Returns
        -------
        dict with keys:
            log_group, time_range_minutes, total_events,
            events (list of dicts), summary
        """
        start_ms, end_ms = self._time_range(minutes)
        minutes_used = minutes or self.default_lookback_minutes

        kwargs: dict = {
            "logGroupName": log_group_name,
            "startTime": start_ms,
            "endTime": end_ms,
            "limit": self.max_results,
        }
        if log_stream_name:
            kwargs["logStreamNames"] = [log_stream_name]
        if filter_pattern:
            kwargs["filterPattern"] = filter_pattern

        events = []
        try:
            paginator = self._client.get_paginator("filter_log_events")
            for page in paginator.paginate(**{k: v for k, v in kwargs.items() if k != "limit"}):
                for event in page.get("events", []):
                    events.append({
                        "timestamp": datetime.fromtimestamp(
                            event["timestamp"] / 1000, tz=timezone.utc
                        ).isoformat(),
                        "message":   event["message"].strip(),
                        "severity":  self._classify_severity(event["message"]),
                        "stream":    event.get("logStreamName", "unknown"),
                    })
                if len(events) >= self.max_results:
                    break

        except ClientError as exc:
            code = exc.response["Error"]["Code"]
            if code == "ResourceNotFoundException":
                logger.warning("Log group not found: %s", log_group_name)
                return self._empty_result(log_group_name, minutes_used, "Log group not found")
            raise CloudWatchLogsError(f"CloudWatch error: {exc}") from exc
        except BotoCoreError as exc:
            raise CloudWatchLogsError(f"BotoCore error: {exc}") from exc

        # Summarise
        error_count   = sum(1 for e in events if e["severity"] == "ERROR")
        warning_count = sum(1 for e in events if e["severity"] == "WARNING")

        return {
            "log_group":          log_group_name,
            "time_range_minutes": minutes_used,
            "total_events":       len(events),
            "error_count":        error_count,
            "warning_count":      warning_count,
            "events":             events,
            "summary": (
                f"Retrieved {len(events)} log events from {log_group_name} "
                f"over the last {minutes_used} minutes. "
                f"Found {error_count} errors and {warning_count} warnings."
            ),
        }

    def get_error_logs(
        self,
        log_group_name: str,
        minutes: Optional[int] = None,
    ) -> dict:
        """
        Return only ERROR-level log events.
        Uses CloudWatch's built-in filter for efficiency.
        """
        error_pattern = "?ERROR ?Exception ?Traceback ?FATAL ?CRITICAL"
        result = self.get_recent_logs(
            log_group_name,
            minutes=minutes,
            filter_pattern=error_pattern,
        )
        result["events"] = [e for e in result["events"] if e["severity"] == "ERROR"]
        result["total_events"] = len(result["events"])
        result["summary"] = (
            f"Found {result['total_events']} error events in {log_group_name} "
            f"over the last {result['time_range_minutes']} minutes."
        )
        return result

    def search_logs(
        self,
        log_group_name: str,
        search_term: str,
        minutes: Optional[int] = None,
    ) -> dict:
        """
        Search logs for a specific term/pattern.
        Returns matching events with context.
        """
        result = self.get_recent_logs(
            log_group_name,
            minutes=minutes,
            filter_pattern=f'"{search_term}"',
        )
        result["search_term"] = search_term
        result["summary"] = (
            f"Found {result['total_events']} events matching '{search_term}' "
            f"in {log_group_name}."
        )
        return result

    def get_log_statistics(
        self,
        log_group_name: str,
        minutes: Optional[int] = None,
    ) -> dict:
        """
        Compute log-level distribution and top recurring error messages.

        Returns
        -------
        dict with keys:
            log_group, total_events, severity_breakdown,
            top_errors (list), error_rate_percent, health_status
        """
        result = self.get_recent_logs(log_group_name, minutes=minutes)
        events = result["events"]

        if not events:
            return {
                "log_group":         log_group_name,
                "total_events":      0,
                "severity_breakdown": {"ERROR": 0, "WARNING": 0, "INFO": 0},
                "top_errors":        [],
                "error_rate_percent": 0.0,
                "health_status":     "NO_DATA",
            }

        severity_breakdown: dict[str, int] = {"ERROR": 0, "WARNING": 0, "INFO": 0}
        error_messages: dict[str, int] = {}

        for event in events:
            sev = event["severity"]
            severity_breakdown[sev] = severity_breakdown.get(sev, 0) + 1
            if sev == "ERROR":
                # Truncate long messages for grouping
                key = event["message"][:120]
                error_messages[key] = error_messages.get(key, 0) + 1

        total = len(events)
        error_count = severity_breakdown["ERROR"]
        error_rate = round((error_count / total) * 100, 2) if total > 0 else 0.0

        if error_rate >= 25:
            health_status = "CRITICAL"
        elif error_rate >= 10:
            health_status = "DEGRADED"
        elif error_rate >= 1:
            health_status = "WARNING"
        else:
            health_status = "HEALTHY"

        top_errors = sorted(error_messages.items(), key=lambda x: x[1], reverse=True)[:5]

        return {
            "log_group":          log_group_name,
            "time_range_minutes": result["time_range_minutes"],
            "total_events":       total,
            "severity_breakdown": severity_breakdown,
            "top_errors":         [{"message": msg, "count": cnt} for msg, cnt in top_errors],
            "error_rate_percent": error_rate,
            "health_status":      health_status,
            "summary": (
                f"{log_group_name}: {total} events — "
                f"{error_count} errors ({error_rate}%) — "
                f"Status: {health_status}"
            ),
        }

    def list_log_groups(self, prefix: str = "/aws/") -> list[dict]:
        """Return available log groups (useful for discovery)."""
        groups = []
        try:
            paginator = self._client.get_paginator("describe_log_groups")
            for page in paginator.paginate(logGroupNamePrefix=prefix):
                for group in page.get("logGroups", []):
                    groups.append({
                        "name":             group["logGroupName"],
                        "retention_days":   group.get("retentionInDays", "Never"),
                        "stored_bytes":     group.get("storedBytes", 0),
                    })
        except ClientError as exc:
            raise CloudWatchLogsError(f"Failed to list log groups: {exc}") from exc
        return groups

    # ── private helpers ───────────────────────

    @staticmethod
    def _empty_result(log_group: str, minutes: int, reason: str) -> dict:
        return {
            "log_group":          log_group,
            "time_range_minutes": minutes,
            "total_events":       0,
            "error_count":        0,
            "warning_count":      0,
            "events":             [],
            "summary":            reason,
        }
