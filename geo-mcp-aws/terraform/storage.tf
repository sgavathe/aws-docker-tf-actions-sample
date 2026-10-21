# One private bucket for this stack:
#   maps/      generated web maps + maps/exports/ GeoJSON (written by geo-mcp, read by chat for signed-in users)
#   import/    crash GeoJSON uploads for the loader job
#   vendor/    MapLibre / pmtiles / basemap JS+CSS (public libraries, served by CloudFront)
#   basemap/   optional offline .pmtiles basemap (served by CloudFront)

resource "aws_s3_bucket" "data" {
  bucket        = "${local.name}-data-${local.account}"
  force_destroy = true
}

resource "aws_s3_bucket_public_access_block" "data" {
  bucket                  = aws_s3_bucket.data.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_ownership_controls" "data" {
  bucket = aws_s3_bucket.data.id
  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "data" {
  bucket = aws_s3_bucket.data.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_versioning" "data" {
  bucket = aws_s3_bucket.data.id
  versioning_configuration {
    status = "Enabled"
  }
}

# Generated maps and exports are disposable; crash uploads are kept until replaced.
resource "aws_s3_bucket_lifecycle_configuration" "data" {
  bucket = aws_s3_bucket.data.id

  rule {
    id     = "expire-generated-maps"
    status = "Enabled"
    filter {
      prefix = "maps/"
    }
    expiration {
      days = 30
    }
    noncurrent_version_expiration {
      noncurrent_days = 1
    }
  }

  rule {
    id     = "old-versions-and-uploads"
    status = "Enabled"
    filter {}
    noncurrent_version_expiration {
      noncurrent_days = 7
    }
    abort_incomplete_multipart_upload {
      days_after_initiation = 1
    }
  }
}

data "aws_iam_policy_document" "data_bucket" {
  # CloudFront may read the public map libraries only, never maps/ or import/.
  statement {
    sid       = "CloudFrontReadsLibrariesOnly"
    actions   = ["s3:GetObject"]
    resources = ["${aws_s3_bucket.data.arn}/vendor/*", "${aws_s3_bucket.data.arn}/basemap/*"]
    principals {
      type        = "Service"
      identifiers = ["cloudfront.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "AWS:SourceArn"
      values   = [aws_cloudfront_distribution.main.arn]
    }
  }

  # TLS only, for every caller.
  statement {
    sid       = "DenyInsecureTransport"
    effect    = "Deny"
    actions   = ["s3:*"]
    resources = [aws_s3_bucket.data.arn, "${aws_s3_bucket.data.arn}/*"]
    principals {
      type        = "*"
      identifiers = ["*"]
    }
    condition {
      test     = "Bool"
      variable = "aws:SecureTransport"
      values   = ["false"]
    }
  }
}

resource "aws_s3_bucket_policy" "data" {
  bucket     = aws_s3_bucket.data.id
  policy     = data.aws_iam_policy_document.data_bucket.json
  depends_on = [aws_s3_bucket_public_access_block.data]
}

# ---- Sessions ----

resource "aws_dynamodb_table" "sessions" {
  name         = "${local.name}-sessions"
  billing_mode = "PAY_PER_REQUEST" # pennies at demo volume
  hash_key     = "sid"             # SHA-256 of the cookie, never the cookie itself

  attribute {
    name = "sid"
    type = "S"
  }

  ttl {
    attribute_name = "expires"
    enabled        = true
  }

  server_side_encryption {
    enabled = true # AWS managed key (no per-key charge)
  }
}
