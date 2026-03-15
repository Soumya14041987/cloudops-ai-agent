# Agent API Reference

## IncidentAgent

**Module:** `agents.incident_agent`

### Class: `IncidentAgent`

```python
IncidentAgent(
    bedrock_model_id: str = "anthropic.claude-3-sonnet-20240229-v1:0",
    region_name: str = "us-east-1",
    max_tokens: int = 1024,
)
```

#### Method: `investigate()`

```python
def investigate(
    incident_description: str,
    resource_hints: dict | None = None,
    override_severity: str | None = None,
) -> IncidentContext
```

**Parameters:**

| Parameter | Type | Description |
|-----------|------|-------------|
| `incident_description` | `str` | Free-form incident text from the operator |
| `resource_hints` | `dict` | Explicit resource IDs: `{"lambda": [...], "ec2": [...], "rds": [...]}` |
| `override_severity` | `str` | Force severity: `CRITICAL / HIGH / MEDIUM / LOW` |

**Returns:** `IncidentContext` (dict subclass) with keys:

| Key | Type | Description |
|-----|------|-------------|
| `incident_id` | `str` | Unique incident identifier, e.g. `INC-20240315140000-AB12CD34` |
| `raw_description` | `str` | Original incident text |
| `severity` | `str` | `CRITICAL / HIGH / MEDIUM / LOW` |
| `affected_resources` | `dict` | `{service: [id, ...]}` |
| `lookback_minutes` | `int` | Suggested metric/log window |
| `symptoms` | `list[str]` | Extracted symptom phrases |
| `initial_triage` | `str` | JSON string with Bedrock triage |
| `start_time` | `str` | ISO-8601 UTC timestamp |

**Raises:** `ValueError` if `incident_description` is empty.

---

## MetricsAgent

**Module:** `agents.metrics_agent`

### Class: `AnomalyDetector`

```python
AnomalyDetector()
```

#### Method: `detect()`

```python
def detect(values: list[float], metric_name: str = "") -> dict
```

Returns: `{is_anomalous, anomaly_score, anomaly_type, description, latest_value, mean_value, max_value}`

### Class: `MetricsAgent`

```python
MetricsAgent(
    metrics_tool: CloudWatchMetricsTool | None = None,
    bedrock_model_id: str = "...",
    region_name: str = "us-east-1",
)
```

#### Method: `analyze()`

```python
def analyze(incident_context: dict) -> dict
```

**Returns:** Context dict with `metrics_report` key added:

| Key | Type | Description |
|-----|------|-------------|
| `overall_health` | `str` | `CRITICAL / WARNING / HEALTHY` |
| `resources_checked` | `int` | Number of resources queried |
| `anomalies_detected` | `int` | Total anomalies found |
| `resource_health` | `dict` | Per-resource health dicts |
| `anomalies` | `list` | List of anomaly objects |
| `interpretation` | `str` | Bedrock JSON interpretation |
| `summary` | `str` | Human-readable summary |

---

## LogAgent

**Module:** `agents.log_agent`

### Class: `LogPatternLibrary`

```python
LogPatternLibrary()

def match(message: str) -> list[dict]
# Returns list of {pattern_name, severity, category}
```

### Class: `LogCorrelationEngine`

```python
LogCorrelationEngine()

def correlate(
    log_patterns: list[dict],
    metric_anomalies: list[dict],
) -> list[dict]
# Returns ranked list of {log_category, log_pattern, correlation_strength, hypothesis}
```

### Class: `LogAgent`

```python
LogAgent(
    logs_tool: CloudWatchLogsTool | None = None,
    bedrock_model_id: str = "...",
    region_name: str = "us-east-1",
)
```

#### Method: `analyze()`

```python
def analyze(incident_context: dict) -> dict
```

**Returns:** Context dict with `log_report` key added:

| Key | Type | Description |
|-----|------|-------------|
| `resources_analyzed` | `int` | Log groups queried |
| `total_error_events` | `int` | Total ERROR events found |
| `unique_patterns` | `int` | Distinct error pattern names |
| `log_statistics` | `dict` | Per-resource stats |
| `top_error_patterns` | `list` | Top 10 recurring patterns by count |
| `error_timeline` | `list` | `[{minute, error_count}]` |
| `correlations` | `list` | Top 5 log-metric correlations |
| `root_cause_hypothesis` | `str` | Bedrock JSON hypothesis |
| `critical_log_groups` | `list` | Resources in CRITICAL/DEGRADED state |
| `summary` | `str` | Human-readable summary |

---

## RemediationAgent

**Module:** `agents.remediation_agent`

### Dataclass: `RemediationAction`

```python
@dataclass
class RemediationAction:
    action_id:            str
    title:                str
    description:          str
    category:             str    # SCALING / RESTART / CONFIG / ALERT / ROLLBACK / INFRA
    risk_level:           str    # LOW / MEDIUM / HIGH / CRITICAL
    estimated_ttr_mins:   int
    confidence:           float  # 0.0 – 1.0
    aws_service:          str
    automated:            bool
    prerequisites:        list[str]
    steps:                list[str]
    rollback_plan:        str
    priority:             int    # 1 = highest; set by _rank_actions()
```

### Class: `RemediationCatalogue`

```python
RemediationCatalogue()

def get_candidates(incident_context: dict) -> list[RemediationAction]
```

Evaluates all rules against the context and returns matching actions.

**Current action IDs:**

| ID | Title | Service | Risk |
|----|-------|---------|------|
| LMB-001 | Increase Lambda Memory / Timeout | Lambda | LOW |
| LMB-002 | Increase Lambda Concurrency Limit | Lambda | MEDIUM |
| LMB-003 | Enable Lambda SnapStart / Optimise Init | Lambda | LOW |
| EC2-001 | Scale-Out EC2 Auto Scaling Group | EC2 | LOW |
| EC2-002 | Kill CPU-Hungry Processes | EC2 | MEDIUM |
| RDS-001 | Enable RDS Read Replicas | RDS | LOW |
| RDS-002 | Kill Long-Running DB Queries | RDS | MEDIUM |
| RDS-003 | Deploy RDS Proxy | RDS | LOW |
| NET-001 | Check Security Groups & VPC NACLs | VPC | LOW |
| ALT-001 | Create CloudWatch Alarm | CloudWatch | LOW |

### Class: `RemediationAgent`

```python
RemediationAgent(
    bedrock_model_id: str = "...",
    region_name: str = "us-east-1",
    auto_execute: bool = False,
    dry_run: bool = True,
)
```

#### Method: `remediate()`

```python
def remediate(incident_context: dict) -> dict
```

**Returns:** Context dict with `remediation_report` key added:

| Key | Type | Description |
|-----|------|-------------|
| `total_actions_identified` | `int` | Total candidate actions |
| `top_actions` | `list[dict]` | Top 5 ranked actions as dicts |
| `immediate_actions` | `list[dict]` | LOW-risk priority 1–2 actions |
| `short_term_actions` | `list[dict]` | Next 3 actions to take |
| `auto_executed` | `list[dict]` | Actions that were auto-executed |
| `auto_executed_count` | `int` | Count of auto-executed actions |
| `final_recommendation` | `str` | Bedrock JSON executive summary |
| `estimated_total_ttr_mins` | `int` | Sum of top-3 action TTRs |
| `summary` | `str` | Human-readable summary |
