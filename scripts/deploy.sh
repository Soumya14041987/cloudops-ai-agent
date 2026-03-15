#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# deploy.sh — Manual Lambda deployment helper
#
# Usage:
#   ./scripts/deploy.sh [staging|prod]
#
# Prerequisites:
#   - AWS CLI v2 configured (profile or env vars)
#   - lambda_package.zip already built by `make package`
#   - .env file (or env vars) with LAMBDA_FUNCTION_NAME, AWS_REGION
# ─────────────────────────────────────────────────────────────────────────────

set -euo pipefail

# ── Load .env if present ──────────────────────────────────────────────────────
if [ -f .env ]; then
  export $(grep -v '^#' .env | xargs)
fi

# ── Arguments ─────────────────────────────────────────────────────────────────
ENV="${1:-staging}"

if [[ "$ENV" != "staging" && "$ENV" != "prod" ]]; then
  echo "Usage: $0 [staging|prod]"
  exit 1
fi

# ── Config ────────────────────────────────────────────────────────────────────
REGION="${AWS_REGION:-us-east-1}"
BASE_FUNCTION_NAME="${LAMBDA_FUNCTION_NAME:-cloudops-ai-agent}"
PACKAGE_FILE="lambda_package.zip"

if [[ "$ENV" == "staging" ]]; then
  FUNCTION_NAME="${BASE_FUNCTION_NAME}-staging"
  AUTO_EXECUTE="false"
  DRY_RUN="true"
  LOG_LEVEL="DEBUG"
else
  FUNCTION_NAME="${BASE_FUNCTION_NAME}"
  AUTO_EXECUTE="${AUTO_EXECUTE:-false}"
  DRY_RUN="${DRY_RUN:-true}"
  LOG_LEVEL="INFO"
fi

echo "========================================================"
echo "  CloudOps AI Agent — Deploy"
echo "  Environment : ${ENV}"
echo "  Function    : ${FUNCTION_NAME}"
echo "  Region      : ${REGION}"
echo "========================================================"

# ── Verify package exists ─────────────────────────────────────────────────────
if [ ! -f "$PACKAGE_FILE" ]; then
  echo "ERROR: ${PACKAGE_FILE} not found. Run 'make package' first."
  exit 1
fi

PACKAGE_SIZE=$(du -sh "$PACKAGE_FILE" | cut -f1)
echo "Package: ${PACKAGE_FILE} (${PACKAGE_SIZE})"

# ── Deploy code ───────────────────────────────────────────────────────────────
echo ""
echo "Uploading code..."
aws lambda update-function-code \
  --function-name "$FUNCTION_NAME" \
  --zip-file "fileb://${PACKAGE_FILE}" \
  --region "$REGION" \
  --output json \
  | python3 -c "import json,sys; r=json.load(sys.stdin); print(f'  CodeSize: {r.get(\"CodeSize\",\"?\")} bytes')"

# ── Wait for update ───────────────────────────────────────────────────────────
echo "Waiting for function update..."
aws lambda wait function-updated \
  --function-name "$FUNCTION_NAME" \
  --region "$REGION"
echo "  Done."

# ── Update environment variables ─────────────────────────────────────────────
echo ""
echo "Updating environment variables..."
aws lambda update-function-configuration \
  --function-name "$FUNCTION_NAME" \
  --environment "Variables={
    AWS_REGION=${REGION},
    BEDROCK_MODEL_ID=${BEDROCK_MODEL_ID:-anthropic.claude-3-sonnet-20240229-v1:0},
    AUTO_EXECUTE=${AUTO_EXECUTE},
    DRY_RUN=${DRY_RUN},
    LOG_LEVEL=${LOG_LEVEL},
    MAX_LOG_LOOKBACK=${MAX_LOG_LOOKBACK:-240}
  }" \
  --region "$REGION" \
  --output text \
  --query 'FunctionName' \
  | xargs -I{} echo "  Updated: {}"

# ── Production: publish version + update alias ────────────────────────────────
if [[ "$ENV" == "prod" ]]; then
  echo ""
  echo "Publishing new version..."
  VERSION=$(aws lambda publish-version \
    --function-name "$FUNCTION_NAME" \
    --region "$REGION" \
    --query 'Version' --output text)
  echo "  Version: $VERSION"

  echo "Updating 'live' alias → $VERSION..."
  aws lambda update-alias \
    --function-name "$FUNCTION_NAME" \
    --name live \
    --function-version "$VERSION" \
    --region "$REGION" \
    --output text --query 'AliasArn' \
    | xargs -I{} echo "  Alias: {}" \
    2>/dev/null || \
  aws lambda create-alias \
    --function-name "$FUNCTION_NAME" \
    --name live \
    --function-version "$VERSION" \
    --region "$REGION" \
    --output text --query 'AliasArn' \
    | xargs -I{} echo "  Alias: {}"
fi

# ── Smoke test ────────────────────────────────────────────────────────────────
echo ""
echo "Running smoke test..."
RESPONSE_FILE=$(mktemp)
aws lambda invoke \
  --function-name "$FUNCTION_NAME" \
  --payload '{"incident_description":"smoke-test from deploy.sh","resource_hints":{}}' \
  --region "$REGION" \
  "$RESPONSE_FILE" \
  --output json \
  | python3 -c "import json,sys; r=json.load(sys.stdin); print(f'  HTTP: {r.get(\"StatusCode\",\"?\")}')"

STATUS=$(python3 -c "
import json, sys
with open('${RESPONSE_FILE}') as f:
    body = json.loads(f.read())
# Handle wrapped or unwrapped response
if 'body' in body:
    inner = json.loads(body['body'])
    print(inner.get('status','UNKNOWN'))
else:
    print(body.get('status','UNKNOWN'))
")
rm -f "$RESPONSE_FILE"

echo "  Pipeline status: $STATUS"

if [[ "$STATUS" == "SUCCESS" || "$STATUS" == "ERROR" ]]; then
  echo ""
  echo "========================================================"
  echo "  ✓ Deploy complete [${ENV}]"
  echo "========================================================"
else
  echo ""
  echo "ERROR: Smoke test returned unexpected status: $STATUS"
  exit 1
fi
