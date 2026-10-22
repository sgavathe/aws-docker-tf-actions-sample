# Limits for an open demo: daily caps enforced by the chat app, plus an AWS Budgets hard stop.
#
#   1. Daily (chat app, app/agent/quota.py): a site-wide estimated spend cap from Bedrock's own
#      token counts, and per-user question limits (tighter for the geo-demo group). Over a
#      limit, the chat answers with a short message and never calls the model.
#   2. Monthly backstop (AWS Budgets action): when the account's actual monthly cost passes
#      hard_stop_usd (Bedrock + Location only), Budgets attaches a deny policy to the chat role, so Bedrock and address
#      search stop until you detach it. Budget data lags by hours; the daily cap is the
#      fast control, this catches anything else.

variable "daily_budget_usd" {
  description = "Site-wide estimated model + search spend per day (UTC); 0 = no cap"
  type        = number
  default     = 1
}

variable "demo_daily_questions" {
  description = "Questions per day for users in the geo-demo group"
  type        = number
  default     = 30
}

variable "user_daily_questions" {
  description = "Questions per day for other users"
  type        = number
  default     = 200
}

variable "quota_exempt_users" {
  description = "Comma-separated usernames with no daily limits (their use still counts toward the site cap)"
  type        = string
  default     = ""
}

variable "bedrock_price_in_per_mtok" {
  description = "USD per million input tokens for var.bedrock_model_id (Nova Lite default; update with the model)"
  type        = number
  default     = 0.06
}

variable "bedrock_price_out_per_mtok" {
  description = "USD per million output tokens for var.bedrock_model_id"
  type        = number
  default     = 0.24
}

variable "hard_stop_usd" {
  description = "Monthly model + address-search cost (var.hard_stop_services only) at which Budgets cuts off the chat's model access (needs budget_email). Above 31 x daily_budget_usd so it only fires if the daily cap fails."
  type        = number
  default     = 40
}

variable "hard_stop_services" {
  description = "Cost Explorer service names the hard stop counts. Check yours: aws ce get-dimension-values --dimension SERVICE --time-period Start=YYYY-MM-01,End=YYYY-MM-DD"
  type        = list(string)
  default     = ["Amazon Bedrock", "Amazon Location Service"]
}

resource "aws_cognito_user_group" "demo" {
  name         = "geo-demo"
  user_pool_id = aws_cognito_user_pool.main.id
  description  = "Guest accounts: daily question limit var.demo_daily_questions"
}

# Attached by the Budgets action below; detach it to resume:
#   aws iam detach-role-policy --role-name <chat role> --policy-arn <this policy>
resource "aws_iam_policy" "chat_hard_stop" {
  name        = "${local.name}-chat-hard-stop"
  description = "Attached by AWS Budgets when the monthly hard stop is reached: no model calls or searches"
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid      = "DemoBudgetReached"
      Effect   = "Deny"
      Action   = ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream", "geo-places:*"]
      Resource = "*"
    }]
  })
}

resource "aws_budgets_budget" "hard_stop" {
  count        = var.budget_email == "" ? 0 : 1
  name         = "${local.name}-hard-stop"
  budget_type  = "COST"
  limit_amount = tostring(var.hard_stop_usd)
  limit_unit   = "USD"
  time_unit    = "MONTHLY"

  # Only the services the chat pays for. Without this filter the whole account's bill (database,
  # other projects) counted, so the stop could fire with almost no model use.
  cost_filter {
    name   = "Service"
    values = var.hard_stop_services
  }
}

resource "aws_iam_role" "budget_action" {
  count = var.budget_email == "" ? 0 : 1
  name  = "${local.name}-budget-action"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "budgets.amazonaws.com" }
      Action    = "sts:AssumeRole"
      Condition = { StringEquals = { "aws:SourceAccount" = local.account } }
    }]
  })
}

resource "aws_iam_role_policy" "budget_action" {
  count = var.budget_email == "" ? 0 : 1
  name  = "${local.name}-budget-action"
  role  = aws_iam_role.budget_action[0].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "AttachOnlyTheStopPolicyToTheChatRole"
      Effect    = "Allow"
      Action    = ["iam:AttachRolePolicy", "iam:DetachRolePolicy"]
      Resource  = aws_iam_role.chat.arn
      Condition = { ArnEquals = { "iam:PolicyARN" = aws_iam_policy.chat_hard_stop.arn } }
    }]
  })
}

resource "aws_budgets_budget_action" "hard_stop" {
  count              = var.budget_email == "" ? 0 : 1
  budget_name        = aws_budgets_budget.hard_stop[0].name
  action_type        = "APPLY_IAM_POLICY"
  approval_model     = "AUTOMATIC"
  notification_type  = "ACTUAL"
  execution_role_arn = aws_iam_role.budget_action[0].arn

  action_threshold {
    action_threshold_type  = "PERCENTAGE"
    action_threshold_value = 100
  }

  definition {
    iam_action_definition {
      policy_arn = aws_iam_policy.chat_hard_stop.arn
      roles      = [aws_iam_role.chat.name]
    }
  }

  subscriber {
    address           = var.budget_email
    subscription_type = "EMAIL"
  }
}
