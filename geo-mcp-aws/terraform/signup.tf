# Open sign-up with email verification, plus owner notifications.
#
#   browser -> chat app /api/signup, /api/confirm -> Cognito SignUp / ConfirmSignUp (email code)
#   Cognito PostConfirmation -> hooks Lambda: adds the account to geo-reader, geo-writer, geo-demo
#                                             (guest limits; no data loading) and emails the owner
#   chat app: emails the owner when today's spend passes 80% and 100% of daily_budget_usd
#
# Every self-registered account gets demo limits (limits.tf); the daily cap and the monthly hard
# stop bound the cost. Turn sign-up off with -var self_signup=false (admin-created users only).

variable "self_signup" {
  description = "true = anyone can create a demo account (email verified); false = admin-created users only"
  type        = bool
  default     = true
}

variable "alert_email" {
  description = "Owner email for sign-up and daily-budget notifications (empty = budget_email)"
  type        = string
  default     = ""
}

locals {
  alert_email = var.alert_email != "" ? var.alert_email : var.budget_email
}

resource "aws_sns_topic" "alerts" {
  name              = "${local.name}-alerts"
  kms_master_key_id = "alias/aws/sns"
}

# AWS emails a confirmation link first; notifications start after you click it.
resource "aws_sns_topic_subscription" "owner" {
  count     = local.alert_email == "" ? 0 : 1
  topic_arn = aws_sns_topic.alerts.arn
  protocol  = "email"
  endpoint  = local.alert_email
}

resource "aws_cloudwatch_log_group" "hooks" {
  name              = "/aws/lambda/${local.name}-signup-hooks"
  retention_in_days = var.log_retention_days
}

resource "aws_iam_role" "hooks" {
  name               = "${local.name}-signup-hooks-role"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
}

resource "aws_iam_role_policy_attachment" "hooks_logs" {
  role       = aws_iam_role.hooks.name
  policy_arn = "arn:${data.aws_partition.current.partition}:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

resource "aws_iam_role_policy" "hooks" {
  name = "${local.name}-signup-hooks"
  role = aws_iam_role.hooks.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "PutNewAccountsInTheDemoGroups"
        Effect   = "Allow"
        Action   = "cognito-idp:AdminAddUserToGroup"
        Resource = aws_cognito_user_pool.main.arn
      },
      {
        Sid      = "NotifyOwner"
        Effect   = "Allow"
        Action   = "sns:Publish"
        Resource = aws_sns_topic.alerts.arn
      }
    ]
  })
}

resource "aws_lambda_function" "hooks" {
  function_name = "${local.name}-signup-hooks"
  role          = aws_iam_role.hooks.arn
  package_type  = "Image"
  image_uri     = var.jobs_image # same image as the loader, different handler
  architectures = ["x86_64"]
  memory_size   = 256
  timeout       = 10

  image_config {
    command = ["geo_mcp.cognito_hooks.handler"]
  }

  environment {
    variables = {
      DEMO_GROUPS     = "geo-reader,geo-writer,geo-demo" # literal names: the groups depend on the pool, which depends on this function
      ALERT_TOPIC_ARN = aws_sns_topic.alerts.arn
    }
  }

  lifecycle {
    ignore_changes = [image_uri]
  }

  depends_on = [aws_cloudwatch_log_group.hooks, aws_iam_role_policy_attachment.hooks_logs]
}

resource "aws_lambda_permission" "cognito_hooks" {
  statement_id  = "AllowCognitoPostConfirmation"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.hooks.function_name
  principal     = "cognito-idp.amazonaws.com"
  source_arn    = aws_cognito_user_pool.main.arn
}
