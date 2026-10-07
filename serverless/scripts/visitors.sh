#!/usr/bin/env bash
# Daily traffic for map.spatialenable.com over the last 7 days
set -euo pipefail
R=us-east-1
START=$(date -u -v-7d +%Y-%m-%dT00:00:00Z)   # macOS date
END=$(date -u +%Y-%m-%dT%H:%M:%SZ)

DIST=$(aws cloudfront list-distributions --query \
  "DistributionList.Items[?contains(Aliases.Items || \`[]\`, 'map.spatialenable.com')].Id | [0]" --output text)
echo "Distribution: $DIST"

echo; echo "CloudFront requests per day (pages, scripts, map files, API calls):"
aws cloudwatch get-metric-statistics --region $R --namespace AWS/CloudFront --metric-name Requests \
  --dimensions Name=DistributionId,Value=$DIST Name=Region,Value=Global \
  --start-time $START --end-time $END --period 86400 --statistics Sum \
  --query 'sort_by(Datapoints,&Timestamp)[].[Timestamp,Sum]' --output text

echo; echo "API calls per day (roughly one per visit plus one per area drawn):"
aws cloudwatch get-metric-statistics --region $R --namespace AWS/Lambda --metric-name Invocations \
  --dimensions Name=FunctionName,Value=geo-devops-demo-sls-api \
  --start-time $START --end-time $END --period 86400 --statistics Sum \
  --query 'sort_by(Datapoints,&Timestamp)[].[Timestamp,Sum]' --output text
