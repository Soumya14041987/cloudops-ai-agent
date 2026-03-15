# 🤖 CloudOps AI Agent

> AI-powered production incident investigation, log analysis, infrastructure metrics correlation, and automated remediation — built on **Amazon Bedrock AgentCore** and **AWS Lambda**.

[![CI](https://github.com/Soumya14041987/cloudops-ai-agent/actions/workflows/ci.yml/badge.svg)](https://github.com/Soumya14041987/cloudops-ai-agent/actions/workflows/ci.yml)
[![Deploy](https://github.com/Soumya14041987/cloudops-ai-agent/actions/workflows/deploy.yml/badge.svg)](https://github.com/Soumya14041987/cloudops-ai-agent/actions/workflows/deploy.yml)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

---

## 📋 Table of Contents

- [Overview](#overview)
- [Architecture](#architecture)
- [Agent Pipeline](#agent-pipeline)
- [Project Structure](#project-structure)
- [Prerequisites](#prerequisites)
- [Quick Start](#quick-start)
- [Configuration](#configuration)
- [Deployment](#deployment)
- [Running Tests](#running-tests)
- [API Reference](#api-reference)
- [Contributing](#contributing)
- [Changelog](#changelog)

---

## Overview

CloudOps AI Agent is a multi-agent system that automatically investigates production incidents by:

1. **Parsing** free-form incident descriptions into structured triage
2. **Querying** CloudWatch Metrics for infrastructure health and anomalies
3. **Analysing** CloudWatch Logs for error patterns and root-cause evidence
4. **Generating** a ranked, actionable remediation plan with Bedrock/Claude

All four specialised agents share a rolling `IncidentContext` — a structured dict that grows richer at each stage and is returned as the final pipeline result.

---

## Architecture

```
User / Operator
      │
      ▼
Amazon API Gateway  (REST — POST /investigate)
      │
      ▼
AWS Lambda          (app.lambda_handler — CloudOpsOrchestrator)
      │
      ▼
Amazon Bedrock AgentCore Runtime  (Claude 3 Sonnet)
      │
  ┌───┼────────────────────────────────┐
  │   │                                │
  ▼   ▼                   ▼            ▼
Incident  Log         Metrics    Remediation
Agent     Agent       Agent      Agent
  │   │                                │
  └───┴──── Correlation Engine ────────┘
                    │
                    ▼
          Final Recommendation
                    │
                    ▼
          AgentCore Gateway
      ┌───────┬────────┬──────────┐
      ▼       ▼        ▼          ▼
   CW Logs  CW Metrics DynamoDB  External APIs
```

---

## Agent Pipeline

| Stage | Agent | Responsibility | Key Output |
|-------|-------|---------------|------------|
| 1 | `IncidentAgent` | Parse description, extract severity & resources, Bedrock triage | `initial_triage`, `affected_resources` |
| 2 | `MetricsAgent` | CloudWatch Metrics + anomaly detection (z-score + IQR) | `metrics_report`, `anomalies` |
| 3 | `LogAgent` | CloudWatch Logs + pattern matching + log-metric correlation | `log_report`, `root_cause_hypothesis` |
| 4 | `RemediationAgent` | Rule-based action catalogue + Bedrock final recommendation | `remediation_report`, `final_recommendation` |

---

## Project Structure

```
cloudops-ai-agent/
│
├── .github/
│   └── workflows/
│       ├── ci.yml              # Lint, type-check, test on every PR
│       └── deploy.yml          # Deploy to AWS Lambda on main merge
│
├── agents/
│   ├── __init__.py
│   ├── incident_agent.py       # Stage 1 — incident parsing & triage
│   ├── log_agent.py            # Stage 3 — CloudWatch Logs analysis
│   ├── metrics_agent.py        # Stage 2 — CloudWatch Metrics analysis
│   └── remediation_agent.py    # Stage 4 — remediation plan generation
│
├── tools/
│   ├── __init__.py
│   ├── cloudwatch_logs.py      # CW Logs API wrapper
│   └── cloudwatch_metrics.py   # CW Metrics API wrapper
│
├── tests/
│   ├── __init__.py
│   ├── conftest.py             # Shared fixtures & mocks
│   ├── test_incident_agent.py
│   ├── test_metrics_agent.py
│   ├── test_log_agent.py
│   ├── test_remediation_agent.py
│   ├── test_cloudwatch_logs.py
│   ├── test_cloudwatch_metrics.py
│   └── test_app.py             # End-to-end orchestrator tests
│
├── docs/
│   ├── architecture.md         # Deep-dive architecture notes
│   ├── agents.md               # Per-agent API reference
│   └── runbook.md              # Operator runbook
│
├── scripts/
│   ├── deploy.sh               # Manual Lambda deploy helper
│   └── invoke_local.py         # Local CLI invocation helper
│
├── app.py                      # Lambda handler + CloudOpsOrchestrator
├── requirements.txt            # Production dependencies
├── requirements-dev.txt        # Dev/test dependencies
├── pyproject.toml              # Build metadata + tool config
├── Makefile                    # Developer workflow shortcuts
├── .env.example                # Template for local env vars
├── .gitignore
├── CHANGELOG.md
└── LICENSE
```

---

## Prerequisites

| Requirement | Version |
|-------------|---------|
| Python | 3.11+ |
| AWS CLI | 2.x |
| boto3 | 1.34+ |
| AWS account with Bedrock access | — |
| IAM role with CW Logs/Metrics + Bedrock | — |

### IAM Permissions Required

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": [
        "logs:FilterLogEvents",
        "logs:DescribeLogGroups",
        "logs:GetLogEvents"
      ],
      "Resource": "arn:aws:logs:*:*:*"
    },
    {
      "Effect": "Allow",
      "Action": [
        "cloudwatch:GetMetricData",
        "cloudwatch:DescribeAlarms"
      ],
      "Resource": "*"
    },
    {
      "Effect": "Allow",
      "Action": [
        "bedrock:InvokeModel"
      ],
      "Resource": "arn:aws:bedrock:*::foundation-model/anthropic.claude-3-sonnet*"
    },
    {
      "Effect": "Allow",
      "Action": [
        "lambda:PutFunctionConcurrency"
      ],
      "Resource": "*"
    }
  ]
}
```

---

## Quick Start

### 1. Clone & install

```bash
git clone https://github.com/Soumya14041987/cloudops-ai-agent.git
cd cloudops-ai-agent

python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate

pip install -r requirements.txt
pip install -r requirements-dev.txt
```

### 2. Configure environment

```bash
cp .env.example .env
# Edit .env with your AWS region, Bedrock model, etc.
```

### 3. Run locally

```bash
# Quick smoke-test (uses fallback when Bedrock/CloudWatch unavailable)
python app.py

# Or via the helper script
python scripts/invoke_local.py \
  --description "Lambda payments-processor has 40% error rate since 14:00 UTC" \
  --resources '{"lambda": ["payments-processor"], "rds": ["prod-db"]}'
```

### 4. Run tests

```bash
make test
```

---

## Configuration

All configuration is driven by environment variables (see `.env.example`):

| Variable | Default | Description |
|----------|---------|-------------|
| `AWS_REGION` | `us-east-1` | AWS region for all API calls |
| `BEDROCK_MODEL_ID` | `anthropic.claude-3-sonnet-20240229-v1:0` | Bedrock model ID |
| `AUTO_EXECUTE` | `false` | Auto-execute LOW-risk remediation actions |
| `DRY_RUN` | `true` | Never call AWS remediation APIs when true |
| `LOG_LEVEL` | `INFO` | Python logging level |
| `MAX_LOG_LOOKBACK` | `240` | Maximum log/metric lookback in minutes |

---

## Deployment

### Using the Makefile

```bash
# Package + deploy to Lambda
make deploy ENV=prod

# Deploy to staging
make deploy ENV=staging
```

### Manual deploy

```bash
chmod +x scripts/deploy.sh
./scripts/deploy.sh prod
```

### GitHub Actions (recommended)

Push to `main` branch triggers `.github/workflows/deploy.yml` automatically.
Set the following repository secrets:

| Secret | Description |
|--------|-------------|
| `AWS_ROLE_ARN` | IAM role ARN for OIDC deployment |
| `LAMBDA_FUNCTION_NAME` | Target Lambda function name |
| `AWS_REGION` | Deployment region |

---

## Running Tests

```bash
# All tests
make test

# With coverage report
make test-cov

# Specific test file
pytest tests/test_incident_agent.py -v

# Only unit tests (no AWS calls)
pytest tests/ -m unit -v

# Only integration tests (requires AWS credentials)
pytest tests/ -m integration -v
```

---

## API Reference

### Lambda / API Gateway

**POST** `/investigate`

**Request body:**
```json
{
  "incident_description": "Lambda payments-processor has 40% error rate since 14:00 UTC",
  "resource_hints": {
    "lambda": ["payments-processor"],
    "rds": ["prod-payments-db"]
  },
  "override_severity": "HIGH"
}
```

**Response:**
```json
{
  "status": "SUCCESS",
  "incident_id": "INC-20240315140523-AB12CD34",
  "severity": "HIGH",
  "pipeline_result": {
    "incident_summary": "...",
    "metrics_summary": "...",
    "log_summary": "...",
    "remediation_summary": "...",
    "final_recommendation": "{ ... }",
    "estimated_ttr_minutes": 15,
    "overall_health": "CRITICAL"
  },
  "agent_reports": { "..." },
  "performance": {
    "total_elapsed_seconds": 4.2,
    "stage_timings": { "incident_agent": 1.1, "..." }
  }
}
```

---

## Contributing

1. Fork the repository
2. Create a feature branch: `git checkout -b feat/your-feature`
3. Run `make lint` and `make test` before committing
4. Open a pull request against `main`

Please read [CONTRIBUTING.md](CONTRIBUTING.md) for detailed guidelines.

---

## Changelog

See [CHANGELOG.md](CHANGELOG.md).

---

## License

MIT — see [LICENSE](LICENSE).
