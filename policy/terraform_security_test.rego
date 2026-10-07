package main

import rego.v1

# Builds a one-resource plan the way `terraform show -json` shapes it.
plan(type, after) := {"resource_changes": [{
	"address": sprintf("%v.test", [type]),
	"type": type,
	"change": {"actions": ["create"], "after": after},
}]}

trust(sub) := json.marshal({"Statement": [{
	"Effect": "Allow",
	"Principal": {"Federated": "arn:aws:iam::123456789012:oidc-provider/token.actions.githubusercontent.com"},
	"Action": "sts:AssumeRoleWithWebIdentity",
	"Condition": {"StringLike": {"token.actions.githubusercontent.com:sub": sub}},
}]})

test_oidc_wildcard_denied if {
	count(deny) > 0 with input as plan("aws_iam_role", {"assume_role_policy": trust("repo:me/app:*")})
}

test_oidc_wildcard_in_list_denied if {
	count(deny) > 0 with input as plan("aws_iam_role", {"assume_role_policy": trust(["repo:me/app:ref:refs/heads/main", "repo:me/app:*"])})
}

test_oidc_main_only_allowed if {
	count(deny) == 0 with input as plan("aws_iam_role", {"assume_role_policy": trust(["repo:me/app:ref:refs/heads/main", "repo:me/app:pull_request"])})
}

test_ssh_open_to_world_denied if {
	count(deny) > 0 with input as plan("aws_security_group", {"ingress": [{"from_port": 22, "to_port": 22, "protocol": "tcp", "cidr_blocks": ["0.0.0.0/0"]}]})
}

test_https_open_to_world_allowed if {
	count(deny) == 0 with input as plan("aws_security_group", {"ingress": [{"from_port": 443, "to_port": 443, "protocol": "tcp", "cidr_blocks": ["0.0.0.0/0"]}]})
}

test_all_traffic_open_denied if {
	count(deny) > 0 with input as plan("aws_security_group", {"ingress": [{"from_port": 0, "to_port": 0, "protocol": "-1", "cidr_blocks": ["0.0.0.0/0"]}]})
}

test_public_access_block_partial_denied if {
	count(deny) > 0 with input as plan("aws_s3_bucket_public_access_block", {
		"block_public_acls": true, "block_public_policy": false,
		"ignore_public_acls": true, "restrict_public_buckets": true,
	})
}

test_public_write_bucket_policy_denied if {
	policy := json.marshal({"Statement": [{"Effect": "Allow", "Principal": "*", "Action": ["s3:GetObject", "s3:PutObject"], "Resource": "arn:aws:s3:::b/*"}]})
	count(deny) > 0 with input as plan("aws_s3_bucket_policy", {"policy": policy})
}

test_public_read_bucket_policy_allowed if {
	policy := json.marshal({"Statement": [{"Effect": "Allow", "Principal": "*", "Action": "s3:GetObject", "Resource": "arn:aws:s3:::b/*"}]})
	count(deny) == 0 with input as plan("aws_s3_bucket_policy", {"policy": policy})
}

test_admin_policy_denied if {
	policy := json.marshal({"Statement": [{"Effect": "Allow", "Action": "*", "Resource": "*"}]})
	count(deny) > 0 with input as plan("aws_iam_role_policy", {"policy": policy})
}

test_open_function_url_denied if {
	count(deny) > 0 with input as plan("aws_lambda_function_url", {"authorization_type": "NONE"})
}

test_old_tls_denied if {
	count(deny) > 0 with input as plan("aws_lb_listener", {"protocol": "HTTPS", "ssl_policy": "ELBSecurityPolicy-2016-08"})
}

test_ecr_without_scan_warns if {
	count(warn) > 0 with input as plan("aws_ecr_repository", {"image_scanning_configuration": [{"scan_on_push": false}]})
}

test_deletes_ignored if {
	count(deny) == 0 with input as {"resource_changes": [{
		"address": "aws_iam_role.old", "type": "aws_iam_role",
		"change": {"actions": ["delete"], "after": null},
	}]}
}
