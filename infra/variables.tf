variable "aws_region" {
  default = "us-east-1"
}

variable "project_name" {
  default = "geo-devops-demo"
}

variable "backend_image" {
  description = "Full ECR image URI:tag for the backend, set by CI"
  type        = string
  default     = ""
  validation {
    condition     = var.backend_image == "" || can(regex("^\\d+\\.dkr\\.ecr\\..+/.+:.+$", var.backend_image))
    error_message = "backend_image must be a full ECR URI with a tag."
  }
}

variable "frontend_image" {
  description = "Full ECR image URI:tag for the frontend, set by CI"
  type        = string
  default     = ""
}
