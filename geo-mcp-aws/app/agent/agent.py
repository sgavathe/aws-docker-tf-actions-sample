"""Minimal agent: a model + MCP tools, authenticated per user.

    docker compose run --rm agent "Which hospitals lose service if SUB-03 fails? Map it."

Flow: get the user's access token -> open MCP sessions with that bearer token ->
offer the MCP tools to the model -> execute tool calls until it answers.

LLM_PROVIDER picks the model backend; nothing else changes:
  ollama   (default) local model, air-gapped enclave
  bedrock  Amazon Bedrock Converse API with IAM (AWS deployment; no API key)
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import uuid
from contextlib import AsyncExitStack

import httpx2
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

LLM_PROVIDER = os.environ.get("LLM_PROVIDER", "ollama")
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")
BEDROCK_MODEL_ID = os.environ.get("BEDROCK_MODEL_ID", "us.amazon.nova-lite-v1:0")
MODEL = BEDROCK_MODEL_ID if LLM_PROVIDER == "bedrock" else os.environ.get("OLLAMA_MODEL", "qwen2.5:14b")
# sigv4: the MCP server is an IAM-protected Lambda URL. The request is signed with this Lambda's
# role and the user's token moves to X-MCP-Authorization (the server puts it back).
MCP_AUTH_MODE = os.environ.get("MCP_AUTH_MODE", "bearer")
MAX_STEPS = int(os.environ.get("MAX_STEPS", "10"))

SYSTEM = (
    "You are a GIS analyst assistant running inside an air-gapped enclave. Answer ONLY from tool "
    "results in this turn: for any question about what data, layers, assets or places exist, call "
    "a tool first (list_layers, assets_near, query_features); never answer from earlier messages or "
    "general knowledge. Never invent features, places or coordinates. Tool guide: list_layers "
    "for what exists; find_place for a named place (or pass place= to assets_near and "
    "hospital_vulnerability); assets_near takes radius_miles; hospital_vulnerability ranks the "
    "worst hospitals and explains why; cascade_impact answers 'what if X fails'. ArcGIS Pro: "
    "whenever the user says ArcGIS, ArcGIS Pro, Pro, aprx or layout, you MUST make a Pro map: "
    "call export_for_pro (with place/radius_miles if given), then build_pro_map with "
    "layers=pro_layers from its result and a title; do not substitute a web map. Web maps: "
    "make_web_map with all_assets=true for 'map everything/all layers'; cascade_from=X to map a "
    "failure; layers=[...] with SQL returning geom for anything else. Crashes: crash_summary for "
    "overviews; crash_hotspots for worst/most-crash intersections (rank_by='severity' for most "
    "dangerous; pass filters like year_from, severity ['K','A'], collision 'angle', route 'US-460', "
    "juris 'Prince George' for a county or city (typos are tolerated), "
    "night true); find_intersection(road_a, road_b, juris, year_from) for 'crashes at X and Y' (street "
    "names like 'Middle Road' or route numbers like 'VA-156'/'Rt 630'; if a name is not found, ask for "
    "the route number): its total_crashes and K/A/B/C/O columns ARE the answer; then "
    "map_crashes(node='<NODE number from its result>', same years) to map it, never node='intersection'; "
    "crash_profile(node) to explain one location; worst_segments for road stretches; "
    "crash_dashboard(juris or place, year_from) for a dashboard / report / overview with patterns and "
    "recommendations; crash_network(juris or place) for the road network: critical / most important / "
    "chokepoint intersections (betweenness x severity); road_closure_cascade(node) for 'what if this "
    "intersection is closed', detours and cascades (it finds the county itself); "
    "map_crashes for a crash web map, with style: 'heatmap' for heat/density maps, 'hotspots' for hot "
    "spot / statistically significant / Gi* maps, 'clusters' for cluster maps, 'grid' for crash counts "
    "per area, 'points' (default) for individual crashes in a small area; weight_by='severity' when "
    "the user cares about severity. For ArcGIS Pro crash maps call export_crashes_for_pro with the "
    "same style (points, heatmap, hotspots or clusters) and filters, then build_pro_map with "
    "layers=pro_layers unchanged. Use query_features only when "
    "no other tool fits. If a tool returns an error, read it and fix your call; never drop an area "
    "filter the user asked for. Crash results include 'scope': state it in your answer, and if it says "
    "ALL jurisdictions when the user asked about a county or city, say the filter was not applied. Always "
    "state the numbers (totals, and by severity for crashes) in the answer before any map link. Every "
    "number must come from a successful tool result in THIS turn: if a tool failed, fix the call or say "
    "you could not get the number; never reuse numbers from an earlier answer for different years or "
    "filters. Only link a map (url) that a tool returned this turn. 'What type of "
    "crashes' questions: crash_summary for the counts, then map_crashes(color_by='collision'). Give the "
    "map URL as plain text. Be concise and give the reasons."
)

NO_PRO = (
    " ArcGIS Pro is NOT connected in this deployment and build_pro_map does not exist: for ArcGIS / "
    "Pro requests call export_for_pro or export_crashes_for_pro and give the user the `downloads` links "
    "(GeoJSON files that open in ArcGIS Pro, QGIS or ArcGIS Online), plus the map URL if one is returned."
)


def system_prompt(servers) -> str:
    text = SYSTEM if "arcgis" in servers else SYSTEM + NO_PRO
    if LLM_PROVIDER == "bedrock":
        text = text.replace("running inside an air-gapped enclave", "running in a private AWS deployment")
    return text


def get_token() -> str:
    """Demo uses the password grant so it works from a headless CLI. A real deployment would
    use authorization code + PKCE (browser) or token exchange from the user's PIV/CAC session."""
    r = httpx2.post(
        os.environ["KEYCLOAK_TOKEN_URL"],
        data={"grant_type": "password", "client_id": os.environ.get("KEYCLOAK_CLIENT_ID", "geo-agent"),
              "username": os.environ["GEO_USER"], "password": os.environ["GEO_PASSWORD"]},
        timeout=15,
    )
    r.raise_for_status()
    return r.json()["access_token"]


async def open_servers(stack: AsyncExitStack, token: str) -> dict[str, ClientSession]:
    urls = {"geo": os.environ.get("MCP_URL", "http://localhost:8000/mcp")}
    if os.environ.get("ARCGIS_MCP_URL"):
        urls["arcgis"] = os.environ["ARCGIS_MCP_URL"]
    sessions = {}
    for name, url in urls.items():
        if MCP_AUTH_MODE == "sigv4" and name == "geo":
            client_kw = {"headers": {"X-MCP-Authorization": f"Bearer {token}"}, "auth": SigV4Lambda()}
        else:
            client_kw = {"headers": {"Authorization": f"Bearer {token}"}}
        http = await stack.enter_async_context(
            httpx2.AsyncClient(**client_kw, timeout=600 if name == "arcgis" else 120))   # Pro hot spot runs can take minutes
        read, write = await stack.enter_async_context(streamable_http_client(url, http_client=http))
        session = await stack.enter_async_context(ClientSession(read, write))
        await session.initialize()
        sessions[name] = session
    return sessions


async def tool_catalog(sessions: dict[str, ClientSession]):
    routes, tools = {}, []
    for server, s in sessions.items():
        for t in (await s.list_tools()).tools:
            routes[t.name] = s
            tools.append({"type": "function", "function": {
                "name": t.name, "description": t.description or "", "parameters": t.input_schema}})
    return routes, tools


MAP_PATH = re.compile(r"/maps/[\w.\-/]+")
_MAP_LINK = re.compile(r"\[([^\]\n]{0,200})\]\(((?:https?://[^\s)/]+)?/maps/[\w.\-/]+)\)|(?:https?://[^\s)/\]]+)?/maps/[\w.\-/]+")


def checked_links(text: str, made: set[str]) -> str:
    """Drop map links the model wrote itself: only /maps/ paths a tool returned this turn survive."""
    def repl(m):
        url = m.group(2) if m.group(2) else m.group(0)
        path = MAP_PATH.search(url).group(0)
        return m.group(0) if path in made else "(no map was made for this; ask me to map it)"
    return _MAP_LINK.sub(repl, text or "")


def describe(exc: BaseException) -> str:
    """Innermost exception as 'Type: message' (unwraps anyio ExceptionGroups; AWS errors show their code)."""
    while isinstance(exc, BaseExceptionGroup) and exc.exceptions:
        exc = exc.exceptions[0]
    code = getattr(exc, "response", None)
    if isinstance(code, dict) and code.get("Error", {}).get("Code"):
        return f"{code['Error']['Code']}: {code['Error'].get('Message', '')[:300]}"
    return f"{type(exc).__name__}: {str(exc)[:300]}"


def result_text(res) -> str:
    parts = [c.text for c in res.content if getattr(c, "type", "") == "text"]
    text = "\n".join(parts) or json.dumps(res.structured_content or {})
    return ("ERROR: " if res.is_error else "") + text[:12_000]


def result_data(res):
    """Best-effort JSON value of a tool result (lists may arrive as one text item per row)."""
    if res.structured_content:
        sc = res.structured_content
        return sc.get("result", sc) if isinstance(sc, dict) else sc
    parsed = []
    for c in res.content:
        if getattr(c, "type", "") != "text":
            return None
        try:
            parsed.append(json.loads(c.text))
        except ValueError:
            return None
    if not parsed:
        return None
    return parsed if len(parsed) > 1 else parsed[0]


async def chat(messages, tools):
    if LLM_PROVIDER == "bedrock":
        return await asyncio.to_thread(bedrock_chat, messages, tools)
    async with httpx2.AsyncClient(timeout=600) as http:
        r = await http.post(f"{OLLAMA_URL}/api/chat", json={
            "model": MODEL, "messages": messages, "tools": tools, "stream": False,
            "options": {"temperature": 0.1, "num_ctx": 16384}})
        r.raise_for_status()
        return r.json()["message"]


# ---------------------------------------------------------------- Amazon Bedrock (AWS)

_bedrock = None


def to_bedrock(messages: list[dict]) -> tuple[list[dict], list[dict]]:
    """Ollama-style history -> Converse (system, messages). Tool results become toolResult blocks
    in a user turn; consecutive same-role turns are merged; history starts at a user question."""
    system, out = [], []
    for m in messages:
        role = m["role"]
        if role == "system":
            system.append({"text": m["content"]})
            continue
        if role == "tool":
            text = str(m.get("content", ""))
            block = {"toolResult": {"toolUseId": m.get("tool_call_id", ""), "content": [{"text": text or "(empty)"}]}}
            if "anthropic" in BEDROCK_MODEL_ID:   # Converse accepts "status" only for Claude; Nova rejects it
                block["toolResult"]["status"] = "error" if text.startswith("ERROR") else "success"
            role, blocks = "user", [block]
        elif role == "assistant":
            blocks = [{"text": m["content"]}] if m.get("content") else []
            blocks += [{"toolUse": {"toolUseId": c["id"], "name": c["function"]["name"],
                                    "input": c["function"].get("arguments") or {}}} for c in m.get("tool_calls") or []]
            blocks = blocks or [{"text": "(no text)"}]
        else:
            blocks = [{"text": str(m.get("content", "")) or "(empty)"}]
        if not out and (role != "user" or "toolResult" in blocks[0]):
            continue   # trimmed history: start at a real user question
        if out and out[-1]["role"] == role:
            out[-1]["content"].extend(blocks)
        else:
            out.append({"role": role, "content": blocks})
    return system, out


def simple_schema(node):
    """Plain JSON Schema for Bedrock models (Nova is stricter than Claude): Optional[X], which MCP
    sends as anyOf [X, null], becomes X (null is dropped from other unions); title keywords and null defaults are dropped. Meaning is unchanged: the MCP server
    still validates every call against its own schema."""
    if isinstance(node, list):
        return [simple_schema(n) for n in node]
    if not isinstance(node, dict):
        return node
    out = {}
    for k, v in node.items():
        if k == "properties" and isinstance(v, dict):   # parameter names (a parameter may be called "title")
            out[k] = {name: simple_schema(sub) for name, sub in v.items()}
        elif k == "title" and isinstance(v, str):      # the schema keyword, not a parameter
            continue
        elif k == "default" and v is None:             # "default": null clashes with "type": "string"
            continue
        else:
            out[k] = simple_schema(v)
    for key in ("anyOf", "oneOf"):
        opts = out.get(key)
        if isinstance(opts, list):
            non_null = [o for o in opts if not (isinstance(o, dict) and o.get("type") == "null")]
            if len(non_null) == 1:
                out.pop(key)
                merged = dict(non_null[0])
                merged.update({k: v for k, v in out.items() if k not in merged})
                out = merged
            elif non_null and all(isinstance(o, dict) and set(o) == {"type"} for o in non_null):
                # str | int and similar: one plain type for the model (the server accepts either)
                types = [o["type"] for o in non_null]
                out.pop(key)
                out = {**{"type": "string" if "string" in types else types[0]}, **out}
            elif non_null:
                out[key] = non_null
    return out


def bedrock_chat(messages, tools) -> dict:
    global _bedrock
    if _bedrock is None:
        import boto3
        from botocore.config import Config
        _bedrock = boto3.client("bedrock-runtime", config=Config(read_timeout=120, retries={"mode": "adaptive"}))
    system, msgs = to_bedrock(messages)
    specs = [{"toolSpec": {"name": t["function"]["name"],
                           "description": (t["function"]["description"] or t["function"]["name"])[:4000],
                           "inputSchema": {"json": simple_schema(t["function"]["parameters"])}}} for t in tools]
    kw = {"modelId": BEDROCK_MODEL_ID, "system": system, "messages": msgs,
          "inferenceConfig": {"temperature": 0.1, "maxTokens": 2048}}
    if specs:
        kw["toolConfig"] = {"tools": specs}
    resp = _bedrock.converse(**kw)
    text, calls = [], []
    for b in resp["output"]["message"]["content"]:
        if "text" in b:
            text.append(b["text"])
        elif "toolUse" in b:
            u = b["toolUse"]
            calls.append({"id": u["toolUseId"], "function": {"name": u["name"], "arguments": u.get("input") or {}}})
    # Some models (Amazon Nova) write their reasoning in <thinking> tags; show only the answer.
    answer = re.sub(r"<thinking>.*?</thinking>\s*", "", "\n".join(text), flags=re.S).strip()
    msg = {"role": "assistant", "content": answer}
    if calls:
        msg["tool_calls"] = calls
    return msg


class SigV4Lambda(httpx2.Auth):
    """Signs MCP requests for an IAM-protected Lambda function URL (service 'lambda')."""

    requires_request_body = True

    def auth_flow(self, request):
        import boto3
        from botocore.auth import SigV4Auth
        from botocore.awsrequest import AWSRequest
        creds = boto3.Session().get_credentials().get_frozen_credentials()
        signed_headers = {k: v for k, v in request.headers.items()
                          if k.lower() in ("host", "content-type", "x-mcp-authorization")}
        aws = AWSRequest(method=request.method, url=str(request.url), data=request.content, headers=signed_headers)
        SigV4Auth(creds, "lambda", os.environ.get("AWS_REGION", "us-east-1")).add_auth(aws)
        for k in ("Authorization", "X-Amz-Date", "X-Amz-Security-Token"):
            if k in aws.headers:
                request.headers[k] = aws.headers[k]
        yield request


async def agent_events(token: str, messages: list[dict]):
    """Run one user turn. `messages` holds the conversation (without the system prompt) and is
    extended in place. Yields events: start, tool_call, tool_result, answer."""
    made: set[str] = set()   # /maps/... paths the tools returned this turn
    async with AsyncExitStack() as stack:
        sessions = await open_servers(stack, token)
        routes, tools = await tool_catalog(sessions)
        yield {"type": "start", "model": MODEL, "servers": list(sessions), "tools": len(tools)}
        for _ in range(MAX_STEPS):
            msg = await chat([{"role": "system", "content": system_prompt(sessions)}] + messages, tools)
            for c in msg.get("tool_calls") or []:
                c.setdefault("id", f"call_{uuid.uuid4().hex[:12]}")   # Bedrock pairs results by id
            messages.append(msg)
            calls = msg.get("tool_calls") or []
            if not calls:
                yield {"type": "answer", "content": checked_links(msg.get("content", ""), made)}
                return
            for call in calls:
                fn = call["function"]
                args = fn.get("arguments") or {}
                if isinstance(args, str):
                    args = json.loads(args or "{}")
                yield {"type": "tool_call", "tool": fn["name"], "args": args}
                session = routes.get(fn["name"])
                if session is None:
                    out, is_error, data = f"ERROR: unknown tool {fn['name']}", True, None
                else:
                    try:
                        res = await session.call_tool(fn["name"], args)
                        out, is_error = result_text(res), bool(res.is_error)
                        data = None if is_error else result_data(res)
                    except Exception as exc:   # transport failure: tell the model, keep the turn going
                        print(f"[agent] {fn['name']} failed: {describe(exc)}", file=sys.stderr)
                        out, is_error, data = f"ERROR: the {fn['name']} call failed ({describe(exc)}); try again or simplify", True, None
                made.update(MAP_PATH.findall(out))
                yield {"type": "tool_result", "tool": fn["name"], "is_error": is_error,
                       "data": data, "text": None if data is not None else out[:4000]}
                messages.append({"role": "tool", "tool_name": fn["name"], "tool_call_id": call["id"], "content": out})
        yield {"type": "answer", "content": "Stopped: reached the step limit without a final answer."}


async def run(question: str) -> str:
    token = get_token()
    answer = ""
    async for ev in agent_events(token, [{"role": "user", "content": question}]):
        if ev["type"] == "start":
            print(f"[agent] model={ev['model']} servers={ev['servers']} tools={ev['tools']}", file=sys.stderr)
        elif ev["type"] == "tool_call":
            print(f"[tool] {ev['tool']}({json.dumps(ev['args'])[:200]})", file=sys.stderr)
        elif ev["type"] == "answer":
            answer = ev["content"]
    return answer


def main():
    question = " ".join(sys.argv[1:]) or input("Question: ")
    print(asyncio.run(run(question)))


if __name__ == "__main__":
    main()
