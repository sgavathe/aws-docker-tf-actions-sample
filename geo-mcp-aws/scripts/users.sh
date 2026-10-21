#!/usr/bin/env bash
# Create (or reset) a chat user. The password is typed at a hidden prompt and goes straight to
# Cognito through boto3: never on a command line, in shell history, on disk or in the repo.
#   geo-mcp-aws/scripts/users.sh <username> reader|writer
set -euo pipefail
USER_NAME="${1:?usage: users.sh <username> reader|writer}"
LEVEL="${2:?usage: users.sh <username> reader|writer}"
TF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../terraform" && pwd)"
POOL="$(terraform -chdir="$TF_DIR" output -raw user_pool_id)"

case "$LEVEL" in
  reader|writer) ;;
  *) echo "level must be reader or writer"; exit 2 ;;
esac

python3 - "$POOL" "$USER_NAME" "$LEVEL" <<'EOF'
import getpass
import sys

import boto3

pool, user, level = sys.argv[1:4]
groups = ["geo-reader"] + (["geo-writer"] if level == "writer" else [])
pw = getpass.getpass(f"Password for {user} (14+ chars, upper, lower, number, symbol): ")
if pw != getpass.getpass("Again: "):
    sys.exit("Passwords differ")
c = boto3.client("cognito-idp")
try:
    c.admin_get_user(UserPoolId=pool, Username=user)
except c.exceptions.UserNotFoundException:
    c.admin_create_user(UserPoolId=pool, Username=user, MessageAction="SUPPRESS")
c.admin_set_user_password(UserPoolId=pool, Username=user, Password=pw, Permanent=True)
for g in groups:
    c.admin_add_user_to_group(UserPoolId=pool, Username=user, GroupName=g)
print(f"User {user} ready ({' '.join(groups)}).")
EOF
