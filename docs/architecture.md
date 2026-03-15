# Architecture Deep Dive

## Pipeline Design Philosophy

The CloudOps AI Agent is built as a **linear four-stage pipeline** where each stage enriches a shared `IncidentContext` dictionary. This design choice means:

- Every agent receives the full context of all previous stages
- Stages can be unit-tested in isolation with a mock context
- The pipeline can be resumed from any stage (useful for debugging)
- A single Lambda function handles the entire pipeline (no inter-service latency)

## Stage Contracts

Each agent exposes one public method and follows this contract:

```
input:  dict  (IncidentContext enriched up to this stage)
output: dict  (same context + new report key added)
```

| Stage | Method | Input keys used | Output key added |
|-------|--------|----------------|-----------------|
| 1 | `IncidentAgent.investigate()` | description, resource_hints | `initial_triage`, `affected_resources`, `severity` |
| 2 | `MetricsAgent.analyze()` | `affected_resources`, `lookback_minutes` | `metrics_report` |
| 3 | `LogAgent.analyze()` | `affected_resources`, `metrics_report.anomalies` | `log_report` |
| 4 | `RemediationAgent.remediate()` | All previous keys | `remediation_report` |

## Bedrock Integration Pattern

Every agent calls Amazon Bedrock (Claude 3 Sonnet) for AI reasoning. All agents use the same pattern:

```python
prompt = f"""..structured prompt with extracted data.."""

body = json.dumps({
    "anthropic_version": "bedrock-2023-05-31",
    "max_tokens": 1024,
    "messages": [{"role": "user", "content": prompt}],
})
response = self._bedrock.invoke_model(modelId=self.model_id, body=body, ...)
content  = json.loads(response["body"].read())["content"][0]["text"]
result   = json.loads(content)   # structured JSON from the model
```

Each agent prompts the model to return structured JSON (no markdown fences), which is then parsed and embedded in the context. If Bedrock fails for any reason (throttle, unavailability, parse error), every agent has a **heuristic fallback** that returns a lower-confidence but structurally identical result — the pipeline never breaks.

## Anomaly Detection (MetricsAgent)

The `AnomalyDetector` class uses three complementary methods:

1. **Z-score** — flags latest value when `|z| > 2.5` (good for gradual drift)
2. **IQR fence** — flags values outside `[Q1 - 1.5·IQR, Q3 + 1.5·IQR]` (robust to outliers)
3. **Hard thresholds** — domain-specific ceilings (CPU > 85 %, Lambda errors > 10, etc.)

The anomaly score (0–1) is the maximum of the three methods, so even a single triggered method elevates the score.

## Log Pattern Matching (LogAgent)

`LogPatternLibrary` holds 15 named regex patterns covering:

| Category | Patterns |
|----------|---------|
| TIMEOUT | Lambda Timeout |
| RESOURCE | Lambda OOM, OOM Killer, Disk Full |
| CONNECTIVITY | Connection Refused, Connection Timeout |
| THROTTLING | ThrottlingException |
| DATABASE | MySQL/Postgres/DynamoDB errors |
| HTTP | 5xx errors, 4xx spikes |
| SECURITY | AccessDenied, SSL/TLS errors |
| EXCEPTION | Unhandled exceptions, Stack overflow |

`LogCorrelationEngine` maps log categories to metric signals using a static lookup table:

```python
CATEGORY_METRIC_MAP = {
    "TIMEOUT":      ["lambda_duration", "rds_read_latency"],
    "THROTTLING":   ["lambda_throttles", "dynamo_read_throttle"],
    "DATABASE":     ["rds_cpu", "rds_connections"],
    ...
}
```

When a log pattern category matches a metric anomaly metric, the correlation strength is boosted above 0.5.

## Remediation Catalogue (RemediationAgent)

The catalogue contains pre-built `RemediationAction` objects keyed by service + anomaly type. Actions are scored using:

```
score = confidence × 0.4 + (1 − risk_penalty) × 0.3 + speed_score × 0.3
```

where `speed_score = 1 - estimated_ttr_mins / 60`. CRITICAL incidents apply a 1.5× severity multiplier. Actions with `automated=True` and `risk_level=LOW` can be auto-executed when `AUTO_EXECUTE=true` and `DRY_RUN=false`.

## Lambda Warm-Start Optimisation

The `CloudOpsOrchestrator` is instantiated once as a module-level singleton (`_orchestrator`) and reused across Lambda invocations. This means boto3 clients (CloudWatch, Bedrock runtime) are created once per container rather than per request — a ~200ms saving on warm invocations.

## Error Handling Strategy

| Layer | Error type | Behaviour |
|-------|-----------|-----------|
| Tools | `ClientError` | Re-raised as `CloudWatch{Logs,Metrics}Error` |
| Agents | Bedrock failure | Fallback heuristic result returned |
| Agents | Tool failure | Resource skipped, logged as warning |
| Orchestrator | Any unhandled exception | `ERROR` response with traceback |
| Lambda handler | JSON parse error | HTTP 400 |
| Lambda handler | Any other error | HTTP 500 |
