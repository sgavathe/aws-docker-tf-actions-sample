# ---- Container registry for the Lambda image ----

resource "aws_ecr_repository" "api" {
  name                 = "${local.name}-api"
  image_tag_mutability = "IMMUTABLE"
  force_delete         = true

  image_scanning_configuration {
    scan_on_push = true
  }
}

# Keep storage cost near zero: only the last 5 images are kept.
resource "aws_ecr_lifecycle_policy" "api" {
  repository = aws_ecr_repository.api.name
  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "Keep only the 5 most recent images"
      selection = {
        tagStatus   = "any"
        countType   = "imageCountMoreThan"
        countNumber = 5
      }
      action = { type = "expire" }
    }]
  })
}

# ---- Lambda function (same .NET API, run via Lambda Web Adapter) ----

resource "aws_iam_role" "lambda" {
  name = "${local.name}-api-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Action    = "sts:AssumeRole"
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
    }]
  })
}

resource "aws_iam_role_policy_attachment" "lambda_logs" {
  role       = aws_iam_role.lambda.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

# Read-only access to the published dependency graph (data/ in the site bucket),
# written by .github/workflows/ci-graph.yml. Nothing else in the bucket.
resource "aws_iam_role_policy" "lambda_graph_read" {
  name = "${local.name}-graph-read"
  role = aws_iam_role.lambda.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "ReadGraph"
        Effect   = "Allow"
        Action   = "s3:GetObject"
        Resource = "${aws_s3_bucket.site.arn}/${var.graph_key_prefix}*"
      },
      {
        # Lets a missing file come back as 404 (falls back to the sample) instead of 403.
        Sid       = "ListGraphPrefix"
        Effect    = "Allow"
        Action    = "s3:ListBucket"
        Resource  = aws_s3_bucket.site.arn
        Condition = { StringLike = { "s3:prefix" = "${var.graph_key_prefix}*" } }
      }
    ]
  })
}

# KMS key cost avoid, dont implement in real work
# tfsec:ignore:aws-logs-log-group-customer-key
resource "aws_cloudwatch_log_group" "api" {
  name              = "/aws/lambda/${local.name}-api"
  retention_in_days = var.log_retention_days
}

resource "aws_lambda_function" "api" {
  function_name = "${local.name}-api"
  role          = aws_iam_role.lambda.arn
  package_type  = "Image"
  image_uri     = var.api_image
  architectures = ["x86_64"]
  memory_size   = var.lambda_memory_mb
  timeout       = 15 # weather.gov client already times out at 8s

  environment {
    variables = {
      ASPNETCORE_ENVIRONMENT = "Production"
      # Where InfrastructureGraphProvider reads the dependency graph (falls back to the bundled sample)
      CiGraph__S3Bucket       = aws_s3_bucket.site.bucket
      CiGraph__S3Key          = "${var.graph_key_prefix}ci-graph.json"
      CiGraph__RefreshMinutes = "15"
    }
  }

  # Not using reserved_concurrent_executions as a cost cap: new accounts with a
  # concurrency quota of 10 reject any reservation. CloudFront + free tier is the cap.

  depends_on = [
    aws_cloudwatch_log_group.api,
    aws_iam_role_policy_attachment.lambda_logs,
    aws_iam_role_policy.lambda_graph_read,
  ]
}

# Function URL is IAM-protected: only CloudFront (signing with OAC) can call it.
resource "aws_lambda_function_url" "api" {
  function_name      = aws_lambda_function.api.function_name
  authorization_type = "AWS_IAM"
}

resource "aws_lambda_permission" "cloudfront_url" {
  statement_id           = "AllowCloudFrontInvokeFunctionUrl"
  action                 = "lambda:InvokeFunctionUrl"
  function_name          = aws_lambda_function.api.function_name
  principal              = "cloudfront.amazonaws.com"
  source_arn             = aws_cloudfront_distribution.site.arn
  function_url_auth_type = "AWS_IAM"
}

# Newer function URLs also require lambda:InvokeFunction for the caller.
resource "aws_lambda_permission" "cloudfront_invoke" {
  statement_id  = "AllowCloudFrontInvokeFunction"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.api.function_name
  principal     = "cloudfront.amazonaws.com"
  source_arn    = aws_cloudfront_distribution.site.arn
}
