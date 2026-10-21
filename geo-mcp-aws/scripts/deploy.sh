#!/usr/bin/env bash
# Create or update the geo MCP stack. Run locally with admin credentials (first create and any
# infrastructure change). Routine code deploys run in GitHub Actions (deploy-geo-mcp.yml).
#
#   ./geo-mcp-aws/scripts/deploy.sh                        # mcp.spatialenable.com
#   ATTACH_DOMAIN=false ./geo-mcp-aws/scripts/deploy.sh    # *.cloudfront.net URL only
#   BUDGET_EMAIL=you@example.com ./geo-mcp-aws/scripts/deploy.sh
#
# Needs: AWS credentials, terraform >= 1.5, docker (buildx), node 20+ (npm), python3 + boto3.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TF_DIR="$HERE/terraform"
export AWS_REGION="${AWS_REGION:-us-east-1}"
ATTACH_DOMAIN="${ATTACH_DOMAIN:-true}"
BUDGET_EMAIL="${BUDGET_EMAIL:-}"
APPROVE=""
if [ "${AUTO_APPROVE:-}" = "true" ]; then APPROVE="-auto-approve"; fi

tf() { terraform -chdir="$TF_DIR" "$@"; }
TFVARS=(-var "attach_domain=$ATTACH_DOMAIN" -var "budget_email=$BUDGET_EMAIL")

echo "==> [1/7] terraform init + ECR repository"
tf init -input=false
# shellcheck disable=SC2086
tf apply -input=false $APPROVE "${TFVARS[@]}" -target=aws_ecr_repository.app -target=aws_ecr_lifecycle_policy.app
REPO="$(tf output -raw ecr_repository_url)"

echo "==> [2/7] build + push images"
"$HERE/scripts/build-images.sh" "$REPO" > /tmp/geo-mcp-images.env
# shellcheck disable=SC1091
. /tmp/geo-mcp-images.env   # MCP_IMAGE, CHAT_IMAGE, JOBS_IMAGE

echo "==> [3/7] terraform apply (Aurora takes ~10 minutes the first time)"
# shellcheck disable=SC2086
tf apply -input=false $APPROVE "${TFVARS[@]}" \
  -var "mcp_image=$MCP_IMAGE" -var "chat_image=$CHAT_IMAGE" -var "jobs_image=$JOBS_IMAGE"

echo "==> [4/7] point the functions at the new images"
for kind in mcp chat jobs; do
  fn="$(tf output -json function_names | python3 -c "import json,sys; print(json.load(sys.stdin)['$kind'])")"
  img_var="$(echo "$kind" | tr '[:lower:]' '[:upper:]')_IMAGE"
  aws lambda update-function-code --function-name "$fn" --image-uri "${!img_var}" > /dev/null
  aws lambda wait function-updated --function-name "$fn"
  echo "    $fn -> ${!img_var##*:}"
done

echo "==> [5/7] map libraries -> s3://$(tf output -raw data_bucket)/vendor/"
"$HERE/scripts/sync-vendor.sh" "$(tf output -raw data_bucket)"

echo "==> [6/7] database: PostGIS, IAM roles, sample data (RDS Data API)"
python3 "$HERE/scripts/db-bootstrap.py" \
  --cluster-arn "$(tf output -raw db_cluster_arn)" --secret-arn "$(tf output -raw db_admin_secret_arn)"

echo "==> [7/7] checks"
MODEL="$(tf output -raw bedrock_model_id)"
# A first call with admin credentials also completes the one-time model access for this account.
if aws bedrock-runtime converse --model-id "$MODEL" \
     --messages '[{"role":"user","content":[{"text":"Reply with OK"}]}]' \
     --inference-config '{"maxTokens":5}' --query 'output.message.content[0].text' --output text >/dev/null 2>&1; then
  echo "    Bedrock $MODEL: OK"
else
  echo "    Bedrock $MODEL: NOT available yet. Enable it in the Bedrock console (Model access), then retry."
fi
SITE="$(tf output -raw site_url)"
curl -fsS --retry 10 --retry-delay 10 --retry-all-errors "$SITE/healthz" > /dev/null && echo "    $SITE/healthz: OK"

cat <<EOF

Deployed: $SITE
Next:
  1. Create users (no self sign-up):   geo-mcp-aws/scripts/users.sh analyst writer
  2. Load crashes (optional):           geo-mcp-aws/scripts/load-crashes.sh ~/Downloads/CrashData.geojson
  3. GitHub secret AWS_GEO_MCP_ROLE_ARN = $(tf output -raw github_deploy_role_arn)
     GitHub variable GEO_MCP_URL        = $SITE
EOF
