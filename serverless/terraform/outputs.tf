output "site_url" {
  value = var.attach_domain ? "https://${var.domain_name}" : "https://${aws_cloudfront_distribution.site.domain_name}"
}

output "cloudfront_url" {
  value = "https://${aws_cloudfront_distribution.site.domain_name}"
}

output "cloudfront_distribution_id" {
  value = aws_cloudfront_distribution.site.id
}

output "site_bucket" {
  value = aws_s3_bucket.site.bucket
}

output "ecr_api_url" {
  value = aws_ecr_repository.api.repository_url
}

output "lambda_function_name" {
  value = aws_lambda_function.api.function_name
}

output "github_deploy_role_arn" {
  description = "Put this in the GitHub secret AWS_SERVERLESS_ROLE_ARN"
  value       = aws_iam_role.github_deploy.arn
}

output "graph_s3_uri" {
  description = "Where .github/workflows/ci-graph.yml publishes the infrastructure graph"
  value       = "s3://${aws_s3_bucket.site.bucket}/${var.graph_key_prefix}ci-graph.json"
}
