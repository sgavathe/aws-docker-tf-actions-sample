# Geo MCP analyst chat on AWS: scale-to-zero, no NAT, no stored passwords.
#
#   browser ── CloudFront ─┬─ /*                    -> Lambda "chat" (Function URL, IAM via OAC; streams NDJSON)
#                          └─ /vendor/*, /basemap/* -> S3 (map libraries; private bucket via OAC)
#
#   chat Lambda (no VPC) ── Bedrock Converse (IAM)          model: var.bedrock_model_id
#                        ── Cognito InitiateAuth            sign-in (user pool, groups geo-reader/geo-writer)
#                        ── DynamoDB                        sessions (TTL)
#                        ── S3 maps/ (read)                 maps and exports, served only to signed-in users
#                        ── geo-mcp Function URL            SigV4 (IAM) + user's Cognito token
#
#   geo-mcp Lambda (private subnets, no internet) ── Aurora PostgreSQL Serverless v2 + PostGIS (IAM auth, min 0 ACU)
#                                                 └─ S3 gateway endpoint (writes maps/, free)
#   jobs Lambda (private subnets)                  ── crash loader: s3 import/ -> Aurora
#
# Separate state key: never touches infra/ (ECS) or serverless/ (site).

terraform {
  required_version = ">= 1.5"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.80" # Aurora Serverless v2 scale-to-zero (min_capacity 0, seconds_until_auto_pause)
    }
    http = {
      source  = "hashicorp/http"
      version = "~> 3.4"
    }
  }

  backend "s3" {
    bucket = "sgavathe-tfstate-390744232980"
    key    = "geo-devops-demo/geo-mcp.tfstate"
    region = "us-east-1"
  }
}

provider "aws" {
  region = var.aws_region

  default_tags {
    tags = {
      Project = var.project_name
      Stack   = "geo-mcp"
    }
  }
}

data "aws_caller_identity" "current" {}
data "aws_partition" "current" {}

data "aws_availability_zones" "available" {
  state = "available"
}

locals {
  name    = "${var.project_name}-geomcp"
  account = data.aws_caller_identity.current.account_id
  region  = var.aws_region
  azs     = slice(data.aws_availability_zones.available.names, 0, 2)
}
