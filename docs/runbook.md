# Operator Runbook

## When to Use the CloudOps AI Agent

Invoke the agent for:

- Lambda functions with elevated error rates (> 5 %)
- EC2 instances with sustained high CPU (> 80 %)
- RDS instances with high CPU, connection exhaustion, or low free storage
- Any CRITICAL or HIGH severity incident where root cause is unclear
- Post-change verification after deployments

## Invoking via API Gateway

### cURL

```bash
curl -X POST https://<api-id>.execute-api.<region>.amazonaws.com/prod/investigate \
  -H "Content-Type: application/json" \
  -d '{
    "incident_description": "Lambda payments-processor has 40% error rate since 14:00 UTC",
    "resource_hints": {
      "lambda": ["payments-processor"],
      "rds": ["prod-payments-db"]
    }
  }'
```

### Python

```python
import boto3, json

lambda_client = boto3.client("lambda", region_name="us-east-1")

response = lambda_client.invoke(
    FunctionName = "cloudops-ai-agent",
    Payload      = json.dumps({
        "incident_description": "RDS prod-db at 95% CPU",
        "resource_hints": {"rds": ["prod-db"]},
    }).encode(),
)
result = json.loads(response["Payload"].read())
print(json.dumps(json.loads(result["body"]), indent=2))
```

## Interpreting the Response

```json
{
  "status": "SUCCESS",
  "incident_id": "INC-20240315140000-AB12CD34",
  "severity": "CRITICAL",
  "pipeline_result": {
    "overall_health": "CRITICAL",
    "metrics_summary": "3 resources checked, 2 anomalies detected.",
    "log_summary": "45 error events across 2 log groups.",
    "remediation_summary": "5 actions identified. Estimated TTR: 25 min.",
    "final_recommendation": "{ ... }",
    "estimated_ttr_minutes": 25
  }
}
```

### Status values

| Status | Meaning |
|--------|---------|
| `SUCCESS` | All 4 stages completed |
| `ERROR` | Pipeline failed; check `error.traceback` |

### Overall health values

| Health | Meaning |
|--------|---------|
| `CRITICAL` | One or more resources in critical state |
| `WARNING` | Degraded but not critical |
| `HEALTHY` | No anomalies detected |

## Common Remediation Actions

### Lambda error rate > 20%

1. Check `log_report.top_error_patterns` for the root error type.
2. If **Lambda Timeout**: increase timeout and/or memory (action LMB-001).
3. If **Connection Refused**: check RDS security groups (action NET-001).
4. If **ThrottlingException**: increase reserved concurrency (action LMB-002).

### EC2 CPU > 90%

1. Trigger ASG scale-out immediately (action EC2-001) — takes ~3 minutes.
2. While scaling, SSH via SSM and identify runaway processes (action EC2-002).
3. Review recent deployments for regression.

### RDS CPU > 80%

1. Enable Performance Insights and kill long-running queries (action RDS-002).
2. If connections are exhausted, deploy RDS Proxy (action RDS-003).
3. For sustained high CPU, create read replica (action RDS-001).

## Escalation

If the agent returns `confidence_overall: LOW` or the pipeline fails:

1. Check `agent_reports.logs.root_cause_hypothesis.next_investigation`
2. Escalate to the on-call senior engineer
3. Open a war room bridge
4. Check [AWS Service Health Dashboard](https://health.aws.amazon.com/health/status)

## Post-Incident

After resolution:

1. Run the agent one more time to verify `overall_health: HEALTHY`
2. Implement the `post_incident_tasks` from `final_recommendation`
3. Schedule a blameless post-mortem within 48 hours
4. Update this runbook if a new failure mode was discovered

## Monitoring the Agent Itself

| Metric | Alarm threshold |
|--------|----------------|
| `cloudops-ai-agent` Lambda errors | > 2 in 5 min |
| `cloudops-ai-agent` duration | > 250 seconds |
| `cloudops-ai-agent` throttles | > 0 |

CloudWatch log group: `/aws/lambda/cloudops-ai-agent`
