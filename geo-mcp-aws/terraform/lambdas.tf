# Three Lambda functions from one ECR repository (tags mcp-*, chat-*, jobs-*).
# Images are set on first create by scripts/deploy.sh; afterwards CI updates the code
# (aws lambda update-function-code), so Terraform ignores image_uri changes.

resource "aws_ecr_repository" "app" {
  name                 = local.name
  image_tag_mutability = "IMMUTABLE"
  force_delete         = true

  image_scanning_configuration {
    scan_on_push = true
  }
}

resource "aws_ecr_lifecycle_policy" "app" {
  repository = aws_ecr_repository.app.name
  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "Keep the 15 most recent images (5 deploys x 3 functions)"
      selection    = { tagStatus = "any", countType = "imageCountMoreThan", countNumber = 15 }
      action       = { type = "expire" }
    }]
  })
}

data "aws_iam_policy_document" "lambda_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

locals {
  db_url = "postgresql://%s@${local.db_host}:5432/geo"
  lambdas = {
    mcp  = "${local.name}-mcp"
    chat = "${local.name}-chat"
    jobs = "${local.name}-jobs"
  }
}

# tfsec:ignore:aws-logs-log-group-customer-key  (KMS CMK ~$1/month; AWS-managed encryption is on)
resource "aws_cloudwatch_log_group" "fn" {
  for_each          = local.lambdas
  name              = "/aws/lambda/${each.value}"
  retention_in_days = var.log_retention_days
}

# ======================================================================== geo-mcp (MCP server)

resource "aws_iam_role" "mcp" {
  name               = "${local.name}-mcp-role"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
}

resource "aws_iam_role_policy_attachment" "mcp_vpc" {
  role       = aws_iam_role.mcp.name
  policy_arn = "arn:${data.aws_partition.current.partition}:iam::aws:policy/service-role/AWSLambdaVPCAccessExecutionRole"
}

resource "aws_iam_role_policy" "mcp" {
  name = "${local.name}-mcp"
  role = aws_iam_role.mcp.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "DbConnectAsAppRolesOnly"
        Effect   = "Allow"
        Action   = "rds-db:connect"
        Resource = ["${local.dbuser_arn}/mcp_reader", "${local.dbuser_arn}/mcp_writer"]
      },
      {
        Sid      = "WriteMapsAndExports"
        Effect   = "Allow"
        Action   = ["s3:PutObject", "s3:GetObject", "s3:DeleteObject"]
        Resource = "${aws_s3_bucket.data.arn}/maps/*"
      },
      {
        Sid       = "ListMapsPrefix"
        Effect    = "Allow"
        Action    = "s3:ListBucket"
        Resource  = aws_s3_bucket.data.arn
        Condition = { StringLike = { "s3:prefix" = "maps/*" } }
      }
    ]
  })
}

resource "aws_lambda_function" "mcp" {
  function_name = local.lambdas.mcp
  role          = aws_iam_role.mcp.arn
  package_type  = "Image"
  image_uri     = var.mcp_image
  architectures = ["x86_64"]
  memory_size   = 1024
  timeout       = 120 # first query after an Aurora pause waits ~15 s; statewide grids take a few more

  vpc_config {
    subnet_ids         = aws_subnet.private[*].id
    security_group_ids = [aws_security_group.lambda.id]
  }

  environment {
    variables = {
      AUTH_PROVIDER                = "cognito"
      TOKEN_ISSUER                 = local.token_issuer
      COGNITO_CLIENT_ID            = aws_cognito_user_pool_client.chat.id
      COGNITO_JWKS                 = data.http.jwks.response_body
      DB_IAM_AUTH                  = "1"
      DATABASE_URL_RO              = format(local.db_url, "mcp_reader")
      DATABASE_URL_RW              = format(local.db_url, "mcp_writer")
      MAPS_BUCKET                  = aws_s3_bucket.data.bucket
      MAP_VIEWER_URL               = "" # relative /maps/... links, served by the chat app on the same origin
      MAP_BASEMAP                  = var.map_basemap
      MCP_STATELESS                = "1"
      MCP_FORWARDED_AUTH           = "1"
      MCP_DNS_REBINDING_PROTECTION = "0" # IAM-only function URL; see server.py
      MCP_PUBLIC_URL               = "https://geo-mcp.invalid/mcp"
      CRASH_EPDO_WEIGHTS           = var.crash_epdo_weights
    }
  }

  lifecycle {
    ignore_changes = [image_uri]
  }

  depends_on = [aws_cloudwatch_log_group.fn, aws_iam_role_policy_attachment.mcp_vpc]
}

# IAM-only URL; the only principal allowed to call it is the chat Lambda's role (chat policy below).
resource "aws_lambda_function_url" "mcp" {
  function_name      = aws_lambda_function.mcp.function_name
  authorization_type = "AWS_IAM"
}

# ======================================================================== jobs (crash loader)

resource "aws_iam_role" "jobs" {
  name               = "${local.name}-jobs-role"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
}

resource "aws_iam_role_policy_attachment" "jobs_vpc" {
  role       = aws_iam_role.jobs.name
  policy_arn = "arn:${data.aws_partition.current.partition}:iam::aws:policy/service-role/AWSLambdaVPCAccessExecutionRole"
}

resource "aws_iam_role_policy" "jobs" {
  name = "${local.name}-jobs"
  role = aws_iam_role.jobs.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "DbConnectAsLoader"
        Effect   = "Allow"
        Action   = "rds-db:connect"
        Resource = "${local.dbuser_arn}/crash_loader"
      },
      {
        Sid      = "ReadImports"
        Effect   = "Allow"
        Action   = "s3:GetObject"
        Resource = "${aws_s3_bucket.data.arn}/import/*"
      },
      {
        Sid       = "ListImports"
        Effect    = "Allow"
        Action    = "s3:ListBucket"
        Resource  = aws_s3_bucket.data.arn
        Condition = { StringLike = { "s3:prefix" = "import/*" } }
      }
    ]
  })
}

resource "aws_lambda_function" "jobs" {
  function_name = local.lambdas.jobs
  role          = aws_iam_role.jobs.arn
  package_type  = "Image"
  image_uri     = var.jobs_image
  architectures = ["x86_64"]
  memory_size   = 2048
  timeout       = 900

  vpc_config {
    subnet_ids         = aws_subnet.private[*].id
    security_group_ids = [aws_security_group.lambda.id]
  }

  environment {
    variables = {
      DB_IAM_AUTH        = "1"
      DATABASE_URL_ADMIN = format(local.db_url, "crash_loader")
      IMPORT_BUCKET      = aws_s3_bucket.data.bucket
    }
  }

  lifecycle {
    ignore_changes = [image_uri]
  }

  depends_on = [aws_cloudwatch_log_group.fn, aws_iam_role_policy_attachment.jobs_vpc]
}

# ======================================================================== chat (UI + agent)

resource "aws_iam_role" "chat" {
  name               = "${local.name}-chat-role"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
}

resource "aws_iam_role_policy_attachment" "chat_logs" {
  role       = aws_iam_role.chat.name
  policy_arn = "arn:${data.aws_partition.current.partition}:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

resource "aws_iam_role_policy" "chat" {
  name = "${local.name}-chat"
  role = aws_iam_role.chat.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        # Cross-region inference profile + the base model in the regions it routes to.
        Sid    = "BedrockOneModel"
        Effect = "Allow"
        Action = ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"]
        Resource = [
          "arn:${data.aws_partition.current.partition}:bedrock:${local.region}:${local.account}:inference-profile/${var.bedrock_model_id}",
          "arn:${data.aws_partition.current.partition}:bedrock:*::foundation-model/${var.bedrock_foundation_model}",
        ]
      },
      {
        Sid      = "CallGeoMcp"
        Effect   = "Allow"
        Action   = ["lambda:InvokeFunctionUrl", "lambda:InvokeFunction"]
        Resource = aws_lambda_function.mcp.arn
      },
      {
        Sid      = "Sessions"
        Effect   = "Allow"
        Action   = ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:DeleteItem"]
        Resource = aws_dynamodb_table.sessions.arn
      },
      {
        Sid      = "ServeMapsToSignedInUsers"
        Effect   = "Allow"
        Action   = "s3:GetObject"
        Resource = "${aws_s3_bucket.data.arn}/maps/*"
      }
    ]
  })
}

resource "aws_lambda_function" "chat" {
  function_name = local.lambdas.chat
  role          = aws_iam_role.chat.arn
  package_type  = "Image"
  image_uri     = var.chat_image
  architectures = ["x86_64"]
  memory_size   = 512
  timeout       = 300 # a multi-step answer: several model calls + tool calls

  environment {
    variables = {
      AUTH_PROVIDER     = "cognito"
      COGNITO_CLIENT_ID = aws_cognito_user_pool_client.chat.id
      SESSION_TABLE     = aws_dynamodb_table.sessions.name
      MAPS_BUCKET       = aws_s3_bucket.data.bucket
      MAP_VIEWER_ORIGIN = "" # maps are same-origin (/maps/...)
      MAP_TILE_ORIGIN   = var.map_basemap == "osm" ? "https://tile.openstreetmap.org" : ""
      COOKIE_SECURE     = "true"
      LLM_PROVIDER      = "bedrock"
      BEDROCK_MODEL_ID  = var.bedrock_model_id
      MCP_URL           = "${aws_lambda_function_url.mcp.function_url}mcp"
      MCP_AUTH_MODE     = "sigv4"
      MAX_STEPS         = "10"
    }
  }

  lifecycle {
    ignore_changes = [image_uri]
  }

  depends_on = [aws_cloudwatch_log_group.fn, aws_iam_role_policy_attachment.chat_logs]
}

resource "aws_lambda_function_url" "chat" {
  function_name      = aws_lambda_function.chat.function_name
  authorization_type = "AWS_IAM"         # only CloudFront (OAC) can call it
  invoke_mode        = "RESPONSE_STREAM" # progress events stream to the browser
}

resource "aws_lambda_permission" "cloudfront_url" {
  statement_id           = "AllowCloudFrontInvokeFunctionUrl"
  action                 = "lambda:InvokeFunctionUrl"
  function_name          = aws_lambda_function.chat.function_name
  principal              = "cloudfront.amazonaws.com"
  source_arn             = aws_cloudfront_distribution.main.arn
  function_url_auth_type = "AWS_IAM"
}

resource "aws_lambda_permission" "cloudfront_invoke" {
  statement_id  = "AllowCloudFrontInvokeFunction"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.chat.function_name
  principal     = "cloudfront.amazonaws.com"
  source_arn    = aws_cloudfront_distribution.main.arn
}
