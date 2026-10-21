#!/usr/bin/env bash
# Create (or reset) a chat user. The password is typed at the prompt, sent straight to Cognito
# and never written to disk, shell history or the repo.
#   geo-mcp-aws/scripts/users.sh <username> reader|writer
set -euo pipefail
USER_NAME="${1:?usage: users.sh <username> reader|writer}"
LEVEL="${2:?usage: users.sh <username> reader|writer}"
TF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../terraform" && pwd)"
POOL="$(terraform -chdir="$TF_DIR" output -raw user_pool_id)"

case "$LEVEL" in
  reader) GROUPS_FOR_USER="geo-reader" ;;
  writer) GROUPS_FOR_USER="geo-reader geo-writer" ;;
  *) echo "level must be reader or writer"; exit 2 ;;
esac

read -r -s -p "Password for $USER_NAME (14+ chars, upper, lower, number, symbol): " PW; echo
read -r -s -p "Again: " PW2; echo
[ "$PW" = "$PW2" ] || { echo "Passwords differ"; exit 1; }

if ! aws cognito-idp admin-get-user --user-pool-id "$POOL" --username "$USER_NAME" > /dev/null 2>&1; then
  aws cognito-idp admin-create-user --user-pool-id "$POOL" --username "$USER_NAME" \
    --message-action SUPPRESS > /dev/null
fi
# The password goes to the AWS CLI as JSON on stdin, never as a command-line argument
# (arguments are visible to other processes via ps).
PW="$PW" python3 -c 'import json, os, sys
print(json.dumps({"UserPoolId": sys.argv[1], "Username": sys.argv[2], "Password": os.environ["PW"], "Permanent": True}))' \
  "$POOL" "$USER_NAME" | aws cognito-idp admin-set-user-password --cli-input-json file:///dev/stdin
unset PW PW2
for g in $GROUPS_FOR_USER; do
  aws cognito-idp admin-add-user-to-group --user-pool-id "$POOL" --username "$USER_NAME" --group-name "$g"
done
echo "User $USER_NAME ready ($GROUPS_FOR_USER)."
