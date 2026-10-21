"""Database connections: password DSN locally, IAM authentication in AWS.

With DB_IAM_AUTH=1 the DSN carries no password. Each connection gets a 15-minute
token signed locally with the Lambda's own role (rds-db:connect on that one DB user),
so no database password exists anywhere: not in env vars, Terraform state or Secrets Manager.
"""

from __future__ import annotations

import os

import psycopg
from psycopg.conninfo import conninfo_to_dict

IAM_AUTH = os.environ.get("DB_IAM_AUTH", "0") == "1"
_rds = None


def _iam_token(host: str, port: int, user: str) -> str:
    global _rds
    if _rds is None:
        import boto3  # only needed in AWS
        _rds = boto3.client("rds", region_name=os.environ.get("AWS_REGION"))
    return _rds.generate_db_auth_token(DBHostname=host, Port=port, DBUsername=user)


def connect(dsn: str, **kwargs) -> psycopg.Connection:
    if not IAM_AUTH:
        return psycopg.connect(dsn, **kwargs)
    info = conninfo_to_dict(dsn)
    host, port, user = info["host"], int(info.get("port") or 5432), info["user"]
    # verify-full against the RDS CA bundle baked into the image (no MITM inside the VPC either)
    ca = os.environ.get("DB_SSLROOTCERT", "")
    tls = {"sslmode": "verify-full", "sslrootcert": ca} if ca and os.path.exists(ca) else {"sslmode": "require"}
    for attempt in (1, 2):   # a paused Aurora takes ~15 s to resume (30 s+ after a day idle)
        try:
            return psycopg.connect(dsn, password=_iam_token(host, port, user), connect_timeout=45, **tls, **kwargs)
        except psycopg.OperationalError as exc:
            if attempt == 2 or "password authentication failed" in str(exc):
                raise
