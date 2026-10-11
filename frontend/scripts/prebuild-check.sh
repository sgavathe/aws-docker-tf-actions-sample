#!/usr/bin/env bash
# prebuild-check.sh - run locally before you push. Catches what CI would catch.
#
# Usage:
#   scripts/prebuild-check.sh [--aws] [--cost] [--no-prowler] [--warn-only]
#
# Always runs (skipped with a hint when the tool is not installed):
#   1. terraform fmt -check        on every Terraform folder
#   2. terraform validate          on every Terraform folder (no backend, no AWS calls)
#   3. Prowler IaC scan            Terraform/Dockerfile/workflow misconfigurations and
#                                  hard-coded secrets (uses the Prowler Docker image,
#                                  or the prowler CLI + trivy)
#   4. actionlint                  GitHub Actions workflow lint (optional tool)
#   5. shellcheck                  shell script lint (optional tool)
#
# Options:
#   --aws         also scan the live AWS account with Prowler (critical/high only)
#                 using your local credentials (AWS_PROFILE etc.)
#   --cost        also run scripts/cost-check.sh quick
#   --no-prowler  skip Prowler scans
#   --warn-only   report Prowler findings but don't fail on them
#
# Environment:
#   PROWLER_VERSION   default 5.44.0 (match the workflow)
#   PROWLER_SEVERITY  default "critical high"
#   AWS_REGION        default us-east-1
#   REPORT_DIR        default ./reports
#
# Exit code is non-zero if any required check fails.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PROWLER_VERSION="${PROWLER_VERSION:-5.44.0}"
PROWLER_SEVERITY="${PROWLER_SEVERITY:-critical high}"
REGION="${AWS_REGION:-${AWS_DEFAULT_REGION:-us-east-1}}"
REPORT_DIR="${REPORT_DIR:-$PWD/reports}"
STAMP="$(date -u +%Y%m%d-%H%M%S)"

RUN_AWS=0; RUN_COST=0; RUN_PROWLER=1; WARN_ONLY=0
for arg in "$@"; do
  case "$arg" in
    --aws) RUN_AWS=1 ;;
    --cost) RUN_COST=1 ;;
    --no-prowler) RUN_PROWLER=0 ;;
    --warn-only) WARN_ONLY=1 ;;
    -h|--help) sed -n '2,29p' "$0"; exit 0 ;;
    *) echo "Unknown option: $arg (see --help)" >&2; exit 2 ;;
  esac
done

if [ -t 1 ]; then
  BOLD=$'\033[1m'; RED=$'\033[31m'; GRN=$'\033[32m'; YEL=$'\033[33m'; DIM=$'\033[2m'; RST=$'\033[0m'
else
  BOLD=""; RED=""; GRN=""; YEL=""; DIM=""; RST=""
fi

SUMMARY=""
FAILED=0
record() { # record PASS|FAIL|WARN|SKIP "name" "detail"
  local color
  case "$1" in PASS) color="$GRN" ;; FAIL) color="$RED"; FAILED=$((FAILED + 1)) ;; WARN) color="$YEL" ;; *) color="$DIM" ;; esac
  SUMMARY="$SUMMARY$(printf '  %s%-4s%s %-28s %s' "$color" "$1" "$RST" "$2" "${3:-}")"$'\n'
}
step() { printf '\n%s== %s ==%s\n' "$BOLD" "$1" "$RST"; }
have() { command -v "$1" >/dev/null 2>&1; }

terraform_dirs() {
  find "$REPO_ROOT" -type f -name '*.tf' \
    -not -path '*/.terraform/*' -not -path '*/node_modules/*' -not -path '*/.git/*' \
    -exec dirname {} \; | sort -u
}

# ---------------------------------------------------------------- terraform

check_terraform() {
  local dirs
  dirs="$(terraform_dirs)"
  if [ -z "$dirs" ]; then
    record SKIP "terraform" "no .tf files found"
    return 0
  fi
  if ! have terraform; then
    record FAIL "terraform" "terraform not installed (brew install hashicorp/tap/terraform)"
    return 0
  fi

  step "terraform fmt -check"
  if terraform fmt -check -recursive -diff "$REPO_ROOT"; then
    record PASS "terraform fmt"
  else
    record FAIL "terraform fmt" "run: terraform fmt -recursive"
  fi

  step "terraform validate"
  export TF_PLUGIN_CACHE_DIR="${TF_PLUGIN_CACHE_DIR:-$HOME/.terraform.d/plugin-cache}"
  mkdir -p "$TF_PLUGIN_CACHE_DIR"
  local d rel tmp bad=""
  tmp="$(mktemp -d)"
  while IFS= read -r d; do
    rel="${d#"$REPO_ROOT"/}"
    [ "$d" = "$REPO_ROOT" ] && rel="."
    printf '  %s ... ' "$rel"
    # Separate data dir so your real .terraform folders and state are untouched.
    if TF_DATA_DIR="$tmp/$(printf '%s' "$rel" | tr '/' '_')" \
         terraform -chdir="$d" init -backend=false -input=false -no-color >"$tmp/init.log" 2>&1 &&
       TF_DATA_DIR="$tmp/$(printf '%s' "$rel" | tr '/' '_')" \
         terraform -chdir="$d" validate -no-color >"$tmp/validate.log" 2>&1; then
      printf '%sok%s\n' "$GRN" "$RST"
    else
      printf '%sfailed%s\n' "$RED" "$RST"
      sed 's/^/      /' "$tmp/init.log" "$tmp/validate.log" 2>/dev/null | tail -20
      bad="$bad $rel"
    fi
  done <<EOF
$dirs
EOF
  rm -rf "$tmp"
  if [ -z "$bad" ]; then
    record PASS "terraform validate"
  else
    record FAIL "terraform validate" "failed in:$bad"
  fi
}

# ---------------------------------------------------------------- prowler

docker_ok() { have docker && docker info >/dev/null 2>&1; }

# Runs prowler with the given args; prints the exit code on the last line.
run_prowler() {
  local out="$1"; shift
  mkdir -p "$out"
  if docker_ok; then
    docker run --rm \
      -v "$REPO_ROOT":/repo:ro -v "$out":/out \
      -e AWS_ACCESS_KEY_ID -e AWS_SECRET_ACCESS_KEY -e AWS_SESSION_TOKEN \
      -e AWS_DEFAULT_REGION="$REGION" -e AWS_REGION="$REGION" \
      "prowlercloud/prowler:$PROWLER_VERSION" "$@" -o /out --no-banner
  else
    prowler "$@" -o "$out" --no-banner
  fi
}

prowler_result() { # prowler_result <exit> <name> <out>
  case "$1" in
    0) record PASS "$2" "no $PROWLER_SEVERITY findings" ;;
    3) if [ "$WARN_ONLY" = "1" ]; then
         record WARN "$2" "findings - see $3"
       else
         record FAIL "$2" "findings - see $3"
       fi ;;
    *) record FAIL "$2" "prowler exited $1 (error, not findings)" ;;
  esac
}

check_prowler_iac() {
  step "Prowler IaC scan ($PROWLER_SEVERITY)"
  local out="$REPORT_DIR/prowler-iac-$STAMP" scan_path rc=0
  if docker_ok; then
    scan_path=/repo
    printf '  Using Docker image prowlercloud/prowler:%s (includes Trivy)\n' "$PROWLER_VERSION"
  elif have prowler && have trivy; then
    scan_path="$REPO_ROOT"
    printf '  Using local prowler CLI + trivy\n'
  else
    record SKIP "prowler iac" "start Docker, or: pipx install prowler==$PROWLER_VERSION && brew install trivy"
    return 0
  fi
  # shellcheck disable=SC2086  # severities are intentionally word-split
  run_prowler "$out" iac --scan-path "$scan_path" --severity $PROWLER_SEVERITY \
    -M csv html || rc=$?
  prowler_result "$rc" "prowler iac" "$out"
}

check_prowler_aws() {
  step "Prowler AWS live scan ($REGION, $PROWLER_SEVERITY)"
  if ! have aws; then
    record FAIL "prowler aws" "aws CLI not installed"
    return 0
  fi
  # Turn whatever profile/SSO session you use into plain env credentials,
  # so the same command works with the CLI or the Docker image.
  local creds
  if ! creds="$(aws configure export-credentials --format env 2>&1)"; then
    record FAIL "prowler aws" "no working AWS credentials ($creds)"
    return 0
  fi
  eval "$creds"
  if ! docker_ok && ! have prowler; then
    record SKIP "prowler aws" "start Docker, or: pipx install prowler==$PROWLER_VERSION"
    return 0
  fi
  local out="$REPORT_DIR/prowler-aws-$STAMP" rc=0
  # shellcheck disable=SC2086
  run_prowler "$out" aws --region "$REGION" --severity $PROWLER_SEVERITY -M csv html || rc=$?
  prowler_result "$rc" "prowler aws" "$out"
}

# ---------------------------------------------------------------- linters

check_actionlint() {
  local wf="$REPO_ROOT/.github/workflows"
  [ -d "$wf" ] || { record SKIP "actionlint" "no .github/workflows"; return 0; }
  if ! have actionlint; then
    record SKIP "actionlint" "brew install actionlint"
    return 0
  fi
  step "actionlint"
  if (cd "$REPO_ROOT" && actionlint); then
    record PASS "actionlint"
  else
    record FAIL "actionlint"
  fi
}

check_shellcheck() {
  local files
  files="$(find "$REPO_ROOT" -type f -name '*.sh' -not -path '*/node_modules/*' -not -path '*/.git/*')"
  [ -n "$files" ] || { record SKIP "shellcheck" "no .sh files"; return 0; }
  if ! have shellcheck; then
    record SKIP "shellcheck" "brew install shellcheck"
    return 0
  fi
  step "shellcheck"
  local f bad=""
  while IFS= read -r f; do
    shellcheck -S warning "$f" || bad="$bad ${f#"$REPO_ROOT"/}"
  done <<EOF
$files
EOF
  if [ -z "$bad" ]; then record PASS "shellcheck"; else record FAIL "shellcheck" "issues in:$bad"; fi
}

# ---------------------------------------------------------------- main

printf '%sPre-build checks for %s%s\n' "$BOLD" "$REPO_ROOT" "$RST"

check_terraform
if [ "$RUN_PROWLER" = "1" ]; then
  check_prowler_iac
  if [ "$RUN_AWS" = "1" ]; then check_prowler_aws; fi
fi
check_actionlint
check_shellcheck

if [ "$RUN_COST" = "1" ]; then
  if "$REPO_ROOT/scripts/cost-check.sh" quick; then
    record PASS "cost check" "see output above"
  else
    record WARN "cost check" "could not complete"
  fi
fi

printf '\n%s== Summary ==%s\n%s' "$BOLD" "$RST" "$SUMMARY"
if [ "$FAILED" -gt 0 ]; then
  printf '\n%s%d check(s) failed. Fix before pushing.%s\n' "$RED" "$FAILED" "$RST"
  exit 1
fi
printf '\n%sAll required checks passed.%s\n' "$GRN" "$RST"
