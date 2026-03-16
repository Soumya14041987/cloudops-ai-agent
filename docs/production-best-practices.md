# 9️⃣ Production Best Practices — CloudOps AI Agent

> A complete, battle-tested guide for running the CloudOps AI Agent reliably,
> securely, and cost-effectively at production scale on AWS.
> Every section includes copy-paste code, exact AWS Console steps, and
> thresholds tuned specifically for this project.

---

## Table of Contents

1. [Security Hardening](#1-security-hardening)
2. [Observability and Monitoring](#2-observability-and-monitoring)
3. [Reliability and Resilience](#3-reliability-and-resilience)
4. [Performance and Scalability](#4-performance-and-scalability)
5. [Cost Optimisation](#5-cost-optimisation)
6. [CI/CD and Zero-Downtime Deployment](#6-cicd-and-zero-downtime-deployment)
7. [AI and Model Governance](#7-ai-and-model-governance)
8. [Incident Response Runbooks](#8-incident-response-runbooks)
9. [Compliance and Data Governance](#9-compliance-and-data-governance)

---

## 1. Security Hardening

### 1.1 IAM — Principle of Least Privilege

The Lambda execution role should contain only what each AWS service needs.
Replace your current wildcard policy with this scoped version:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "LambdaLogs",
      "Effect": "Allow",
      "Action": [
        "logs:CreateLogGroup",
        "logs:CreateLogStream",
        "logs:PutLogEvents"
      ],
      "Resource": "arn:aws:logs:us-east-1:ACCOUNT_ID:log-group:/aws/lambda/cloudops-ai-agent:*"
    },
    {
      "Sid": "BedrockInvokeScopedModels",
      "Effect": "Allow",
      "Action": ["bedrock:InvokeModel"],
      "Resource": [
        "arn:aws:bedrock:us-east-1::foundation-model/amazon.nova-pro-v1:0",
        "arn:aws:bedrock:us-east-1::foundation-model/amazon.nova-lite-v1:0",
        "arn:aws:bedrock:us-east-1::foundation-model/amazon.nova-micro-v1:0",
        "arn:aws:bedrock:us-east-1::foundation-model/anthropic.claude-3-sonnet*"
      ]
    },
    {
      "Sid": "CloudWatchMetricsRead",
      "Effect": "Allow",
      "Action": ["cloudwatch:GetMetricData", "cloudwatch:DescribeAlarms"],
      "Resource": "*"
    },
    {
      "Sid": "CloudWatchLogsRead",
      "Effect": "Allow",
      "Action": [
        "logs:FilterLogEvents",
        "logs:DescribeLogGroups",
        "logs:GetLogEvents"
      ],
      "Resource": "arn:aws:logs:us-east-1:ACCOUNT_ID:log-group:/aws/*:*"
    },
    {
      "Sid": "DynamoDBIncidentHistory",
      "Effect": "Allow",
      "Action": [
        "dynamodb:PutItem",
        "dynamodb:GetItem",
        "dynamodb:Query"
      ],
      "Resource": "arn:aws:dynamodb:us-east-1:ACCOUNT_ID:table/cloudops-incidents"
    },
    {
      "Sid": "LambdaConcurrencyUpdate",
      "Effect": "Allow",
      "Action": ["lambda:PutFunctionConcurrency"],
      "Resource": "arn:aws:lambda:us-east-1:ACCOUNT_ID:function:*"
    }
  ]
}
```

> Rule: Start with `Resource: "*"` during development. Before production,
> replace every `*` with the exact ARN.

---

### 1.2 Never Store Secrets in Environment Variables

| What | Bad practice | Good practice |
|------|-------------|---------------|
| Slack webhook URL | Lambda env var (plaintext) | AWS Secrets Manager |
| PagerDuty routing key | Hardcoded in code | Secrets Manager + auto-rotation |
| Database passwords | `.env` file | SSM Parameter Store (SecureString) |
| API tokens | Git repo | Secrets Manager |

Fetch secrets at cold start (cached for warm invocations):

```python
# agents/secrets.py
import boto3, json, logging

logger = logging.getLogger(__name__)
_cache: dict = {}

def get_secret(secret_name: str, region: str = "us-east-1") -> dict:
    """
    Fetch from Secrets Manager. Cached per Lambda container lifecycle.
    Fetched only once per cold start — free on warm invocations.
    """
    if secret_name not in _cache:
        client = boto3.client("secretsmanager", region_name=region)
        response = client.get_secret_value(SecretId=secret_name)
        _cache[secret_name] = json.loads(response["SecretString"])
        logger.info("Secret loaded: %s", secret_name)
    return _cache[secret_name]

def get_ssm_param(param_name: str, region: str = "us-east-1") -> str:
    """Fetch a single SecureString from SSM Parameter Store."""
    client = boto3.client("ssm", region_name=region)
    return client.get_parameter(Name=param_name, WithDecryption=True)["Parameter"]["Value"]
```

---

### 1.3 API Gateway Authentication

For production every endpoint must require authentication.

Option A — API Key (simplest, 2 minutes):
```
API Gateway -> cloudops-ai-agent -> Usage Plans -> Create plan
  Name: CloudOpsUsagePlan
  Throttling: 100 req/sec, Burst: 200
  Quota: 10,000 req/day
-> API Keys -> Create key -> Auto Generate
-> Associate key with usage plan
-> Method Request -> API Key Required: true
```

Call with:
```bash
curl -X POST https://YOUR_API.execute-api.us-east-1.amazonaws.com/prod/investigate \
  -H "x-api-key: YOUR_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"incident_description": "Lambda has errors"}'
```

Option B — IAM Auth (for internal AWS services):
```
Method Request -> Authorization -> AWS_IAM
Callers sign requests with AWS Signature Version 4
```

---

### 1.4 VPC Network Isolation

```
Architecture:
  Lambda (private subnet) -> NAT Gateway -> Internet (Bedrock, CloudWatch)
                          -> VPC Endpoints -> AWS APIs (free, faster, stays on backbone)

VPC Endpoints to create (Interface type):
  com.amazonaws.us-east-1.bedrock-runtime
  com.amazonaws.us-east-1.logs
  com.amazonaws.us-east-1.monitoring
  com.amazonaws.us-east-1.secretsmanager
  com.amazonaws.us-east-1.ssm

Lambda Security Group:
  Inbound:  None
  Outbound: TCP 443 to VPC Endpoint security group only
```

---

### 1.5 Encryption at Every Layer

| Layer | Key type | Where to enable |
|-------|----------|-----------------|
| Lambda env vars | KMS CMK | Configuration -> Encryption -> Customer managed key |
| CloudWatch Logs | KMS CMK | Log group -> Actions -> Associate KMS key |
| DynamoDB | KMS CMK | Table -> Additional settings -> Encryption |
| S3 deployment bucket | SSE-S3 | Bucket -> Properties -> Default encryption |
| Secrets Manager | KMS CMK | Secret -> Encryption key -> Choose CMK |

---

### 1.6 Security Scanning in CI

Add to `.github/workflows/ci.yml`:

```yaml
security-scan:
  runs-on: ubuntu-latest
  steps:
    - uses: actions/checkout@v4

    - name: Scan for secrets (Gitleaks)
      uses: gitleaks/gitleaks-action@v2
      env:
        GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}

    - name: Dependency vulnerability scan (Safety)
      run: |
        pip install safety
        safety check -r requirements.txt --output text

    - name: Static analysis (Bandit)
      run: |
        pip install bandit
        bandit -r agents/ tools/ app.py -ll -ii
```

---

## 2. Observability and Monitoring

### 2.1 Structured JSON Logging

Replace the default logging format with structured JSON.
This makes CloudWatch Logs Insights queries 10x faster and enables
metric filters, alerting on specific fields, and dashboards.

```python
# app.py — add at the very top, before any logger usage
import json, logging, os

class StructuredJSONFormatter(logging.Formatter):
    """Single-line JSON per record. Compatible with CW Logs Insights."""

    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "timestamp":   self.formatTime(record),
            "level":       record.levelname,
            "logger":      record.name,
            "message":     record.getMessage(),
            "function":    os.environ.get("AWS_LAMBDA_FUNCTION_NAME", "local"),
            "request_id":  getattr(record, "aws_request_id", ""),
            "incident_id": getattr(record, "incident_id", ""),
            "stage":       getattr(record, "stage", ""),
            "duration_ms": getattr(record, "duration_ms", None),
        }
        entry = {k: v for k, v in entry.items() if v is not None and v != ""}
        if record.exc_info:
            entry["exception"] = self.formatException(record.exc_info)
        return json.dumps(entry, default=str)

def configure_logging() -> None:
    root = logging.getLogger()
    root.handlers.clear()
    handler = logging.StreamHandler()
    handler.setFormatter(StructuredJSONFormatter())
    root.addHandler(handler)
    root.setLevel(getattr(logging, os.getenv("LOG_LEVEL", "INFO").upper(), logging.INFO))

configure_logging()
```

CloudWatch Logs Insights queries:

```sql
-- All errors in the last hour
fields @timestamp, incident_id, message
| filter level = "ERROR"
| sort @timestamp desc
| limit 50

-- Average pipeline duration per hour
fields incident_id, duration_ms
| filter stage = "pipeline_complete"
| stats avg(duration_ms), max(duration_ms) by bin(1h)

-- Failed Bedrock calls
fields @timestamp, message, incident_id
| filter message like /Bedrock.*failed/
| sort @timestamp desc
```

---

### 2.2 Custom CloudWatch Metrics

```python
# app.py — add metric emission helper
_cw_client = boto3.client("cloudwatch", region_name=os.getenv("AWS_REGION", "us-east-1"))

def emit_metric(name: str, value: float, unit: str = "Count",
                dimensions: list[dict] | None = None) -> None:
    """Emit to CloudOpsAIAgent namespace. Silently swallows errors."""
    try:
        _cw_client.put_metric_data(
            Namespace  = "CloudOpsAIAgent",
            MetricData = [{
                "MetricName": name, "Value": value, "Unit": unit,
                "Dimensions": dimensions or [],
            }],
        )
    except Exception as exc:
        logger.warning("Failed to emit metric %s: %s", name, exc)

# Call after each pipeline run:
def _emit_pipeline_metrics(context: dict, elapsed: float, success: bool) -> None:
    severity = context.get("severity", "UNKNOWN")
    dims     = [{"Name": "Severity", "Value": severity}]
    emit_metric("PipelineDuration",   elapsed,  "Seconds", dims)
    emit_metric("PipelineSuccess",    1 if success else 0, "Count", dims)
    emit_metric("AnomaliesDetected",
                context.get("metrics_report", {}).get("anomalies_detected", 0), "Count", dims)
    emit_metric("RemediationActions",
                context.get("remediation_report", {}).get("total_actions_identified", 0),
                "Count", dims)
```

---

### 2.3 CloudWatch Alarms — Minimum Production Set

Create all five alarms at CloudWatch -> Alarms -> Create alarm:

| # | Alarm Name | Metric | Threshold | Action |
|---|-----------|--------|-----------|--------|
| 1 | `CloudOps-LambdaErrors` | AWS/Lambda Errors | > 5 in 5 min | SNS -> PagerDuty |
| 2 | `CloudOps-HighDuration` | AWS/Lambda Duration p99 | > 240,000 ms | SNS -> Slack |
| 3 | `CloudOps-Throttles` | AWS/Lambda Throttles | > 0 | SNS -> Slack |
| 4 | `CloudOps-PipelineFailures` | CloudOpsAIAgent/PipelineSuccess | < 0.8 (15 min) | SNS -> PagerDuty |
| 5 | `CloudOps-BedrockCostSpike` | AWS/Billing EstimatedCharges | > $10/day | SNS -> email |

---

### 2.4 CloudWatch Dashboard

```
CloudWatch -> Dashboards -> Create -> Name: CloudOps-AI-Agent-Prod

Row 1 — Lambda Health
  Line: AWS/Lambda Invocations (Sum, 5min)
  Line: AWS/Lambda Errors (Sum, 5min)
  Number: AWS/Lambda Duration p99 (latest)
  Number: AWS/Lambda Throttles (Sum, 5min)

Row 2 — Pipeline Business Metrics
  Line: CloudOpsAIAgent/PipelineSuccess (Average, 5min)
  Line: CloudOpsAIAgent/AnomaliesDetected (Sum, 1h)
  Line: CloudOpsAIAgent/PipelineDuration (p99, 5min)
  Line: CloudOpsAIAgent/RemediationActions (Sum, 1h)

Row 3 — Bedrock
  Line: AWS/Bedrock InvocationLatency (p99)
  Number: AWS/Bedrock InvocationClientErrors (Sum, 1h)
```

---

### 2.5 AWS X-Ray Distributed Tracing

```python
# Add to requirements.txt: aws-xray-sdk

from aws_xray_sdk.core import xray_recorder, patch_all
patch_all()   # auto-instruments all boto3 calls

# In CloudOpsOrchestrator.run():
with xray_recorder.in_subsegment("stage_1_incident"):
    context = self._incident_agent.investigate(...)

with xray_recorder.in_subsegment("stage_2_metrics"):
    context = self._metrics_agent.analyze(context)

with xray_recorder.in_subsegment("stage_3_logs"):
    context = self._log_agent.analyze(context)

with xray_recorder.in_subsegment("stage_4_remediation"):
    context = self._remediation_agent.remediate(context)
```

Enable in Lambda:
```
Lambda -> Configuration -> Monitoring and operations tools -> Active tracing -> Enable
```

---

## 3. Reliability and Resilience

### 3.1 Dead Letter Queue

```
Step 1 — Create SQS queue:
  SQS -> Create queue -> Standard -> Name: cloudops-ai-agent-dlq
  Message retention: 14 days

Step 2 — Attach to Lambda:
  Lambda -> Configuration -> Asynchronous invocation -> Edit
    Maximum age of event: 1 hour
    Retry attempts: 2
    Dead-letter queue: cloudops-ai-agent-dlq

Step 3 — Alert on depth:
  CloudWatch -> Alarm on SQS ApproximateNumberOfMessagesVisible > 0
  Action: SNS -> PagerDuty
```

---

### 3.2 Timeout Defence in Depth

```python
# agents/model_adapter.py — enforce per-call timeout
import botocore.config

BEDROCK_CONFIG = botocore.config.Config(
    connect_timeout = 10,    # 10s to establish TCP connection
    read_timeout    = 30,    # 30s for model response
    retries = {
        "max_attempts": 3,
        "mode":         "adaptive",   # exponential backoff with jitter
    },
)

self._client = boto3.client(
    "bedrock-runtime",
    region_name = region_name,
    config      = BEDROCK_CONFIG,
)
```

Recommended timeout stack for this project:

```
API Gateway:          29 seconds  (hard AWS limit)
  Lambda:            300 seconds
    Per-agent:        60 seconds  (concurrent.futures timeout)
      Bedrock call:   30 seconds  (botocore read_timeout)
      CW Metrics:     10 seconds
      CW Logs:        10 seconds
```

---

### 3.3 Circuit Breaker for Bedrock

```python
# agents/circuit_breaker.py
import time, logging

logger = logging.getLogger(__name__)

class CircuitBreaker:
    """
    Three-state circuit breaker: CLOSED (normal) -> OPEN (failing) -> HALF_OPEN (testing).

    Usage:
        breaker = CircuitBreaker(failure_threshold=5, reset_timeout=60)
        try:
            result = breaker.call(adapter.invoke_json, model_id, prompt)
        except CircuitBreaker.CircuitOpenError:
            result = fallback_response()
    """
    class CircuitOpenError(Exception): pass
    CLOSED = "CLOSED"; OPEN = "OPEN"; HALF_OPEN = "HALF_OPEN"

    def __init__(self, failure_threshold: int = 5, reset_timeout: int = 60):
        self._state = self.CLOSED
        self._failures = 0
        self._threshold = failure_threshold
        self._reset_timeout = reset_timeout
        self._last_failure_ts = 0.0

    @property
    def state(self) -> str:
        if self._state == self.OPEN:
            if time.monotonic() - self._last_failure_ts > self._reset_timeout:
                self._state = self.HALF_OPEN
                logger.info("Circuit breaker -> HALF_OPEN")
        return self._state

    def call(self, fn, *args, **kwargs):
        if self.state == self.OPEN:
            raise self.CircuitOpenError("Circuit OPEN — Bedrock unavailable, using fallback")
        try:
            result = fn(*args, **kwargs)
            if self._state == self.HALF_OPEN:
                self._state = self.CLOSED
                self._failures = 0
                logger.info("Circuit breaker -> CLOSED (recovered)")
            return result
        except Exception as exc:
            self._failures += 1
            self._last_failure_ts = time.monotonic()
            if self._failures >= self._threshold:
                self._state = self.OPEN
                logger.warning("Circuit breaker -> OPEN after %d failures", self._failures)
            raise
```

---

### 3.4 Idempotency — Prevent Duplicate Investigations

```python
# app.py
import hashlib, time
from datetime import datetime, timezone

def get_idempotency_key(incident_description: str) -> str:
    """Same description within the same 5-minute window returns the same key."""
    minute_bucket = datetime.now(timezone.utc).strftime("%Y%m%d%H%M")[:-1]
    raw = f"{incident_description[:200]}:{minute_bucket}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16].upper()

def check_idempotency(key: str) -> dict | None:
    """Returns cached result if already processed. None if new request."""
    table = boto3.resource("dynamodb").Table("cloudops-incidents")
    try:
        response = table.get_item(Key={"incident_id": key})
        if "Item" in response:
            return response["Item"].get("result")
        table.put_item(
            Item={"incident_id": key, "status": "IN_PROGRESS",
                  "ttl": int(time.time()) + 300},
            ConditionExpression="attribute_not_exists(incident_id)",
        )
    except Exception as exc:
        logger.warning("Idempotency check failed: %s — proceeding anyway", exc)
    return None
```

---

### 3.5 Chaos Engineering Tests

```python
# tests/test_resilience.py
import json, pytest
from unittest.mock import patch, MagicMock
from botocore.exceptions import ClientError
from app import CloudOpsOrchestrator

class TestResilienceUnderFailure:

    def test_bedrock_down_returns_fallback_result(self):
        """Pipeline must return a usable result even when Bedrock is 100% unavailable."""
        with patch("agents.model_adapter.BedrockModelAdapter.invoke_json",
                   side_effect=ClientError({"Error": {"Code": "ServiceUnavailableException"}},
                                           "InvokeModel")):
            result = CloudOpsOrchestrator().run("Lambda has 40% error rate")

        assert result["status"] == "SUCCESS"
        triage = json.loads(result["agent_reports"]["incident"]["initial_triage"])
        assert "triage_summary" in triage

    def test_cloudwatch_metrics_down_pipeline_continues(self):
        """MetricsAgent failure must not crash the pipeline."""
        from tools.cloudwatch_metrics import CloudWatchMetricsError
        with patch("tools.cloudwatch_metrics.CloudWatchMetricsTool.get_lambda_health",
                   side_effect=CloudWatchMetricsError("Metrics unavailable")):
            result = CloudOpsOrchestrator().run(
                "Lambda has errors", resource_hints={"lambda": ["fn"]})
        assert result["status"] == "SUCCESS"

    def test_cloudwatch_logs_down_pipeline_continues(self):
        """LogAgent failure must not crash the pipeline."""
        from tools.cloudwatch_logs import CloudWatchLogsError
        with patch("tools.cloudwatch_logs.CloudWatchLogsTool.get_log_statistics",
                   side_effect=CloudWatchLogsError("Logs unavailable")):
            result = CloudOpsOrchestrator().run(
                "Lambda has errors", resource_hints={"lambda": ["fn"]})
        assert result["status"] == "SUCCESS"

    def test_lambda_handler_returns_400_on_empty_body(self):
        from app import lambda_handler
        assert lambda_handler({"body": "{}"}, MagicMock())["statusCode"] == 400

    def test_lambda_handler_returns_400_on_invalid_json(self):
        from app import lambda_handler
        assert lambda_handler({"body": "not-json"}, MagicMock())["statusCode"] == 400
```

---

## 4. Performance and Scalability

### 4.1 Parallel Agent Execution — 40% Faster

Metrics and Log agents are independent. Run them concurrently:

```python
# app.py — replace sequential stages 2 & 3 in CloudOpsOrchestrator.run()
import concurrent.futures

t0 = time.perf_counter()

with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
    metrics_future = executor.submit(self._metrics_agent.analyze, context)
    log_future     = executor.submit(self._log_agent.analyze,     context)

    try:
        metrics_ctx = metrics_future.result(timeout=60)
    except concurrent.futures.TimeoutError:
        logger.warning("MetricsAgent timed out")
        metrics_ctx = {**context, "metrics_report": {"anomalies": [], "overall_health": "UNKNOWN"}}

    try:
        log_ctx = log_future.result(timeout=60)
    except concurrent.futures.TimeoutError:
        logger.warning("LogAgent timed out")
        log_ctx = {**context, "log_report": {"top_error_patterns": [], "summary": "Timed out"}}

context = {**context,
           "metrics_report": metrics_ctx.get("metrics_report", {}),
           "log_report":     log_ctx.get("log_report", {})}

stage_timings["stages_2_and_3_parallel"] = round(time.perf_counter() - t0, 3)
```

Benchmark:
| Mode | Stages 2+3 combined |
|------|---------------------|
| Sequential | 1.5s |
| Parallel | 0.9s (40% faster) |

---

### 4.2 Lambda Warm-Start Optimisation

```python
# Move ALL boto3 clients to module level — created once per container, not per request
import boto3, os

AWS_REGION       = os.getenv("AWS_REGION", "us-east-1")
_bedrock_client  = boto3.client("bedrock-runtime", region_name=AWS_REGION)
_logs_client     = boto3.client("logs",             region_name=AWS_REGION)
_metrics_client  = boto3.client("cloudwatch",       region_name=AWS_REGION)

# Bad — runs every invocation
def lambda_handler(event, context):
    client = boto3.client("bedrock-runtime")   # cold start cost on EVERY call

# Good — runs once per container (already done via _get_orchestrator singleton)
_orchestrator = None
def _get_orchestrator():
    global _orchestrator
    if _orchestrator is None:
        _orchestrator = CloudOpsOrchestrator()
    return _orchestrator
```

---

### 4.3 Provisioned Concurrency — Eliminate Cold Starts

```
Step 1 — Publish Lambda version:
  Lambda -> cloudops-ai-agent -> Actions -> Publish new version -> Publish

Step 2 — Create alias:
  Lambda -> Aliases -> Create alias -> Name: live -> Version: 1 -> Save

Step 3 — Set provisioned concurrency on the alias:
  Lambda -> Aliases -> live -> Configuration -> Concurrency
  Provisioned concurrency: 2

Step 4 — Point API Gateway to the alias:
  API Gateway -> Integration request -> Lambda: cloudops-ai-agent:live
```

Cost: ~$3/month for 2 provisioned containers.

---

### 4.4 Reserved Concurrency — Blast Radius Limit

```
Lambda -> Configuration -> Concurrency -> Edit
  Reserved concurrency: 50
```

Prevents one incident storm from consuming all Lambda concurrency in your account.

---

## 5. Cost Optimisation

### 5.1 Per-Agent Model Tiering — 61% Cost Saving

```
Agent               Task                     Model                  Cost/1M in+out
Incident agent     Parse + extract info      amazon.nova-micro-v1:0  $0.035 + $0.14
Metrics agent      Interpret numbers         amazon.nova-micro-v1:0  $0.035 + $0.14
Log agent          Pattern correlation       amazon.nova-lite-v1:0   $0.06  + $0.24
Remediation agent  Generate action plan      amazon.nova-pro-v1:0    $0.80  + $3.20
```

Set in Lambda environment variables:
```
INCIDENT_AGENT_MODEL    = amazon.nova-micro-v1:0
METRICS_AGENT_MODEL     = amazon.nova-micro-v1:0
LOG_AGENT_MODEL         = amazon.nova-lite-v1:0
REMEDIATION_AGENT_MODEL = amazon.nova-pro-v1:0
```

Update each agent's DEFAULT_MODEL:
```python
# agents/incident_agent.py
class IncidentAgent:
    DEFAULT_MODEL = os.getenv(
        "INCIDENT_AGENT_MODEL",
        os.getenv("BEDROCK_MODEL_ID", "amazon.nova-micro-v1:0"),
    )
```

Monthly cost comparison at 100 incidents/day:

| Configuration | Monthly |
|--------------|---------|
| All Nova Pro | ~$23 |
| Tiered models | ~$9 |
| Saving | ~$14/month (61%) |

---

### 5.2 Prompt Length Limits

```python
# agents/incident_agent.py
MAX_DESCRIPTION_TOKENS = 500   # approx 375 words

def _truncate_description(self, text: str) -> str:
    words = text.split()
    if len(words) > MAX_DESCRIPTION_TOKENS:
        return " ".join(words[:MAX_DESCRIPTION_TOKENS]) + " [truncated]"
    return text
```

---

### 5.3 CloudWatch Cost Controls

```
1. Log retention — set 30 days (never "Never"):
   CloudWatch -> Log groups -> /aws/lambda/cloudops-ai-agent
   -> Actions -> Edit retention -> 30 days

2. In production, set LOG_LEVEL = WARNING (fewer log lines ingested)

3. Metric resolution — use 5-minute periods (period=300, already the default)

4. DynamoDB TTL — auto-delete after 90 days (see section 9.3)
```

### 5.4 Monthly Cost Estimate at 100 incidents/day

| Service | Monthly |
|---------|---------|
| Lambda | $0.08 |
| Bedrock (tiered) | $9.00 |
| CloudWatch Logs | $0.15 |
| CloudWatch Metrics | $0.35 |
| API Gateway | $0.01 |
| DynamoDB | $0.01 |
| **Total** | **~$9.60/month** |

---

## 6. CI/CD and Zero-Downtime Deployment

### 6.1 Branch to Environment Mapping

```
Branch        Environment   Auto-deploy  DRY_RUN  Models
feature/*     local only    No           true     nova-micro
develop       staging       On PR merge  true     nova-lite
main          production    On PR merge  true     nova-pro
```

### 6.2 Blue/Green Deployment Script

```bash
#!/usr/bin/env bash
# scripts/bluegreen_deploy.sh
set -euo pipefail
FUNCTION="cloudops-ai-agent"; ALIAS="live"; REGION="${AWS_REGION:-us-east-1}"

echo "Step 1 — Upload code"
aws lambda update-function-code --function-name "$FUNCTION" \
  --zip-file fileb://cloudops-ai-agent.zip --region "$REGION"
aws lambda wait function-updated --function-name "$FUNCTION" --region "$REGION"

echo "Step 2 — Publish new version"
NEW_VER=$(aws lambda publish-version --function-name "$FUNCTION" \
  --region "$REGION" --query 'Version' --output text)
echo "New version: $NEW_VER"

echo "Step 3 — Canary: 10% traffic to new version"
aws lambda update-alias --function-name "$FUNCTION" --name "$ALIAS" \
  --routing-config "AdditionalVersionWeights={\"$NEW_VER\"=0.1}" --region "$REGION"

echo "Step 4 — Monitor canary for 5 minutes"
sleep 300

ERRORS=$(aws cloudwatch get-metric-statistics --namespace AWS/Lambda \
  --metric-name Errors --dimensions "Name=FunctionName,Value=$FUNCTION" \
  --start-time "$(date -u -d '5 minutes ago' +%Y-%m-%dT%H:%M:%S)" \
  --end-time "$(date -u +%Y-%m-%dT%H:%M:%S)" \
  --period 300 --statistics Sum --query 'Datapoints[0].Sum' --output text 2>/dev/null || echo "0")

if [ "${ERRORS:-0}" -gt "5" ]; then
  echo "ROLLBACK: $ERRORS errors in canary"
  OLD_VER=$(aws lambda get-alias --function-name "$FUNCTION" --name "$ALIAS" \
    --query 'FunctionVersion' --output text --region "$REGION")
  aws lambda update-alias --function-name "$FUNCTION" --name "$ALIAS" \
    --function-version "$OLD_VER" --region "$REGION"
  exit 1
fi

echo "Step 5 — Promote: 100% traffic to $NEW_VER"
aws lambda update-alias --function-name "$FUNCTION" --name "$ALIAS" \
  --function-version "$NEW_VER" --region "$REGION"
echo "Deploy complete"
```

---

### 6.3 Required GitHub Actions Secrets

| Secret | Value |
|--------|-------|
| `AWS_ROLE_ARN` | OIDC deployment role ARN |
| `AWS_REGION` | `us-east-1` |
| `LAMBDA_FUNCTION_NAME` | `cloudops-ai-agent` |
| `BEDROCK_MODEL_ID` | `amazon.nova-pro-v1:0` |
| `SLACK_WEBHOOK_URL` | `https://hooks.slack.com/...` |

---

### 6.4 Dependency Pinning

```bash
# Generate pinned requirements (reproducible builds):
pip install pip-tools
pip-compile requirements.in --output-file requirements.txt

# requirements.txt (pinned):
boto3==1.34.162
botocore==1.34.162
```

---

## 7. AI and Model Governance

### 7.1 Prompt Engineering Best Practices

```python
# Rules for reliable JSON output from any Bedrock model:

PROMPT_TEMPLATE = """You are an expert AWS SRE.

INCIDENT: {description}
SEVERITY: {severity}

Return ONLY a valid JSON object — no markdown, no explanation text.
Use exactly these keys:
{{
  "triage_summary":     "<2-3 sentences>",
  "investigation_plan": ["<step 1>", "<step 2>"],
  "key_questions":      ["<question 1>", "<question 2>"],
  "risk_assessment":    "<one sentence>"
}}"""

# Rules applied:
# 1. "Return ONLY a valid JSON object" — prevents preamble text
# 2. Show exact key names — model mirrors them reliably
# 3. Double braces {{ }} to escape literal braces in f-strings
# 4. Keep under 2,000 tokens for Micro/Lite models
```

---

### 7.2 Response Validation

```python
# agents/validators.py
from typing import Any

REQUIRED_TRIAGE_KEYS     = {"triage_summary", "investigation_plan",
                             "key_questions", "risk_assessment"}
REQUIRED_METRICS_KEYS    = {"metrics_interpretation", "likely_bottleneck",
                             "confidence_level"}
REQUIRED_HYPOTHESIS_KEYS = {"root_cause_hypothesis", "evidence_chain",
                             "confidence_level"}
VALID_CONFIDENCE         = {"LOW", "MEDIUM", "HIGH"}


def validate_ai_response(response: Any, required_keys: set, name: str = "") -> tuple[bool, list]:
    """Validate AI response dict for required keys and enum values."""
    if not isinstance(response, dict):
        return False, [f"{name}: expected dict, got {type(response).__name__}"]

    errors = []
    missing = required_keys - response.keys()
    if missing:
        errors.append(f"{name}: missing keys {missing}")

    if "confidence_level" in response:
        if response["confidence_level"] not in VALID_CONFIDENCE:
            errors.append(f"{name}: invalid confidence '{response['confidence_level']}'")

    return len(errors) == 0, errors
```

---

### 7.3 Hallucination Guard

```python
# agents/hallucination_guard.py
import re, logging

logger = logging.getLogger(__name__)


def guard_metric_claims(ai_text: str, actual_metrics: dict) -> tuple[str, list]:
    """
    Scan AI text for numeric percentage claims.
    Flag any claim diverging from actual CloudWatch data by > 20pp.
    """
    warnings = []
    claims   = re.findall(r'(\d+(?:\.\d+)?)\s*%', ai_text)
    actual_cpu    = actual_metrics.get("avg_cpu_percent", 0)
    actual_errors = actual_metrics.get("error_rate_percent", 0)

    for claim_str in claims:
        claim = float(claim_str)
        if actual_cpu > 0 and abs(claim - actual_cpu) > 20:
            warnings.append(
                f"AI claimed {claim}% but actual CPU is {actual_cpu:.1f}%"
            )
        if actual_errors > 0 and abs(claim - actual_errors) > 20:
            warnings.append(
                f"AI claimed {claim}% but actual error rate is {actual_errors:.1f}%"
            )

    if warnings:
        logger.warning("Hallucination guard: %s", warnings)
    return ai_text, warnings
```

---

### 7.4 Model Version Pinning

```python
# Never use version-less model IDs — new versions may change output format

# Bad:  "amazon.nova-pro"           (points to latest — may change silently)
# Good: "amazon.nova-pro-v1:0"      (pinned to v1 — predictable output)

APPROVED_MODELS = {
    "prod": {
        "incident":    "amazon.nova-micro-v1:0",
        "metrics":     "amazon.nova-micro-v1:0",
        "log":         "amazon.nova-lite-v1:0",
        "remediation": "amazon.nova-pro-v1:0",
    },
}
```

---

## 8. Incident Response Runbooks

### Runbook A — Bedrock AccessDenied

```
Symptom:
  WARNING Bedrock triage failed (AccessDeniedException)

Diagnosis:
  1. IAM -> Roles -> Cloudops-ai-agent-lambda-role -> Simulate
     Action: bedrock:InvokeModel
     Resource: arn:aws:bedrock:us-east-1::foundation-model/amazon.nova-pro-v1:0
  2. If "denied": policy is missing or wrong ARN

Fix Option A — Missing inline policy:
  IAM -> Role -> Add permissions -> Create inline policy
  Paste the BedrockInvokeScopedModels policy from section 1.1

Fix Option B — Wrong model ARN in policy:
  Policy Resource must exactly match BEDROCK_MODEL_ID env var

Fix Option C — Model not enabled:
  Bedrock Console -> Model access -> Enable amazon.nova-pro-v1:0

Verify: Re-run API Gateway test -> AI output (not [Fallback]) in response
```

---

### Runbook B — Lambda Timeout

```
Symptom:
  REPORT Duration: 300000 ms — function timed out

Diagnosis — find slow stage in CloudWatch Logs:
  fields @timestamp, message
  | filter message like /Stage [0-9] done/
  The last stage printed = the slow one

If MetricsAgent is slow:
  -> Reduce: Lambda env var MAX_LOG_LOOKBACK = 60
  -> Reduce: resource_hints to 2-3 resources max
  -> Enable parallel execution (section 4.1)

If LogAgent is slow:
  -> Large log groups — reduce max_results: 1000 -> 200
  -> Add filter_pattern to skip non-error logs

If Bedrock is slow:
  -> Check: health.aws.amazon.com for service disruption
  -> Switch: BEDROCK_MODEL_ID = amazon.nova-micro-v1:0 (fastest)

Last resort:
  -> Increase Lambda timeout: 300s -> 900s (max)
```

---

### Runbook C — High Lambda Error Rate

```
Symptom:
  CloudOps-LambdaErrors alarm triggered (> 5 errors in 5 min)

Step 1 — Identify error type:
  CloudWatch Logs Insights:
    fields message | filter level = "ERROR"
    | stats count(*) by message | sort count desc | limit 10

Step 2 — Map error to fix:

  "ValidationException: extraneous key [max_tokens]"
  -> Wrong model body format. Redeploy latest code from main branch.

  "AccessDeniedException: not authorized to perform bedrock:InvokeModel"
  -> See Runbook A

  "ResourceNotFoundException: Log group not found"
  -> Normal warning — log group does not exist yet. Not an error.

  "ThrottlingException: Rate exceeded"
  -> Increase reserved concurrency or switch to higher-quota model

Step 3 — Check recent deployments:
  git log --oneline -10
  If error started after a recent commit:
    git revert HEAD && git push origin main
```

---

### Runbook D — Bedrock Cost Spike

```
Symptom:
  CloudOps-BedrockCostSpike alarm triggered (> $10/day)

Step 1 — Find cause:
  CloudWatch -> Bedrock -> InvocationCount by model
  Look for: high count OR expensive model used unexpectedly

Step 2 — Common causes:

  Incident loop (same incident retried hundreds of times):
  -> Enable idempotency (section 3.4)
  -> Add API Gateway throttling (100 req/sec max)

  Wrong model assigned to cheap-model agents:
  -> Check Lambda env vars INCIDENT_AGENT_MODEL, METRICS_AGENT_MODEL
  -> Both should be amazon.nova-micro-v1:0

  Long prompts from huge log groups:
  -> Enable prompt truncation (section 5.2)

Step 3 — Emergency cost cap:
  Lambda -> Configuration -> Environment variables
  Set all *_MODEL vars to amazon.nova-micro-v1:0
  Cost drops immediately for new invocations
```

---

## 9. Compliance and Data Governance

### 9.1 Data Classification

| Data type | Classification | Sent to Bedrock? | Action required |
|-----------|---------------|-----------------|-----------------|
| Incident description | Internal | Yes | OK as-is |
| CloudWatch metric values | Internal | Yes | OK as-is |
| CloudWatch log messages | Confidential | Yes with caveat | Scrub PII first |
| Customer emails in logs | PII | No | Strip before sending |
| Credit card numbers in logs | PCI | No | Strip before sending |
| AWS account IDs | Internal | Partial | Mask in prompts |
| Remediation actions taken | Internal | Audit log | Store in DynamoDB |

---

### 9.2 PII Scrubbing Before Bedrock Calls

```python
# agents/pii_scrubber.py
import re, logging

logger = logging.getLogger(__name__)

PII_PATTERNS: list[tuple[str, str]] = [
    (r'\b(?:\d[ -]?){13,16}\b',                                "[CARD_NUMBER]"),
    (r'\b\d{3}-\d{2}-\d{4}\b',                                "[SSN]"),
    (r'\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b', "[EMAIL]"),
    (r'\b(?:\d{1,3}\.){3}\d{1,3}\b',                          "[IP_ADDR]"),
    (r'\b(?:\+?1[-.\s]?)?\(?\d{3}\)?[-.\s]\d{3}[-.\s]\d{4}\b', "[PHONE]"),
    (r'\b\d{12}\b',                                            "[AWS_ACCOUNT]"),
    (r'eyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+', "[JWT_TOKEN]"),
    (r'\b[A-Za-z0-9+/]{40,}={0,2}\b',                         "[SECRET_VALUE]"),
]

def scrub_pii(text: str) -> tuple[str, int]:
    """Remove PII from text before sending to Bedrock. Returns (cleaned, count)."""
    total = 0
    for pattern, replacement in PII_PATTERNS:
        text, count = re.subn(pattern, replacement, text)
        if count > 0:
            logger.info("PII scrubbed: %d x %s", count, replacement)
            total += count
    return text, total

def scrub_log_events(events: list[dict]) -> list[dict]:
    """Scrub PII from a list of CloudWatch log event dicts."""
    result = []
    for event in events:
        msg, count = scrub_pii(event.get("message", ""))
        result.append({**event, "message": msg,
                       **({"pii_scrubbed": count} if count else {})})
    return result
```

---

### 9.3 Audit Trail in DynamoDB

```python
# agents/audit.py
import boto3, json, time
from datetime import datetime, timezone

_dynamo = boto3.resource("dynamodb")

def record_pipeline_run(result: dict) -> None:
    """
    Store audit record after every pipeline run.
    Auto-deleted after 90 days via DynamoDB TTL.
    """
    _dynamo.Table("cloudops-incidents").put_item(Item={
        "incident_id":         result.get("incident_id", "UNKNOWN"),
        "start_time":          datetime.now(timezone.utc).isoformat(),
        "severity":            result.get("severity"),
        "status":              result.get("status"),
        "overall_health":      result.get("pipeline_result", {}).get("overall_health"),
        "anomalies_detected":  result.get("agent_reports", {})
                                     .get("metrics", {})
                                     .get("anomalies_detected", 0),
        "actions_recommended": [
            a.get("action_id")
            for a in result.get("agent_reports", {})
                           .get("remediation", {})
                           .get("top_actions", [])
        ],
        "actions_auto_executed": result.get("agent_reports", {})
                                       .get("remediation", {})
                                       .get("auto_executed", []),
        "pipeline_duration_s": result.get("performance", {})
                                     .get("total_elapsed_seconds", 0),
        "ttl": int(time.time()) + (90 * 24 * 3600),   # 90-day retention
    })
```

Enable TTL:
```
DynamoDB -> cloudops-incidents -> Additional settings -> Time to Live
-> TTL attribute: ttl -> Enable
```

---

### 9.4 Bedrock Data Privacy

```
Key facts:
  Bedrock does NOT train on your prompts or responses (by default)
  Data encrypted in transit (TLS 1.2+) and at rest
  Processed in your specified AWS region only

Optional invocation logging (for debugging):
  Bedrock -> Settings -> Model invocation logging -> S3 or CloudWatch
  WARNING: logs your full prompts and responses
  Only enable after confirming PII scrubbing is in place (section 9.2)

For regulated industries (HIPAA, PCI, FedRAMP):
  Request Bedrock PrivateLink from AWS Support
  Compliance reports available via AWS Artifact
```

---

### 9.5 Production Readiness Checklist

**Security**
- [ ] IAM role uses specific ARNs (no `Resource: "*"` in production)
- [ ] API Gateway requires authentication (API key or IAM)
- [ ] Lambda deployed in VPC with private subnets
- [ ] VPC endpoints created for Bedrock, CloudWatch, SSM, Secrets Manager
- [ ] All secrets in Secrets Manager (zero plaintext env vars for sensitive values)
- [ ] Lambda env vars encrypted with KMS CMK
- [ ] PII scrubbing enabled and tested with real production log samples
- [ ] Gitleaks scanning passing in CI
- [ ] Bandit static analysis passing in CI

**Reliability**
- [ ] Dead Letter Queue configured and alarmed on SQS depth > 0
- [ ] Bedrock call timeout = 30s configured via botocore
- [ ] Circuit breaker deployed for Bedrock calls
- [ ] Idempotency tested with duplicate API Gateway requests
- [ ] All 5 chaos engineering tests passing
- [ ] Fallback mode verified: pipeline returns a result with Bedrock down

**Observability**
- [ ] Structured JSON logging enabled
- [ ] Custom metrics emitting per pipeline stage
- [ ] X-Ray tracing enabled on Lambda alias
- [ ] CloudWatch Dashboard created with 10 widgets
- [ ] All 5 production alarms active and SNS notifications confirmed
- [ ] Log retention set to 30 days
- [ ] DLQ depth alarm configured

**Performance**
- [ ] Parallel agent execution enabled (stages 2 and 3)
- [ ] Provisioned concurrency set on `live` alias (minimum 2)
- [ ] Reserved concurrency set to 50
- [ ] Cold start under 800ms verified
- [ ] P99 pipeline duration under 10s verified under load

**Cost**
- [ ] Per-agent model tiering configured (see section 5.1)
- [ ] Bedrock cost alarm set at $10/day
- [ ] DynamoDB TTL enabled at 90-day retention
- [ ] Log retention confirmed at 30 days
- [ ] Monthly cost estimate reviewed and approved

**CI/CD**
- [ ] All CI checks passing: lint, typecheck, tests, coverage >= 75%
- [ ] Blue/green deployment script tested end-to-end on staging
- [ ] Automated rollback triggered and verified in staging
- [ ] OIDC used for GitHub Actions (no stored AWS access keys)
- [ ] Staging and production environment parity confirmed

**AI and Compliance**
- [ ] All prompt templates tested for consistent JSON output
- [ ] Response validation running on every Bedrock call
- [ ] Hallucination guard cross-checking AI claims against real metrics
- [ ] Model versions pinned (no version-less IDs anywhere)
- [ ] Audit trail writing to DynamoDB on every pipeline run
- [ ] Data classification documented and reviewed by stakeholders
