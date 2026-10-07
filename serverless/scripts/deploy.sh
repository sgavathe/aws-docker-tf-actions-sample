#!/usr/bin/env bash
# Build and deploy the low-cost serverless stack. Same script runs locally and in CI.
#
#   ./serverless/scripts/deploy.sh                       # serve on map.spatialenable.com
#   ATTACH_DOMAIN=false ./serverless/scripts/deploy.sh   # *.cloudfront.net URL only
#
# Needs: AWS credentials, terraform >= 1.5, docker (with buildx), node 20+.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
TF_DIR="$ROOT/serverless/terraform"
PROJECT_NAME="${PROJECT_NAME:-geo-devops-demo}"
ATTACH_DOMAIN="${ATTACH_DOMAIN:-true}"
export AWS_REGION="${AWS_REGION:-us-east-1}"

APPROVE=""
if [ "${CI:-}" = "true" ] || [ "${AUTO_APPROVE:-}" = "true" ]; then
  APPROVE="-auto-approve"
fi

tf() { terraform -chdir="$TF_DIR" "$@"; }

echo "==> [1/6] terraform init"
tf init -input=false

echo "==> [2/6] ensure ECR repo exists (needed before the Lambda can be created)"
# shellcheck disable=SC2086
tf apply -input=false $APPROVE \
  -target=aws_ecr_repository.api \
  -target=aws_ecr_lifecycle_policy.api

REPO_URI="$(aws ecr describe-repositories \
  --repository-names "${PROJECT_NAME}-sls-api" \
  --query 'repositories[0].repositoryUri' --output text)"
REGISTRY="${REPO_URI%%/*}"
TAG="$(git -C "$ROOT" rev-parse --short=8 HEAD 2>/dev/null || echo local)-$(date -u +%Y%m%d%H%M%S)"
IMAGE="$REPO_URI:$TAG"

echo "==> [3/6] build + push Lambda image $IMAGE"
aws ecr get-login-password | docker login --username AWS --password-stdin "$REGISTRY"
# linux/amd64 to match the function's x86_64 architecture (also on Apple Silicon).
# No provenance/SBOM attestations: Lambda rejects image indexes that carry them.
docker buildx build \
  --platform linux/amd64 \
  --provenance=false --sbom=false \
  -f "$ROOT/serverless/lambda/Dockerfile" \
  -t "$IMAGE" \
  --load \
  "$ROOT/backend"
docker push "$IMAGE"

echo "==> [4/6] terraform apply (attach_domain=$ATTACH_DOMAIN)"
# shellcheck disable=SC2086
tf apply -input=false $APPROVE \
  -var "api_image=$IMAGE" \
  -var "attach_domain=$ATTACH_DOMAIN"

SITE_URL="$(tf output -raw site_url)"
BUCKET="$(tf output -raw site_bucket)"
DIST_ID="$(tf output -raw cloudfront_distribution_id)"

echo "==> [5/6] build + upload frontend to s3://$BUCKET"
(cd "$ROOT/frontend" && npm ci && npm run build)
DIST="$ROOT/frontend/dist"

# Same runtime-config idea as docker-entrypoint.sh, written at deploy time instead.
printf 'window.__env = {\n  apiBase: "%s"\n};\n' "$SITE_URL" > "$DIST/env.js"

# Hashed assets first (cache forever; old ones kept so open tabs don't break),
# then everything else, then the HTML shell + runtime config last (never cached).
aws s3 sync "$DIST/assets" "s3://$BUCKET/assets" \
  --cache-control "public, max-age=31536000, immutable"
# data/ holds the published infrastructure graph (ci-graph.yml), not part of the build.
aws s3 sync "$DIST" "s3://$BUCKET" --delete \
  --exclude "assets/*" --exclude "index.html" --exclude "env.js" --exclude "data/*" \
  --cache-control "public, max-age=300"
aws s3 cp "$DIST/index.html" "s3://$BUCKET/index.html" \
  --cache-control "no-cache" --content-type "text/html; charset=utf-8"
aws s3 cp "$DIST/env.js" "s3://$BUCKET/env.js" \
  --cache-control "no-store" --content-type "application/javascript"

aws cloudfront create-invalidation --distribution-id "$DIST_ID" \
  --paths "/index.html" "/env.js" > /dev/null

echo "==> [6/6] smoke test $SITE_URL (first call is a Lambda cold start)"
curl -fsS --retry 10 --retry-delay 15 --retry-all-errors "$SITE_URL/health"; echo
curl -fsS --retry 3 --retry-delay 5 --retry-all-errors "$SITE_URL/api/ports" > /dev/null
curl -fsS --retry 3 --retry-delay 5 --retry-all-errors "$SITE_URL/api/infrastructure" > /dev/null
code="$(curl -s -o /dev/null -w '%{http_code}' "$SITE_URL/")"
[ "$code" = "200" ] || { echo "Expected 200 for /, got $code"; exit 1; }

echo
echo "Deployed: $SITE_URL"
echo "GitHub secret AWS_SERVERLESS_ROLE_ARN = $(tf output -raw github_deploy_role_arn)"
