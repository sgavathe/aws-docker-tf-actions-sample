"""Tests for the agent's AWS pieces: Bedrock message conversion and parsing, SigV4 signing of
MCP calls, and the chat app with Cognito sign-in, DynamoDB sessions and private map serving."""

import importlib
import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_REGION", "us-east-1")
for k in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"):
    os.environ.setdefault(k, "testing")

boto3 = pytest.importorskip("boto3")
moto = pytest.importorskip("moto")


# ---------------------------------------------------------------- Bedrock conversion

def test_to_bedrock_pairs_tool_results_and_merges_turns():
    import agent
    history = [
        {"role": "tool", "tool_call_id": "old", "content": "orphan from trimmed history"},
        {"role": "assistant", "content": "orphan"},
        {"role": "system", "content": "SYS"},
        {"role": "user", "content": "Which hospitals?"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "c1", "function": {"name": "list_layers", "arguments": {}}},
            {"id": "c2", "function": {"name": "assets_near", "arguments": {"place": "RIC"}}}]},
        {"role": "tool", "tool_call_id": "c1", "content": "[...]"},
        {"role": "tool", "tool_call_id": "c2", "content": "ERROR: unknown place"},
        {"role": "user", "content": "try Richmond"},
    ]
    system, msgs = agent.to_bedrock(history)
    assert system == [{"text": "SYS"}]
    assert [m["role"] for m in msgs] == ["user", "assistant", "user"]          # orphans dropped, tool+user merged
    uses = [b["toolUse"]["toolUseId"] for b in msgs[1]["content"] if "toolUse" in b]
    results = [b["toolResult"] for b in msgs[2]["content"] if "toolResult" in b]
    assert uses == ["c1", "c2"] and [r["toolUseId"] for r in results] == ["c1", "c2"]
    assert msgs[2]["content"][-1] == {"text": "try Richmond"}
    assert all("status" not in r for r in results)          # Nova (default) rejects toolResult.status


def test_tool_result_status_only_for_claude(monkeypatch):
    import agent
    hist = [{"role": "user", "content": "q"},
            {"role": "assistant", "content": "", "tool_calls": [{"id": "c1", "function": {"name": "t", "arguments": {}}}]},
            {"role": "tool", "tool_call_id": "c1", "content": "ERROR: bad"}]
    monkeypatch.setattr(agent, "BEDROCK_MODEL_ID", "us.anthropic.claude-haiku-5-5")
    _, msgs = agent.to_bedrock(hist)
    assert msgs[2]["content"][0]["toolResult"]["status"] == "error"


def test_describe_unwraps_exception_groups():
    import agent
    from botocore.exceptions import ClientError
    err = ClientError({"Error": {"Code": "ValidationException", "Message": "bad field"}}, "Converse")
    assert agent.describe(ExceptionGroup("outer", [ExceptionGroup("inner", [err])])) == "ValidationException: bad field"
    assert agent.describe(RuntimeError("x")) == "RuntimeError: x"


def test_bedrock_chat_parses_tool_use(monkeypatch):
    import agent
    from botocore.stub import Stubber
    client = boto3.client("bedrock-runtime")
    st = Stubber(client)
    st.add_response("converse", {
        "output": {"message": {"role": "assistant", "content": [
            {"text": "Checking."},
            {"toolUse": {"toolUseId": "tu-1", "name": "crash_hotspots", "input": {"juris": "Prince George"}}}]}},
        "stopReason": "tool_use", "usage": {"inputTokens": 10, "outputTokens": 5, "totalTokens": 15},
        "metrics": {"latencyMs": 10}}, {
        "modelId": agent.BEDROCK_MODEL_ID,
        "system": [{"text": "S"}], "messages": [{"role": "user", "content": [{"text": "hi"}]}],
        "inferenceConfig": {"temperature": 0.1, "maxTokens": 2048},
        "toolConfig": {"tools": [{"toolSpec": {"name": "crash_hotspots", "description": "d",
                                               "inputSchema": {"json": {"type": "object"}}}}]}})
    st.activate()
    monkeypatch.setattr(agent, "_bedrock", client)
    msg = agent.bedrock_chat([{"role": "system", "content": "S"}, {"role": "user", "content": "hi"}],
                             [{"type": "function", "function": {"name": "crash_hotspots", "description": "d",
                                                                "parameters": {"type": "object"}}}])
    assert msg["content"] == "Checking."
    assert msg["tool_calls"] == [{"id": "tu-1", "function": {"name": "crash_hotspots",
                                                             "arguments": {"juris": "Prince George"}}}]


def test_system_prompt_without_pro_points_to_downloads(monkeypatch):
    import agent
    p = agent.system_prompt({"geo": object()})
    assert "NOT connected" in p and "downloads" in p
    assert "NOT connected" not in agent.system_prompt({"geo": 1, "arcgis": 1})


def test_sigv4_signs_and_keeps_user_token():
    import agent
    import httpx2
    req = httpx2.Request("POST", "https://abc.lambda-url.us-east-1.on.aws/mcp",
                         headers={"X-MCP-Authorization": "Bearer user", "Content-Type": "application/json"},
                         content=b'{"jsonrpc":"2.0"}')
    signed = next(agent.SigV4Lambda().auth_flow(req))
    auth = signed.headers["Authorization"]
    assert auth.startswith("AWS4-HMAC-SHA256") and "/lambda/aws4_request" in auth
    assert "x-mcp-authorization" in auth.split("SignedHeaders=")[1]          # user token is signed too
    assert signed.headers["X-MCP-Authorization"] == "Bearer user"


# ---------------------------------------------------------------- chat app in AWS mode

@pytest.fixture
def aws_app(monkeypatch):
    with moto.mock_aws():
        idp = boto3.client("cognito-idp")
        pool = idp.create_user_pool(PoolName="p")["UserPool"]["Id"]
        client = idp.create_user_pool_client(UserPoolId=pool, ClientName="chat",
                                             ExplicitAuthFlows=["ALLOW_USER_PASSWORD_AUTH",
                                                                "ALLOW_REFRESH_TOKEN_AUTH"])["UserPoolClient"]["ClientId"]
        for g in ("geo-reader", "geo-writer"):
            idp.create_group(GroupName=g, UserPoolId=pool)
        idp.admin_create_user(UserPoolId=pool, Username="analyst", MessageAction="SUPPRESS")
        idp.admin_set_user_password(UserPoolId=pool, Username="analyst", Password="Str0ng!Passw0rd", Permanent=True)
        idp.admin_add_user_to_group(UserPoolId=pool, Username="analyst", GroupName="geo-reader")
        boto3.client("dynamodb").create_table(
            TableName="sessions", KeySchema=[{"AttributeName": "sid", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "sid", "AttributeType": "S"}], BillingMode="PAY_PER_REQUEST")
        s3 = boto3.client("s3")
        s3.create_bucket(Bucket="maps")
        s3.put_object(Bucket="maps", Key="maps/crash-1.html", Body=b"<html>map</html>")
        s3.put_object(Bucket="maps", Key="maps/exports/crashes.geojson", Body=b"{}")
        for k, v in {"AUTH_PROVIDER": "cognito", "COGNITO_CLIENT_ID": client, "SESSION_TABLE": "sessions",
                     "MAPS_BUCKET": "maps", "COOKIE_SECURE": "false",
                     "MAP_VIEWER_ORIGIN": "https://geo.example"}.items():
            monkeypatch.setenv(k, v)
        import chat_app
        importlib.reload(chat_app)
        from fastapi.testclient import TestClient
        yield TestClient(chat_app.app), chat_app


def test_cognito_login_session_and_private_maps(aws_app):
    c, chat_app = aws_app
    assert c.get("/maps/crash-1.html").status_code == 401                       # signed-out: no maps
    assert c.post("/api/login", json={"username": "analyst", "password": "wrong"}).status_code == 401
    r = c.post("/api/login", json={"username": "analyst", "password": "Str0ng!Passw0rd"})
    assert r.status_code == 200 and r.json()["roles"] == ["geo-reader"]
    sid = c.cookies.get("geo_session")
    # the table key is a hash, never the cookie itself
    items = boto3.client("dynamodb").scan(TableName="sessions")["Items"]
    assert len(items) == 1 and items[0]["sid"]["S"] != sid
    assert c.get("/api/me").json()["user"] == "analyst"
    m = c.get("/maps/crash-1.html")
    assert m.status_code == 200 and "unsafe-inline" in m.headers["content-security-policy"]
    assert "frame-src 'self'" in m.headers["content-security-policy"]          # dashboards frame their map
    d = c.get("/maps/exports/crashes.geojson")
    assert d.status_code == 200 and "attachment" in d.headers["content-disposition"]
    for bad in ("../secret.html", "exports/../../x.geojson", "x.js", "exports/a.geojson.html.exe"):
        assert c.get(f"/maps/{bad}").status_code == 404
    assert c.post("/api/logout", content="x").status_code == 415                  # JSON only (CSRF)
    assert c.post("/api/logout", json={}).status_code == 200
    assert c.get("/api/me").status_code == 401


def test_stream_pings_while_waiting(monkeypatch):
    import asyncio
    monkeypatch.setenv("KEYCLOAK_TOKEN_URL", "http://unused")
    import chat_app

    async def slow():
        await asyncio.sleep(0.25)
        yield {"type": "answer", "content": "done"}

    async def collect():
        return [e async for e in chat_app._with_pings(slow(), every=0.1)]

    out = asyncio.run(collect())
    assert out[-1]["type"] == "answer" and sum(e["type"] == "ping" for e in out) >= 2


def test_simple_schema_flattens_optional():
    import agent
    s = {"type": "object", "title": "args", "properties": {
        "juris": {"anyOf": [{"type": "string"}, {"type": "null"}], "default": None, "title": "Juris"},
        "severity": {"anyOf": [{"type": "array", "items": {"type": "string"}}, {"type": "string"}, {"type": "null"}]},
        "top": {"type": "integer", "default": 10},
        "title": {"type": "string", "default": "Crash analysis", "title": "Title"}}, "required": []}
    out = agent.simple_schema(s)
    assert out["properties"]["juris"] == {"type": "string"}
    assert out["properties"]["title"] == {"type": "string", "default": "Crash analysis"}   # a parameter named title stays
    assert len(out["properties"]["severity"]["anyOf"]) == 2          # real unions are kept, minus null
    assert "title" not in out and out["properties"]["top"] == {"type": "integer", "default": 10}


def test_thinking_tags_are_stripped(monkeypatch):
    import agent
    from botocore.stub import Stubber
    client = boto3.client("bedrock-runtime")
    st = Stubber(client)
    st.add_response("converse", {
        "output": {"message": {"role": "assistant", "content": [
            {"text": "<thinking>I made a map.</thinking>\n\nHere is the map: /maps/x.html"}]}},
        "stopReason": "end_turn", "usage": {"inputTokens": 1, "outputTokens": 1, "totalTokens": 2},
        "metrics": {"latencyMs": 1}})
    st.activate()
    monkeypatch.setattr(agent, "_bedrock", client)
    assert agent.bedrock_chat([{"role": "user", "content": "hi"}], [])["content"] == "Here is the map: /maps/x.html"


def test_map_pages_send_origin_referrer(aws_app):
    c, _ = aws_app
    c.post("/api/login", json={"username": "analyst", "password": "Str0ng!Passw0rd"})
    assert c.get("/maps/crash-1.html").headers["referrer-policy"] == "strict-origin-when-cross-origin"
    assert c.get("/api/me").headers["referrer-policy"] == "no-referrer"


def test_geocode_signed_in_only_and_server_side(aws_app, monkeypatch):
    c, chat_app = aws_app
    monkeypatch.setattr(chat_app, "GEOCODE_PROVIDER", "amazon-location")
    calls = []

    class Places:
        def geocode(self, **kw):
            calls.append(("geocode", kw))
            return {"ResultItems": []}

        def search_text(self, **kw):
            calls.append(("search_text", kw))
            return {"ResultItems": [{"Title": "Middle Rd", "Position": [-77.28, 37.26],
                                     "Address": {"Label": "Middle Rd, Prince George, VA"}}]}

    monkeypatch.setattr(chat_app, "_places", Places())
    q = {"q": "Middle Rd Prince George VA", "lon": -77.3, "lat": 37.2}
    assert c.post("/api/geocode", json=q).status_code == 401                    # signed-out
    c.post("/api/login", json={"username": "analyst", "password": "Str0ng!Passw0rd"})
    assert c.post("/api/geocode", content="q=x").status_code in (415, 422)     # non-JSON rejected (CSRF)
    assert c.post("/api/geocode", json={"q": "x" * 201}).status_code == 422
    r = c.post("/api/geocode", json=q)
    assert r.json() == {"results": [{"label": "Middle Rd, Prince George, VA", "lon": -77.28, "lat": 37.26}]}
    assert calls[0] == ("geocode", {"QueryText": q["q"], "MaxResults": 5, "BiasPosition": [-77.3, 37.2],
                                    "Filter": {"IncludeCountries": ["USA"]}})
    assert calls[1][0] == "search_text"                                       # fallback for landmarks


def test_invented_map_links_are_removed():
    import agent
    made = {"/maps/crash-analysis-1.html"}
    text = ("Real: [a](/maps/crash-analysis-1.html). Fake: [b](https://mcp.example.com/maps/crash-analysis-9.html) "
            "and /maps/crash-analysis-7.html")
    out = agent.checked_links(text, made)
    assert "[a](/maps/crash-analysis-1.html)" in out
    assert "crash-analysis-9" not in out and "crash-analysis-7" not in out
    assert out.count("no map was made") == 2


def test_simple_schema_collapses_scalar_unions():
    import agent
    sch = {"type": "object", "properties": {"node": {"anyOf": [{"type": "string"}, {"type": "integer"}, {"type": "null"}],
                                                     "default": None, "title": "Node"}}}
    assert agent.simple_schema(sch)["properties"]["node"] == {"type": "string"}


# ---------------------------------------------------------------- demo limits

def test_quota_limits_and_spend(monkeypatch):
    import quota
    monkeypatch.setattr(quota, "DAILY_BUDGET_USD", 1.0)
    monkeypatch.setattr(quota, "DEMO_DAILY_QUESTIONS", 2)
    monkeypatch.setattr(quota, "EXEMPT", {"owner"})
    q = quota.Quota(quota.MemoryQuota())
    assert q.blocked("guest", demo=True) is None
    q.count_question("guest"); q.count_question("guest")
    assert "2 questions" in q.blocked("guest", demo=True)
    assert q.blocked("Guest", demo=False) is None                      # other users: higher limit
    assert abs(quota.model_cost(1_000_000, 1_000_000) - 0.30) < 1e-9   # Nova Lite 0.06 in + 0.24 out
    q.add_spend(0.99); assert q.blocked("other", demo=False) is None
    q.add_spend(0.02)
    assert "usage budget" in q.blocked("other", demo=False) and q.search_blocked("other")
    assert q.blocked("owner", demo=True) is None and not q.search_blocked("owner")   # exempt
    assert q.status("guest", True)["questions_left"] == 0


def test_chat_stops_at_daily_budget_without_calling_the_model(aws_app, monkeypatch):
    c, chat_app = aws_app
    import quota
    monkeypatch.setattr(quota, "DAILY_BUDGET_USD", 0.01)
    calls = []

    async def fake_events(token, messages):
        calls.append(1)
        yield {"type": "usage", "input": 100_000, "output": 10_000}     # $0.0084
        yield {"type": "usage", "input": 100_000, "output": 10_000}     # total $0.0168 > $0.01
        yield {"type": "answer", "content": "ok"}

    monkeypatch.setattr(chat_app, "agent_events", fake_events)
    monkeypatch.setattr(chat_app, "_fresh_token", lambda s: _done("tok"))
    c.post("/api/login", json={"username": "analyst", "password": "Str0ng!Passw0rd"})
    r1 = c.post("/api/chat", json={"message": "q1"})
    lines = [json.loads(l) for l in r1.text.splitlines() if l.strip()]
    assert [l["type"] for l in lines if l["type"] != "ping"] == ["answer"]       # usage events not sent
    assert c.get("/api/me").json()["quota"]["site_spent_usd"] > 0.01
    r2 = c.post("/api/chat", json={"message": "q2"})
    assert "usage budget" in r2.text and len(calls) == 1                         # model not called again
    monkeypatch.setattr(chat_app, "GEOCODE_PROVIDER", "amazon-location")
    assert c.post("/api/geocode", json={"q": "Hopewell VA"}).status_code == 429


async def _done(v):
    return v


# ---------------------------------------------------------------- agent loop guards

class _Res:
    def __init__(self, text, err=False):
        from types import SimpleNamespace
        self.content = [SimpleNamespace(type="text", text=text)]
        self.structured_content = None if err else {"result": {"url": "/maps/m-1.html"}}
        self.is_error = err


def _run_agent(monkeypatch, replies, tool):
    import asyncio
    import agent
    script = iter(replies)

    async def fake_chat(msgs, tools):
        return dict(next(script))

    class Session:
        async def call_tool(self, name, args):
            return tool(name, args)

    async def fake_open(stack, token):
        return {"geo": Session()}

    async def fake_catalog(sessions):
        return ({"make_web_map": sessions["geo"], "map_crashes": sessions["geo"]}, [])

    monkeypatch.setattr(agent, "chat", fake_chat)
    monkeypatch.setattr(agent, "open_servers", fake_open)
    monkeypatch.setattr(agent, "tool_catalog", fake_catalog)
    history = [{"role": "user", "content": "map those"}]

    async def go():
        return [e async for e in agent.agent_events("t", history)]
    return asyncio.run(go()), history


def _call(name, args, i):
    return {"role": "assistant", "content": "", "tool_calls": [{"id": f"c{i}", "function": {"name": name, "arguments": args}}]}


def test_repeated_failures_stop_the_turn(monkeypatch):
    import agent
    bad = {"layers": [{"name": "S", "sql": "SELECT id, lon, lat FROM infra.substations"}], "title": "t"}
    events, history = _run_agent(monkeypatch, [_call("make_web_map", bad, i) for i in range(10)],
                                 lambda n, a: _Res('column "lon" does not exist', err=True))
    results = [e for e in events if e["type"] == "tool_result"]
    assert len(results) == 3                                              # not 10
    assert "already made this exact call" in results[1]["text"]
    answer = [e for e in events if e["type"] == "answer"][0]["content"]
    assert answer.startswith("I couldn't complete this: make_web_map failed 3 times") and "lon" in answer
    agent.to_bedrock(history)                                             # history still valid for the next turn


def test_tool_call_written_as_text_is_run(monkeypatch):
    replies = [{"role": "assistant", "content": "Here: [map_crashes(node='536154', year_from=2020)](x)"},
               _call("map_crashes", {"node": "536154", "year_from": 2020}, 1),
               {"role": "assistant", "content": "29 crashes since 2020. Map: /maps/m-1.html"}]
    events, history = _run_agent(monkeypatch, replies, lambda n, a: _Res('{"url": "/maps/m-1.html"}'))
    assert [e["tool"] for e in events if e["type"] == "tool_call"] == ["map_crashes"]
    answer = [e for e in events if e["type"] == "answer"][0]["content"]
    assert answer == "29 crashes since 2020. Map: /maps/m-1.html"         # real link kept
    assert any("[agent note]" in str(m.get("content")) for m in history)
