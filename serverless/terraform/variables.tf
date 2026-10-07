variable "aws_region" {
  type    = string
  default = "us-east-1" # must stay us-east-1: CloudFront only uses ACM certs from here
}

variable "project_name" {
  type    = string
  default = "geo-devops-demo"
}

variable "zone_name" {
  description = "Existing public Route 53 hosted zone"
  type        = string
  default     = "spatialenable.com"
}

variable "domain_name" {
  type    = string
  default = "map.spatialenable.com"
}

variable "attach_domain" {
  description = <<-EOT
    true  = CloudFront serves map.spatialenable.com (cert + alias + DNS record).
    false = CloudFront URL only (*.cloudfront.net). Use false while the ECS stack
            still owns the DNS record, e.g. to test before cutover or when switching back.
  EOT
  type        = bool
  default     = true
}

variable "api_image" {
  description = "Full ECR image URI:tag for the Lambda API, set by serverless/scripts/deploy.sh"
  type        = string
  default     = ""
  validation {
    condition     = var.api_image == "" || can(regex("^\\d+\\.dkr\\.ecr\\..+/.+:.+$", var.api_image))
    error_message = "api_image must be a full ECR URI with a tag."
  }
}

variable "lambda_memory_mb" {
  description = "Lambda CPU scales with memory; 1024 keeps .NET + ML.NET cold starts reasonable"
  type        = number
  default     = 1024
}

variable "graph_key_prefix" {
  description = "S3 prefix (in the site bucket) holding the published infrastructure graph"
  type        = string
  default     = "data/"
  validation {
    condition     = can(regex("^[a-z0-9-]+/$", var.graph_key_prefix))
    error_message = "Use a single folder name ending in '/', e.g. data/."
  }
}

variable "log_retention_days" {
  type    = number
  default = 7
}

variable "github_oidc_subs" {
  description = "OIDC 'sub' values allowed to assume the serverless deploy role (main branch only)"
  type        = list(string)
  default     = ["repo:sgavathe@6047188/aws-docker-tf-actions-sample@1373663842:ref:refs/heads/main"]
}
