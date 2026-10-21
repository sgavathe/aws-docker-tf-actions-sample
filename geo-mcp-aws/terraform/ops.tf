# ---- Cost alarm: emails at 80% forecast and 100% actual of the monthly budget (first 2 budgets are free) ----

resource "aws_budgets_budget" "monthly" {
  count        = var.budget_email == "" ? 0 : 1
  name         = "${local.name}-monthly"
  budget_type  = "COST"
  limit_amount = tostring(var.budget_usd)
  limit_unit   = "USD"
  time_unit    = "MONTHLY" # whole account: catches anything left running, not just this stack

  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 80
    threshold_type             = "PERCENTAGE"
    notification_type          = "FORECASTED"
    subscriber_email_addresses = [var.budget_email]
  }

  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 100
    threshold_type             = "PERCENTAGE"
    notification_type          = "ACTUAL"
    subscriber_email_addresses = [var.budget_email]
  }
}

# ---- GitHub Actions deploy role: routine code deploys only ----
# Pushes images and points the three functions at them, and syncs the map libraries.
# It cannot change infrastructure, IAM, the database or Cognito: those changes are applied
# with Terraform by an admin (scripts/deploy.sh), the same split as serverless/.

data "aws_iam_openid_connect_provider" "github" {
  url = "https://token.actions.githubusercontent.com"
}

resource "aws_iam_role" "github_deploy" {
  name = "github-actions-geo-mcp-deploy-role"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Action    = "sts:AssumeRoleWithWebIdentity"
      Effect    = "Allow"
      Principal = { Federated = data.aws_iam_openid_connect_provider.github.arn }
      Condition = {
        StringEquals = { "token.actions.githubusercontent.com:aud" = "sts.amazonaws.com" }
        StringLike   = { "token.actions.githubusercontent.com:sub" = var.github_oidc_subs }
      }
    }]
  })
}

resource "aws_iam_role_policy" "github_deploy" {
  name = "${local.name}-github-deploy"
  role = aws_iam_role.github_deploy.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "EcrLogin"
        Effect   = "Allow"
        Action   = "ecr:GetAuthorizationToken"
        Resource = "*"
      },
      {
        Sid    = "EcrPushThisRepo"
        Effect = "Allow"
        Action = [
          "ecr:BatchCheckLayerAvailability", "ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer",
          "ecr:InitiateLayerUpload", "ecr:UploadLayerPart", "ecr:CompleteLayerUpload", "ecr:PutImage",
          "ecr:DescribeImages", "ecr:DescribeImageScanFindings"
        ]
        Resource = aws_ecr_repository.app.arn
      },
      {
        Sid    = "UpdateTheseFunctionsCode"
        Effect = "Allow"
        Action = ["lambda:UpdateFunctionCode", "lambda:GetFunction", "lambda:GetFunctionConfiguration"]
        Resource = [aws_lambda_function.mcp.arn, aws_lambda_function.chat.arn, aws_lambda_function.jobs.arn,
        aws_lambda_function.ingest.arn, aws_lambda_function.hooks.arn]
      },
      {
        Sid      = "SyncMapLibraries"
        Effect   = "Allow"
        Action   = ["s3:PutObject", "s3:DeleteObject", "s3:GetObject"]
        Resource = "${aws_s3_bucket.data.arn}/vendor/*"
      },
      {
        Sid       = "ListMapLibraries"
        Effect    = "Allow"
        Action    = "s3:ListBucket"
        Resource  = aws_s3_bucket.data.arn
        Condition = { StringLike = { "s3:prefix" = "vendor/*" } }
      }
    ]
  })
}
