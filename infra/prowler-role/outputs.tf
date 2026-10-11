output "prowler_role_arn" {
  description = "Save as the GitHub repo variable PROWLER_ROLE_ARN."
  value       = aws_iam_role.prowler.arn
}
