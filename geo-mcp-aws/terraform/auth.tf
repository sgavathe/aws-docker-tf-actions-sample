# Cognito replaces Keycloak in AWS: same two roles (as groups), no server to run.
# Admin-created users only (no self sign-up). See scripts/users.sh.

resource "aws_cognito_user_pool" "main" {
  name                     = "${local.name}-users"
  deletion_protection      = "INACTIVE" # demo stack
  auto_verified_attributes = []

  username_configuration {
    case_sensitive = false
  }

  admin_create_user_config {
    allow_admin_create_user_only = true
  }

  password_policy {
    minimum_length                   = 14
    require_lowercase                = true
    require_uppercase                = true
    require_numbers                  = true
    require_symbols                  = true
    temporary_password_validity_days = 1
  }

  account_recovery_setting {
    recovery_mechanism {
      name     = "admin_only"
      priority = 1
    }
  }
}

resource "aws_cognito_user_group" "reader" {
  name         = "geo-reader"
  user_pool_id = aws_cognito_user_pool.main.id
  description  = "Query and analyze (read-only tools)"
}

resource "aws_cognito_user_group" "writer" {
  name         = "geo-writer"
  user_pool_id = aws_cognito_user_pool.main.id
  description  = "Also make maps, exports and load work layers"
}

# Public client (no secret to store). The chat Lambda signs users in server-side and keeps the
# tokens in its session table; the browser only ever holds an opaque HttpOnly cookie.
resource "aws_cognito_user_pool_client" "chat" {
  name                          = "${local.name}-chat"
  user_pool_id                  = aws_cognito_user_pool.main.id
  generate_secret               = false
  explicit_auth_flows           = ["ALLOW_USER_PASSWORD_AUTH", "ALLOW_REFRESH_TOKEN_AUTH"]
  prevent_user_existence_errors = "ENABLED"
  enable_token_revocation       = true
  access_token_validity         = 60
  id_token_validity             = 60
  refresh_token_validity        = 8

  token_validity_units {
    access_token  = "minutes"
    id_token      = "minutes"
    refresh_token = "hours"
  }
}

locals {
  token_issuer = "https://cognito-idp.${local.region}.amazonaws.com/${aws_cognito_user_pool.main.id}"
}

# The pool's PUBLIC signing keys, read at deploy time and passed to geo-mcp as an env var,
# because that Lambda has no internet route to fetch them. Re-apply after a key rotation.
data "http" "jwks" {
  url = "${local.token_issuer}/.well-known/jwks.json"

  request_headers = {
    Accept = "application/json"
  }

  lifecycle {
    postcondition {
      condition     = self.status_code == 200
      error_message = "Could not read the Cognito JWKS."
    }
  }
}
