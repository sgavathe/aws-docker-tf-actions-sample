#!/usr/bin/env python3
"""Create PostGIS, schemas and the IAM-authenticated app roles in Aurora, then load the synthetic
infrastructure sample. Idempotent: safe to re-run.

Runs from your laptop or CI through the RDS Data API (HTTPS, IAM), so the database never needs
a public endpoint, tunnel or bastion. The admin password stays inside the RDS-managed secret;
this script only passes the secret's ARN.

    python3 geo-mcp-aws/scripts/db-bootstrap.py --cluster-arn <arn> --secret-arn <arn>
"""

from __future__ import annotations

import argparse
import re
import sys
import time
from pathlib import Path

import boto3
from botocore.exceptions import ClientError

APP = Path(__file__).resolve().parents[1] / "app" / "db"

ROLES_SQL = [
    "CREATE EXTENSION IF NOT EXISTS postgis",
    "CREATE SCHEMA IF NOT EXISTS infra",
    "CREATE SCHEMA IF NOT EXISTS work",
    # LOGIN without a password + rds_iam = IAM token logins only (no password exists to leak)
    """DO $$ BEGIN
         IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'mcp_reader')   THEN CREATE ROLE mcp_reader LOGIN;   END IF;
         IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'mcp_writer')   THEN CREATE ROLE mcp_writer LOGIN;   END IF;
         IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'crash_loader') THEN CREATE ROLE crash_loader LOGIN; END IF;
       END $$""",
    "GRANT rds_iam TO mcp_reader, mcp_writer, crash_loader",
    # Least privilege, same as the enclave (db/init/01_roles.sh)
    "ALTER ROLE mcp_reader SET default_transaction_read_only = on",
    "ALTER ROLE mcp_reader SET statement_timeout = '60s'",
    "ALTER ROLE mcp_writer SET statement_timeout = '60s'",
    "GRANT USAGE ON SCHEMA infra, work TO mcp_reader, mcp_writer",
    "ALTER DEFAULT PRIVILEGES IN SCHEMA infra GRANT SELECT ON TABLES TO mcp_reader, mcp_writer",
    "ALTER DEFAULT PRIVILEGES IN SCHEMA work GRANT SELECT ON TABLES TO mcp_reader",
    "GRANT CREATE ON SCHEMA work TO mcp_writer",
    # Crash data: the admin owns the schema; the loader may create its table there and owns that
    # table (it grants SELECT to the app roles itself). No role membership is granted to the admin:
    # membership in an IAM-auth role would make the admin IAM-only too and lock out the Data API.
    "GRANT CREATE ON DATABASE geo TO crash_loader",
    "CREATE SCHEMA IF NOT EXISTS crash",
    "GRANT USAGE, CREATE ON SCHEMA crash TO crash_loader",
    "GRANT USAGE ON SCHEMA crash TO mcp_reader, mcp_writer",
]


def statements(path: Path) -> list[str]:
    text = "\n".join(l for l in path.read_text().splitlines() if not l.strip().startswith("--"))
    out = [s.strip() for s in re.split(r";\s*\n", text)]
    return [s.rstrip(";") for s in out if s and s.upper() not in ("BEGIN", "COMMIT")]


class DataApi:
    def __init__(self, cluster_arn: str, secret_arn: str, database: str = "geo"):
        self.c = boto3.client("rds-data")
        self.kw = {"resourceArn": cluster_arn, "secretArn": secret_arn, "database": database}

    def run(self, sql: str) -> dict:
        for attempt in range(30):
            try:
                return self.c.execute_statement(sql=sql, **self.kw)
            except ClientError as exc:
                if exc.response["Error"]["Code"] == "DatabaseResumingException":
                    if attempt == 0:
                        print("  database is resuming from a pause, waiting ...", flush=True)
                    time.sleep(5)
                    continue
                raise
        raise RuntimeError("database did not resume")

    def scalar(self, sql: str):
        rec = self.run(sql).get("records") or [[{}]]
        return next(iter(rec[0][0].values()), None)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cluster-arn", required=True)
    ap.add_argument("--secret-arn", required=True)
    a = ap.parse_args()
    db = DataApi(a.cluster_arn, a.secret_arn)

    print("==> roles, schemas, PostGIS")
    for sql in ROLES_SQL:
        db.run(sql)
    # Guard: if the admin ever ends up inheriting rds_iam, RDS makes it IAM-only and this script
    # (Data API, password from the RDS-managed secret) can no longer log in.
    if db.scalar("SELECT pg_has_role(current_user, 'rds_iam', 'USAGE')"):
        sys.exit("ERROR: the admin role inherits rds_iam; revoke that membership before re-running")

    if db.scalar("SELECT to_regclass('infra.substations') IS NOT NULL"):
        print("==> sample infrastructure already loaded")
    else:
        print("==> synthetic infrastructure sample")
        for f in ("02_schema.sql", "03_seed.sql"):
            for sql in statements(APP / f):
                db.run(sql)
    print("    substations:", db.scalar("SELECT count(*) FROM infra.substations"),
          " hospitals:", db.scalar("SELECT count(*) FROM infra.hospitals"))
    if db.scalar("SELECT to_regclass('crash.crashes') IS NOT NULL"):
        print("    crashes:", db.scalar("SELECT count(*) FROM crash.crashes"))
    else:
        print("    crashes: not loaded yet (scripts/load-crashes.sh)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
