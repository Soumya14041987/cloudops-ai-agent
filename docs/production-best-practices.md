# Production Best Practices — CloudOps AI Agent

> A comprehensive guide covering security, observability, reliability, cost optimisation,
> and operational excellence for running the CloudOps AI Agent at production scale.

---

## Table of Contents

1. [Security](#1-security)
2. [Observability & Monitoring](#2-observability--monitoring)
3. [Reliability & Resilience](#3-reliability--resilience)
4. [Performance & Scalability](#4-performance--scalability)
5. [Cost Optimisation](#5-cost-optimisation)
6. [CI/CD & Deployment](#6-cicd--deployment)
7. [Model & AI Best Practices](#7-model--ai-best-practices)
8. [Incident Response](#8-incident-response)
9. [Compliance & Data Governance](#9-compliance--data-governance)
10. [Production Readiness Checklist](#10-production-readiness-checklist)

---

## 1. Security

### 1.1 IAM — Least Privilege

Never use `AdministratorAccess`. The Lambda role should have only what it needs:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "BedrockScopedModels",
      "Effect": "Allow",
      "Action": ["bedrock:InvokeModel"],
      "Resource": [
        "arn:aws:bedrock:us-east-1::foundation-model/anthropic.claude-3-sonnet*",
        "arn:aws:bedrock:us-east-1::foundation-model/amazon.nova*"
      ]
    },
    {
      "Sid": "CloudWatchLogsScoped",
      "Effect": "Allow",
      "Action": ["logs:FilterLogEvents", "logs:GetLogEvents"],
      "Resource": "arn:aws:logs:us-east-1:ACCOUNT_ID:log-group:/aws/*:*"
    },
    {
      "Sid": "CloudWatchMetricsReadOnly",
      "Effect": "Allow",
      "Action": ["cloudwatch:GetMetricData"],
      "Resource": "*"
    }
  ]
}
```

> Rule: Start with `Resource: "*"` during development, narrow to specific ARNs before production.

### 1.2 Secrets — Never in Environment Variables

| What | Bad practice | Good practice |
|------|-------------|---------------|
| API keys | Lambda env var (plaintext) | AWS Secrets Manager |
| Slack webhook | Lambda env var | Secrets Manager + rotation |
| DB passwords | Hardcoded in code | SSM Parameter Store (SecureString) |
| Model IDs | Hardcoded | SSM Parameter Store |

```python
# Good — fetch from Secrets Manager at cold start (cached for warm invocations)
import boto3, json
_secrets_cache = {}

def get_secret(secret_name: str) -> dict:
    if secret_name not in _secrets_cache:
        client = boto3.client("secretsmanager")
        response = client.get_secret_value(SecretId=secret_name)
        _secrets_cache[secret_name] = json.loads(response["SecretString"])
    return _secrets_cache[secret_name]
```

### 1.3 API Gateway — Authentication

For production, never leave the API endpoint unauthenticated:

```
Option A — API Key (simplest):
  API Gateway → Usage Plans → API Keys → require key in x-api-key header

Option B — IAM Auth (for AWS-to-AWS calls):
  Method Request → Authorization → AWS_IAM

Option C — Cognito (for user-facing apps):
  Attach a Cognito User Pool Authorizer to the POST /investigate method

Option D — Lambda Authorizer (custom JWT/OIDC):
  Write a separate Lambda that validates tokens before CloudOps runs
```

### 1.4 Network Isolation — VPC

For production deployments that touch internal databases or private APIs:

```
1. Deploy Lambda inside a VPC
   - Private subnet (no direct internet access)
   - NAT Gateway for outbound calls (Bedrock, CloudWatch)

2. VPC Endpoints (stay on AWS backbone — no NAT needed):
   - com.amazonaws.us-east-1.bedrock-runtime
   - com.amazonaws.us-east-1.logs
   - com.amazonaws.us-east-1.monitoring

3. Security Group on Lambda:
   - Outbound: 443 to VPC endpoints only
   - Inbound: none
```

### 1.5 Encryption

| Layer | Encryption |
|-------|-----------|
| DynamoDB at rest | AWS-owned KMS key (default) or CMK |
| CloudWatch Logs | KMS CMK for sensitive log data |
| Lambda env vars | KMS encryption enabled |
| Bedrock requests | TLS 1.2+ in transit (automatic) |
| S3 deployment bucket | SSE-S3 or SSE-KMS |

---

## 2. Observability & Monitoring

### 2.1 Structured Logging

Replace the default logging with structured JSON — makes CloudWatch Logs Insights queries 10x faster:

```python
# Add to app.py before any logger usage
import json, logging

class JSONFormatter(logging.Formatter):
    def format(self, record):
        log_obj = {
            "timestamp":   self.formatTime(record),
            "level":       record.levelname,
            "logger":      record.name,
            "message":     record.getMessage(),
            "request_id":  getattr(record, "request_id", ""),
            "incident_id": getattr(record, "incident_id", ""),
        }
        if record.exc_info:
            log_obj["exception"] = self.formatException(record.exc_info)
        return json.dumps(log_obj)

handler = logging.StreamHandler()
handler.setFormatter(JSONFormatter())
logging.root.handlers = [handler]
logging.root.setLevel(logging.INFO)
```

Then query in CloudWatch Logs Insights:
```sql
fields @timestamp, incident_id, level, message
| filter level = "ERROR"
| sort @timestamp desc
| limit 50
```

### 2.2 Custom CloudWatch Metrics

Emit custom metrics per pipeline stage for dashboards and alarms:

```python
import boto3
cw = boto3.client("cloudwatch")

def emit_metric(name: str, value: float, unit: str = "Count", dimensions: list = []):
    cw.put_metric_data(
        Namespace = "CloudOpsAIAgent",
        MetricData = [{
            "MetricName": name,
            "Value":      value,
            "Unit":       unit,
            "Dimensions": dimensions,
        }]
    )

# In orchestrator, after each stage:
emit_metric("PipelineDuration",    elapsed,       "Seconds")
emit_metric("BedrockCallsTotal",   4,             "Count")
emit_metric("AnomaliesDetected",   anomaly_count, "Count")
emit_metric("RemediationActions",  action_count,  "Count")
emit_metric("PipelineSuccess",     1 if success else 0, "Count")
```

### 2.3 CloudWatch Dashboard — Key Widgets

Create a dashboard at `CloudWatch → Dashboards → Create`:

| Widget | Metric | Why |
|--------|--------|-----|
| Lambda invocations | `AWS/Lambda Invocations` | Usage volume |
| Lambda errors | `AWS/Lambda Errors` | Error rate |
| Lambda P99 duration | `AWS/Lambda Duration p99` | Tail latency |
| Pipeline success rate | `CloudOpsAIAgent/PipelineSuccess` | Health |
| Bedrock latency | `CloudOpsAIAgent/BedrockDuration` | AI bottleneck |
| Anomalies detected | `CloudOpsAIAgent/AnomaliesDetected` | Business value |

### 2.4 Alarms — Minimum Set for Production

```
Alarm 1: High error rate
  Metric:    AWS/Lambda Errors
  Threshold: > 5 in 5 minutes
  Action:    SNS → PagerDuty / Slack

Alarm 2: Duration approaching timeout
  Metric:    AWS/Lambda Duration (p99)
  Threshold: > 240,000 ms (80% of 300s)
  Action:    SNS

Alarm 3: Throttling
  Metric:    AWS/Lambda Throttles
  Threshold: > 0
  Action:    SNS

Alarm 4: Pipeline failure rate
  Metric:    CloudOpsAIAgent/PipelineSuccess
  Threshold: < 0.8 (80% success) over 15 min
  Action:    SNS → PagerDuty

Alarm 5: Bedrock cost spike
  Metric:    AWS/Billing EstimatedCharges
  Threshold: > $50/day
  Action:    SNS → email
```

### 2.5 AWS X-Ray Tracing

Enable distributed tracing to see exactly where time is spent:

```python
# In app.py
from aws_xray_sdk.core import xray_recorder, patch_all
patch_all()   # auto-instruments boto3 calls

# Wrap each agent stage
with xray_recorder.in_subsegment("IncidentAgent"):
    context = self._incident_agent.investigate(...)

with xray_recorder.in_subsegment("MetricsAgent"):
    context = self._metrics_agent.analyze(context)
```

Enable in Lambda: `Configuration → Monitoring → Active tracing → ON`

---

## 3. Reliability & Resilience

### 3.1 Dead Letter Queue (DLQ)

If the Lambda is invoked asynchronously (EventBridge, SNS), failed events must not be silently dropped:

```
Lambda → Configuration → Asynchronous invocation:
  Maximum age of event:    1 hour
  Retry attempts:          2
  Dead-letter queue:       SQS queue "cloudops-ai-agent-dlq"
```

Set an alarm on `ApproximateNumberOfMessagesVisible > 0` on the DLQ.

### 3.2 Timeouts — Defense in Depth

```
API Gateway timeout:   29 seconds (hard AWS limit)
Lambda timeout:        300 seconds (5 minutes)
Per-agent timeout:     60 seconds (implement with concurrent.futures)
Bedrock call timeout:  30 seconds (botocore config)
CloudWatch call:       10 seconds
```

```python
# Enforce Bedrock timeout in model_adapter.py
import botocore

config = botocore.config.Config(
    connect_timeout = 10,
    read_timeout    = 30,
    retries         = {"max_attempts": 3, "mode": "adaptive"},
)
self._client = boto3.client("bedrock-runtime", config=config)
```

### 3.3 Circuit Breaker Pattern

If Bedrock is down, fall back immediately instead of timing out on every call:

```python
class CircuitBreaker:
    def __init__(self, failure_threshold=5, reset_timeout=60):
        self.failures        = 0
        self.threshold       = failure_threshold
        self.reset_timeout   = reset_timeout
        self.last_failure_ts = 0
        self.state           = "CLOSED"   # CLOSED=normal, OPEN=failing, HALF_OPEN=testing

    def call(self, fn, *args, **kwargs):
        if self.state == "OPEN":
            if time.time() - self.last_failure_ts > self.reset_timeout:
                self.state = "HALF_OPEN"
            else:
                raise Exception("Circuit open — using fallback")
        try:
            result = fn(*args, **kwargs)
            if self.state == "HALF_OPEN":
                self.state    = "CLOSED"
                self.failures = 0
            return result
        except Exception as exc:
            self.failures += 1
            self.last_failure_ts = time.time()
            if self.failures >= self.threshold:
                self.state = "OPEN"
            raise
```

### 3.4 Idempotency

Use the incident_id as an idempotency key so retried requests don't trigger duplicate investigations:

```python
import hashlib

def get_or_create_incident_id(description: str, timestamp: str) -> str:
    """Return the same ID for identical incident+timestamp combos."""
    key = f"{description[:100]}:{timestamp[:16]}"   # minute-level dedup
    return "INC-" + hashlib.md5(key.encode()).hexdigest()[:12].upper()
```

---

## 4. Performance & Scalability

### 4.1 Lambda Warm-Start Optimisation

```python
# Already in app.py — these run ONCE per container (cold start only):
_orchestrator: CloudOpsOrchestrator | None = None

def _get_orchestrator() -> CloudOpsOrchestrator:
    global _orchestrator
    if _orchestrator is None:
        _orchestrator = CloudOpsOrchestrator()   # boto3 clients created here
    return _orchestrator

# lambda_handler just calls _get_orchestrator() — warm invocations skip init
```

Additional warm-start tips:
- Keep the deployment ZIP under 10 MB (boto3 is pre-installed in Lambda runtime)
- Use `import` statements at module level, not inside functions
- Move `boto3.client()` calls to module/class level, not inside handlers

### 4.2 Parallelise Independent Agents

Metrics and Log analysis are independent — run them concurrently:

```python
import concurrent.futures

with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
    metrics_future = executor.submit(self._metrics_agent.analyze, context)
    log_future     = executor.submit(self._log_agent.analyze, context)

    metrics_context = metrics_future.result(timeout=60)
    log_context     = log_future.result(timeout=60)

# Merge results
context = {**context,
           "metrics_report": metrics_context["metrics_report"],
           "log_report":     log_context["log_report"]}
```

This cuts pipeline time from ~4s to ~2.5s for typical incidents.

### 4.3 Provisioned Concurrency

Eliminate cold starts for latency-sensitive deployments:

```
Lambda → Configuration → Concurrency → Provisioned concurrency
  Provisioned concurrency: 2  (keeps 2 warm containers always)
```

Use with an alias: point the `live` alias at a published version, set provisioned concurrency on the alias (not `$LATEST`).

### 4.4 Caching CloudWatch Data

CloudWatch Metrics has a cost per `GetMetricData` API call. Cache results within a pipeline run:

```python
from functools import lru_cache

@lru_cache(maxsize=64)
def _cached_lambda_health(self, function_name: str, lookback: int) -> str:
    result = self.metrics_tool.get_lambda_health(function_name, minutes=lookback)
    return json.dumps(result)   # lru_cache requires hashable return
```

---

## 5. Cost Optimisation

### 5.1 Bedrock Cost Breakdown

At 100 incidents/day with Nova Pro:

| Component | Tokens/incident | Cost/1M tokens | Daily cost |
|-----------|----------------|----------------|------------|
| Input (4 calls × ~800 tokens) | 3,200 | $0.80 | ~$0.26 |
| Output (4 calls × ~400 tokens) | 1,600 | $3.20 | ~$0.51 |
| **Total Bedrock** | | | **~$0.77/day** |

Compare models:

| Model | Input $/1M | Output $/1M | Best for |
|-------|-----------|------------|---------|
| Nova Micro | $0.035 | $0.14 | High-volume, simple incidents |
| Nova Lite | $0.06 | $0.24 | Balanced cost/quality |
| Nova Pro | $0.80 | $3.20 | Complex multi-service incidents |
| Claude 3 Haiku | $0.25 | $1.25 | Fast + cheap |
| Claude 3 Sonnet | $3.00 | $15.00 | Best reasoning |

> Strategy: Use Nova Micro for Metrics + Log agents (structured analysis), Nova Pro only for the final Remediation recommendation.

### 5.2 Per-Agent Model Selection

Set different models per agent via environment variables:

```python
# In each agent __init__:
import os

class IncidentAgent:
    DEFAULT_MODEL = os.getenv(
        "INCIDENT_AGENT_MODEL",
        os.getenv("BEDROCK_MODEL_ID", "amazon.nova-lite-v1:0")
    )

class RemediationAgent:
    DEFAULT_MODEL = os.getenv(
        "REMEDIATION_AGENT_MODEL",
        os.getenv("BEDROCK_MODEL_ID", "amazon.nova-pro-v1:0")
    )
```

Lambda env vars:
```
INCIDENT_AGENT_MODEL    = amazon.nova-micro-v1:0   (cheapest — just parsing)
METRICS_AGENT_MODEL     = amazon.nova-micro-v1:0   (structured data — easy)
LOG_AGENT_MODEL         = amazon.nova-lite-v1:0    (pattern analysis — medium)
REMEDIATION_AGENT_MODEL = amazon.nova-pro-v1:0     (final plan — needs quality)
```

This cuts costs by ~60% vs using Nova Pro for all agents.

### 5.3 CloudWatch Cost Controls

```
Metric API calls:    ~$0.01 per 1,000 calls
Logs ingestion:      $0.50 per GB
Logs storage:        $0.03 per GB/month

Controls:
- Set log retention to 30 days (not Never)
- Use metric filters instead of log insights for frequent queries
- Cache CloudWatch results within a pipeline run (see 4.4)
- Use GetMetricData with longer periods (300s) to reduce data points
```

### 5.4 Lambda Cost

Lambda pricing: $0.0000166667 per GB-second

At 100 incidents/day, 3s avg, 512 MB:
```
100 × 3s × 0.5 GB = 150 GB-seconds/day
150 × $0.0000166667 = $0.0025/day ≈ $0.075/month
```

Lambda is negligible — the cost driver is always Bedrock.

---

## 6. CI/CD & Deployment

### 6.1 Environment Strategy

```
Branch      → Environment  → Auto-deploy?  → DRY_RUN  → Model
─────────────────────────────────────────────────────────────────
feature/*   → (local only)  No             true       nova-micro
develop     → staging       Yes (on PR)    true       nova-lite
main        → production    Yes (on merge) false*     nova-pro

* AUTO_EXECUTE=false always; DRY_RUN=false allows real remediation actions
```

### 6.2 Blue/Green Deployment with Lambda Aliases

```bash
# 1. Deploy new code
aws lambda update-function-code \
  --function-name cloudops-ai-agent \
  --zip-file fileb://package.zip

# 2. Publish new version
VERSION=$(aws lambda publish-version \
  --function-name cloudops-ai-agent \
  --query 'Version' --output text)

# 3. Shift 10% of traffic to new version (canary)
aws lambda update-alias \
  --function-name cloudops-ai-agent \
  --name live \
  --function-version $VERSION \
  --routing-config AdditionalVersionWeights={"$VERSION"=0.1}

# 4. Monitor for 10 minutes, then shift 100%
aws lambda update-alias \
  --function-name cloudops-ai-agent \
  --name live \
  --function-version $VERSION
```

### 6.3 Automated Rollback

Add this to `.github/workflows/deploy.yml`:

```yaml
- name: Monitor post-deploy (5 minutes)
  run: |
    sleep 300
    ERROR_COUNT=$(aws cloudwatch get-metric-statistics \
      --namespace AWS/Lambda \
      --metric-name Errors \
      --dimensions Name=FunctionName,Value=cloudops-ai-agent \
      --start-time $(date -u -d '5 minutes ago' +%Y-%m-%dT%H:%M:%S) \
      --end-time $(date -u +%Y-%m-%dT%H:%M:%S) \
      --period 300 --statistics Sum \
      --query 'Datapoints[0].Sum' --output text)
    
    if [ "$ERROR_COUNT" -gt "10" ]; then
      echo "ERROR: $ERROR_COUNT errors detected — rolling back"
      aws lambda update-alias \
        --function-name cloudops-ai-agent \
        --name live \
        --function-version $PREVIOUS_VERSION
      exit 1
    fi
```

### 6.4 Dependency Pinning

Pin exact versions in `requirements.txt` for reproducible builds:

```
boto3==1.34.162
botocore==1.34.162
```

Use `pip-compile` (from `pip-tools`) to generate pinned files from abstract requirements.

---

## 7. Model & AI Best Practices

### 7.1 Prompt Engineering

Structure prompts for reliable JSON output:

```python
# Good prompt structure
prompt = f"""You are an expert AWS SRE.

CONTEXT
-------
{incident_summary}

DATA
----
{json.dumps(metrics_data, indent=2)}

Return ONLY a JSON object with these exact keys (no markdown, no explanation):
{{
  "root_cause": "one sentence",
  "confidence": "LOW | MEDIUM | HIGH",
  "evidence":   ["item1", "item2"]
}}"""
```

Key rules:
- Always specify exact output keys
- Say "ONLY a JSON object" — prevents markdown fences
- Keep prompts under 2,000 tokens for Nova Micro/Lite
- Use concrete examples of expected output format

### 7.2 Response Validation

Never trust AI output blindly — validate before using:

```python
REQUIRED_KEYS = {"root_cause", "confidence", "evidence"}
VALID_CONFIDENCE = {"LOW", "MEDIUM", "HIGH"}

def validate_ai_response(response: dict, required_keys: set) -> bool:
    if not required_keys.issubset(response.keys()):
        logger.warning("Missing keys: %s", required_keys - response.keys())
        return False
    if "confidence" in response and response["confidence"] not in VALID_CONFIDENCE:
        logger.warning("Invalid confidence: %s", response["confidence"])
        return False
    return True
```

### 7.3 Hallucination Guards

The model may hallucinate metric values or log patterns. Guard against this:

```python
def cross_validate_ai_claim(claim: str, actual_data: dict) -> bool:
    """
    If AI claims CPU is 95%, verify it against actual CloudWatch data.
    Returns False if the claim contradicts observed data by > 20%.
    """
    # Extract numeric claims using regex
    import re
    numbers = re.findall(r'(\d+(?:\.\d+)?)\s*%', claim)
    for n in numbers:
        value = float(n)
        # Check against actual metrics if available
        actual_cpu = actual_data.get("avg_cpu_percent", 0)
        if actual_cpu > 0 and abs(value - actual_cpu) > 20:
            logger.warning("AI claim %.1f%% diverges from actual %.1f%%", value, actual_cpu)
            return False
    return True
```

### 7.4 Model Version Locking

New model versions can change output format and break JSON parsing:

```python
# Pin exact model versions per environment
MODEL_VERSIONS = {
    "prod":    "amazon.nova-pro-v1:0",     # pin to v1:0
    "staging": "amazon.nova-pro-v1:0",
    "dev":     "amazon.nova-lite-v1:0",
}

# Never use "latest" or omit version suffix
# BAD:  "amazon.nova-pro"
# GOOD: "amazon.nova-pro-v1:0"
```

---

## 8. Incident Response

### 8.1 Runbook for Agent Failures

**Symptom: All Bedrock calls failing**
```
1. Check AWS Service Health: https://health.aws.amazon.com
2. Verify model access: Bedrock Console → Model access
3. Check IAM: CloudTrail → Filter by bedrock:InvokeModel → Look for Denied
4. Fallback: Set BEDROCK_MODEL_ID to a different model and redeploy
```

**Symptom: Lambda timing out (> 300s)**
```
1. Check which stage is slow: CloudWatch Logs → filter "Stage X done"
2. If MetricsAgent: reduce lookback_minutes (env var MAX_LOG_LOOKBACK)
3. If LogAgent:     reduce max_results in CloudWatchLogsTool
4. Enable parallel agent execution (see section 4.2)
5. Increase Lambda timeout to max 900s (15 min) if needed
```

**Symptom: High Bedrock costs**
```
1. Check token usage: CloudWatch → Bedrock metrics
2. Switch heavy agents to cheaper models (see section 5.2)
3. Add prompt length limits: truncate description to 500 chars
4. Enable caching for repeated identical incidents
```

### 8.2 Chaos Engineering — Test Failure Modes

Test that fallbacks work before production:

```python
# Add to tests/test_resilience.py
def test_pipeline_succeeds_when_bedrock_down(sample_incident):
    """Pipeline must return a result even if Bedrock is completely unavailable."""
    with patch.object(BedrockModelAdapter, "invoke_json", side_effect=Exception("Bedrock down")):
        orch = CloudOpsOrchestrator()
        result = orch.run("Lambda has errors")
    assert result["status"] == "SUCCESS"   # fallback mode
    triage = json.loads(result["agent_reports"]["incident"]["initial_triage"])
    assert "triage_summary" in triage      # fallback content present

def test_pipeline_succeeds_when_cloudwatch_down(sample_incident):
    """Pipeline must not crash if CloudWatch APIs are unavailable."""
    with patch("boto3.client") as mock:
        mock.return_value.get_metric_data.side_effect = Exception("CW down")
        mock.return_value.filter_log_events.side_effect = Exception("CW down")
        orch = CloudOpsOrchestrator()
        result = orch.run("Lambda has errors")
    assert result["status"] in ("SUCCESS", "ERROR")   # graceful, not a crash
```

---

## 9. Compliance & Data Governance

### 9.1 Data Classification

| Data type | Classification | Handling |
|-----------|---------------|---------|
| Incident descriptions | Internal | OK to send to Bedrock |
| CloudWatch log content | Internal/Confidential | Sanitise PII before sending to Bedrock |
| AWS account IDs | Internal | Mask in logs |
| Customer data in logs | Confidential/PII | Strip before Bedrock calls |
| Remediation actions taken | Internal | Store in DynamoDB with audit trail |

### 9.2 PII Scrubbing

Before sending log content to Bedrock, strip PII:

```python
import re

PII_PATTERNS = [
    (r'\b\d{4}[-\s]?\d{4}[-\s]?\d{4}[-\s]?\d{4}\b', '[CARD]'),       # credit cards
    (r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b', '[EMAIL]'),  # emails
    (r'\b(?:\d{1,3}\.){3}\d{1,3}\b', '[IP]'),                          # IP addresses
    (r'\b\d{3}-\d{2}-\d{4}\b', '[SSN]'),                               # SSN
]

def scrub_pii(text: str) -> str:
    for pattern, replacement in PII_PATTERNS:
        text = re.sub(pattern, replacement, text)
    return text
```

### 9.3 Audit Trail

Every remediation action must be logged with who/what/when:

```python
# Store in DynamoDB after every pipeline run
{
    "incident_id":    "INC-20260316-XXXX",
    "timestamp":      "2026-03-16T17:47:00Z",
    "triggered_by":   "API Gateway",           # or "EventBridge", "Manual"
    "severity":       "CRITICAL",
    "actions_taken":  ["LMB-002"],             # auto-executed actions
    "actions_recommended": ["RDS-003", "ALT-001"],
    "model_used":     "amazon.nova-pro-v1:0",
    "pipeline_duration_s": 2.4,
    "ttl":            1783296000               # 90-day retention (DynamoDB TTL)
}
```

### 9.4 Bedrock Data Privacy

- Bedrock does NOT train on your data by default
- Enable **Model invocation logging** only if needed for debugging (logs go to S3/CloudWatch — govern access carefully)
- For regulated industries: request a **Bedrock Private Endpoints** quote from AWS

---

## 10. Production Readiness Checklist

Run through this before going live:

### Security
- [ ] IAM role uses least-privilege (no `*` actions in prod)
- [ ] API Gateway has authentication (API key, IAM, or Cognito)
- [ ] Lambda runs inside a VPC with private subnets
- [ ] VPC endpoints configured for Bedrock, CloudWatch, Logs
- [ ] Secrets in Secrets Manager (not env vars)
- [ ] All env vars encrypted with KMS
- [ ] PII scrubbing in place before Bedrock calls
- [ ] S3 deployment bucket has versioning + encryption

### Reliability
- [ ] Dead Letter Queue configured for async invocations
- [ ] Bedrock timeout set (30s) + retry with backoff
- [ ] Lambda timeout tested at 300s
- [ ] Fallback mode tested (Bedrock unavailable)
- [ ] Fallback mode tested (CloudWatch unavailable)
- [ ] Idempotency implemented for duplicate requests
- [ ] Circuit breaker in place for Bedrock calls

### Observability
- [ ] Structured JSON logging enabled
- [ ] Custom metrics emitted per pipeline stage
- [ ] X-Ray tracing enabled
- [ ] CloudWatch Dashboard created
- [ ] Alarms set: errors, duration, throttles, cost
- [ ] SNS topic → PagerDuty/Slack notifications confirmed
- [ ] Log retention set to 30 days (not Never)
- [ ] DLQ alarm configured

### Performance
- [ ] Provisioned concurrency set on `live` alias
- [ ] Parallel agent execution enabled (Metrics + Log)
- [ ] Deployment ZIP < 10 MB
- [ ] Cold start < 2 seconds verified
- [ ] p99 latency < 10 seconds under load

### Cost
- [ ] Per-agent model selection configured
- [ ] Bedrock cost alarm set ($50/day threshold)
- [ ] Log retention = 30 days
- [ ] DynamoDB TTL set (90 days)
- [ ] Monthly cost estimate documented and approved

### CI/CD
- [ ] CI passes on all PRs (lint + typecheck + tests)
- [ ] Coverage ≥ 75%
- [ ] Staging deploy + smoke test automated
- [ ] Blue/green alias deployment configured
- [ ] Automated rollback on error spike
- [ ] Secrets in GitHub Actions (not in code)
- [ ] OIDC used (no stored AWS credentials)

### Compliance
- [ ] Audit trail in DynamoDB with TTL
- [ ] Data classification documented
- [ ] PII scrubbing tested with sample log data
- [ ] Bedrock invocation logging decision documented
- [ ] Post-incident process documented and practiced
