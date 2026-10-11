#!/usr/bin/env bash
# cost-check.sh - open-source, Trusted-Advisor-style cost checks for one AWS account.
#
# Usage:
#   scripts/cost-check.sh [quick|thrifty|thrifty-ui|infracost|all]
#
#   quick       (default) AWS CLI only, nothing to install. Spend by service
#               plus common leftovers: idle ALBs/NAT, unattached EBS, unused
#               Elastic IPs, old snapshots, log groups that never expire,
#               ECR repos without lifecycle rules, running ECS tasks, etc.
#   thrifty     Steampipe + Powerpipe "AWS Thrifty" benchmarks -> HTML report.
#   thrifty-ui  Same checks as a live dashboard at http://localhost:9033.
#   infracost   Monthly cost estimate of every Terraform folder in the repo.
#   all         quick + thrifty + infracost.
#
# Environment:
#   AWS_PROFILE      profile to use (default: your default credentials)
#   AWS_REGION       region to check (default: us-east-1)
#   SNAPSHOT_DAYS    snapshot age to flag (default: 90)
#   SKIP_CE=1        skip the Cost Explorer spend query (it costs $0.01 per call)
#   REPORT_DIR       where reports go (default: ./reports)
#   THRIFTY_DIR      where the Thrifty mod is cloned (default: ~/.cache/aws-thrifty)
#
# Everything here is read-only. Works with macOS bash 3.2 and Linux.

# JMESPath literals use backticks inside single quotes on purpose.
# shellcheck disable=SC2016

set -euo pipefail

REGION="${AWS_REGION:-${AWS_DEFAULT_REGION:-us-east-1}}"
SNAPSHOT_DAYS="${SNAPSHOT_DAYS:-90}"
REPORT_DIR="${REPORT_DIR:-$PWD/reports}"
THRIFTY_DIR="${THRIFTY_DIR:-$HOME/.cache/aws-thrifty}"
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
STAMP="$(date -u +%Y%m%d-%H%M%S)"

if [ -t 1 ]; then
  BOLD=$'\033[1m'; DIM=$'\033[2m'; RED=$'\033[31m'; GRN=$'\033[32m'; YEL=$'\033[33m'; RST=$'\033[0m'
else
  BOLD=""; DIM=""; RED=""; GRN=""; YEL=""; RST=""
fi

FLAGGED=0

section() { printf '\n%s== %s ==%s\n' "$BOLD" "$1" "$RST"; }
ok()      { printf '  %sOK%s   %s\n' "$GRN" "$RST" "$1"; }
flag()    { printf '  %sCHECK%s %s\n' "$YEL" "$RST" "$1"; FLAGGED=$((FLAGGED + 1)); }
die()     { printf '%sERROR:%s %s\n' "$RED" "$RST" "$1" >&2; exit 1; }
need()    { command -v "$1" >/dev/null 2>&1 || die "$1 not found. $2"; }

# Indent multi-line output under a finding.
show() { sed 's/^/          /'; }

# Portable "N days ago" in YYYY-MM-DD (BSD date on macOS, GNU date on Linux).
days_ago() {
  if date -u -v-1d +%Y-%m-%d >/dev/null 2>&1; then
    date -u -v-"$1"d +%Y-%m-%d
  else
    date -u -d "$1 days ago" +%Y-%m-%d
  fi
}

month_start()      { date -u +%Y-%m-01; }
prev_month_start() {
  if date -u -v1d -v-1m +%Y-%m-01 >/dev/null 2>&1; then
    date -u -v1d -v-1m +%Y-%m-01
  else
    date -u -d "$(date -u +%Y-%m-01) -1 month" +%Y-%m-01
  fi
}
tomorrow() {
  if date -u -v+1d +%Y-%m-%d >/dev/null 2>&1; then
    date -u -v+1d +%Y-%m-%d
  else
    date -u -d tomorrow +%Y-%m-%d
  fi
}

# Run an AWS CLI query that prints text; flag it if there is any output.
#   check "<label when found>" "<label when clean>" aws ... --output text
check() {
  local found="$1" clean="$2"; shift 2
  local out
  if ! out="$("$@" 2>&1)"; then
    printf '  %sSKIP%s  %s %s(%s)%s\n' "$DIM" "$RST" "$clean" "$DIM" "$(printf '%s' "$out" | head -1)" "$RST"
    return 0
  fi
  out="$(printf '%s\n' "$out" | grep -v '^None$' | sed '/^[[:space:]]*$/d' || true)"
  if [ -n "$out" ]; then
    flag "$found"
    printf '%s\n' "$out" | show
  else
    ok "$clean"
  fi
}

aws_() { aws --region "$REGION" "$@"; }

# ---------------------------------------------------------------- quick

spend_by_service() {
  local label="$1" start="$2" end="$3"
  printf '  %s%s (%s to %s, exclusive end)%s\n' "$BOLD" "$label" "$start" "$end" "$RST"
  local rows
  if ! rows="$(aws ce get-cost-and-usage --region us-east-1 \
      --time-period "Start=$start,End=$end" --granularity MONTHLY \
      --metrics UnblendedCost --group-by Type=DIMENSION,Key=SERVICE \
      --query 'ResultsByTime[].Groups[].[Keys[0],Metrics.UnblendedCost.Amount]' \
      --output text 2>&1)"; then
    printf '    %sCost Explorer not available: %s%s\n' "$DIM" "$(printf '%s' "$rows" | head -1)" "$RST"
    return 0
  fi
  printf '%s\n' "$rows" | awk -F'\t' '
    $2 + 0 >= 0.005 { printf "    %-50s $%9.2f\n", $1, $2; total += $2 }
    END { printf "    %-50s $%9.2f\n", "TOTAL", total }' | sort -t'$' -k2 -gr
}

run_quick() {
  need aws "Install the AWS CLI v2: https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html"

  section "Account"
  local who
  who="$(aws sts get-caller-identity --query '[Account,Arn]' --output text 2>&1)" \
    || die "AWS credentials not working: $who"
  printf '  Account/identity: %s\n  Region checked:   %s\n' "$who" "$REGION"

  if [ "${SKIP_CE:-0}" != "1" ]; then
    section "Spend by service (Cost Explorer, \$0.01 per query)"
    spend_by_service "Last month" "$(prev_month_start)" "$(month_start)"
    spend_by_service "Month to date" "$(month_start)" "$(tomorrow)"
  fi

  section "Always-on network costs"
  check "Load balancers still running (~\$16+/month each, even with no traffic)" \
        "No application/network load balancers" \
    aws_ elbv2 describe-load-balancers \
      --query 'LoadBalancers[].[LoadBalancerName,Type,State.Code,CreatedTime]' --output text
  check "Classic load balancers still running" "No classic load balancers" \
    aws_ elb describe-load-balancers \
      --query 'LoadBalancerDescriptions[].[LoadBalancerName,CreatedTime]' --output text
  check "NAT gateways running (~\$33/month each plus data)" "No NAT gateways" \
    aws_ ec2 describe-nat-gateways --filter Name=state,Values=available \
      --query 'NatGateways[].[NatGatewayId,VpcId,CreateTime]' --output text
  check "Elastic IPs not attached to anything (billed hourly)" "No unattached Elastic IPs" \
    aws_ ec2 describe-addresses \
      --query 'Addresses[?AssociationId==`null`].[PublicIp,AllocationId]' --output text
  check "VPC interface endpoints (~\$7/month per AZ each)" "No interface VPC endpoints" \
    aws_ ec2 describe-vpc-endpoints --filters Name=vpc-endpoint-type,Values=Interface \
      --query 'VpcEndpoints[].[VpcEndpointId,ServiceName,State]' --output text

  section "Compute"
  check "Stopped EC2 instances (their EBS volumes are still billed)" "No stopped EC2 instances" \
    aws_ ec2 describe-instances --filters Name=instance-state-name,Values=stopped \
      --query 'Reservations[].Instances[].[InstanceId,InstanceType,LaunchTime]' --output text
  check "Running EC2 instances (confirm each is still needed)" "No running EC2 instances" \
    aws_ ec2 describe-instances --filters Name=instance-state-name,Values=running \
      --query 'Reservations[].Instances[].[InstanceId,InstanceType,LaunchTime]' --output text

  local clusters c running services insights
  clusters="$(aws_ ecs list-clusters --query 'clusterArns[]' --output text 2>/dev/null || true)"
  if [ -z "$clusters" ] || [ "$clusters" = "None" ]; then
    ok "No ECS clusters"
  else
    for c in $clusters; do
      running="$(aws_ ecs describe-clusters --clusters "$c" --query 'clusters[0].runningTasksCount' --output text)"
      services="$(aws_ ecs describe-clusters --clusters "$c" --query 'clusters[0].activeServicesCount' --output text)"
      insights="$(aws_ ecs describe-clusters --clusters "$c" --include SETTINGS \
        --query 'clusters[0].settings[?name==`containerInsights`].value | [0]' --output text)"
      if [ "$running" != "0" ] || [ "$services" != "0" ]; then
        flag "ECS cluster ${c##*/}: $running running task(s), $services active service(s)"
      else
        ok "ECS cluster ${c##*/}: empty (costs nothing)"
      fi
      case "$insights" in
        enabled|enhanced) flag "ECS cluster ${c##*/}: Container Insights is '$insights' (custom metrics are billed)" ;;
      esac
    done
  fi

  local fns f pc
  fns="$(aws_ lambda list-functions --query 'Functions[].FunctionName' --output text 2>/dev/null || true)"
  pc=""
  for f in $fns; do
    [ "$f" = "None" ] && continue
    pc="$pc$(aws_ lambda list-provisioned-concurrency-configs --function-name "$f" \
      --query 'ProvisionedConcurrencyConfigs[].[`'"$f"'`,FunctionArn,AllocatedProvisionedConcurrentExecutions]' \
      --output text 2>/dev/null || true)"
  done
  if [ -n "$pc" ]; then
    flag "Lambda provisioned concurrency (billed even when idle)"
    printf '%s\n' "$pc" | show
  else
    ok "No Lambda provisioned concurrency"
  fi

  check "RDS instances (confirm each is still needed)" "No RDS instances" \
    aws_ rds describe-db-instances \
      --query 'DBInstances[].[DBInstanceIdentifier,DBInstanceClass,DBInstanceStatus]' --output text

  section "Storage"
  check "Unattached EBS volumes" "No unattached EBS volumes" \
    aws_ ec2 describe-volumes --filters Name=status,Values=available \
      --query 'Volumes[].[VolumeId,Size,VolumeType,CreateTime]' --output text
  check "gp2 volumes (gp3 is ~20% cheaper)" "No gp2 volumes" \
    aws_ ec2 describe-volumes --filters Name=volume-type,Values=gp2 \
      --query 'Volumes[].[VolumeId,Size,State]' --output text
  local cutoff
  cutoff="$(days_ago "$SNAPSHOT_DAYS")"
  check "EBS snapshots older than $SNAPSHOT_DAYS days" "No EBS snapshots older than $SNAPSHOT_DAYS days" \
    aws_ ec2 describe-snapshots --owner-ids self \
      --query "Snapshots[?StartTime<='$cutoff'].[SnapshotId,VolumeSize,StartTime]" --output text

  check "CloudWatch log groups that never expire (storage grows forever)" \
        "Every log group has a retention period" \
    aws_ logs describe-log-groups \
      --query 'logGroups[?retentionInDays==`null`].[logGroupName,storedBytes]' --output text

  local repos r nolc="" count
  repos="$(aws_ ecr describe-repositories --query 'repositories[].repositoryName' --output text 2>/dev/null || true)"
  for r in $repos; do
    [ "$r" = "None" ] && continue
    count="$(aws_ ecr describe-images --repository-name "$r" --query 'length(imageDetails)' --output text 2>/dev/null || echo '?')"
    if ! aws_ ecr get-lifecycle-policy --repository-name "$r" >/dev/null 2>&1; then
      nolc="$nolc$r	images=$count"$'\n'
    fi
  done
  if [ -n "$nolc" ]; then
    flag "ECR repositories without a lifecycle policy (old images pile up)"
    printf '%s' "$nolc" | show
  elif [ -n "$repos" ] && [ "$repos" != "None" ]; then
    ok "All ECR repositories have lifecycle policies"
  else
    ok "No ECR repositories"
  fi

  section "Free AWS recommendations"
  local coh
  coh="$(aws cost-optimization-hub list-enrollment-statuses --region us-east-1 \
    --query 'items[0].status' --output text 2>&1 || true)"
  case "$coh" in
    Active)
      ok "Cost Optimization Hub enabled. Recommendations: Billing console -> Cost Optimization Hub" ;;
    *AccessDenied*|*Unauthorized*)
      printf '  %sSKIP%s  Cost Optimization Hub (no permission to read enrollment)\n' "$DIM" "$RST" ;;
    *)
      flag "Cost Optimization Hub is not enabled (free). Billing console -> Cost Optimization Hub -> Enable" ;;
  esac

  section "Summary"
  if [ "$FLAGGED" -eq 0 ]; then
    printf '  %sNothing flagged in %s.%s\n' "$GRN" "$REGION" "$RST"
  else
    printf '  %s%d item(s) to review in %s.%s Not every item is waste; check each one.\n' \
      "$YEL" "$FLAGGED" "$REGION" "$RST"
  fi
}

# ---------------------------------------------------------------- thrifty

prepare_thrifty() {
  need steampipe "Install: brew install turbot/tap/steampipe   (Linux: see https://steampipe.io/downloads)"
  need powerpipe "Install: brew install turbot/tap/powerpipe   (Linux: see https://powerpipe.io/downloads)"
  need git "Install git."

  steampipe plugin install aws >/dev/null

  # Run from inside the mod itself: 'benchmark run all' only covers the
  # current mod, not mods installed as dependencies.
  if [ -d "$THRIFTY_DIR/.git" ]; then
    git -C "$THRIFTY_DIR" pull --quiet --ff-only || true
  else
    mkdir -p "$(dirname "$THRIFTY_DIR")"
    git clone --quiet --depth 1 https://github.com/turbot/steampipe-mod-aws-thrifty.git "$THRIFTY_DIR"
  fi
  (cd "$THRIFTY_DIR" && powerpipe mod install >/dev/null)

  if ! steampipe service status 2>/dev/null | grep -qi 'running'; then
    steampipe service start >/dev/null
    STARTED_STEAMPIPE=1
  fi
}

stop_steampipe_if_started() {
  if [ "${STARTED_STEAMPIPE:-0}" = "1" ]; then
    steampipe service stop >/dev/null 2>&1 || true
  fi
}

run_thrifty() {
  section "AWS Thrifty (Steampipe + Powerpipe)"
  prepare_thrifty
  trap stop_steampipe_if_started EXIT
  mkdir -p "$REPORT_DIR"
  local html="$REPORT_DIR/thrifty-$STAMP.html" csv="$REPORT_DIR/thrifty-$STAMP.csv"
  printf '  Steampipe uses the regions in ~/.steampipe/config/aws.spc (default: your profile region).\n'
  # Powerpipe exits non-zero when any control alarms; that is a result, not a failure.
  (cd "$THRIFTY_DIR" && powerpipe benchmark run all --progress=false --output brief \
     --export "$html" --export "$csv") || true
  if [ -f "$html" ]; then
    printf '\n  Report: %s\n  CSV:    %s\n' "$html" "$csv"
    if command -v open >/dev/null 2>&1; then open "$html"; fi
  else
    die "Thrifty run produced no report. Run 'steampipe query \"select account_id from aws_account\"' to check credentials."
  fi
}

run_thrifty_ui() {
  section "AWS Thrifty dashboard"
  prepare_thrifty
  trap stop_steampipe_if_started EXIT
  printf '  Open http://localhost:9033 (Ctrl-C to stop)\n'
  cd "$THRIFTY_DIR" && powerpipe server
}

# ---------------------------------------------------------------- infracost

terraform_dirs() {
  find "$REPO_ROOT" -type f -name '*.tf' \
    -not -path '*/.terraform/*' -not -path '*/node_modules/*' -not -path '*/.git/*' \
    -exec dirname {} \; | sort -u
}

run_infracost() {
  section "Infracost (Terraform cost estimate)"
  need infracost "Install: brew install infracost, then: infracost auth login (free)"
  if [ -z "$(infracost configure get api_key 2>/dev/null || true)" ]; then
    die "No Infracost API key. Run: infracost auth login"
  fi
  local d dirs
  dirs="$(terraform_dirs)"
  [ -n "$dirs" ] || { ok "No Terraform folders found under $REPO_ROOT"; return 0; }
  printf '%s\n' "$dirs" | while IFS= read -r d; do
    printf '\n  %s%s%s\n' "$BOLD" "${d#"$REPO_ROOT"/}" "$RST"
    infracost breakdown --path "$d" --format table --no-color 2>&1 | show || true
  done
  printf '\n  Usage-based items (Lambda, CloudFront, S3 requests) show as $0 unless you add an infracost-usage.yml.\n'
}

# ---------------------------------------------------------------- main

mode="${1:-quick}"
case "$mode" in
  quick)       run_quick ;;
  thrifty)     run_thrifty ;;
  thrifty-ui)  run_thrifty_ui ;;
  infracost)   run_infracost ;;
  all)         run_quick; (run_infracost) || true; (run_thrifty) || true ;;
  -h|--help|help) sed -n '2,25p' "$0" ;;
  *) die "Unknown mode '$mode'. Use: quick | thrifty | thrifty-ui | infracost | all" ;;
esac
