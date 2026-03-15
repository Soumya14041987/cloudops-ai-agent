# Contributing

Thank you for taking the time to contribute to CloudOps AI Agent!

## Development Setup

```bash
git clone https://github.com/your-org/cloudops-ai-agent.git
cd cloudops-ai-agent

# Create venv and install all dependencies
make install-dev

# Copy environment template
make env
# Edit .env with your AWS credentials/region
```

## Branching Strategy

| Branch | Purpose |
|--------|---------|
| `main` | Production-ready code — triggers deploy to prod |
| `develop` | Integration branch |
| `feat/<name>` | New features |
| `fix/<name>` | Bug fixes |
| `chore/<name>` | Non-functional changes (deps, CI, docs) |

## Making Changes

1. Create a branch: `git checkout -b feat/your-feature develop`
2. Make your changes.
3. Run the full quality suite: `make ci`
4. Commit using [Conventional Commits](https://www.conventionalcommits.org/):
   ```
   feat: add DynamoDB incident history storage
   fix: handle missing log group gracefully in LogAgent
   docs: add runbook section for RDS connection exhaustion
   chore: upgrade boto3 to 1.35.0
   ```
5. Push and open a pull request against `develop`.

## Code Standards

### Style

- **Formatter:** `ruff format` (runs as part of `make format`)
- **Linter:** `ruff check` (runs as part of `make lint`)
- **Type checker:** `mypy` (runs as part of `make typecheck`)
- Line length: 100 characters
- All public methods must have docstrings.
- All modules must have a module-level docstring.

### Tests

- Every new feature needs at least one unit test.
- Bug fixes must include a regression test.
- Use `pytest.mark.unit` for tests that don't call AWS.
- Use `pytest.mark.integration` for tests that need real AWS credentials.
- Mock all AWS calls — never hardcode credentials.
- Minimum coverage: 75 % (enforced by `make test-cov`).

### Adding a New Agent

1. Create `agents/your_agent.py` following the existing pattern:
   - One public `analyze()` (or equivalent) method
   - Accepts `incident_context: dict`, returns enriched `dict`
   - Bedrock fallback in every AI call
   - Injected tool dependencies for testability
2. Add to `agents/__init__.py`
3. Wire into `CloudOpsOrchestrator` in `app.py`
4. Write tests in `tests/test_your_agent.py`
5. Document in `docs/agents.md`

### Adding a New Remediation Action

1. Open `agents/remediation_agent.py`
2. Add a new `RemediationAction` inside `RemediationCatalogue.get_candidates()`
3. Follow the ID convention: `{SERVICE}-{NNN}` (e.g. `ECS-001`)
4. Set `automated=True` only if the action is safe to execute without human review
5. Always provide `steps` and `rollback_plan`

## Pull Request Checklist

- [ ] `make ci` passes (lint + typecheck + tests + coverage ≥ 75 %)
- [ ] New/changed code has docstrings
- [ ] Tests added for new functionality
- [ ] `CHANGELOG.md` updated under `[Unreleased]`
- [ ] PR description explains the *why*, not just the *what*

## Reporting Issues

Please use [GitHub Issues](https://github.com/your-org/cloudops-ai-agent/issues) and include:

- Python version (`python --version`)
- boto3 version (`pip show boto3`)
- AWS region
- Sanitised error traceback (no credentials!)
- Minimal reproduction steps
