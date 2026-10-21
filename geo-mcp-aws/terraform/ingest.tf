# Daily crash sync from VDOT's public ArcGIS FeatureServer (geo_mcp/ingest_arcgis.py).
#
#   EventBridge Scheduler (daily) -> ingest Lambda (no VPC: needs the internet, never touches the DB)
#       pages the layer -> s3://<bucket>/import/arcgis/<run>/part-NNNNN.geojson
#       -> invokes the jobs Lambda (in the VPC) once to upsert every part into Aurora.
# Skips the download when the layer's dataLastEditDate hasn't changed. Same image as jobs.

variable "crash_source_url" {
  description = "ArcGIS FeatureServer layer with the VDOT crash records (public)"
  type        = string
  default     = "https://services.arcgis.com/p5v98VHDX9Atv3l7/arcgis/rest/services/Full_Crash/FeatureServer/0"
}

variable "crash_sync_schedule" {
  description = "When the daily sync runs (EventBridge Scheduler cron, America/New_York)"
  type        = string
  default     = "cron(17 5 * * ? *)" # 05:17 ET, before the workday
}

variable "crash_sync_enabled" {
  type    = bool
  default = true
}

resource "aws_cloudwatch_log_group" "ingest" {
  name              = "/aws/lambda/${local.name}-ingest"
  retention_in_days = var.log_retention_days
}

resource "aws_iam_role" "ingest" {
  name               = "${local.name}-ingest-role"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
}

resource "aws_iam_role_policy_attachment" "ingest_logs" {
  role       = aws_iam_role.ingest.name
  policy_arn = "arn:${data.aws_partition.current.partition}:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

resource "aws_iam_role_policy" "ingest" {
  name = "${local.name}-ingest"
  role = aws_iam_role.ingest.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "WriteSyncParts"
        Effect   = "Allow"
        Action   = ["s3:PutObject", "s3:GetObject"]
        Resource = "${aws_s3_bucket.data.arn}/import/arcgis/*"
      },
      {
        Sid      = "StartLoadAndContinue"
        Effect   = "Allow"
        Action   = "lambda:InvokeFunction"
        Resource = [aws_lambda_function.jobs.arn, "arn:${data.aws_partition.current.partition}:lambda:${local.region}:${local.account}:function:${local.name}-ingest"]
      }
    ]
  })
}

resource "aws_lambda_function" "ingest" {
  function_name = "${local.name}-ingest"
  role          = aws_iam_role.ingest.arn
  package_type  = "Image"
  image_uri     = var.jobs_image # same image as the loader, different handler
  architectures = ["x86_64"]
  memory_size   = 1024
  timeout       = 900

  image_config {
    command = ["geo_mcp.ingest_arcgis.handler"]
  }

  environment {
    variables = {
      CRASH_SOURCE_URL = var.crash_source_url
      IMPORT_BUCKET    = aws_s3_bucket.data.bucket
      JOBS_FUNCTION    = aws_lambda_function.jobs.function_name
    }
  }

  lifecycle {
    ignore_changes = [image_uri]
  }

  depends_on = [aws_cloudwatch_log_group.ingest, aws_iam_role_policy_attachment.ingest_logs]
}

# ---- Schedule ----

data "aws_iam_policy_document" "scheduler_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["scheduler.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [local.account]
    }
  }
}

resource "aws_iam_role" "scheduler" {
  name               = "${local.name}-scheduler-role"
  assume_role_policy = data.aws_iam_policy_document.scheduler_assume.json
}

resource "aws_iam_role_policy" "scheduler" {
  name = "${local.name}-scheduler"
  role = aws_iam_role.scheduler.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid      = "InvokeIngestOnly"
      Effect   = "Allow"
      Action   = "lambda:InvokeFunction"
      Resource = aws_lambda_function.ingest.arn
    }]
  })
}

resource "aws_scheduler_schedule" "crash_sync" {
  name                         = "${local.name}-crash-sync"
  schedule_expression          = var.crash_sync_schedule
  schedule_expression_timezone = "America/New_York"
  state                        = var.crash_sync_enabled ? "ENABLED" : "DISABLED"

  flexible_time_window {
    mode = "OFF"
  }

  target {
    arn      = aws_lambda_function.ingest.arn
    role_arn = aws_iam_role.scheduler.arn
    input    = jsonencode({})

    retry_policy {
      maximum_retry_attempts = 2
    }
  }
}
