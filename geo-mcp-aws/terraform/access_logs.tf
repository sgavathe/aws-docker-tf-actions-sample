# CloudFront access logs for the chat site, delivered to CloudWatch Logs (CloudFront standard
# logging v2), for a visitor dashboard in Logs Insights. Same shape as the homepage's
# /cloudfront/map-spatialenable group: JSON records with W3C field names (c-ip, cs-uri-stem,
# cs(User-Agent), cs(Referer), sc-status, cs-method, ...).
# Cost: vended-log delivery is billed per GB (cents at demo traffic) plus log storage.
# CloudFront log delivery sources only exist in us-east-1, so this is skipped elsewhere.

variable "access_logs" {
  description = "Send the chat site's CloudFront access logs to CloudWatch Logs"
  type        = bool
  default     = true
}

variable "access_log_retention_days" {
  description = "Days to keep the CloudFront access logs"
  type        = number
  default     = 90
}

locals {
  access_logs = var.access_logs && var.aws_region == "us-east-1"
}

resource "aws_cloudwatch_log_group" "cloudfront" {
  count             = local.access_logs ? 1 : 0
  name              = "/cloudfront/${local.name}"
  retention_in_days = var.access_log_retention_days
}

resource "aws_cloudwatch_log_delivery_source" "cloudfront" {
  count        = local.access_logs ? 1 : 0
  name         = "${local.name}-cloudfront"
  log_type     = "ACCESS_LOGS"
  resource_arn = aws_cloudfront_distribution.main.arn
}

resource "aws_cloudwatch_log_delivery_destination" "cloudfront" {
  count         = local.access_logs ? 1 : 0
  name          = "${local.name}-cloudfront-logs"
  output_format = "json"

  delivery_destination_configuration {
    destination_resource_arn = aws_cloudwatch_log_group.cloudfront[0].arn
  }
}

resource "aws_cloudwatch_log_delivery" "cloudfront" {
  count                    = local.access_logs ? 1 : 0
  delivery_source_name     = aws_cloudwatch_log_delivery_source.cloudfront[0].name
  delivery_destination_arn = aws_cloudwatch_log_delivery_destination.cloudfront[0].arn
}

output "access_log_group" {
  value = local.access_logs ? aws_cloudwatch_log_group.cloudfront[0].name : "(off)"
}
