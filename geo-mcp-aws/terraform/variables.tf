variable "aws_region" {
  type    = string
  default = "us-east-1" # CloudFront ACM certs must be in us-east-1
}

variable "project_name" {
  type    = string
  default = "geo-devops-demo"
}

# ---- Model ----

variable "bedrock_model_id" {
  description = <<-EOT
    Bedrock model or inference profile for the agent (Converse API with tool use). Examples:
      us.amazon.nova-lite-v1:0        cheapest; available on every account (default)
      us.amazon.nova-pro-v1:0         stronger multi-step tool use, still low cost
      us.anthropic.claude-haiku-5-5   best tool use; Anthropic models can be restricted on new accounts
    Check it answers in the Bedrock Playground first. All profiles route within the US.
  EOT
  type        = string
  default     = "us.amazon.nova-lite-v1:0"
}

variable "bedrock_foundation_model" {
  description = "Base model id behind the inference profile above (IAM needs both ARNs)"
  type        = string
  default     = "amazon.nova-lite-v1:0"
}

# ---- Images (set by scripts/deploy.sh on first create; CI updates code afterwards) ----

variable "mcp_image" {
  type    = string
  default = ""
}

variable "chat_image" {
  type    = string
  default = ""
}

variable "jobs_image" {
  type    = string
  default = ""
}

# ---- Database ----

variable "aurora_engine_version" {
  description = "Aurora PostgreSQL version; scale-to-zero needs 16.3+ (or 15.7+, 14.12+, 13.15+)"
  type        = string
  default     = "16.15"
}

variable "aurora_max_acu" {
  description = "Upper bound for Aurora capacity (1 ACU ~ 2 GiB RAM). Raise to 4 for big crash loads."
  type        = number
  default     = 2
}

variable "aurora_pause_after_seconds" {
  description = "Idle time before Aurora pauses to 0 ACU (storage-only billing). First query after a pause waits ~15 s."
  type        = number
  default     = 600
}

variable "db_deletion_protection" {
  type    = bool
  default = false # demo stack: destroyable on purpose
}

# ---- Web ----

variable "attach_domain" {
  description = "true = serve on var.domain_name (ACM cert + DNS in var.zone_name); false = *.cloudfront.net only"
  type        = bool
  default     = true # free ACM cert + alias record in the existing zone; set false for *.cloudfront.net only
}

variable "zone_name" {
  type    = string
  default = "spatialenable.com"
}

variable "domain_name" {
  type    = string
  default = "mcp.spatialenable.com"
}

variable "map_basemap" {
  description = "osm = OpenStreetMap tiles in the browser; none = data only"
  type        = string
  default     = "osm"
  validation {
    condition     = contains(["osm", "none"], var.map_basemap)
    error_message = "map_basemap must be osm or none."
  }
}

variable "crash_epdo_weights" {
  description = "Illustrative EPDO weights; replace with VDOT/FHWA values before using rankings for decisions"
  type        = string
  default     = "K=1500,A=100,B=25,C=10,O=1"
}

# ---- Operations ----

variable "log_retention_days" {
  type    = number
  default = 7
}

variable "budget_email" {
  description = "Email for the monthly cost alarm (empty = no budget alarm)"
  type        = string
  default     = ""
}

variable "budget_usd" {
  type    = number
  default = 10
}

variable "github_oidc_subs" {
  description = "OIDC 'sub' values allowed to assume the geo-mcp deploy role (main branch only)"
  type        = list(string)
  default     = ["repo:sgavathe@6047188/aws-docker-tf-actions-sample@1373663842:ref:refs/heads/main"]
}
