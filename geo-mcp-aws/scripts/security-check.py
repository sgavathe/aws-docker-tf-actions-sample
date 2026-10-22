#!/usr/bin/env python3
"""Read-only security check of the AWS account (one region). Changes nothing.

    AWS_PROFILE=sgavathe-udemy python3 geo-mcp-aws/scripts/security-check.py

Prints PASS / WARN / FAIL per finding. WARN = worth a look (may be intended, e.g. a public
website bucket); FAIL = should be fixed. Needs only read permissions.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone

import boto3
from botocore.exceptions import ClientError

REGION = os.environ.get("AWS_REGION", "us-east-1")
counts = {"PASS": 0, "WARN": 0, "FAIL": 0}


def report(level: str, area: str, msg: str) -> None:
    counts[level] += 1
    mark = {"PASS": "  ok ", "WARN": " WARN", "FAIL": " FAIL"}[level]
    print(f"{mark}  {area:<11} {msg}")


def safe(fn, *a, **k):
    try:
        return fn(*a, **k)
    except ClientError as e:
        return e


def check_account(sts, iam, s3control):
    who = sts.get_caller_identity()
    print(f"Account {who['Account']} as {who['Arn']} in {REGION}\n")
    summ = iam.get_account_summary()["SummaryMap"]
    report("PASS" if summ.get("AccountMFAEnabled") else "FAIL", "root", "root MFA " + ("on" if summ.get("AccountMFAEnabled") else "OFF"))
    report("PASS" if not summ.get("AccountAccessKeysPresent") else "FAIL", "root",
           "no root access keys" if not summ.get("AccountAccessKeysPresent") else "ROOT ACCESS KEYS EXIST: delete them")
    bpa = safe(s3control.get_public_access_block, AccountId=who["Account"])
    if isinstance(bpa, ClientError):
        report("WARN", "s3-account", "account-level S3 Block Public Access not set")
    else:
        c = bpa["PublicAccessBlockConfiguration"]
        report("PASS" if all(c.values()) else "WARN", "s3-account",
               "account-level Block Public Access: " + ", ".join(f"{k}={v}" for k, v in c.items()))
    return who["Account"]


def check_iam(iam):
    now = datetime.now(timezone.utc)
    for page in iam.get_paginator("list_users").paginate():
        for u in page["Users"]:
            name = u["UserName"]
            mfa = iam.list_mfa_devices(UserName=name)["MFADevices"]
            has_console = not isinstance(safe(iam.get_login_profile, UserName=name), ClientError)
            if has_console and not mfa:
                report("FAIL", "iam-user", f"{name}: console password without MFA")
            for k in iam.list_access_keys(UserName=name)["AccessKeyMetadata"]:
                age = (now - k["CreateDate"]).days
                lvl = "PASS" if age <= 90 else "WARN"
                report(lvl, "iam-user", f"{name}: access key {k['AccessKeyId'][-4:]} {k['Status']}, {age} days old")


def check_s3(s3):
    for b in s3.list_buckets()["Buckets"]:
        name = b["Name"]
        loc = safe(s3.get_bucket_location, Bucket=name)
        bpa = safe(s3.get_public_access_block, Bucket=name)
        status = safe(s3.get_bucket_policy_status, Bucket=name)
        public = (not isinstance(status, ClientError)) and status["PolicyStatus"]["IsPublic"]
        blocked = (not isinstance(bpa, ClientError)) and all(bpa["PublicAccessBlockConfiguration"].values())
        website = not isinstance(safe(s3.get_bucket_website, Bucket=name), ClientError)
        if public:
            report("WARN" if website else "FAIL", "s3", f"{name}: PUBLIC via bucket policy" + (" (website bucket)" if website else ""))
        elif blocked:
            report("PASS", "s3", f"{name}: private, Block Public Access on")
        else:
            report("WARN", "s3", f"{name}: not public now, but Block Public Access is not fully on")
        pol = safe(s3.get_bucket_policy, Bucket=name)
        tls_only = (not isinstance(pol, ClientError)) and "aws:SecureTransport" in pol["Policy"]
        if not tls_only:
            report("WARN", "s3", f"{name}: no TLS-only (aws:SecureTransport) deny in the bucket policy")
        acl = safe(s3.get_bucket_acl, Bucket=name)
        if not isinstance(acl, ClientError):
            for g in acl["Grants"]:
                uri = g.get("Grantee", {}).get("URI", "")
                if uri.endswith("AllUsers") or uri.endswith("AuthenticatedUsers"):
                    report("FAIL", "s3", f"{name}: ACL grants {g['Permission']} to {uri.rsplit('/', 1)[-1]}")


def check_ecr(ecr):
    for page in ecr.get_paginator("describe_repositories").paginate():
        for r in page["repositories"]:
            name = r["repositoryName"]
            pol = safe(ecr.get_repository_policy, repositoryName=name)
            open_policy = (not isinstance(pol, ClientError)) and '"Principal":"*"' in pol["policyText"].replace(" ", "")
            report("FAIL" if open_policy else "PASS", "ecr", f"{name}: " + ("repository policy allows everyone" if open_policy else "private (no public policy)"))
            report("PASS" if r.get("imageScanningConfiguration", {}).get("scanOnPush") else "WARN", "ecr",
                   f"{name}: scan on push {'on' if r.get('imageScanningConfiguration', {}).get('scanOnPush') else 'off'}")
            report("PASS" if r.get("imageTagMutability") == "IMMUTABLE" else "WARN", "ecr",
                   f"{name}: tags {r.get('imageTagMutability', '?').lower()}")


def check_lambda(lam):
    for page in lam.get_paginator("list_functions").paginate():
        for f in page["Functions"]:
            name = f["FunctionName"]
            urls = safe(lam.list_function_url_configs, FunctionName=name)
            for u in ([] if isinstance(urls, ClientError) else urls["FunctionUrlConfigs"]):
                lvl = "PASS" if u["AuthType"] == "AWS_IAM" else "FAIL"
                report(lvl, "lambda", f"{name}: function URL auth {u['AuthType']}")
            env = (f.get("Environment") or {}).get("Variables", {})
            risky = [k for k in env if any(w in k.upper() for w in ("SECRET", "PASSWORD", "TOKEN", "PRIVATE_KEY", "API_KEY"))
                     and not k.upper().endswith(("_ARN", "_URL", "_TABLE", "_ISSUER", "_PROVIDER"))]
            if risky:
                report("WARN", "lambda", f"{name}: env vars that look like secrets: {', '.join(risky)}")
            pol = safe(lam.get_policy, FunctionName=name)
            if not isinstance(pol, ClientError):
                for st in json.loads(pol["Policy"]).get("Statement", []):
                    p = st.get("Principal")
                    if p == "*" or (isinstance(p, dict) and p.get("AWS") == "*"):
                        cond = st.get("Condition")
                        report("WARN" if cond else "FAIL", "lambda", f"{name}: resource policy allows principal * "
                               + ("with conditions" if cond else "WITHOUT conditions"))


def check_rds(rds):
    for page in rds.get_paginator("describe_db_instances").paginate():
        for d in page["DBInstances"]:
            n = d["DBInstanceIdentifier"]
            report("FAIL" if d.get("PubliclyAccessible") else "PASS", "rds", f"{n}: publicly accessible = {d.get('PubliclyAccessible')}")
            report("PASS" if d.get("StorageEncrypted") else "FAIL", "rds", f"{n}: storage encrypted = {d.get('StorageEncrypted')}")
    for c in rds.describe_db_clusters()["DBClusters"]:
        n = c["DBClusterIdentifier"]
        report("PASS" if c.get("StorageEncrypted") else "FAIL", "rds", f"{n}: cluster encrypted = {c.get('StorageEncrypted')}")
        report("PASS" if c.get("IAMDatabaseAuthenticationEnabled") else "WARN", "rds", f"{n}: IAM database auth = {c.get('IAMDatabaseAuthenticationEnabled')}")


def check_ec2(ec2):
    for page in ec2.get_paginator("describe_security_groups").paginate():
        for g in page["SecurityGroups"]:
            for p in g["IpPermissions"]:
                open4 = any(r.get("CidrIp") == "0.0.0.0/0" for r in p.get("IpRanges", []))
                open6 = any(r.get("CidrIpv6") == "::/0" for r in p.get("Ipv6Ranges", []))
                if not (open4 or open6):
                    continue
                ports = (p.get("FromPort"), p.get("ToPort"))
                web = ports in ((80, 80), (443, 443))
                desc = "all ports" if p.get("IpProtocol") == "-1" else f"port {ports[0]}-{ports[1]}"
                report("WARN" if web else "FAIL", "sg", f"{g['GroupId']} ({g['GroupName']}): {desc} open to the internet")
    for page in ec2.get_paginator("describe_instances").paginate():
        for r in page["Reservations"]:
            for i in r["Instances"]:
                if i["State"]["Name"] == "running" and i.get("PublicIpAddress"):
                    report("WARN", "ec2", f"{i['InstanceId']}: running with public IP {i['PublicIpAddress']}")


def check_cloudfront(cf):
    for page in cf.get_paginator("list_distributions").paginate():
        for d in (page.get("DistributionList") or {}).get("Items", []) or []:
            name = ", ".join((d.get("Aliases") or {}).get("Items", []) or [d["DomainName"]])
            pol = d["DefaultCacheBehavior"]["ViewerProtocolPolicy"]
            report("PASS" if pol in ("redirect-to-https", "https-only") else "FAIL", "cloudfront", f"{name}: viewer protocol {pol}")
            tls = (d.get("ViewerCertificate") or {}).get("MinimumProtocolVersion", "")
            report("PASS" if tls.startswith("TLSv1.2") else "WARN", "cloudfront", f"{name}: minimum TLS {tls or 'default cert'}")
            for o in d["Origins"]["Items"]:
                if ".s3." in o["DomainName"] or o["DomainName"].endswith(".s3.amazonaws.com"):
                    oac = bool(o.get("OriginAccessControlId"))
                    report("PASS" if oac else "WARN", "cloudfront", f"{name}: S3 origin {o['Id']} " + ("uses OAC" if oac else "has no OAC (bucket may be public)"))
                if ".s3-website" in o["DomainName"]:
                    report("WARN", "cloudfront", f"{name}: origin is an S3 website endpoint (bucket must be public)")


def check_access_analyzer(aa):
    analyzers = aa.list_analyzers(type="ACCOUNT").get("analyzers", [])
    if not analyzers:
        report("WARN", "analyzer", "no IAM Access Analyzer (free): aws accessanalyzer create-analyzer --analyzer-name account --type ACCOUNT")
        return
    arn = analyzers[0]["arn"]
    active = aa.list_findings(analyzerArn=arn, filter={"status": {"eq": ["ACTIVE"]}}).get("findings", [])
    report("PASS" if not active else "WARN", "analyzer", f"{len(active)} resources shared outside the account")
    for f in active[:15]:
        report("WARN", "analyzer", f"{f.get('resourceType')} {f.get('resource')} shared with {f.get('principal')}")


def main() -> int:
    s = boto3.session.Session(region_name=REGION)
    check_account(s.client("sts"), s.client("iam"), s.client("s3control"))
    for name, fn, client in [("IAM users", check_iam, "iam"), ("S3", check_s3, "s3"), ("ECR", check_ecr, "ecr"),
                             ("Lambda", check_lambda, "lambda"), ("RDS / Aurora", check_rds, "rds"),
                             ("Security groups / EC2", check_ec2, "ec2"), ("CloudFront", check_cloudfront, "cloudfront"),
                             ("IAM Access Analyzer", check_access_analyzer, "accessanalyzer")]:
        print(f"\n== {name}")
        try:
            fn(s.client(client))
        except ClientError as e:
            report("WARN", name.split()[0].lower(), f"could not check: {e.response['Error']['Code']}")
    print(f"\n{counts['PASS']} ok, {counts['WARN']} to review, {counts['FAIL']} to fix")
    return 1 if counts["FAIL"] else 0


if __name__ == "__main__":
    sys.exit(main())
