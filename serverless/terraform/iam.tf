# GitHub Actions deploy role for the serverless stack.
#
# Reuses the account's existing GitHub OIDC provider (created by infra/iam.tf; an
# account can only have one per URL) via a data source, so this stack never
# creates or destroys it.
#
# Like the ECS role, this policy covers routine deploys (new image, new frontend
# build, config changes). Creating the stack the first time is done locally with
# admin credentials via serverless/scripts/deploy.sh.

data "aws_iam_openid_connect_provider" "github" {
  url = "https://token.actions.githubusercontent.com"
}

resource "aws_iam_role" "github_deploy" {
  name = "github-actions-serverless-deploy-role"

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

locals {
  zone_arn = "arn:aws:route53:::hostedzone/${data.aws_route53_zone.main.zone_id}"
  cf_arn   = "arn:aws:cloudfront::${local.account}"
}

resource "aws_iam_role_policy" "github_deploy" {
  name = "${local.name}-github-deploy-policy"
  role = aws_iam_role.github_deploy.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "TfState"
        Effect   = "Allow"
        Action   = ["s3:GetObject", "s3:PutObject", "s3:ListBucket"]
        Resource = ["arn:aws:s3:::sgavathe-tfstate-390744232980", "arn:aws:s3:::sgavathe-tfstate-390744232980/*"]
      },
      # --- ECR: login is account-level; everything else only this repo ---
      {
        Sid      = "EcrLogin"
        Effect   = "Allow"
        Action   = "ecr:GetAuthorizationToken"
        Resource = "*"
      },
      {
        Sid    = "EcrRepo"
        Effect = "Allow"
        Action = [
          "ecr:BatchCheckLayerAvailability", "ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer",
          "ecr:InitiateLayerUpload", "ecr:UploadLayerPart", "ecr:CompleteLayerUpload", "ecr:PutImage",
          "ecr:DescribeRepositories", "ecr:DescribeImages", "ecr:ListTagsForResource",
          "ecr:GetLifecyclePolicy", "ecr:GetRepositoryPolicy", "ecr:SetRepositoryPolicy"
        ]
        Resource = aws_ecr_repository.api.arn
      },
      # --- Lambda: only this function ---
      {
        Sid      = "Lambda"
        Effect   = "Allow"
        Action   = ["lambda:Get*", "lambda:List*", "lambda:UpdateFunctionCode", "lambda:UpdateFunctionConfiguration", "lambda:TagResource"]
        Resource = aws_lambda_function.api.arn
      },
      {
        Sid      = "PassLambdaRole"
        Effect   = "Allow"
        Action   = "iam:PassRole"
        Resource = aws_iam_role.lambda.arn
      },
      # --- IAM: read-only on this stack's roles + the OIDC provider (Terraform refresh) ---
      {
        Sid      = "IamRead"
        Effect   = "Allow"
        Action   = ["iam:GetRole", "iam:GetRolePolicy", "iam:ListRolePolicies", "iam:ListAttachedRolePolicies", "iam:GetOpenIDConnectProvider"]
        Resource = [aws_iam_role.lambda.arn, "arn:aws:iam::${local.account}:role/github-actions-serverless-deploy-role", data.aws_iam_openid_connect_provider.github.arn]
      },
      {
        Sid      = "IamList"
        Effect   = "Allow"
        Action   = "iam:ListOpenIDConnectProviders"
        Resource = "*"
      },
      # --- Logs ---
      {
        Sid      = "LogsDescribe"
        Effect   = "Allow"
        Action   = "logs:DescribeLogGroups"
        Resource = "*"
      },
      {
        Sid      = "LogsGroup"
        Effect   = "Allow"
        Action   = ["logs:ListTagsForResource", "logs:ListTagsLogGroup", "logs:PutRetentionPolicy"]
        Resource = "${aws_cloudwatch_log_group.api.arn}*"
      },
      # --- Site bucket: read config + upload the frontend build ---
      {
        Sid      = "SiteBucket"
        Effect   = "Allow"
        Action   = ["s3:Get*", "s3:List*", "s3:PutObject", "s3:DeleteObject"]
        Resource = [aws_s3_bucket.site.arn, "${aws_s3_bucket.site.arn}/*"]
      },
      # --- CloudFront: this distribution, its OACs and function; managed-policy lookups ---
      {
        Sid    = "CloudFrontDistribution"
        Effect = "Allow"
        Action = [
          "cloudfront:GetDistribution", "cloudfront:GetDistributionConfig", "cloudfront:UpdateDistribution",
          "cloudfront:ListTagsForResource", "cloudfront:TagResource",
          "cloudfront:CreateInvalidation", "cloudfront:GetInvalidation"
        ]
        Resource = aws_cloudfront_distribution.site.arn
      },
      {
        Sid    = "CloudFrontRead"
        Effect = "Allow"
        Action = [
          "cloudfront:GetOriginAccessControl", "cloudfront:DescribeFunction", "cloudfront:GetFunction",
          "cloudfront:ListCachePolicies", "cloudfront:GetCachePolicy",
          "cloudfront:ListOriginRequestPolicies", "cloudfront:GetOriginRequestPolicy",
          "cloudfront:ListResponseHeadersPolicies", "cloudfront:GetResponseHeadersPolicy"
        ]
        Resource = "*"
      },
      # --- ACM + Route 53: read for refresh; record changes only in this zone ---
      {
        Sid      = "AcmRead"
        Effect   = "Allow"
        Action   = ["acm:DescribeCertificate", "acm:ListTagsForCertificate"]
        Resource = "arn:aws:acm:${var.aws_region}:${local.account}:certificate/*"
      },
      {
        Sid      = "Route53Zone"
        Effect   = "Allow"
        Action   = ["route53:GetHostedZone", "route53:ListResourceRecordSets", "route53:ChangeResourceRecordSets", "route53:ListTagsForResource"]
        Resource = local.zone_arn
      },
      {
        Sid      = "Route53Read"
        Effect   = "Allow"
        Action   = ["route53:ListHostedZones", "route53:ListHostedZonesByName", "route53:GetChange"]
        Resource = "*"
      }
    ]
  })
}
