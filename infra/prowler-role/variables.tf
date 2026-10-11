variable "aws_region" {
  description = "Region the provider talks to (IAM is global)."
  type        = string
  default     = "us-east-1"
}

variable "github_repo" {
  description = "owner/repo allowed to assume the role."
  type        = string
  default     = "sgavathe/aws-docker-tf-actions-sample"
}

variable "allowed_refs" {
  description = <<-EOT
    OIDC subject patterns allowed to assume the role, relative to the repo.
    Scheduled and manually dispatched runs on the default branch use
    "ref:refs/heads/main". Add "pull_request" to let PR runs scan too.
  EOT
  type        = list(string)
  default     = ["ref:refs/heads/main"]
}

variable "role_name" {
  description = "IAM role name."
  type        = string
  default     = "ProwlerReadOnly"
}

variable "max_session_duration" {
  description = "Seconds. A full Prowler AWS scan of a small account takes 5-20 minutes."
  type        = number
  default     = 3600
}
