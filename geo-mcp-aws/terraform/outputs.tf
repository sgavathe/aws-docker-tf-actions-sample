output "site_url" {
  value = local.site_url
}

output "cloudfront_distribution_id" {
  value = aws_cloudfront_distribution.main.id
}

output "data_bucket" {
  value = aws_s3_bucket.data.bucket
}

output "ecr_repository_url" {
  value = aws_ecr_repository.app.repository_url
}

output "function_names" {
  value = local.lambdas
}

output "user_pool_id" {
  value = aws_cognito_user_pool.main.id
}

output "db_cluster_arn" {
  value = aws_rds_cluster.main.arn
}

output "db_admin_secret_arn" {
  description = "RDS-managed admin secret (used only by scripts/db-bootstrap.py via the Data API)"
  value       = aws_rds_cluster.main.master_user_secret[0].secret_arn
}

output "github_deploy_role_arn" {
  description = "Put this in the GitHub secret AWS_GEO_MCP_ROLE_ARN"
  value       = aws_iam_role.github_deploy.arn
}

output "bedrock_model_id" {
  value = var.bedrock_model_id
}
