#!/usr/bin/env bash
# =============================================================================
# github_setup.sh — One-shot GitHub repo creation + code push
#
# What this script does (fully automated):
#   1.  Validates prerequisites (git, curl, jq, GitHub token)
#   2.  Creates the GitHub repository via GitHub REST API
#   3.  Configures branch protection rules on main
#   4.  Sets all required GitHub Actions secrets
#   5.  Adds the remote origin to your local git repo
#   6.  Pushes all code + sets upstream tracking
#   7.  Creates the develop branch and pushes it
#   8.  Prints the repo URL and next steps
#
# Usage:
#   chmod +x scripts/github_setup.sh
#   ./scripts/github_setup.sh
#
# Or pass everything as arguments:
#   GITHUB_TOKEN=ghp_xxx GITHUB_USERNAME=your-handle ./scripts/github_setup.sh
#
# Requirements:
#   - git, curl, jq  (see Prerequisites section below)
#   - A GitHub Personal Access Token with scopes:
#       repo (full), workflow, admin:repo_hook, delete_repo (optional)
#     → https://github.com/settings/tokens/new
# =============================================================================

set -euo pipefail
IFS=$'\n\t'

# ── Colours ───────────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; BOLD='\033[1m'; RESET='\033[0m'

info()    { echo -e "${CYAN}[INFO]${RESET}  $*"; }
success() { echo -e "${GREEN}[OK]${RESET}    $*"; }
warn()    { echo -e "${YELLOW}[WARN]${RESET}  $*"; }
error()   { echo -e "${RED}[ERROR]${RESET} $*" >&2; }
step()    { echo -e "\n${BOLD}━━━ $* ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${RESET}"; }

# ── Load .env if present ─────────────────────────────────────────────────────
if [ -f .env ]; then
  set -a; source .env; set +a
fi

# =============================================================================
# CONFIGURATION — edit these or set as environment variables
# =============================================================================

GITHUB_TOKEN="${GITHUB_TOKEN:-}"
GITHUB_USERNAME="${GITHUB_USERNAME:-}"
REPO_NAME="${REPO_NAME:-cloudops-ai-agent}"
REPO_DESCRIPTION="${REPO_DESCRIPTION:-AI-powered production incident investigation agent built on Amazon Bedrock}"
REPO_PRIVATE="${REPO_PRIVATE:-false}"          # "true" for private repo
DEFAULT_BRANCH="${DEFAULT_BRANCH:-main}"

# AWS secrets to register in GitHub Actions
# (can be empty — you can add them later in GitHub Settings → Secrets)
AWS_ROLE_ARN="${AWS_ROLE_ARN:-}"
AWS_REGION="${AWS_REGION:-us-east-1}"
LAMBDA_FUNCTION_NAME="${LAMBDA_FUNCTION_NAME:-cloudops-ai-agent}"
BEDROCK_MODEL_ID="${BEDROCK_MODEL_ID:-anthropic.claude-3-sonnet-20240229-v1:0}"
SLACK_WEBHOOK_URL="${SLACK_WEBHOOK_URL:-}"

GITHUB_API="https://api.github.com"

# =============================================================================
# STEP 0 — Banner
# =============================================================================
echo ""
echo -e "${BOLD}╔══════════════════════════════════════════════════════╗${RESET}"
echo -e "${BOLD}║   CloudOps AI Agent — GitHub Setup Automation       ║${RESET}"
echo -e "${BOLD}╚══════════════════════════════════════════════════════╝${RESET}"
echo ""

# =============================================================================
# STEP 1 — Prerequisites check
# =============================================================================
step "Step 1: Checking prerequisites"

MISSING=()
for cmd in git curl jq; do
  if ! command -v "$cmd" &>/dev/null; then
    MISSING+=("$cmd")
  fi
done

if [ ${#MISSING[@]} -gt 0 ]; then
  error "Missing required tools: ${MISSING[*]}"
  echo ""
  echo "  Install them with:"
  echo "    macOS:   brew install ${MISSING[*]}"
  echo "    Ubuntu:  sudo apt-get install -y ${MISSING[*]}"
  exit 1
fi

success "git, curl, jq found"

# Confirm we're in the project root
if [ ! -f "app.py" ] || [ ! -d "agents" ]; then
  error "Run this script from the project root (where app.py lives)."
  exit 1
fi
success "Running from project root"

# =============================================================================
# STEP 2 — Collect credentials interactively if not set
# =============================================================================
step "Step 2: Collecting GitHub credentials"

if [ -z "$GITHUB_TOKEN" ]; then
  echo ""
  echo -e "  ${YELLOW}You need a GitHub Personal Access Token (PAT) with these scopes:${RESET}"
  echo "    ✓ repo (full control)"
  echo "    ✓ workflow (to create GitHub Actions secrets)"
  echo "    ✓ admin:repo_hook"
  echo ""
  echo -e "  Create one at: ${CYAN}https://github.com/settings/tokens/new${RESET}"
  echo ""
  read -rsp "  Paste your token (input hidden): " GITHUB_TOKEN
  echo ""
fi

if [ -z "$GITHUB_USERNAME" ]; then
  echo ""
  # Try to auto-detect username from the token
  AUTO_USER=$(curl -sf -H "Authorization: token ${GITHUB_TOKEN}" \
    "${GITHUB_API}/user" | jq -r '.login' 2>/dev/null || true)
  if [ -n "$AUTO_USER" ] && [ "$AUTO_USER" != "null" ]; then
    GITHUB_USERNAME="$AUTO_USER"
    info "Auto-detected GitHub username: ${BOLD}${GITHUB_USERNAME}${RESET}"
  else
    read -rp "  Your GitHub username: " GITHUB_USERNAME
  fi
fi

# Validate token
info "Validating token..."
HTTP_CODE=$(curl -so /dev/null -w "%{http_code}" \
  -H "Authorization: token ${GITHUB_TOKEN}" "${GITHUB_API}/user")

if [ "$HTTP_CODE" != "200" ]; then
  error "Token validation failed (HTTP $HTTP_CODE). Check your token and try again."
  exit 1
fi
success "Token valid for user: ${BOLD}${GITHUB_USERNAME}${RESET}"

REPO_FULL="${GITHUB_USERNAME}/${REPO_NAME}"
REPO_URL="https://github.com/${REPO_FULL}"
REMOTE_URL="https://github.com/${REPO_FULL}.git"

echo ""
info "Repository will be created at: ${CYAN}${REPO_URL}${RESET}"
echo -e "  Private: ${REPO_PRIVATE}"
echo ""
read -rp "  Continue? [y/N] " CONFIRM
if [[ ! "$CONFIRM" =~ ^[Yy]$ ]]; then
  echo "Aborted."
  exit 0
fi

# =============================================================================
# STEP 3 — Create GitHub repository
# =============================================================================
step "Step 3: Creating GitHub repository"

# Check if repo already exists
EXISTING=$(curl -so /dev/null -w "%{http_code}" \
  -H "Authorization: token ${GITHUB_TOKEN}" \
  "${GITHUB_API}/repos/${REPO_FULL}")

if [ "$EXISTING" == "200" ]; then
  warn "Repository ${REPO_FULL} already exists — skipping creation."
else
  CREATE_RESPONSE=$(curl -sf \
    -H "Authorization: token ${GITHUB_TOKEN}" \
    -H "Content-Type: application/json" \
    -X POST "${GITHUB_API}/user/repos" \
    -d "{
      \"name\":        \"${REPO_NAME}\",
      \"description\": \"${REPO_DESCRIPTION}\",
      \"private\":     ${REPO_PRIVATE},
      \"auto_init\":   false,
      \"has_issues\":  true,
      \"has_wiki\":    false
    }")

  CREATED_URL=$(echo "$CREATE_RESPONSE" | jq -r '.html_url' 2>/dev/null || echo "")
  if [ -z "$CREATED_URL" ] || [ "$CREATED_URL" == "null" ]; then
    error "Failed to create repository. Response:"
    echo "$CREATE_RESPONSE" | jq . 2>/dev/null || echo "$CREATE_RESPONSE"
    exit 1
  fi
  success "Repository created: ${CYAN}${CREATED_URL}${RESET}"
fi

# =============================================================================
# STEP 4 — Configure git remote
# =============================================================================
step "Step 4: Configuring git remote"

# Remove existing origin if present
if git remote get-url origin &>/dev/null 2>&1; then
  warn "Existing remote 'origin' found — updating to new repo URL."
  git remote set-url origin "$REMOTE_URL"
else
  git remote add origin "$REMOTE_URL"
fi
success "Remote origin → ${REMOTE_URL}"

# =============================================================================
# STEP 5 — Push all code
# =============================================================================
step "Step 5: Pushing code to GitHub"

# Ensure we're on main
CURRENT_BRANCH=$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo "")
if [ "$CURRENT_BRANCH" != "$DEFAULT_BRANCH" ]; then
  warn "Current branch is '${CURRENT_BRANCH}', switching to '${DEFAULT_BRANCH}'..."
  git checkout -b "$DEFAULT_BRANCH" 2>/dev/null || git checkout "$DEFAULT_BRANCH"
fi

# Push main
info "Pushing ${DEFAULT_BRANCH} branch..."
git push -u origin "$DEFAULT_BRANCH" \
  -c "http.extraheader=Authorization: token ${GITHUB_TOKEN}" \
  --force-with-lease 2>&1 | sed 's/^/  /'
success "Branch '${DEFAULT_BRANCH}' pushed."

# Create and push develop branch
info "Creating develop branch..."
git checkout -b develop 2>/dev/null || git checkout develop
git push -u origin develop \
  -c "http.extraheader=Authorization: token ${GITHUB_TOKEN}" \
  --force-with-lease 2>&1 | sed 's/^/  /'
git checkout "$DEFAULT_BRANCH"
success "Branch 'develop' pushed."

# =============================================================================
# STEP 6 — Branch protection rules
# =============================================================================
step "Step 6: Setting branch protection rules (main)"

PROTECTION_RESPONSE=$(curl -so /dev/null -w "%{http_code}" \
  -H "Authorization: token ${GITHUB_TOKEN}" \
  -H "Accept: application/vnd.github+json" \
  -X PUT "${GITHUB_API}/repos/${REPO_FULL}/branches/${DEFAULT_BRANCH}/protection" \
  -d '{
    "required_status_checks": {
      "strict": true,
      "contexts": ["Lint & format check", "Type check (mypy)", "Unit tests (Python 3.11)"]
    },
    "enforce_admins": false,
    "required_pull_request_reviews": {
      "required_approving_review_count": 1,
      "dismiss_stale_reviews": true
    },
    "restrictions": null
  }')

if [ "$PROTECTION_RESPONSE" == "200" ]; then
  success "Branch protection rules applied to '${DEFAULT_BRANCH}'."
else
  warn "Branch protection returned HTTP ${PROTECTION_RESPONSE} — may need Pro/Team plan. Skipping."
fi

# =============================================================================
# STEP 7 — GitHub Actions secrets
# =============================================================================
step "Step 7: Setting GitHub Actions secrets"

# Helper to get the repo's public key (needed to encrypt secrets)
get_public_key() {
  curl -sf \
    -H "Authorization: token ${GITHUB_TOKEN}" \
    "${GITHUB_API}/repos/${REPO_FULL}/actions/secrets/public-key"
}

# Helper to encrypt and set a secret using Python (no sodium dep needed)
set_secret() {
  local SECRET_NAME="$1"
  local SECRET_VALUE="$2"

  if [ -z "$SECRET_VALUE" ]; then
    warn "Skipping empty secret: ${SECRET_NAME}"
    return
  fi

  # Get repo public key
  PK_RESPONSE=$(get_public_key)
  KEY_ID=$(echo "$PK_RESPONSE" | jq -r '.key_id')
  PUBLIC_KEY=$(echo "$PK_RESPONSE" | jq -r '.key')

  # Encrypt using Python (libsodium sealed box via PyNaCl if available, else base64 fallback)
  ENCRYPTED=$(python3 - <<PYEOF 2>/dev/null || echo "__FALLBACK__"
import base64, sys
try:
    from nacl import encoding, public
    public_key = public.PublicKey(
        base64.b64decode("${PUBLIC_KEY}"),
        encoding.RawEncoder,
    )
    sealed_box = public.SealedBox(public_key)
    encrypted  = sealed_box.encrypt("${SECRET_VALUE}".encode())
    print(base64.b64encode(encrypted).decode())
except ImportError:
    # PyNaCl not installed — print sentinel
    print("__NEED_PYNACL__")
PYEOF
  )

  if [ "$ENCRYPTED" == "__NEED_PYNACL__" ] || [ "$ENCRYPTED" == "__FALLBACK__" ]; then
    warn "PyNaCl not installed — cannot encrypt secret ${SECRET_NAME} automatically."
    warn "Set it manually: GitHub repo → Settings → Secrets → Actions → New secret"
    warn "  Name : ${SECRET_NAME}"
    warn "  Value: (your value)"
    return
  fi

  HTTP=$(curl -so /dev/null -w "%{http_code}" \
    -H "Authorization: token ${GITHUB_TOKEN}" \
    -H "Accept: application/vnd.github+json" \
    -X PUT "${GITHUB_API}/repos/${REPO_FULL}/actions/secrets/${SECRET_NAME}" \
    -d "{\"encrypted_value\":\"${ENCRYPTED}\",\"key_id\":\"${KEY_ID}\"}")

  if [ "$HTTP" == "201" ] || [ "$HTTP" == "204" ]; then
    success "Secret set: ${SECRET_NAME}"
  else
    warn "Failed to set secret ${SECRET_NAME} (HTTP $HTTP) — set it manually."
  fi
}

set_secret "AWS_REGION"            "$AWS_REGION"
set_secret "LAMBDA_FUNCTION_NAME"  "$LAMBDA_FUNCTION_NAME"
set_secret "BEDROCK_MODEL_ID"      "$BEDROCK_MODEL_ID"
[ -n "$AWS_ROLE_ARN"       ] && set_secret "AWS_ROLE_ARN"        "$AWS_ROLE_ARN"
[ -n "$SLACK_WEBHOOK_URL"  ] && set_secret "SLACK_WEBHOOK_URL"   "$SLACK_WEBHOOK_URL"

# =============================================================================
# STEP 8 — Repository topics / labels
# =============================================================================
step "Step 8: Adding repository topics"

curl -so /dev/null \
  -H "Authorization: token ${GITHUB_TOKEN}" \
  -H "Accept: application/vnd.github+json" \
  -X PUT "${GITHUB_API}/repos/${REPO_FULL}/topics" \
  -d '{"names":["aws","bedrock","ai-agent","cloudops","sre","devops","python","lambda","cloudwatch"]}' \
  2>/dev/null && success "Topics added." || warn "Could not add topics."

# =============================================================================
# DONE — Summary
# =============================================================================
echo ""
echo -e "${BOLD}╔══════════════════════════════════════════════════════╗${RESET}"
echo -e "${BOLD}║   ✅  Setup Complete!                                ║${RESET}"
echo -e "${BOLD}╚══════════════════════════════════════════════════════╝${RESET}"
echo ""
echo -e "  ${GREEN}Repository URL :${RESET} ${CYAN}${REPO_URL}${RESET}"
echo -e "  ${GREEN}Clone command  :${RESET}"
echo ""
echo -e "    ${BOLD}git clone ${REMOTE_URL}${RESET}"
echo ""
echo -e "  ${YELLOW}Next steps:${RESET}"
echo "  1. Clone the repo on your local machine (command above)"
echo "  2. cd cloudops-ai-agent && make install-dev"
echo "  3. cp .env.example .env  →  fill in your AWS credentials"
echo "  4. make test              →  run the test suite"
echo "  5. make run               →  local smoke-test"
echo ""
echo -e "  ${YELLOW}GitHub Actions secrets still needed:${RESET}"
echo "  • AWS_ROLE_ARN      → IAM role ARN for OIDC deployment"
echo "    (Repo → Settings → Secrets and variables → Actions → New secret)"
echo ""
echo -e "  ${YELLOW}To deploy to Lambda:${RESET}"
echo "  • make deploy ENV=staging"
echo "  • make deploy ENV=prod"
echo ""
