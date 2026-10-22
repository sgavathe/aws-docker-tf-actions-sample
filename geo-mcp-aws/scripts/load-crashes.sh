#!/usr/bin/env bash
# Load a VDOT crash GeoJSON into Aurora: upload to s3://<bucket>/import/, run the loader Lambda
# (inside the VPC) and follow its log until it finishes.
#   geo-mcp-aws/scripts/load-crashes.sh ~/Downloads/CrashData.geojson [--replace]
# Only public data (e.g. VDOT CrashData) belongs here.
set -euo pipefail
FILE="${1:?usage: load-crashes.sh <file.geojson> [--replace]}"
REPLACE=false
[ "${2:-}" = "--replace" ] && REPLACE=true
TF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../terraform" && pwd)"
BUCKET="$(terraform -chdir="$TF_DIR" output -raw data_bucket)"
FN="$(terraform -chdir="$TF_DIR" output -json function_names | python3 -c 'import json,sys; print(json.load(sys.stdin)["jobs"])')"
NAME="$(basename "$FILE" | tr ' ' '_')"
case "$NAME" in *.geojson|*.json) ;; *) echo "expected a .geojson file"; exit 2 ;; esac

echo "==> upload $(du -h "$FILE" | cut -f1) to s3://$BUCKET/import/$NAME"
aws s3 cp "$FILE" "s3://$BUCKET/import/$NAME" --only-show-errors

echo "==> start loader ($FN); about 5-15 minutes for 1 million crashes"
aws lambda invoke --function-name "$FN" --invocation-type Event \
  --cli-binary-format raw-in-base64-out \
  --payload "{\"job\":\"load_crashes\",\"s3_uri\":\"s3://$BUCKET/import/$NAME\",\"replace\":$REPLACE}" \
  /dev/null > /dev/null

echo "==> following /aws/lambda/$FN (stops at the summary line)"
aws logs tail "/aws/lambda/$FN" --follow --since 1m --format short \
  | awk '{ print; fflush() } /crash.crashes now holds|not found:|Error|Traceback/ { exit }'
