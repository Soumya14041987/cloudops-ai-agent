# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/)
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [Unreleased]

### Added
- Nothing yet.

---

## [1.0.0] — 2024-03-15

### Added
- `IncidentAgent` — free-form incident parsing with severity/resource extraction and Bedrock triage.
- `MetricsAgent` — CloudWatch Metrics analysis with z-score + IQR + hard-threshold anomaly detection.
- `LogAgent` — CloudWatch Logs analysis with 15-pattern library, timeline, and log-metric correlation.
- `RemediationAgent` — Rule-based remediation catalogue (10 actions) with confidence scoring and optional auto-execution.
- `CloudOpsOrchestrator` — Linear four-stage pipeline with warm-start Lambda optimisation.
- `CloudWatchLogsTool` — Paginated log retrieval, error filtering, keyword search, and health stats.
- `CloudWatchMetricsTool` — Lambda, EC2, and RDS health wrappers with statistical summaries.
- AWS Lambda handler (`lambda_handler`) with API Gateway compatibility.
- Local CLI runner (`run_local`, `scripts/invoke_local.py`).
- GitHub Actions CI pipeline (lint → typecheck → test → package).
- GitHub Actions deploy pipeline (staging → prod with OIDC).
- Full test suite with moto-mocked AWS services.
- Makefile with `install-dev`, `lint`, `format`, `typecheck`, `test`, `test-cov`, `package`, `deploy` targets.
- `pyproject.toml` with ruff, mypy, pytest, and coverage configuration.
- Operator runbook (`docs/runbook.md`).
- Architecture deep-dive (`docs/architecture.md`).
- Per-agent API reference (`docs/agents.md`).

### Technical decisions
- Single Lambda function for the full pipeline (no inter-service latency).
- Bedrock fallback on every agent (pipeline never breaks due to Bedrock unavailability).
- Dependency injection for tools (easy mocking in tests).
- `IncidentContext` as a plain dict subclass (no Pydantic dependency, Lambda-package-size friendly).

---

[Unreleased]: https://github.com/Soumya14041987/cloudops-ai-agent/compare/v1.0.0...HEAD
[1.0.0]: https://github.com/Soumya14041987/cloudops-ai-agent/releases/tag/v1.0.0
