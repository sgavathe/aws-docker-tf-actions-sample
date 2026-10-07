#!/usr/bin/env bash
# "Park" the ECS stack in infra/: remove only the parts that bill by the hour.
#
# Destroyed (and re-created by the next "Build and Deploy" run):
#   - ALB + its 2 public IPv4 addresses, listeners, listener rules
#   - both ECS services (Fargate tasks + their public IPv4 addresses)
#   - the 5xx alarms and the map.spatialenable.com A record (they reference the ALB)
#
# Kept (free or pennies): VPC, subnets, security groups, ECS cluster, task
# definitions, target groups, ECR repos + images, IAM roles, the GitHub OIDC
# provider (the serverless stack depends on it), ACM cert, Terraform state.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

terraform -chdir="$ROOT/infra" init -input=false
terraform -chdir="$ROOT/infra" destroy -input=false \
  -target=aws_route53_record.map \
  -target=aws_ecs_service.backend \
  -target=aws_ecs_service.frontend \
  -target=aws_lb.main

echo
echo "ECS stack parked. To bring it back: release the domain from the serverless stack"
echo "(ATTACH_DOMAIN=false ./serverless/scripts/deploy.sh), then run the 'Build and Deploy' workflow."
