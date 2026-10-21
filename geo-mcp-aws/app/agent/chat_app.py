"""Browser chat for the geo MCP agent.

    uvicorn chat_app:app --host 0.0.0.0 --port 8090

- Sign-in posts username/password to Keycloak (password grant, demo only; production would
  use authorization code + PKCE). Tokens stay on the server; the browser holds only an
  opaque, HttpOnly, SameSite=Strict session cookie.
- Every chat turn calls the MCP server with THAT user's access token, so the server's role
  checks and audit log apply per person, exactly as with the CLI.
- Turns stream back as NDJSON events (tool_call, tool_result, answer) so the page can show
  progress while the local model works.

AWS mode (set by the Terraform in geo-mcp-aws/):
- AUTH_PROVIDER=cognito: sign-in with Cognito USER_PASSWORD_AUTH on a public app client (no secret).
- SESSION_TABLE: sessions in DynamoDB (Lambda instances share nothing in memory). The table key is
  a SHA-256 of the cookie, so reading the table never yields a usable cookie.
- MAPS_BUCKET: generated maps and GeoJSON exports are served from a private S3 bucket by this app,
  and only to signed-in users.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import secrets
import time
import zlib
from pathlib import Path

import httpx2
import jwt
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from agent import agent_events

AUTH_PROVIDER = os.environ.get("AUTH_PROVIDER", "keycloak")
TOKEN_URL = os.environ.get("KEYCLOAK_TOKEN_URL", "")
CLIENT_ID = os.environ.get("COGNITO_CLIENT_ID") or os.environ.get("KEYCLOAK_CLIENT_ID", "geo-agent")
SESSION_TABLE = os.environ.get("SESSION_TABLE", "")
MAPS_BUCKET = os.environ.get("MAPS_BUCKET", "")
COOKIE_SECURE = os.environ.get("COOKIE_SECURE", "false") == "true"
ROLES = {"geo-reader", "geo-writer"}
MAP_ORIGIN = os.environ.get("MAP_VIEWER_ORIGIN", "http://localhost:8088")
COOKIE = "geo_session"
SESSION_TTL = 8 * 3600
MAX_HISTORY = 40
STATIC = Path(__file__).parent / "static"



# ---------------------------------------------------------------- sessions
# session = {"tokens": {...}, "user": str, "roles": [...], "messages": [...], "seen": float}

class MemorySessions:
    def __init__(self):
        self.d: dict[str, dict] = {}

    def get(self, sid: str) -> dict | None:
        return self.d.get(sid)

    def put(self, sid: str, s: dict) -> None:
        self.d[sid] = s

    def delete(self, sid: str) -> None:
        self.d.pop(sid, None)


class DynamoSessions:
    """One item per session: zlib-compressed JSON, expiring by TTL. Items are capped well below
    DynamoDB's 400 KB limit by dropping the oldest conversation turns."""

    LIMIT = 350_000

    def __init__(self, table: str):
        import boto3
        self.t = boto3.resource("dynamodb").Table(table)

    @staticmethod
    def _key(sid: str) -> str:
        return hashlib.sha256(sid.encode()).hexdigest()

    def get(self, sid: str) -> dict | None:
        item = self.t.get_item(Key={"sid": self._key(sid)}, ConsistentRead=True).get("Item")
        if not item or item.get("expires", 0) < time.time():
            return None
        return json.loads(zlib.decompress(bytes(item["data"])))

    def put(self, sid: str, s: dict) -> None:
        while True:
            blob = zlib.compress(json.dumps(s, default=str).encode(), 6)
            if len(blob) < self.LIMIT or not s["messages"]:
                break
            del s["messages"][: max(2, len(s["messages"]) // 4)]
        self.t.put_item(Item={"sid": self._key(sid), "data": blob, "expires": int(time.time()) + SESSION_TTL})

    def delete(self, sid: str) -> None:
        self.t.delete_item(Key={"sid": self._key(sid)})


SESSIONS = DynamoSessions(SESSION_TABLE) if SESSION_TABLE else MemorySessions()

app = FastAPI(title="Geo MCP chat", docs_url=None, redoc_url=None, openapi_url=None)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    resp = await call_next(request)
    if request.url.path.startswith("/maps/"):
        resp.headers["Content-Security-Policy"] = MAP_CSP   # generated MapLibre page, framed by the chat
        resp.headers["X-Frame-Options"] = "SAMEORIGIN"
    else:
        resp.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
            f"connect-src 'self'; frame-src {MAP_ORIGIN or "'self'"}; frame-ancestors 'none'; base-uri 'none'; "
            "form-action 'self'"
        )
    if COOKIE_SECURE:
        resp.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    # Map pages send only the site origin (no path) so tile servers can identify the app:
    # OpenStreetMap's tile policy blocks requests that carry no Referer.
    resp.headers["Referrer-Policy"] = ("strict-origin-when-cross-origin" if request.url.path.startswith("/maps/")
                                       else "no-referrer")
    resp.headers["Cache-Control"] = "no-store"
    return resp


TILES = os.environ.get("MAP_TILE_ORIGIN", "")   # e.g. https://tile.openstreetmap.org when MAP_BASEMAP=osm
MAP_CSP = (f"default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
           f"img-src 'self' data: blob: {TILES}; worker-src blob:; child-src blob:; connect-src 'self' {TILES}; "
           f"frame-ancestors 'self'; base-uri 'none'")


class Login(BaseModel):
    username: str = Field(min_length=1, max_length=100)
    password: str = Field(min_length=1, max_length=200)


class Ask(BaseModel):
    message: str = Field(min_length=1, max_length=4000)


def _claims(access_token: str) -> dict:
    # Display only: the MCP server verifies the signature on every call.
    return jwt.decode(access_token, options={"verify_signature": False})


def _store(tokens: dict) -> dict:
    tokens = dict(tokens)
    tokens["expires_at"] = time.time() + int(tokens.get("expires_in", 300))
    return tokens


async def _token_request(data: dict) -> dict:
    if AUTH_PROVIDER == "cognito":
        return await asyncio.to_thread(_cognito, data)
    async with httpx2.AsyncClient(timeout=15) as http:
        r = await http.post(TOKEN_URL, data={"client_id": CLIENT_ID, **data})
    if r.status_code != 200:
        raise HTTPException(401, "Sign-in failed")
    return r.json()


_idp = None


def _cognito(data: dict) -> dict:
    """Same shape as an OAuth token response, from Cognito's InitiateAuth."""
    global _idp
    if _idp is None:
        import boto3
        _idp = boto3.client("cognito-idp")
    if data["grant_type"] == "password":
        flow, params = "USER_PASSWORD_AUTH", {"USERNAME": data["username"], "PASSWORD": data["password"]}
    else:
        flow, params = "REFRESH_TOKEN_AUTH", {"REFRESH_TOKEN": data["refresh_token"]}
    try:
        r = _idp.initiate_auth(ClientId=CLIENT_ID, AuthFlow=flow, AuthParameters=params)
    except Exception:
        raise HTTPException(401, "Sign-in failed")
    res = r.get("AuthenticationResult")
    if not res:   # e.g. NEW_PASSWORD_REQUIRED: set a permanent password with scripts/users.sh
        raise HTTPException(401, "Sign-in failed: this account needs a password reset by an admin")
    return {"access_token": res["AccessToken"], "expires_in": res.get("ExpiresIn", 3600),
            "refresh_token": res.get("RefreshToken") or data.get("refresh_token", "")}


def _user_roles(c: dict, fallback: str) -> tuple[str, list[str]]:
    if AUTH_PROVIDER == "cognito":
        return c.get("username", fallback), sorted(set(c.get("cognito:groups", [])) & ROLES)
    return c.get("preferred_username", fallback), sorted(set(c.get("realm_access", {}).get("roles", [])) & ROLES)


def _session(request: Request) -> tuple[str, dict]:
    sid = request.cookies.get(COOKIE) or ""
    s = SESSIONS.get(sid) if sid else None
    if not s or time.time() - s["seen"] > SESSION_TTL:
        if sid:
            SESSIONS.delete(sid)
        raise HTTPException(401, "Not signed in")
    s["seen"] = time.time()
    return sid, s


async def _fresh_token(s: dict) -> str:
    t = s["tokens"]
    if time.time() < t["expires_at"] - 60:
        return t["access_token"]
    s["tokens"] = _store(await _token_request(
        {"grant_type": "refresh_token", "refresh_token": t.get("refresh_token", "")}))
    return s["tokens"]["access_token"]


def _require_json(request: Request) -> None:
    # Cross-site forms cannot send application/json without a CORS preflight, which this
    # app never grants; together with SameSite=Strict this blocks CSRF.
    if not request.headers.get("content-type", "").startswith("application/json"):
        raise HTTPException(415, "JSON required")


@app.post("/api/login")
async def login(body: Login, request: Request, response: Response):
    _require_json(request)
    tokens = _store(await _token_request(
        {"grant_type": "password", "username": body.username, "password": body.password}))
    user, roles = _user_roles(_claims(tokens["access_token"]), body.username)
    sid = secrets.token_urlsafe(32)
    SESSIONS.put(sid, {"tokens": tokens, "user": user, "roles": roles, "messages": [], "seen": time.time()})
    response.set_cookie(COOKIE, sid, httponly=True, samesite="strict", max_age=SESSION_TTL, secure=COOKIE_SECURE)
    return {"user": user, "roles": roles, "map_origin": MAP_ORIGIN}


@app.get("/api/me")
async def me(request: Request):
    _, s = _session(request)
    return {"user": s["user"], "roles": s["roles"], "map_origin": MAP_ORIGIN}


@app.post("/api/logout")
async def logout(request: Request, response: Response):
    _require_json(request)
    sid = request.cookies.get(COOKIE, "")
    if sid:
        SESSIONS.delete(sid)
    response.delete_cookie(COOKIE)
    return {"ok": True}


@app.post("/api/reset")
async def reset(request: Request):
    _require_json(request)
    sid, s = _session(request)
    s["messages"] = []
    SESSIONS.put(sid, s)
    return {"ok": True}


async def _with_pings(events, every: float = 15.0):
    """Yield the agent's events, plus a ping when nothing happened for `every` seconds, so
    CloudFront (60 s origin timeout) keeps the stream open while the model or database works."""
    q: asyncio.Queue = asyncio.Queue()
    done = object()

    async def pump():
        try:
            async for ev in events:
                await q.put(ev)
        except Exception as exc:  # report, don't leak internals
            await q.put({"type": "error", "message": f"{type(exc).__name__}: the request failed"})
        finally:
            await q.put(done)

    task = asyncio.create_task(pump())
    try:
        while True:
            try:
                ev = await asyncio.wait_for(q.get(), timeout=every)
            except asyncio.TimeoutError:
                yield {"type": "ping"}
                continue
            if ev is done:
                return
            yield ev
    finally:
        task.cancel()


@app.post("/api/chat")
async def ask(body: Ask, request: Request):
    _require_json(request)
    sid, s = _session(request)
    token = await _fresh_token(s)
    s["messages"].append({"role": "user", "content": body.message})
    SESSIONS.put(sid, s)

    async def stream():
        try:
            async for ev in _with_pings(agent_events(token, s["messages"])):
                yield json.dumps(ev, default=str) + "\n"
        finally:
            # Keep the conversation bounded; tool outputs are the bulk of it.
            del s["messages"][:-MAX_HISTORY]
            SESSIONS.put(sid, s)

    return StreamingResponse(stream(), media_type="application/x-ndjson")


# ---------------------------------------------------------------- maps and exports (AWS)
# Locally the map-viewer nginx serves these. In AWS they live in a private bucket and are served
# here, so only signed-in users can open a map or download an export.

MAP_FILE = re.compile(r"^(?:[A-Za-z0-9][\w.\-]{0,120}\.html|exports/[A-Za-z0-9][\w.\-]{0,120}\.geojson)$")
_s3 = None


@app.get("/maps/{path:path}")
async def map_file(path: str, request: Request):
    if not MAPS_BUCKET:
        raise HTTPException(404)
    _session(request)
    if not MAP_FILE.match(path):
        raise HTTPException(404)
    global _s3
    if _s3 is None:
        import boto3
        _s3 = boto3.client("s3")
    try:
        obj = await asyncio.to_thread(_s3.get_object, Bucket=MAPS_BUCKET, Key=f"maps/{path}")
    except Exception:
        raise HTTPException(404)
    body = await asyncio.to_thread(obj["Body"].read)
    if path.endswith(".html"):
        return Response(body, media_type="text/html; charset=utf-8")
    return Response(body, media_type="application/geo+json",
                    headers={"Content-Disposition": f'attachment; filename="{path.rsplit("/", 1)[-1]}"'})


@app.get("/healthz")
async def healthz():
    return JSONResponse({"ok": True})


app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/")
async def index():
    return FileResponse(STATIC / "index.html")
