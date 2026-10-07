#!/usr/bin/env bash
# Read-only: reports any S3 bucket that could be read or written by the public.
export AWS_PAGER=""
ACCT=$(aws sts get-caller-identity --query Account --output text)

echo "Account-level Block Public Access ($ACCT):"
aws s3control get-public-access-block --account-id "$ACCT" \
  --query 'PublicAccessBlockConfiguration' --output text 2>/dev/null || echo "  NOT SET (buckets rely on their own settings)"
echo

printf "%-50s %-12s %-14s %-12s\n" BUCKET BLOCK_ALL POLICY_PUBLIC ACL_PUBLIC
for b in $(aws s3api list-buckets --query 'Buckets[].Name' --output text); do
  block=$(aws s3api get-public-access-block --bucket "$b" \
    --query 'PublicAccessBlockConfiguration.[BlockPublicAcls,IgnorePublicAcls,BlockPublicPolicy,RestrictPublicBuckets]' \
    --output text 2>/dev/null | tr '\t' ' ')
  [ "$block" = "True True True True" ] && block=yes || block="NO(${block:-none})"
  pol=$(aws s3api get-bucket-policy-status --bucket "$b" --query 'PolicyStatus.IsPublic' --output text 2>/dev/null || echo "no-policy")
  acl=$(aws s3api get-bucket-acl --bucket "$b" \
    --query "Grants[?Grantee.URI=='http://acs.amazonaws.com/groups/global/AllUsers' || Grantee.URI=='http://acs.amazonaws.com/groups/global/AuthenticatedUsers'].Permission" \
    --output text 2>/dev/null)
  printf "%-50s %-12s %-14s %-12s\n" "$b" "$block" "$pol" "${acl:-none}"
done
