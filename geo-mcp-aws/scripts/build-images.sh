#!/usr/bin/env bash
# Build and push the three Lambda images; prints MCP_IMAGE=..., CHAT_IMAGE=..., JOBS_IMAGE=... on stdout.
#   geo-mcp-aws/scripts/build-images.sh <ecr-repository-url>
set -euo pipefail
REPO="$1"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REGISTRY="${REPO%%/*}"
TAG="$(git -C "$HERE" rev-parse --short=8 HEAD 2>/dev/null || echo local)-$(date -u +%Y%m%d%H%M%S)"

aws ecr get-login-password | docker login --username AWS --password-stdin "$REGISTRY" >&2
# Sign in to ECR Public too: anonymous pulls of public.ecr.aws base images (the Lambda Web Adapter)
# share a data quota across CI runners and fail with 429 "Data limit exceeded". Best effort.
aws ecr-public get-login-password --region us-east-1 2>/dev/null \
  | docker login --username AWS --password-stdin public.ecr.aws >&2 \
  || echo "    (ECR Public sign-in unavailable; pulling public images anonymously)" >&2

build() {  # kind dockerfile context
  local image="$REPO:$1-$TAG"
  # linux/amd64 to match the functions (also on Apple Silicon). No provenance/SBOM attestations:
  # Lambda rejects image indexes that carry them.
  docker buildx build --platform linux/amd64 --provenance=false --sbom=false \
    -f "$HERE/lambda/$2" -t "$image" --load "$HERE/app/$3" >&2
  docker push "$image" >&2
  echo "$image"
}

echo "MCP_IMAGE=$(build mcp geo-mcp.Dockerfile mcp-server)"
echo "JOBS_IMAGE=$(build jobs jobs.Dockerfile mcp-server)"
echo "CHAT_IMAGE=$(build chat chat.Dockerfile agent)"
