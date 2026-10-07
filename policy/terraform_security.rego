package main

# Policy-as-code for `terraform show -json` plan output, run by Conftest in CI.
# deny = blocks the PR, warn = reported only. Tests: policy/terraform_security_test.rego
#   conftest verify -p policy

import rego.v1

# Resources being created or updated (deletes are ignored)
changes contains rc if {
	some rc in input.resource_changes
	some action in rc.change.actions
	action in {"create", "update"}
}

# ---- TLS -------------------------------------------------------------------------------

outdated_tls := {"ELBSecurityPolicy-2016-08", "ELBSecurityPolicy-TLS-1-0-2015-04"}

deny contains msg if {
	some rc in changes
	rc.type == "aws_lb_listener"
	rc.change.after.protocol == "HTTPS"
	rc.change.after.ssl_policy in outdated_tls
	msg := sprintf("%v uses outdated TLS policy '%v'; use ELBSecurityPolicy-TLS13-1-2-2021-06", [rc.address, rc.change.after.ssl_policy])
}

# ---- Network ---------------------------------------------------------------------------

# Only the load balancer's web ports may be open to the internet.
public_ports := {80, 443}

deny contains msg if {
	some rc in changes
	rc.type == "aws_security_group"
	some rule in rc.change.after.ingress
	"0.0.0.0/0" in rule.cidr_blocks
	some port in numbers.range(rule.from_port, min([rule.to_port, rule.from_port + 1024]))
	not port in public_ports
	msg := sprintf("%v allows port %v from the whole internet; only 80/443 may be public", [rc.address, port])
}

deny contains msg if {
	some rc in changes
	rc.type == "aws_security_group"
	some rule in rc.change.after.ingress
	"0.0.0.0/0" in rule.cidr_blocks
	rule.protocol == "-1"
	msg := sprintf("%v allows all protocols from the whole internet", [rc.address])
}

# ---- S3 --------------------------------------------------------------------------------

deny contains msg if {
	some rc in changes
	rc.type == "aws_s3_bucket_public_access_block"
	some setting in ["block_public_acls", "block_public_policy", "ignore_public_acls", "restrict_public_buckets"]
	rc.change.after[setting] != true
	msg := sprintf("%v: %v must be true", [rc.address, setting])
}

write_actions := {"s3:*", "*", "s3:PutObject", "s3:DeleteObject", "s3:PutBucketPolicy", "s3:PutObjectAcl"}

deny contains msg if {
	some rc in changes
	rc.type == "aws_s3_bucket_policy"
	is_string(rc.change.after.policy)
	some stmt in as_list(json.unmarshal(rc.change.after.policy).Statement)
	stmt.Effect == "Allow"
	public_principal(stmt.Principal)
	some action in as_list(stmt.Action)
	action in write_actions
	msg := sprintf("%v lets anyone %v", [rc.address, action])
}

deny contains msg if {
	some rc in changes
	rc.type == "aws_s3_bucket_acl"
	rc.change.after.acl in {"public-read-write", "authenticated-read"}
	msg := sprintf("%v uses canned ACL '%v'", [rc.address, rc.change.after.acl])
}

# ---- IAM -------------------------------------------------------------------------------

# GitHub OIDC trust must name exact branches/events, never "repo:...:*".
deny contains msg if {
	some rc in changes
	rc.type == "aws_iam_role"
	is_string(rc.change.after.assume_role_policy)
	some stmt in as_list(json.unmarshal(rc.change.after.assume_role_policy).Statement)
	some op in ["StringLike", "StringEquals"]
	some sub in as_list(stmt.Condition[op]["token.actions.githubusercontent.com:sub"])
	endswith(sub, ":*")
	msg := sprintf("%v trusts every branch and event of %v; list exact refs (e.g. ref:refs/heads/main)", [rc.address, trim_suffix(sub, ":*")])
}

deny contains msg if {
	some rc in changes
	rc.type == "aws_iam_role"
	is_string(rc.change.after.assume_role_policy)
	some stmt in as_list(json.unmarshal(rc.change.after.assume_role_policy).Statement)
	stmt.Effect == "Allow"
	public_principal(stmt.Principal)
	msg := sprintf("%v can be assumed by anyone (Principal *)", [rc.address])
}

deny contains msg if {
	some rc in changes
	rc.type in {"aws_iam_role_policy", "aws_iam_policy"}
	is_string(rc.change.after.policy)
	some stmt in as_list(json.unmarshal(rc.change.after.policy).Statement)
	stmt.Effect == "Allow"
	"*" in as_list(stmt.Action)
	msg := sprintf("%v grants Action \"*\" (full admin)", [rc.address])
}

# ---- Lambda ----------------------------------------------------------------------------

deny contains msg if {
	some rc in changes
	rc.type == "aws_lambda_function_url"
	rc.change.after.authorization_type == "NONE"
	msg := sprintf("%v is open to the internet (authorization_type NONE); use AWS_IAM behind CloudFront", [rc.address])
}

# ---- Hygiene (warnings) ----------------------------------------------------------------

warn contains msg if {
	some rc in changes
	rc.type == "aws_ecr_repository"
	not scan_on_push(rc.change.after)
	msg := sprintf("%v: turn on image scanning on push", [rc.address])
}

warn contains msg if {
	some rc in changes
	rc.type == "aws_cloudwatch_log_group"
	not rc.change.after.retention_in_days
	msg := sprintf("%v keeps logs forever; set retention_in_days", [rc.address])
}

# ---- Helpers ---------------------------------------------------------------------------

as_list(x) := x if is_array(x)

as_list(x) := [x] if not is_array(x)

public_principal("*")

public_principal(p) if p.AWS == "*"

public_principal(p) if "*" in p.AWS

scan_on_push(after) if {
	some cfg in after.image_scanning_configuration
	cfg.scan_on_push == true
}
