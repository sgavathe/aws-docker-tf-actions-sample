# Low-cost, scale-to-zero deployment of the same app:
#
#   map.spatialenable.com -> CloudFront ─┬─ /*              -> S3 (React/Vite build, private, OAC)
#                                        └─ /api/*, /health -> Lambda Function URL (.NET API, IAM auth via OAC)
#
# No VPC, ALB, NAT, Fargate tasks or public IPv4 addresses -> nothing bills while idle.
# Separate state key from infra/ so the ECS stack stays untouched on main.

terraform {
  required_version = ">= 1.5"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.70" # Lambda OAC support for CloudFront
    }
  }

  backend "s3" {
    bucket = "sgavathe-tfstate-390744232980"
    key    = "geo-devops-demo/serverless.tfstate"
    region = "us-east-1"
  }
}

provider "aws" {
  region = var.aws_region

  default_tags {
    tags = {
      Project = var.project_name
      Stack   = "serverless"
    }
  }
}

data "aws_caller_identity" "current" {}

locals {
  name    = "${var.project_name}-sls"
  account = data.aws_caller_identity.current.account_id
}
