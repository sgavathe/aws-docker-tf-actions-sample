terraform {
  required_version = ">= 1.5.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 5.0"
    }
  }

  # Standalone stack with local state on purpose: it stays out of the app's
  # state so applying it can't touch the site. Move to your S3 backend later
  # if you want, e.g.:
  # backend "s3" {
  #   bucket = "<your-tf-state-bucket>"
  #   key    = "security/prowler-role.tfstate"
  #   region = "us-east-1"
  # }
}

provider "aws" {
  region = var.aws_region

  default_tags {
    tags = {
      Project   = "geo-devops-demo"
      Purpose   = "prowler-security-scan"
      ManagedBy = "terraform"
    }
  }
}
