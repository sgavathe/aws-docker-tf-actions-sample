locals {
  acct       = "390744232980"
  region     = var.aws_region
  ecr_repos  = "arn:aws:ecr:${local.region}:${local.acct}:repository/${var.project_name}-*"
  elb        = "arn:aws:elasticloadbalancing:${local.region}:${local.acct}"
  alb_name   = "${var.project_name}-alb"
  ecs        = "arn:aws:ecs:${local.region}:${local.acct}"
}

# Execution role: used by ECS agent to pull images from ECR and write logs.
resource "aws_iam_role" "ecs_execution" {
  name = "${var.project_name}-ecs-execution-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Action    = "sts:AssumeRole"
      Effect    = "Allow"
      Principal = { Service = "ecs-tasks.amazonaws.com" }
    }]
  })
}

resource "aws_iam_role_policy_attachment" "ecs_execution_managed" {
  role       = aws_iam_role.ecs_execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

# Task role: used by application code inside the container for any AWS API calls.
# Kept minimal/least-privilege - add specific permissions as the app needs them.
resource "aws_iam_role" "ecs_task" {
  name = "${var.project_name}-ecs-task-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Action    = "sts:AssumeRole"
      Effect    = "Allow"
      Principal = { Service = "ecs-tasks.amazonaws.com" }
    }]
  })
}

# GitHub Actions OIDC federation - lets CI assume an AWS role without long-lived
# access keys stored as GitHub secrets.
resource "aws_iam_openid_connect_provider" "github" {
  url             = "https://token.actions.githubusercontent.com"
  client_id_list  = ["sts.amazonaws.com"]
  thumbprint_list = ["6938fd4d98bab03faadb97b34396831e3780aea1"]
}

resource "aws_iam_role" "github_actions" {
  name = "github-actions-ecs-deploy-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Action = "sts:AssumeRoleWithWebIdentity"
      Effect = "Allow"
      Principal = {
        Federated = aws_iam_openid_connect_provider.github.arn
      }
      Condition = {
        StringEquals = {
          "token.actions.githubusercontent.com:aud" = "sts.amazonaws.com"
        }
        StringLike = {
          # Pushes to main deploy; pull requests only run terraform plan.
          "token.actions.githubusercontent.com:sub" = [
            "repo:sgavathe@6047188/aws-docker-tf-actions-sample@1373663842:ref:refs/heads/main",
            "repo:sgavathe@6047188/aws-docker-tf-actions-sample@1373663842:pull_request",
          ]
        }
      }
    }]
  })
}

resource "aws_iam_role_policy" "github_actions_deploy" {
  name = "${var.project_name}-github-actions-deploy-policy"
  role = aws_iam_role.github_actions.id

    policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      # --- Can't be scoped: ECR login token is account-level ---
      {
        Sid      = "EcrLogin"
        Effect   = "Allow"
        Action   = "ecr:GetAuthorizationToken"
        Resource = "*"
      },
      # --- Push images + repo settings: only this project's repos ---
      {
        Sid    = "EcrRepos"
        Effect = "Allow"
        Action = [
          "ecr:BatchCheckLayerAvailability",
          "ecr:GetDownloadUrlForLayer",
          "ecr:BatchGetImage",
          "ecr:PutImage",
          "ecr:InitiateLayerUpload",
          "ecr:UploadLayerPart",
          "ecr:CompleteLayerUpload",
          "ecr:PutImageTagMutability",
          "ecr:PutImageScanningConfiguration"
        ]
        Resource = local.ecr_repos
      },
      # --- ECS: only this project's cluster, services, task definitions ---
      {
        Sid    = "EcsProjectResources"
        Effect = "Allow"
        Action = [
          "ecs:UpdateService",
          "ecs:DescribeServices",
          "ecs:UpdateCluster",
          "ecs:UpdateClusterSettings",
          "ecs:TagResource"
        ]
        Resource = [
          "${local.ecs}:cluster/${var.project_name}-cluster",
          "${local.ecs}:service/${var.project_name}-cluster/*",
          "${local.ecs}:task-definition/${var.project_name}-*:*"
        ]
      },
      # --- Can't be scoped: task definition register/describe/deregister are
      #     account-level in ECS. Deregistering a revision does not stop running tasks. ---
      {
        Sid    = "EcsTaskDefinitions"
        Effect = "Allow"
        Action = [
          "ecs:RegisterTaskDefinition",
          "ecs:DescribeTaskDefinition",
          "ecs:DeregisterTaskDefinition"
        ]
        Resource = "*"
      },
      # --- ALB: only this load balancer, its listeners/rules, and our target groups ---
      {
        Sid    = "AlbProjectResources"
        Effect = "Allow"
        Action = [
          "elasticloadbalancing:ModifyLoadBalancerAttributes",
          "elasticloadbalancing:ModifyListener",
          "elasticloadbalancing:DeleteRule",
          "elasticloadbalancing:ModifyRule",
          "elasticloadbalancing:SetRulePriorities",
          "elasticloadbalancing:CreateTargetGroup",
          "elasticloadbalancing:DeleteTargetGroup",
          "elasticloadbalancing:ModifyTargetGroup",
          "elasticloadbalancing:ModifyTargetGroupAttributes",
          "elasticloadbalancing:AddTags"
        ]
        Resource = [
          "${local.elb}:loadbalancer/app/${local.alb_name}/*",
          "${local.elb}:listener/app/${local.alb_name}/*",
          "${local.elb}:listener-rule/app/${local.alb_name}/*",
          "${local.elb}:targetgroup/${var.project_name}-*/*",
          "${local.elb}:targetgroup/fe-*/*"
        ]
      },
      # --- Network: security groups and subnets, but ONLY inside this project's VPC ---
      {
        Sid    = "NetworkInProjectVpc"
        Effect = "Allow"
        Action = [
          "ec2:AuthorizeSecurityGroupIngress",
          "ec2:AuthorizeSecurityGroupEgress",
          "ec2:RevokeSecurityGroupIngress",
          "ec2:RevokeSecurityGroupEgress",
          "ec2:UpdateSecurityGroupRuleDescriptionsIngress",
          "ec2:UpdateSecurityGroupRuleDescriptionsEgress",
          "ec2:ModifySubnetAttribute"
        ]
        Resource = [
          "arn:aws:ec2:${local.region}:${local.acct}:security-group/*",
          "arn:aws:ec2:${local.region}:${local.acct}:subnet/*"
        ]
        Condition = {
          ArnEquals = { "ec2:Vpc" = aws_vpc.main.arn }
        }
      },
      # --- Alarms: only ones named for this project ---
      {
        Sid    = "ProjectAlarms"
        Effect = "Allow"
        Action = [
          "cloudwatch:PutMetricAlarm",
          "cloudwatch:DeleteAlarms",
          "cloudwatch:TagResource",
          "cloudwatch:ListTagsForResource"
        ]
        Resource = "arn:aws:cloudwatch:${local.region}:${local.acct}:alarm:${var.project_name}-*"
      },
      {
        "Sid": "AlarmsRead",
        "Effect": "Allow",
        "Action": "cloudwatch:DescribeAlarms",
        "Resource": "*"
      },
      # --- Pass only the two ECS roles ---
      {
        Sid      = "PassEcsRoles"
        Effect   = "Allow"
        Action   = "iam:PassRole"
        Resource = [aws_iam_role.ecs_execution.arn, aws_iam_role.ecs_task.arn]
      },
      # --- Terraform state bucket only ---
      {
        Sid      = "TfState"
        Effect   = "Allow"
        Action   = ["s3:GetObject", "s3:PutObject", "s3:ListBucket"]
        Resource = [
          "arn:aws:s3:::sgavathe-tfstate-390744232980",
          "arn:aws:s3:::sgavathe-tfstate-390744232980/*"
        ]
      }
    ]
  })
}
