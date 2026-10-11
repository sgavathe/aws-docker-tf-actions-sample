# Read-only role that GitHub Actions assumes (via OIDC) to run Prowler.
#
# Reuses the GitHub OIDC provider your deploy workflow already created in this
# account; it is only looked up here, never created or changed.

data "aws_iam_openid_connect_provider" "github" {
  url = "https://token.actions.githubusercontent.com"
}

data "aws_partition" "current" {}

data "aws_iam_policy_document" "trust" {
  statement {
    sid     = "GitHubOIDC"
    effect  = "Allow"
    actions = ["sts:AssumeRoleWithWebIdentity"]

    principals {
      type        = "Federated"
      identifiers = [data.aws_iam_openid_connect_provider.github.arn]
    }

    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:aud"
      values   = ["sts.amazonaws.com"]
    }

    # Only this repo, only the listed refs (main by default).
    condition {
      test     = "StringLike"
      variable = "token.actions.githubusercontent.com:sub"
      values   = [for ref in var.allowed_refs : "repo:${var.github_repo}:${ref}"]
    }
  }
}

resource "aws_iam_role" "prowler" {
  name                 = var.role_name
  description          = "Read-only role for Prowler scans from GitHub Actions (${var.github_repo})"
  assume_role_policy   = data.aws_iam_policy_document.trust.json
  max_session_duration = var.max_session_duration
}

# AWS-managed read-only policies Prowler documents as its baseline.
resource "aws_iam_role_policy_attachment" "security_audit" {
  role       = aws_iam_role.prowler.name
  policy_arn = "arn:${data.aws_partition.current.partition}:iam::aws:policy/SecurityAudit"
}

resource "aws_iam_role_policy_attachment" "view_only" {
  role       = aws_iam_role.prowler.name
  policy_arn = "arn:${data.aws_partition.current.partition}:iam::aws:policy/job-function/ViewOnlyAccess"
}

# Prowler's "additions" policy (permissions/prowler-additions-policy.json in
# the Prowler repo), minus securityhub:BatchImportFindings, which is a write
# action only needed for --security-hub output. Everything here is read-only.
#
# Note: lambda:GetFunction*, ecr:BatchGetImage and ecr:GetDownloadUrlForLayer
# let Prowler download function code and image layers to look for hard-coded
# secrets. Remove them if you'd rather skip those checks.
data "aws_iam_policy_document" "prowler_additions" {
  statement {
    sid    = "AllowMoreReadOnly"
    effect = "Allow"
    actions = [
      "account:Get*",
      "amplify:ListApps",
      "amplify:ListBranches",
      "appstream:Describe*",
      "appstream:List*",
      "backup:Get*",
      "backup:List*",
      "bedrock:Get*",
      "bedrock:List*",
      "cloudtrail:GetInsightSelectors",
      "codeartifact:List*",
      "codebuild:BatchGet*",
      "codebuild:ListReportGroups",
      "cognito-idp:GetUserPoolMfaConfig",
      "datapipeline:DescribePipelines",
      "datapipeline:GetPipelineDefinition",
      "datapipeline:ListPipelines",
      "dlm:Get*",
      "drs:Describe*",
      "ds:Describe*",
      "ds:Get*",
      "ds:List*",
      "dynamodb:GetResourcePolicy",
      "ec2:GetEbsEncryptionByDefault",
      "ec2:GetInstanceMetadataDefaults",
      "ec2:GetSnapshotBlockPublicAccessState",
      "ecr:BatchGetImage",
      "ecr:Describe*",
      "ecr:GetDownloadUrlForLayer",
      "ecr:GetRegistryScanningConfiguration",
      "elasticfilesystem:DescribeBackupPolicy",
      "glue:GetConnections",
      "glue:GetMLTransforms",
      "glue:GetSecurityConfiguration*",
      "glue:SearchTables",
      "inspector2:BatchGetFindingDetails",
      "lambda:GetFunction*",
      "lambda:GetLayerVersion",
      "lightsail:GetRelationalDatabases",
      "logs:FilterLogEvents",
      "macie2:GetAutomatedDiscoveryConfiguration",
      "macie2:GetMacieSession",
      "rolesanywhere:ListProfiles",
      "rolesanywhere:ListTagsForResource",
      "rolesanywhere:ListTrustAnchors",
      "s3:GetAccountPublicAccessBlock",
      "s3:GetObjectAcl",
      "s3:ListBucket",
      "securityhub:GetFindings",
      "servicecatalog:Describe*",
      "servicecatalog:List*",
      "shield:DescribeProtection",
      "shield:GetSubscriptionState",
      "ssm-incidents:List*",
      "ssm:GetDocument",
      "states:ListTagsForResource",
      "support:Describe*",
      "tag:GetTagKeys",
      "wellarchitected:List*",
    ]
    resources = ["*"]
  }

  statement {
    sid     = "AllowAPIGatewayReadOnly"
    effect  = "Allow"
    actions = ["apigateway:GET"]
    resources = [
      "arn:${data.aws_partition.current.partition}:apigateway:*::/restapis/*",
      "arn:${data.aws_partition.current.partition}:apigateway:*::/apis/*",
      "arn:${data.aws_partition.current.partition}:apigateway:*::/domainnames",
      "arn:${data.aws_partition.current.partition}:apigateway:*::/domainnames/*",
    ]
  }
}

resource "aws_iam_role_policy" "prowler_additions" {
  name   = "prowler-additions-readonly"
  role   = aws_iam_role.prowler.id
  policy = data.aws_iam_policy_document.prowler_additions.json
}
