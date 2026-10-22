"""Geospatial MCP server for an air-gapped enclave.

Tools (role in brackets):
  list_layers         [geo-reader]  layers in PostGIS with counts
  describe_layer      [geo-reader]  columns, extent and sample rows for one layer
  query_features      [geo-reader]  guarded read-only SQL
  assets_near         [geo-reader]  critical assets within a radius of a point
  cascade_impact      [geo-reader]  downstream dependency cascade if an asset fails
  make_web_map        [geo-writer]  publish an offline MapLibre web map
  export_geojson      [geo-writer]  write GeoJSON for ArcGIS Pro to pick up
  load_geojson        [geo-writer]  load GeoJSON into the work schema

Every call is written as one JSON audit line to stdout (ship it to the SIEM; NIST AU-2/AU-12).
"""

from __future__ import annotations

import functools
import json
import logging
import os
import re
import sys
import time
from decimal import Decimal
from pathlib import Path
from typing import Any

import psycopg
from psycopg.rows import dict_row
from mcp.server.auth.settings import AuthSettings
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations

from . import db, storage
from .auth import DEMO, READER, WRITER, current_user, make_verifier, require
from .mapgen import render_map
from .sqlguard import guard_select

logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stdout)
audit_log = logging.getLogger("geo_mcp.audit")

DB_RO = os.environ.get("DATABASE_URL_RO", "")
DB_RW = os.environ.get("DATABASE_URL_RW", "")
MAPS_DIR = storage.MAPS_DIR
MAP_VIEWER_URL = os.environ.get("MAP_VIEWER_URL", "http://localhost:8088")
MAP_BASEMAP = os.environ.get("MAP_BASEMAP", "offline")  # offline | osm | none
NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,40}$")
ASSET_TYPES = {"substation", "water_plant", "cell_tower", "hospital"}
METERS_PER_MILE = 1609.344

# Offline gazetteer: the enclave has no geocoding service, so named places are a vetted list.
PLACES = {
    "richmond": ("Richmond, VA city center", -77.4360, 37.5407),
    "richmond center": ("Richmond, VA city center", -77.4360, 37.5407),
    "downtown richmond": ("Richmond, VA city center", -77.4360, 37.5407),
    "richmond airport": ("Richmond International Airport", -77.3197, 37.5052),
    "ric": ("Richmond International Airport", -77.3197, 37.5052),
    "west end": ("Richmond West End", -77.5800, 37.6000),
}


def resolve_place(name: str) -> tuple[str, float, float]:
    key = re.sub(r"[^a-z ]+", " ", name.lower()).strip()
    key = re.sub(r"\b(va|virginia|city|the)\b", " ", key)
    key = re.sub(r"\s+", " ", key).strip()
    if key in PLACES:
        return PLACES[key]
    for k, v in PLACES.items():
        if k in key or key in k:
            return v
    raise ValueError(f"unknown place {name!r}; known places: {sorted({v[0] for v in PLACES.values()})}")

RO = ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False)
RW = ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False)


# ---------------------------------------------------------------- helpers

def audited(role: str):
    """Enforce `role` and write one audit record per call."""

    def deco(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            start = time.time()
            user, _ = current_user()
            record = {"event": "mcp_tool_call", "tool": fn.__name__, "user": user, "role_required": role,
                      "args": {k: (str(v)[:200]) for k, v in kwargs.items()}}
            try:
                require(role)
                result = fn(*args, **kwargs)
                record["outcome"] = "success"
                return result
            except PermissionError as exc:
                record["outcome"] = "denied"
                raise ToolError(str(exc)) from exc
            except (ValueError, psycopg.Error) as exc:
                # Expected failures (bad input, unsafe or invalid SQL): the model sees the
                # message so it can correct itself. Anything else stays a generic crash.
                record["outcome"] = "rejected"
                record["error"] = type(exc).__name__
                raise ToolError(str(exc).splitlines()[0][:300]) from exc
            except Exception as exc:
                record["outcome"] = "error"
                record["error"] = type(exc).__name__
                raise
            finally:
                record["ms"] = int((time.time() - start) * 1000)
                audit_log.info(json.dumps(record))

        return wrapper

    return deco


def _jsonable(v: Any) -> Any:
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, (dict, list, str, int, float, bool)) or v is None:
        return v
    return str(v)


def fetch(sql: str, params: tuple | dict = (), dsn: str | None = None) -> list[dict]:
    with db.connect(dsn or DB_RO, autocommit=True) as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(sql, params)
            return [{k: _jsonable(v) for k, v in row.items()} for row in cur.fetchall()]


def known_layers() -> dict[str, str]:
    rows = fetch(
        """SELECT f_table_schema || '.' || f_table_name AS layer, type
           FROM geometry_columns WHERE f_table_schema IN ('infra', 'work')"""
    )
    return {r["layer"]: r["type"] for r in rows}


def as_feature_collection(sql: str, max_rows: int = 2000) -> dict:
    """Run guarded SQL and return a GeoJSON FeatureCollection (needs a `geom` column)."""
    inner = guard_select(sql, max_rows)
    rows = fetch(f"SELECT ST_AsGeoJSON(t.*)::json AS f FROM ({inner}) AS t")
    return {"type": "FeatureCollection", "features": [r["f"] for r in rows]}


ASSET_LAYERS = [
    ("Substations", "substations", "#7b2cbf", "SELECT id, name, voltage_kv, geom FROM infra.substations"),
    ("Water facilities", "water_facilities", "#1f77b4", "SELECT id, name, plant_type, capacity_mgd, geom FROM infra.water_plants"),
    ("Cell towers", "cell_towers", "#2ca02c", "SELECT id, name, carrier, backup_hours, geom FROM infra.cell_towers"),
    ("Hospitals", "hospitals", "#d62728", "SELECT id, name, beds, trauma_level, backup_hours, geom FROM infra.hospitals"),
]


def asset_layer_sql(base_sql: str, lon: float | None, lat: float | None, radius_miles: float | None) -> str:
    """Wrap a layer query with an optional distance filter (numbers only, so safe to inline)."""
    if lon is None or lat is None or not radius_miles:
        return base_sql
    meters = float(radius_miles) * METERS_PER_MILE
    return (f"SELECT * FROM ({base_sql}) AS a WHERE ST_DWithin(a.geom::geography, "
            f"ST_SetSRID(ST_MakePoint({float(lon)}, {float(lat)}), 4326)::geography, {meters})")


def cascade(failed_id: str, deps: list[dict], info: dict, max_depth: int = 4) -> dict:
    """Fixpoint failure propagation over the dependency graph (pure function, unit-tested)."""
    feeds: dict[str, dict[str, set[str]]] = {}
    for d in deps:
        feeds.setdefault(d["dst_id"], {}).setdefault(d["dep_type"], set()).add(d["src_id"])
    lost = {failed_id: 0}
    degraded: dict[str, int] = {}
    reasons: dict[str, list[str]] = {}
    for depth in range(1, max_depth + 1):
        newly = {}
        for asset, by_type in feeds.items():
            if asset in lost:
                continue
            why = []
            power = by_type.get("power", set())
            dead_power = power & lost.keys()
            if power and dead_power == power:
                why.append("all power feeds down: " + ", ".join(sorted(dead_power)))
            for typ in ("water", "comms"):
                dead = by_type.get(typ, set()) & lost.keys()
                if dead:
                    why.append(f"{typ} source down: " + ", ".join(sorted(dead)))
            if why:
                newly[asset] = why
            elif dead_power and asset not in degraded:
                degraded[asset] = depth
                reasons[asset] = [f"on backup feed; lost {', '.join(sorted(dead_power))}"]
        if not newly:
            break
        for asset, why in newly.items():
            lost[asset] = depth
            degraded.pop(asset, None)
            reasons[asset] = why
    rows = []
    for asset, depth in sorted({**degraded, **lost}.items(), key=lambda kv: (kv[1], kv[0])):
        if asset == failed_id:
            continue
        rows.append({"id": asset, "name": info.get(asset, {}).get("name"),
                     "asset_type": info.get(asset, {}).get("asset_type"),
                     "status": "lost" if asset in lost else "degraded",
                     "order": depth, "reasons": reasons[asset]})
    return {"failed_asset": failed_id, "impacted": rows,
            "summary": {"lost": sum(r["status"] == "lost" for r in rows),
                        "degraded": sum(r["status"] == "degraded" for r in rows),
                        "max_order": max((r["order"] for r in rows), default=0)}}


def rank_hospitals(hospitals: list[dict], deps: list[dict], info: dict) -> list[dict]:
    """Pure ranking used by hospital_vulnerability (unit-tested)."""
    subs = sorted(i for i, a in info.items() if a.get("asset_type") == "substation")
    lost_by = {h["id"]: [] for h in hospitals}
    for sid in subs:
        for r in cascade(sid, deps, info)["impacted"]:
            if r["id"] in lost_by and r["status"] == "lost":
                lost_by[r["id"]].append(sid)
    out = []
    for h in hospitals:
        feeds = {}
        for d in deps:
            if d["dst_id"] == h["id"]:
                feeds.setdefault(d["dep_type"], []).append(d["src_id"])
        weak = []
        if lost_by[h["id"]]:
            weak.append("lost if any of " + ", ".join(lost_by[h["id"]]) + " fails")
        if len(feeds.get("power", [])) == 1:
            weak.append("single power feed " + feeds["power"][0])
        for typ in ("water", "comms"):
            if len(feeds.get(typ, [])) == 1:
                weak.append(f"single {typ} source {feeds[typ][0]}")
        if (h.get("backup_hours") or 0) < 72:
            weak.append(f"backup power only {h.get('backup_hours')} h")
        out.append({**h, "power_feeds": len(feeds.get("power", [])),
                    "substations_that_cut_it": len(lost_by[h["id"]]), "weaknesses": "; ".join(weak) or "none found"})
    out.sort(key=lambda r: (-r["substations_that_cut_it"], r["power_feeds"], r.get("backup_hours") or 0, -(r.get("beds") or 0)))
    for i, r in enumerate(out, 1):
        r["rank"] = i
    return out


def cascade_layers(failed_id: str) -> list[dict]:
    """Map layers for a failure cascade: links first (drawn underneath), then points."""
    deps = fetch("SELECT src_id, dst_id, dep_type FROM infra.dependencies")
    pts = {r["id"]: r for r in fetch(
        "SELECT id, name, asset_type, ST_X(geom) AS lon, ST_Y(geom) AS lat FROM infra.assets")}
    if failed_id not in pts:
        raise ValueError(f"unknown asset {failed_id!r}")
    result = cascade(failed_id, deps, pts)
    status = {r["id"]: r for r in result["impacted"]}
    down = {failed_id} | {i for i, r in status.items() if r["status"] == "lost"}

    def point(i, extra):
        a = pts[i]
        return {"type": "Feature", "geometry": {"type": "Point", "coordinates": [a["lon"], a["lat"]]},
                "properties": {"id": i, "name": a["name"], "type": a["asset_type"], **extra}}

    links = {"lost": [], "degraded": []}
    for d in deps:
        if d["src_id"] in down and d["dst_id"] in status:
            a, b = pts[d["src_id"]], pts[d["dst_id"]]
            links[status[d["dst_id"]]["status"]].append({"type": "Feature", "geometry": {
                "type": "LineString", "coordinates": [[a["lon"], a["lat"]], [b["lon"], b["lat"]]]},
                "properties": {"from": d["src_id"], "to": d["dst_id"], "dependency": d["dep_type"]}})
    fc = lambda feats: {"type": "FeatureCollection", "features": feats}  # noqa: E731
    by = lambda st: [point(i, {"status": st, "order": r["order"], "why": "; ".join(r["reasons"])})  # noqa: E731
                     for i, r in status.items() if r["status"] == st]
    return [
        {"name": "Broken dependencies", "color": "#111111", "geojson": fc(links["lost"])},
        {"name": "On backup feed", "color": "#ff9f1c", "geojson": fc(links["degraded"])},
        {"name": "Unaffected assets", "color": "#9aa5b1", "geojson": fc(
            [point(i, {"status": "ok"}) for i in pts if i not in status and i != failed_id])},
        {"name": f"Failed: {failed_id}", "color": "#7b2cbf", "geojson": fc([point(failed_id, {"status": "failed"})])},
        {"name": "Lost service", "color": "#d62728", "geojson": fc(by("lost"))},
        {"name": "Degraded (backup holds)", "color": "#ff9f1c", "geojson": fc(by("degraded"))},
    ]


def export_url(fname: str) -> str:
    """Link to an exported GeoJSON file (opens in ArcGIS Pro, QGIS or ArcGIS Online)."""
    return f"{MAP_VIEWER_URL}/maps/exports/{fname}"


def slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:60]
    return s or "map"


# ---------------------------------------------------------------- server

def build_server(verifier=None) -> MCPServer:
    verifier, issuer = verifier or make_verifier(), os.environ.get("TOKEN_ISSUER") or os.environ.get("KEYCLOAK_ISSUER", "")
    mcp = MCPServer(
        name="geo-mcp",
        version="0.1.0",
        instructions=(
            "Geospatial tools over an air-gapped PostGIS of synthetic critical infrastructure near "
            "Richmond, VA (EPSG:4326). Layers: infra.substations, infra.water_plants, infra.cell_towers, "
            "infra.hospitals, infra.dependencies, view infra.assets. Prefer the purpose-built tools "
            "(find_place, assets_near, hospital_vulnerability, cascade_impact) over query_features. "
            "For maps, write SQL that returns a `geom` column."
        ),
        token_verifier=verifier,
        auth=AuthSettings(
            issuer_url=issuer,
            resource_server_url=os.environ.get("MCP_PUBLIC_URL", "http://localhost:8000/mcp"),
            required_scopes=[READER],
            # Audience / client is checked by the token verifier, not by URL.
            validate_token_resource=False,
        ),
    )

    @mcp.tool(annotations=RO)
    @audited(READER)
    def list_layers() -> list[dict]:
        """List spatial layers with geometry type and feature count."""
        out = []
        for layer, gtype in sorted(known_layers().items()):
            n = fetch(f"SELECT count(*) AS n FROM {layer}")[0]["n"]
            out.append({"layer": layer, "geometry": gtype, "features": n})
        return out

    @mcp.tool(annotations=RO)
    @audited(READER)
    def describe_layer(layer: str) -> dict:
        """Columns, extent and three sample rows for a layer such as 'infra.hospitals'."""
        if layer not in known_layers():
            raise ValueError(f"unknown layer {layer!r}; call list_layers")
        schema, table = layer.split(".")
        cols = fetch(
            """SELECT column_name, data_type FROM information_schema.columns
               WHERE table_schema = %s AND table_name = %s ORDER BY ordinal_position""",
            (schema, table),
        )
        ext = fetch(f"SELECT ST_AsText(ST_Extent(geom)) AS extent FROM {layer}")[0]["extent"]
        sample_cols = ", ".join(c["column_name"] for c in cols if c["column_name"] != "geom")
        sample = fetch(f"SELECT {sample_cols}, ST_AsText(geom) AS wkt FROM {layer} LIMIT 3")
        return {"layer": layer, "columns": cols, "extent": ext, "sample": sample}

    @mcp.tool(annotations=RO)
    @audited(READER)
    def query_features(sql: str, max_rows: int = 200) -> list[dict]:
        """Run ONE read-only SELECT/WITH query. Raw `geom` columns are dropped; use
        ST_AsText(geom) or ST_X/ST_Y if you need coordinates. Distances in meters:
        ST_Distance(a.geom::geography, b.geom::geography)."""
        rows = fetch(guard_select(sql, min(max_rows, 1000)))
        return [{k: v for k, v in r.items() if k != "geom"} for r in rows]

    @mcp.tool(annotations=RO)
    @audited(READER)
    def find_place(name: str) -> dict:
        """Coordinates for a named place (offline list), e.g. 'Richmond center'. Use the result
        as lon/lat for assets_near or hospital_vulnerability."""
        label, lon, lat = resolve_place(name)
        return {"place": label, "lon": lon, "lat": lat}

    @mcp.tool(annotations=RO)
    @audited(READER)
    def assets_near(lon: float | None = None, lat: float | None = None, radius_m: float | None = None,
                    radius_miles: float | None = None, asset_type: str | None = None,
                    place: str | None = None) -> list[dict]:
        """Critical assets near a point, nearest first. Give lon/lat, or place (e.g. 'Richmond center').
        Radius in radius_miles or radius_m (default 5 km). asset_type: substation | water_plant |
        cell_tower | hospital (optional)."""
        if asset_type and asset_type not in ASSET_TYPES:
            raise ValueError(f"asset_type must be one of {sorted(ASSET_TYPES)}")
        if place:
            _, lon, lat = resolve_place(place)
        if lon is None or lat is None:
            raise ValueError("give lon and lat, or a place name")
        r = radius_miles * METERS_PER_MILE if radius_miles else (radius_m or 5000)
        return fetch(
            """SELECT id, name, asset_type,
                      round((ST_Distance(geom::geography, ST_MakePoint(%(lon)s, %(lat)s)::geography) / 1609.344)::numeric, 1) AS miles,
                      ST_X(geom) AS lon, ST_Y(geom) AS lat
               FROM infra.assets
               WHERE ST_DWithin(geom::geography, ST_MakePoint(%(lon)s, %(lat)s)::geography, %(r)s)
                 AND (%(t)s::text IS NULL OR asset_type = %(t)s)
               ORDER BY miles""",
            {"lon": lon, "lat": lat, "r": min(r, 160_000), "t": asset_type},
        )

    @mcp.tool(annotations=RO)
    @audited(READER)
    def hospital_vulnerability(place: str | None = None, lon: float | None = None, lat: float | None = None,
                               radius_miles: float | None = None) -> dict:
        """Rank hospitals from most to least vulnerable ('worst hospitals'). A hospital is worse when
        more single substation failures cut it off (directly or through its water or comms), when it
        has a single power feed, and when its backup power lasts fewer hours. Optionally limit to
        hospitals within radius_miles of a place or lon/lat."""
        if place:
            _, lon, lat = resolve_place(place)
        deps = fetch("SELECT src_id, dst_id, dep_type FROM infra.dependencies")
        info = {r["id"]: r for r in fetch("SELECT id, name, asset_type FROM infra.assets")}
        where, params = "", {}
        if lon is not None and lat is not None and radius_miles:
            where = "WHERE ST_DWithin(geom::geography, ST_MakePoint(%(lon)s, %(lat)s)::geography, %(r)s)"
            params = {"lon": lon, "lat": lat, "r": radius_miles * METERS_PER_MILE}
        dist = ("round((ST_Distance(geom::geography, ST_MakePoint(%(lon)s, %(lat)s)::geography) / 1609.344)::numeric, 1)"
                if lon is not None and lat is not None else "NULL")
        if dist != "NULL" and not params:
            params = {"lon": lon, "lat": lat}
        hospitals = fetch(f"SELECT id, name, beds, trauma_level, backup_hours, {dist} AS miles "
                          f"FROM infra.hospitals {where}", params)
        rows = rank_hospitals(hospitals, deps, info)
        return {"hospitals": rows, "within_miles": radius_miles, "count": len(rows),
                "method": "ranked by substations whose single failure cuts the hospital off, "
                          "then single power feed, then fewest backup hours"}

    @mcp.tool(annotations=RO)
    @audited(READER)
    def cascade_impact(asset_id: str, max_depth: int = 4) -> dict:
        """If asset_id fails (e.g. 'SUB-07'), which assets lose service, including
        second-order effects? Rules: an asset is LOST if every one of its power feeds has
        failed, or any water/comms source it depends on has failed. It is DEGRADED if it
        lost some but not all power feeds. Lost assets propagate failure; degraded ones don't."""
        deps = fetch("SELECT src_id, dst_id, dep_type FROM infra.dependencies")
        info = {r["id"]: r for r in fetch("SELECT id, name, asset_type FROM infra.assets")}
        if asset_id not in info:
            raise ValueError(f"unknown asset {asset_id!r}; ids look like SUB-01, WTR-02, CEL-03, HSP-04")
        return cascade(asset_id, deps, info, min(max_depth, 8))

    @mcp.tool(annotations=RW)
    @audited(WRITER)
    def make_web_map(title: str = "Map", layers: list[dict] | str | None = None, cascade_from: str | None = None,
                     all_assets: bool = False, asset_ids: list[str] | str | None = None,
                     place: str | None = None, lon: float | None = None, lat: float | None = None,
                     radius_miles: float | None = None) -> dict:
        """Publish an offline WEB map (browser). For ArcGIS Pro use export_for_pro + build_pro_map.
        Easiest options first (no SQL needed):
        - asset_ids=['SUB-06','WTR-03']: map those assets (e.g. the ids assets_near returned).
        - all_assets=true: every infrastructure layer, optionally within radius_miles of place
          (e.g. place='Richmond center') or of lon + lat.
        - cascade_from='SUB-06': that asset's failure cascade (failed, lost, degraded, links).
        - layers=[{"name": "Hospitals", "sql": "SELECT id, name, geom FROM infra.hospitals",
          "color": "#d62728"}] only for custom SQL; tables store location in `geom` (there are no
          lon/lat columns). Returns the URL to open."""
        if isinstance(layers, str):   # some models send the list as JSON text
            try:
                layers = json.loads(layers)
            except ValueError:
                raise ValueError("layers must be a list of {name, sql}; or use asset_ids / all_assets / cascade_from")
        layers = list(layers or [])
        for l in layers:
            if not isinstance(l, dict) or not l.get("sql"):
                raise ValueError("each layer needs name and sql (SELECT ..., geom FROM ...); "
                                 "to map known assets use asset_ids=['SUB-06', ...] instead")
        if place:
            _, lon, lat = resolve_place(place)
        if isinstance(asset_ids, str):
            asset_ids = [a for a in re.split(r"[,\s]+", asset_ids) if a]
        if asset_ids:
            ids = [str(a).strip().upper() for a in asset_ids][:200]
            bad = [a for a in ids if not re.fullmatch(r"[A-Z]{2,5}-\d{1,4}", a)]
            if bad:
                raise ValueError(f"asset ids look like SUB-06, WTR-03, CEL-14, HOSP-02; not {bad[0]!r}")
            quoted = ",".join(f"'{a}'" for a in ids)   # validated above: letters, digits and '-' only
            layers = [{"name": n, "color": c, "sql": f"SELECT * FROM ({q}) AS a WHERE a.id IN ({quoted})"}
                      for n, _, c, q in ASSET_LAYERS] + layers
        elif all_assets:
            layers = [{"name": n, "color": c, "sql": asset_layer_sql(q, lon, lat, radius_miles or (5 if lon is not None else None))}
                      for n, _, c, q in ASSET_LAYERS] + layers
        if (not layers and not cascade_from) or len(layers) > 8:
            raise ValueError("give asset_ids, all_assets=true, cascade_from, or 1-8 layers")
        built = []
        for l in layers:
            try:
                fc = as_feature_collection(l["sql"])
            except ValueError:
                raise
            except Exception as exc:   # bad model SQL: say how to fix it instead of the raw error alone
                raise ValueError(f"layer {str(l.get('name'))[:40]!r}: {str(exc).splitlines()[0][:200]}. Tables store "
                                 "location in geom (no lon/lat columns): SELECT id, name, geom FROM infra.<table>; "
                                 "or skip SQL with asset_ids=[...] or all_assets=true") from None
            if asset_ids and not fc["features"]:
                continue   # asset types with none of the requested ids
            built.append({"name": str(l["name"])[:60], "color": l.get("color"), "geojson": fc})
        if asset_ids and not built:
            raise ValueError(f"none of {asset_ids} exist; check the ids with assets_near or list_layers")
        if cascade_from:
            built = cascade_layers(cascade_from) + built
        built = [{k: v for k, v in b.items() if v is not None} for b in built]
        name = f"{slug(title)}-{int(time.time())}.html"
        storage.put_text(name, render_map(title, built, MAP_BASEMAP))
        return {"url": f"{MAP_VIEWER_URL}/maps/{name}",
                "layers": {b["name"]: len(b["geojson"]["features"]) for b in built}}

    @mcp.tool(annotations=RW)
    @audited(WRITER)
    def export_for_pro(place: str | None = None, radius_miles: float | None = None,
                       layers: list[str] | None = None) -> dict:
        """STEP 1 of an ArcGIS Pro map. Exports infrastructure layers as GeoJSON for the ArcGIS Pro
        server, optionally limited to radius_miles around place. layers: any of substations,
        water_facilities, cell_towers, hospitals (default: all four). Returns `pro_layers`: pass it
        UNCHANGED as `layers` to build_pro_map (STEP 2)."""
        lon = lat = None
        if place:
            _, lon, lat = resolve_place(place)
        wanted = set(layers or [k for _, k, _, _ in ASSET_LAYERS])
        unknown = wanted - {k for _, k, _, _ in ASSET_LAYERS}
        if unknown:
            raise ValueError(f"unknown layers {sorted(unknown)}; use substations, water_facilities, cell_towers, hospitals")
        pro_layers, counts = [], {}
        for name, key, color, q in ASSET_LAYERS:
            if key not in wanted:
                continue
            fc = as_feature_collection(asset_layer_sql(q, lon, lat, radius_miles), max_rows=50_000)
            storage.put_text(f"exports/{key}.geojson", json.dumps(fc))
            counts[name] = len(fc["features"])
            if fc["features"]:
                pro_layers.append({"file": f"{key}.geojson", "name": name, "color": color})
        if not pro_layers:
            raise ValueError("no features matched; widen radius_miles")
        return {"pro_layers": pro_layers, "features": counts,
                "downloads": [export_url(l["file"]) for l in pro_layers],
                "next": "call build_pro_map with layers=pro_layers and a title"}

    @mcp.tool(annotations=RW)
    @audited(WRITER)
    def export_geojson(name: str, sql: str) -> dict:
        """Write a GeoJSON file to the shared exports folder for the ArcGIS Pro MCP server.
        `sql` must return a `geom` column. Returns the file name to pass to build_pro_map."""
        fc = as_feature_collection(sql, max_rows=50_000)
        fname = f"{slug(name)}.geojson"
        storage.put_text(f"exports/{fname}", json.dumps(fc))
        return {"file": fname, "features": len(fc["features"]), "folder": "maps/exports",
                "download": export_url(fname)}

    @mcp.tool(annotations=RW)
    @audited(WRITER)
    def load_geojson(table: str, feature_collection: dict) -> dict:
        """Load a GeoJSON FeatureCollection (EPSG:4326) into work.<table>. Properties are
        stored as jsonb in `props`. Replaces the table if it exists."""
        if DEMO in current_user()[1]:
            raise PermissionError("demo accounts can make maps and dashboards but not load data into the database")
        if not NAME_RE.match(table):
            raise ValueError("table must be lowercase letters, digits, underscore")
        feats = feature_collection.get("features", [])
        if not feats or len(feats) > 50_000:
            raise ValueError("need 1-50,000 features")
        with db.connect(DB_RW) as conn, conn.cursor() as cur:
            cur.execute(f"DROP TABLE IF EXISTS work.{table}")
            cur.execute(f"CREATE TABLE work.{table} (fid serial PRIMARY KEY, props jsonb, "
                        f"geom geometry(Geometry, 4326))")
            cur.executemany(
                f"INSERT INTO work.{table} (props, geom) "
                f"VALUES (%s::jsonb, ST_SetSRID(ST_GeomFromGeoJSON(%s), 4326))",
                [(json.dumps(f.get("properties") or {}), json.dumps(f["geometry"])) for f in feats],
            )
            cur.execute(f"CREATE INDEX ON work.{table} USING gist (geom)")
            cur.execute(f"GRANT SELECT ON work.{table} TO mcp_reader")
        return {"layer": f"work.{table}", "features": len(feats)}

    from . import crash_tools
    crash_tools.register(mcp, sys.modules[__name__])
    return mcp


class ForwardedAuth:
    """ASGI middleware for AWS: the function URL's own Authorization header carries the caller's
    IAM signature (only the chat Lambda's role may invoke), so the user's Cognito token travels in
    X-MCP-Authorization. Move it into Authorization before the MCP auth middleware runs; any
    other Authorization value is dropped, so a request without the user token is a 401."""

    HEADER = b"x-mcp-authorization"

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            headers = [(k, v) for k, v in scope["headers"] if k not in (b"authorization", self.HEADER)]
            fwd = [v for k, v in scope["headers"] if k == self.HEADER]
            if fwd:
                headers.append((b"authorization", fwd[0]))
            scope = {**scope, "headers": headers}
        await self.app(scope, receive, send)


def main() -> None:
    import uvicorn

    port = int(os.environ.get("PORT", "8000"))
    extra_hosts = [h.strip() for h in os.environ.get("MCP_ALLOWED_HOSTS", "").split(",") if h.strip()]
    stateless = os.environ.get("MCP_STATELESS", "0") == "1"   # Lambda: no sessions kept between calls
    app = build_server().streamable_http_app(
        stateless_http=stateless,
        json_response=stateless,
        host="0.0.0.0",
        transport_security=TransportSecuritySettings(
            # DNS-rebinding protection guards browser-reachable local servers. In AWS the function
            # URL only accepts IAM-signed calls from the chat Lambda, and its host name isn't known
            # until it exists, so the deployment turns the check off (MCP_DNS_REBINDING_PROTECTION=0).
            enable_dns_rebinding_protection=os.environ.get("MCP_DNS_REBINDING_PROTECTION", "1") == "1",
            allowed_hosts=[f"localhost:{port}", f"127.0.0.1:{port}", f"geo-mcp:{port}", *extra_hosts],
            allowed_origins=["http://localhost:*", "http://127.0.0.1:*"],
        ),
    )
    if os.environ.get("MCP_FORWARDED_AUTH", "0") == "1":
        app = ForwardedAuth(app)
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="warning")


if __name__ == "__main__":
    main()
