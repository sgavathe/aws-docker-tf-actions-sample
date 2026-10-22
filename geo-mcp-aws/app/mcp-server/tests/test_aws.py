"""Tests for the AWS deployment pieces: S3 storage, Cognito tokens, forwarded auth header,
IAM database auth wiring and the loader job's input checks. No AWS account needed (moto)."""

import asyncio
import importlib
import json
import os
import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

os.environ.setdefault("KEYCLOAK_ISSUER", "http://localhost:8080/realms/geo")
os.environ.setdefault("KEYCLOAK_JWKS_URL", "http://keycloak.invalid/certs")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
for k in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"):
    os.environ.setdefault(k, "testing")

boto3 = pytest.importorskip("boto3")
moto = pytest.importorskip("moto")


# ---------------------------------------------------------------- storage

def test_s3_storage_put_and_delete(monkeypatch):
    with moto.mock_aws():
        s3 = boto3.client("s3")
        s3.create_bucket(Bucket="maps-test")
        monkeypatch.setenv("MAPS_BUCKET", "maps-test")
        from geo_mcp import storage
        importlib.reload(storage)
        storage.put_text("crash-map-1.html", "<html></html>")
        storage.put_text("exports/crash_a.geojson", "{}")
        storage.put_text("exports/crash_b.layer.json", "{}")
        storage.put_text("exports/hospitals.geojson", "{}")
        obj = s3.get_object(Bucket="maps-test", Key="maps/crash-map-1.html")
        assert obj["ContentType"].startswith("text/html") and obj["ServerSideEncryption"] == "AES256"
        storage.delete_matching("exports", "crash_*")
        keys = sorted(o["Key"] for o in s3.list_objects_v2(Bucket="maps-test")["Contents"])
        assert keys == ["maps/crash-map-1.html", "maps/exports/hospitals.geojson"]
        with pytest.raises(ValueError):
            storage.put_text("../escape.html", "x")
    monkeypatch.delenv("MAPS_BUCKET")
    importlib.reload(storage)


def test_local_storage_unchanged(tmp_path, monkeypatch):
    monkeypatch.setenv("MAPS_DIR", str(tmp_path))
    monkeypatch.delenv("MAPS_BUCKET", raising=False)
    from geo_mcp import storage
    importlib.reload(storage)
    storage.put_text("exports/crash_x.geojson", "{}")
    storage.put_text("exports/keep.geojson", "{}")
    storage.delete_matching("exports", "crash_*")
    assert sorted(p.name for p in (tmp_path / "exports").iterdir()) == ["keep.geojson"]


# ---------------------------------------------------------------- Cognito

KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
ISS = "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_TEST"


def _jwks():
    k = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(KEY.public_key()))
    k.update(kid="k1", alg="RS256", use="sig")
    return json.dumps({"keys": [k]})


def _token(**over):
    now = int(time.time())
    c = {"iss": ISS, "sub": "u-1", "client_id": "chat-client", "token_use": "access", "iat": now,
         "exp": now + 300, "username": "analyst", "cognito:groups": ["geo-reader", "geo-writer", "admins"]}
    c.update(over)
    return jwt.encode(c, KEY, algorithm="RS256", headers={"kid": over.pop("_kid", "k1")})


@pytest.fixture
def cognito():
    from geo_mcp.auth import CognitoConfig, CognitoTokenVerifier
    return CognitoTokenVerifier(CognitoConfig(ISS, "chat-client", _jwks()))


def test_cognito_access_token_maps_groups(cognito):
    at = asyncio.run(cognito.verify_token(_token()))
    assert at.scopes == ["geo-reader", "geo-writer"]          # unknown group dropped
    assert at.claims["preferred_username"] == "analyst"


@pytest.mark.parametrize("over", [
    {"token_use": "id"},                    # ID token, not an access token
    {"client_id": "other-client"},          # issued to another app
    {"iss": "https://evil/us-east-1_X"},    # other pool
    {"exp": int(time.time()) - 120},        # expired
])
def test_cognito_rejects(cognito, over):
    assert asyncio.run(cognito.verify_token(_token(**over))) is None


def test_cognito_rejects_unknown_key(cognito):
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    now = int(time.time())
    bad = jwt.encode({"iss": ISS, "sub": "x", "client_id": "chat-client", "token_use": "access", "iat": now,
                      "exp": now + 300}, other, algorithm="RS256", headers={"kid": "k1"})
    assert asyncio.run(cognito.verify_token(bad)) is None


def test_make_verifier_picks_cognito(monkeypatch):
    from geo_mcp import auth
    monkeypatch.setenv("AUTH_PROVIDER", "cognito")
    monkeypatch.setenv("TOKEN_ISSUER", ISS)
    monkeypatch.setenv("COGNITO_CLIENT_ID", "chat-client")
    monkeypatch.setenv("COGNITO_JWKS", _jwks())
    assert isinstance(auth.make_verifier(), auth.CognitoTokenVerifier)


# ---------------------------------------------------------------- forwarded auth header

def test_forwarded_auth_moves_user_token_and_drops_iam_header():
    from geo_mcp.server import ForwardedAuth
    seen = {}

    async def app(scope, receive, send):
        seen.update(dict(scope["headers"]))

    mw = ForwardedAuth(app)
    asyncio.run(mw({"type": "http", "headers": [(b"authorization", b"AWS4-HMAC-SHA256 ..."),
                                                (b"x-mcp-authorization", b"Bearer user-jwt"),
                                                (b"content-type", b"application/json")]}, None, None))
    assert seen[b"authorization"] == b"Bearer user-jwt" and b"x-mcp-authorization" not in seen
    seen.clear()
    asyncio.run(mw({"type": "http", "headers": [(b"authorization", b"Bearer forged")]}, None, None))
    assert b"authorization" not in seen        # no user token -> the MCP auth layer returns 401


# ---------------------------------------------------------------- IAM database auth

def test_db_connect_uses_iam_token(monkeypatch):
    from geo_mcp import db
    calls = {}
    monkeypatch.setattr(db, "IAM_AUTH", True)
    monkeypatch.setattr(db, "_iam_token", lambda h, p, u: f"tok-{h}-{p}-{u}")
    monkeypatch.setattr(db.psycopg, "connect", lambda dsn, **kw: calls.update(dsn=dsn, **kw))
    monkeypatch.setenv("DB_SSLROOTCERT", "/nonexistent.pem")
    db.connect("postgresql://mcp_reader@db.example:5432/geo", autocommit=True)
    assert calls["password"] == "tok-db.example-5432-mcp_reader"
    assert calls["sslmode"] == "require" and calls["autocommit"] is True
    assert "password" not in calls["dsn"]


# ---------------------------------------------------------------- loader job

@pytest.mark.parametrize("event", [
    {"job": "drop_tables"},
    {"job": "load_crashes", "s3_uri": "s3://other-bucket/import/x.geojson"},
    {"job": "load_crashes", "s3_uri": "s3://imports/maps/x.geojson"},
    {"job": "load_crashes", "s3_uri": "s3://imports/import/x.geojson; rm -rf /"},
])
def test_loader_job_rejects_bad_input(event, monkeypatch):
    monkeypatch.setenv("IMPORT_BUCKET", "imports")
    from geo_mcp import lambda_jobs
    assert lambda_jobs.handler(event, None)["ok"] is False
