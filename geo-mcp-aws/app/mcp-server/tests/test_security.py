"""Security tests that run without Docker: token verification, role mapping, SQL guard,
and role enforcement through a real in-process MCP client."""

import asyncio
import os
import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

os.environ.setdefault("KEYCLOAK_ISSUER", "http://localhost:8080/realms/geo")
os.environ.setdefault("KEYCLOAK_JWKS_URL", "http://keycloak.invalid/certs")

from geo_mcp import auth  # noqa: E402
from geo_mcp.sqlguard import UnsafeSQL, guard_select  # noqa: E402

ISS = os.environ["KEYCLOAK_ISSUER"]
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
OTHER_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


class FakeJWKS:
    class _K:
        key = KEY.public_key()

    def get_signing_key_from_jwt(self, token):
        return self._K()


def make_token(roles=("geo-reader",), aud="geo-mcp", iss=ISS, key=KEY, exp_in=300):
    now = int(time.time())
    return jwt.encode(
        {"iss": iss, "aud": aud, "sub": "u-1", "azp": "geo-agent", "iat": now, "exp": now + exp_in,
         "preferred_username": "analyst", "realm_access": {"roles": list(roles) + ["offline_access"]}},
        key, algorithm="RS256", headers={"kid": "k1"},
    )


@pytest.fixture
def verifier():
    return auth.KeycloakTokenVerifier(auth.KeycloakConfig(ISS, "unused", "geo-mcp"), FakeJWKS())


def verify(v, tok):
    return asyncio.run(v.verify_token(tok))


def test_valid_token_maps_only_known_roles(verifier):
    at = verify(verifier, make_token(roles=("geo-reader", "geo-writer")))
    assert at.scopes == ["geo-reader", "geo-writer"]  # offline_access dropped
    assert at.claims["preferred_username"] == "analyst"


@pytest.mark.parametrize("kwargs", [
    {"aud": "some-other-api"},              # wrong audience
    {"iss": "http://evil/realms/geo"},     # wrong issuer
    {"exp_in": -120},                       # expired beyond leeway
    {"key": OTHER_KEY},                     # signed by someone else
])
def test_bad_tokens_rejected(verifier, kwargs):
    assert verify(verifier, make_token(**kwargs)) is None


def test_garbage_token_rejected(verifier):
    assert verify(verifier, "not-a-jwt") is None


@pytest.mark.parametrize("sql", [
    "SELECT id, name FROM infra.hospitals",
    "with x as (select 1 as a) select * from x;",
    "SELECT name FROM infra.assets WHERE name = 'drop table'",  # keyword inside a string is fine
])
def test_guard_allows_selects(sql):
    assert guard_select(sql, 10).endswith("LIMIT 10")


@pytest.mark.parametrize("sql", [
    "DELETE FROM infra.hospitals",
    "SELECT 1; DROP TABLE infra.hospitals",
    "SELECT pg_sleep(100)",
    "SELECT pg_read_file('/etc/passwd')",
    "/* hi */ UPDATE infra.substations SET name = 'x'",
    "COPY infra.hospitals TO '/tmp/x'",
    "",
])
def test_guard_blocks_writes(sql):
    with pytest.raises(UnsafeSQL):
        guard_select(sql)


def test_role_enforcement_through_mcp(monkeypatch):
    """Call tools through a real MCP client session, with the access token set as the
    auth middleware would set it, and check reader vs writer behavior and audit output."""
    from mcp import Client
    from mcp.server.auth.middleware import auth_context
    from geo_mcp import server as srv

    monkeypatch.setattr(srv, "known_layers", lambda: {"infra.hospitals": "POINT"})
    monkeypatch.setattr(srv, "fetch", lambda sql, params=(), dsn=None: [{"n": 5}])
    app = srv.build_server(verifier=auth.KeycloakTokenVerifier(
        auth.KeycloakConfig(ISS, "unused", "geo-mcp"), FakeJWKS()))

    async def run(roles):
        token = auth.claims_to_access_token(
            "t", jwt.decode(make_token(roles=roles), options={"verify_signature": False}), "geo-mcp")
        ctx_token = auth_context.auth_context_var.set(auth_context.AuthenticatedUser(token))
        try:
            async with Client(app) as c:
                names = {t.name for t in (await c.list_tools()).tools}
                ok = await c.call_tool("list_layers", {})
                denied = await c.call_tool("make_web_map", {"title": "x", "layers": [{"name": "a", "sql": "select 1"}]})
                return names, ok, denied
        finally:
            auth_context.auth_context_var.reset(ctx_token)

    names, ok, denied = asyncio.run(run(("geo-reader",)))
    assert {"list_layers", "cascade_impact", "make_web_map", "load_geojson"} <= names
    assert not ok.is_error and "infra.hospitals" in ok.content[0].text
    assert denied.is_error and "lacks role 'geo-writer'" in denied.content[0].text


def test_cascade_redundancy_and_second_order():
    from geo_mcp.server import cascade
    deps = [
        {"src_id": "SUB-A", "dst_id": "WTR-1", "dep_type": "power"},
        {"src_id": "SUB-A", "dst_id": "HSP-1", "dep_type": "power"},
        {"src_id": "SUB-B", "dst_id": "HSP-1", "dep_type": "power"},   # redundant feed
        {"src_id": "WTR-1", "dst_id": "HSP-1", "dep_type": "water"},   # but water comes from WTR-1
        {"src_id": "SUB-A", "dst_id": "HSP-2", "dep_type": "power"},
        {"src_id": "SUB-B", "dst_id": "HSP-2", "dep_type": "power"},
    ]
    out = cascade("SUB-A", deps, {})
    st = {r["id"]: (r["status"], r["order"]) for r in out["impacted"]}
    assert st["WTR-1"] == ("lost", 1)
    assert st["HSP-1"] == ("lost", 2)        # backup power doesn't save it from losing water
    assert st["HSP-2"] == ("degraded", 1)    # backup feed holds
    assert out["summary"] == {"lost": 2, "degraded": 1, "max_order": 2}


def test_rank_hospitals_worst_first():
    from geo_mcp.server import rank_hospitals
    info = {"SUB-A": {"asset_type": "substation"}, "SUB-B": {"asset_type": "substation"},
            "WTR-1": {"asset_type": "water_plant"}, "HSP-1": {"asset_type": "hospital"}, "HSP-2": {"asset_type": "hospital"}}
    deps = [
        {"src_id": "SUB-A", "dst_id": "WTR-1", "dep_type": "power"},
        {"src_id": "SUB-A", "dst_id": "HSP-1", "dep_type": "power"},
        {"src_id": "SUB-B", "dst_id": "HSP-1", "dep_type": "power"},
        {"src_id": "WTR-1", "dst_id": "HSP-1", "dep_type": "water"},   # cut by SUB-A via water
        {"src_id": "SUB-A", "dst_id": "HSP-2", "dep_type": "power"},
        {"src_id": "SUB-B", "dst_id": "HSP-2", "dep_type": "power"},   # fully redundant
    ]
    hospitals = [{"id": "HSP-2", "name": "Two", "beds": 100, "backup_hours": 96},
                 {"id": "HSP-1", "name": "One", "beds": 100, "backup_hours": 96}]
    out = rank_hospitals(hospitals, deps, info)
    assert [h["id"] for h in out] == ["HSP-1", "HSP-2"]
    assert out[0]["substations_that_cut_it"] == 1 and "SUB-A" in out[0]["weaknesses"]
    assert out[1]["weaknesses"] == "none found"


@pytest.mark.parametrize("name,expect", [("Richmond center", "Richmond, VA city center"),
                                         ("downtown Richmond, VA", "Richmond, VA city center"),
                                         ("RIC airport", "Richmond International Airport")])
def test_place_lookup(name, expect):
    from geo_mcp.server import resolve_place
    assert resolve_place(name)[0] == expect


def test_unknown_place_is_a_clear_error():
    from geo_mcp.server import resolve_place
    with pytest.raises(ValueError, match="known places"):
        resolve_place("Norfolk")


def test_crash_route_labels():
    from geo_mcp.load_crashes import route_label
    assert route_label("R-VA   US00460EB") == "US-460"
    assert route_label("R-VA   SR00156NB") == "VA-156"
    assert route_label("R-VA074SC00625SB") == "Rt 625"
    assert route_label("R-VA   SR99999NB") is None   # VDOT placeholder


def test_crash_filters_are_parameterized():
    import types
    from geo_mcp.crash_tools import build_where
    srv = types.SimpleNamespace(resolve_place=lambda n: ("x", -77.4, 37.5), METERS_PER_MILE=1609.344)
    where, p = build_where(srv, {"severity": "K, A", "route": "'; DROP TABLE x; --", "year_from": 2022,
                                 "night": True, "place": "Richmond", "radius_miles": 2})
    assert "DROP" not in where and p["route"] == "%'; DROP TABLE x; --%"
    assert p["severity"] == ["K", "A"] and p["year_from"] == 2022 and p["night"] is True
    with pytest.raises(ValueError):
        build_where(srv, {"severity": ["X"]})


def test_gi_star_finds_planted_hot_spot_and_ignores_noise():
    import random
    from geo_mcp.crash_tools import gi_star
    random.seed(7)
    cells = {(x, y): random.randint(1, 4) for x in range(30) for y in range(30)}
    assert all(b == 0 for _, b in gi_star(cells).values())          # noise only: nothing significant (FDR)
    for x in range(10, 13):
        for y in range(10, 13):
            cells[(x, y)] = 60
    out = gi_star(cells)
    assert out[(11, 11)][1] == 3 and out[(11, 11)][0] > 2.58      # centre of the cluster: 99% hot
    assert out[(25, 25)][1] == 0                                   # far away: not significant
