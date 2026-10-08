#!/usr/bin/env python3
"""
Build an inferred critical-infrastructure dependency graph from OpenStreetMap data.

    python pipeline/build_ci_graph.py virginia-latest.osm.pbf \
        --center 37.5407,-77.4360 --radius-mi 70 --region "Richmond, VA" \
        -o build/ci-graph.json

Input:  one or more .osm.pbf / .osm files (e.g. Geofabrik state extracts).
Output: one compact JSON file (nodes + edges) read by the backend's
        /api/infrastructure endpoints and drawn by the frontend.

What it does
  1. Reads power, water, telecom, health and emergency-services features.
  2. Rebuilds the power grid from the mapped wires: every line vertex becomes a graph
     vertex, wires are snapped to substations/plants they end in or pass through,
     then collapsed into substation-to-substation links that follow the real route.
  3. Marks power sources: plants, and substations wired to anything outside the
     study area (imports over transmission ties).
  4. Infers cross-sector dependencies with simple, documented rules
     (DEPENDENCY_RULES below), e.g. "a hospital is fed by its nearest substation".

Everything in the output is INFERRED from geometry and OSM tags. OSM does not record
which substation actually feeds which facility. Every edge carries a `basis` string.

Data: (c) OpenStreetMap contributors, ODbL 1.0.
"""
from __future__ import annotations

import argparse
import json
import math
import re
import sys
from collections import Counter, defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import osmium
from pyproj import CRS, Transformer
import shapely
from shapely import STRtree
from shapely.geometry import LineString, Point, shape

MI_TO_M = 1609.344

# --------------------------------------------------------------------------------------
# Feature classification: OSM tags -> (sector, kind)
# --------------------------------------------------------------------------------------

WANTED_TAGS = [
    ("power", "plant"), ("power", "substation"),
    ("power", "line"), ("power", "cable"), ("power", "minor_line"),
    ("man_made", "water_works"), ("man_made", "wastewater_plant"),
    ("man_made", "pumping_station"), ("man_made", "water_tower"),
    ("telecom", "exchange"), ("telecom", "data_center"), ("building", "data_center"),
    ("man_made", "mast"), ("man_made", "tower"),
    ("amenity", "hospital"), ("amenity", "fire_station"), ("amenity", "police"),
    ("emergency", "ambulance_station"),
]

LINE_VALUES = {"line", "cable", "minor_line"}
SEWER_NAME = re.compile(r"\b(sewer|sewage|sanitary|wastewater|waste water|lift station|storm ?water|effluent)\b")


def classify(tags: dict[str, str]) -> tuple[str, str] | None:
    """Return (sector, kind) for a point/area feature, or None to skip it."""
    p = tags.get("power")
    if p == "plant":
        return "energy", "plant"
    if p == "substation":
        return "energy", "substation"
    mm = tags.get("man_made")
    if mm == "water_works":
        return "water", "water_treatment"
    if mm == "wastewater_plant":
        return "water", "wastewater_plant"
    if mm == "pumping_station":
        substance = (tags.get("substance") or tags.get("pumping_station") or "").lower()
        # Many stations carry no substance tag; their name usually says what they pump.
        name = (tags.get("name") or "").lower()
        if any(s in substance for s in ("sewage", "wastewater", "storm")) or SEWER_NAME.search(name):
            return "water", "sewage_pumping"
        if any(s in substance for s in ("gas", "oil", "fuel")):
            return None
        return "water", "pumping_station"
    if mm == "water_tower":
        return "water", "water_tower"
    if tags.get("telecom") == "data_center" or tags.get("building") == "data_center":
        return "it", "data_center"
    if tags.get("telecom") == "exchange":
        return "communications", "telecom_exchange"
    if mm in ("mast", "tower"):
        tt = tags.get("tower:type", "")
        if tt == "communication" or any(k.startswith("communication:") for k in tags):
            return "communications", "comm_tower"
        return None
    am = tags.get("amenity")
    if am == "hospital":
        return "health", "hospital"
    if am == "fire_station":
        return "emergency", "fire_station"
    if am == "police":
        return "emergency", "police"
    if tags.get("emergency") == "ambulance_station":
        return "emergency", "ambulance_station"
    return None


# What each kind can ride out on its own (backup generators, stored water, radio).
# Losing a dependency listed here makes the asset "degraded" instead of "failed".
BACKUP = {
    "hospital": ["power", "water", "comms"],
    "data_center": ["power"],
    "telecom_exchange": ["power"],
    "water_treatment": ["power"],
    "fire_station": ["power", "water", "comms"],
    "police": ["power", "comms"],
    "ambulance_station": ["power", "comms"],
}

# Inferred cross-sector dependencies: consumer kind -> list of
# (dependency type, [(supplier kind, max distance km[, FALLBACK]), ...] in preference order).
# The first option with a supplier in range wins. Options marked FALLBACK cover areas where
# OSM is thin (telecom exchanges especially) and say so in the edge's `basis`.
FALLBACK = True
WATER_SOURCES = [("water_tower", 10), ("pumping_station", 10), ("water_treatment", 10),
                 ("water_tower", 20, FALLBACK), ("pumping_station", 20, FALLBACK)]
COMMS_SOURCES = [("telecom_exchange", 25), ("comm_tower", 10, FALLBACK)]   # radio/cell when no exchange is mapped
POWER = [("substation", 8), ("substation", 15, FALLBACK)]                    # rural feeders run long
DEPENDENCY_RULES: dict[str, list[tuple[str, list[tuple]]]] = {
    "water_treatment":   [("power", POWER)],
    "wastewater_plant":  [("power", POWER)],
    "pumping_station":   [("power", POWER), ("water", [("water_treatment", 40)])],
    "sewage_pumping":    [("power", POWER)],
    "water_tower":       [("water", [("pumping_station", 15), ("water_treatment", 15),
                                     ("pumping_station", 30, FALLBACK), ("water_treatment", 30, FALLBACK)])],
    "telecom_exchange":  [("power", POWER)],
    "comm_tower":        [("power", POWER), ("comms", [("telecom_exchange", 25), ("telecom_exchange", 50, FALLBACK)])],
    "data_center":       [("power", POWER), ("comms", COMMS_SOURCES)],
    "hospital":          [("power", POWER), ("water", WATER_SOURCES), ("comms", COMMS_SOURCES)],
    "fire_station":      [("power", POWER), ("water", WATER_SOURCES), ("comms", COMMS_SOURCES)],
    "police":            [("power", POWER), ("comms", COMMS_SOURCES)],
    "ambulance_station": [("power", POWER), ("comms", COMMS_SOURCES)],
}

SNAP_END_M = 60       # a wire ending this close to a substation/plant connects to it
SNAP_PASS_M = 10      # a wire passing this close (inside the fence) also connects
DIST_LINK_KM = 15     # substation with no mapped wires -> nearest wired substation
PLANT_TIE_KM = 3      # plant with no mapped wires -> nearest substation (its grid connection)
ISLAND_LINK_KM = 15   # wired group with no power source -> nearest powered substation
DEDUPE_M = 150        # same-kind point + polygon closer than this are one asset
SIMPLIFY_M = 20       # wire geometry simplification for the output
MIN_SOURCE_MW = 20    # plants at least this big count as power sources (rooftop solar doesn't)
MIN_TIE_KV = 115      # a wire leaving the study area counts as an import only at this voltage or above;
                      # distribution feeders and untagged lines crossing the edge don't

# Plants with no output tag still count as sources if they burn or spin something.
DISPATCHABLE = {"gas", "coal", "nuclear", "oil", "diesel", "hydro", "biomass", "biogas", "waste"}


# --------------------------------------------------------------------------------------
# Data model
# --------------------------------------------------------------------------------------

@dataclass
class Asset:
    osm: str                    # "n123" / "w456" / "r789"
    sector: str
    kind: str
    name: str | None
    geom: object                # shapely geometry in metres (UTM)
    tags: dict
    is_area: bool
    id: str = ""
    voltage_kv: float = 0
    source: str | None = None   # why this asset counts as a power source
    pt: Point | None = None


@dataclass
class Graph:
    assets: list[Asset] = field(default_factory=list)
    edges: list[dict] = field(default_factory=list)


def _xy(tf: Transformer, xy):
    """shapely.transform callback: (N, 2) array of lon/lat -> projected metres."""
    x, y = tf.transform(xy[:, 0], xy[:, 1])
    return np.column_stack([x, y])


def max_voltage_kv(tags: dict) -> float:
    nums = [float(x) for x in re.findall(r"\d+(?:\.\d+)?", tags.get("voltage", "") or "")]
    if not nums:
        return 0.0
    v = max(nums)
    return v / 1000 if v >= 1000 else v   # OSM uses volts; tolerate kV values


def plant_mw(tags: dict) -> float | None:
    """'20 MW' -> 20, '500 kW' -> 0.5, '1.2 GW' -> 1200; None when untagged or unparseable."""
    m = re.match(r"\s*(\d+(?:\.\d+)?)\s*([kMG]?W)p?\b", tags.get("plant:output:electricity", "") or "")
    if not m:
        return None
    return float(m.group(1)) * {"kW": 0.001, "MW": 1.0, "GW": 1000.0, "W": 1e-6}[m.group(2)]


def plant_source(tags: dict, min_mw: float) -> str | None:
    """Why this plant counts as a power source, or None if it's too small / unknown solar."""
    mw = plant_mw(tags)
    fuel = (tags.get("plant:source") or "").lower()
    if mw is not None:
        return f"power plant, {mw:g} MW" if mw >= min_mw else None
    if any(f in DISPATCHABLE for f in re.split(r"[;,]\s*", fuel)):
        return f"power plant ({fuel})"
    return None


def label(a: Asset) -> str:
    if a.name:
        return a.name
    pretty = a.kind.replace("_", " ")
    if a.kind == "substation" and a.voltage_kv:
        return f"{a.voltage_kv:g} kV substation"
    op = a.tags.get("operator")
    return f"{pretty} ({op})" if op else pretty


# --------------------------------------------------------------------------------------
# 1. Read OSM
# --------------------------------------------------------------------------------------

def read_osm(paths: list[str], fwd: Transformer, core_m: Point, core_radius_m: float, lonlat_bbox):
    """Return (assets, lines). Lines = [(osm_id, voltage_kv, LineString in metres)]."""

    def to_metres(g):
        return shapely.transform(g, lambda xy: _xy(fwd, xy))

    tag_filter = osmium.filter.TagFilter(*WANTED_TAGS)
    gj = osmium.geom.GeoJSONFactory()
    w, s, e, n = lonlat_bbox
    core = core_m.buffer(core_radius_m)
    assets: list[Asset] = []
    lines: list[tuple[str, float, LineString]] = []
    seen_lines: set[str] = set()
    seen_assets: set[str] = set()     # neighbouring extracts overlap at state borders

    def in_bbox(lon, lat):
        return w <= lon <= e and s <= lat <= n

    for path in paths:
        fp = (osmium.FileProcessor(path)
              .with_locations()
              .with_areas(tag_filter)
              .with_filter(tag_filter))
        for obj in fp:
            tags = dict(obj.tags)
            try:
                if obj.is_node():
                    if not in_bbox(obj.location.lon, obj.location.lat):
                        continue
                    geom = shape(json.loads(gj.create_point(obj)))
                    osm_id, is_area = f"n{obj.id}", False
                elif obj.is_way():
                    if tags.get("power") not in LINE_VALUES:
                        continue        # closed ways come back again as areas
                    osm_id = f"w{obj.id}"
                    if osm_id in seen_lines:
                        continue
                    geom = shape(json.loads(gj.create_linestring(obj)))
                    lon0, lat0, lon1, lat1 = geom.bounds
                    if lon1 < w or lon0 > e or lat1 < s or lat0 > n:
                        continue
                    gm = to_metres(geom)
                    if not gm.intersects(core):
                        continue
                    seen_lines.add(osm_id)
                    lines.append((osm_id, max_voltage_kv(tags), gm))
                    continue
                elif obj.is_area():
                    if tags.get("power") in LINE_VALUES:
                        continue
                    geom = shape(json.loads(gj.create_multipolygon(obj)))
                    osm_id = f"{'w' if obj.from_way() else 'r'}{obj.orig_id()}"
                    is_area = True
                    c = geom.representative_point()
                    if not in_bbox(c.x, c.y):
                        continue
                else:
                    continue
            except (osmium.InvalidLocationError, RuntimeError, ValueError):
                continue    # broken geometry in the source data; skip it

            cls = classify(tags)
            if cls is None:
                continue
            gm = to_metres(geom)
            pt = gm.representative_point() if is_area else gm
            if not core.contains(pt):
                continue
            if osm_id in seen_assets:
                continue
            seen_assets.add(osm_id)
            a = Asset(osm=osm_id, sector=cls[0], kind=cls[1], name=tags.get("name"),
                      geom=gm, tags=tags, is_area=is_area, pt=pt)
            if a.kind == "substation":
                a.voltage_kv = max_voltage_kv(tags)
            assets.append(a)
    return dedupe(assets), lines


def dedupe(assets: list[Asset]) -> list[Asset]:
    """Drop a point that sits on (or next to) a polygon of the same kind."""
    areas = [a for a in assets if a.is_area]
    points = [a for a in assets if not a.is_area]
    if not areas:
        return assets
    tree = STRtree([a.geom for a in areas])
    kept = list(areas)
    for p in points:
        dup = False
        for i in tree.query(p.geom.buffer(DEDUPE_M)):
            ar = areas[int(i)]
            if ar.kind == p.kind and ar.geom.distance(p.geom) <= DEDUPE_M:
                if not ar.name and p.name:
                    ar.name = p.name
                if ar.kind == "substation" and not ar.voltage_kv:
                    ar.voltage_kv = p.voltage_kv
                dup = True
                break
        if not dup:
            kept.append(p)
    return kept


# --------------------------------------------------------------------------------------
# 2. Power grid topology
# --------------------------------------------------------------------------------------

def build_grid(assets: list[Asset], lines, core: object, to_ll, min_source_mw: float = MIN_SOURCE_MW,
               min_tie_kv: float = MIN_TIE_KV) -> list[dict]:
    terminals = [a for a in assets if a.kind in ("plant", "substation")]
    if not terminals or not lines:
        return []
    tree = STRtree([t.geom for t in terminals])

    def snap(pt: Point, tol: float) -> Asset | None:
        best, best_d = None, None
        for i in tree.query(pt.buffer(tol)):
            t = terminals[int(i)]
            d = t.geom.distance(pt)
            if d <= tol and (best_d is None or d < best_d):
                best, best_d = t, d
        return best

    def key(x, y):
        return (round(x / 2), round(y / 2))      # 2 m grid: shared towers merge

    vpt: dict = {}
    adj: dict = defaultdict(dict)
    ends: set = set()
    for _osm, kv, ln in lines:
        coords = list(ln.coords)
        keys = []
        for x, y in coords:
            k = key(x, y)
            vpt.setdefault(k, (x, y))
            keys.append(k)
        ends.update((keys[0], keys[-1]))
        for a, b in zip(keys, keys[1:]):
            if a != b:
                adj[a][b] = max(adj[a].get(b, 0), kv)
                adj[b][a] = max(adj[b].get(a, 0), kv)

    term_of: dict = {}
    outside: set = set()
    for k, (x, y) in vpt.items():
        p = Point(x, y)
        t = snap(p, SNAP_END_M if k in ends else SNAP_PASS_M)
        if t is not None:
            term_of[k] = t
        elif not core.contains(p):
            outside.add(k)
    term_vertices = defaultdict(list)
    for k, t in term_of.items():
        term_vertices[id(t)].append(k)
    by_pyid = {id(t): t for t in terminals}

    pairs: dict = {}
    for tid, vs in term_vertices.items():
        t = by_pyid[tid]
        parent = {v: None for v in vs}
        q = deque((v, 0.0) for v in vs)
        while q:
            cur, mv = q.popleft()
            for nb, kv in adj[cur].items():
                if nb in parent:
                    continue
                parent[nb] = cur
                v = max(mv, kv)
                t2 = term_of.get(nb)
                if t2 is not None and t2 is not t:
                    pk = tuple(sorted((t.osm, t2.osm)))
                    if pk not in pairs:
                        path, x = [], nb
                        while x is not None:
                            path.append(x)
                            x = parent[x]
                        pairs[pk] = {"a": t, "b": t2, "kv": v, "frm": t, "path": path[::-1]}
                    else:
                        pairs[pk]["kv"] = max(pairs[pk]["kv"], v)
                    continue                    # don't walk through another substation
                if nb in outside:
                    if not t.source and v >= min_tie_kv:
                        t.source = f"{v:g} kV tie to the grid outside the study area"
                    continue
                q.append((nb, v))

    for t in terminals:
        if t.kind == "plant":
            t.source = plant_source(t.tags, min_source_mw)

    # Direction: voltage step-down first, then distance from a source.
    tadj = defaultdict(set)
    for p in pairs.values():
        tadj[p["a"].osm].add(p["b"].osm)
        tadj[p["b"].osm].add(p["a"].osm)
    by_osm = {t.osm: t for t in terminals}
    level = {t.osm: 0 for t in terminals if t.source and t.osm in tadj}
    q = deque(level)
    while q:
        cur = q.popleft()
        for nb in tadj[cur]:
            if nb not in level:
                level[nb] = level[cur] + 1
                q.append(nb)

    edges = []
    for p in pairs.values():
        a, b = p["a"], p["b"]
        va = 1e9 if a.kind == "plant" else a.voltage_kv
        vb = 1e9 if b.kind == "plant" else b.voltage_kv
        la, lb = level.get(a.osm), level.get(b.osm)
        directed = True
        if va and vb and va != vb:
            src, dst = (a, b) if va > vb else (b, a)
            basis = "wired link; higher voltage feeds lower"
        elif la is not None and lb is not None and la != lb:
            src, dst = (a, b) if la < lb else (b, a)
            basis = "wired link; side nearer a power source feeds the other"
        else:
            src, dst, directed = a, b, False
            basis = "wired link; flow direction unknown"
        path = p["path"] if p["frm"] is src else p["path"][::-1]
        ln = LineString([vpt[k] for k in path]).simplify(SIMPLIFY_M)
        edges.append({"from": src, "to": dst, "type": "power", "kind": "grid_link",
                      "directed": directed, "voltageKv": p["kv"] or None, "basis": basis,
                      "coords": [[round(x, 5), round(y, 5)] for x, y in
                                 (to_ll(cx, cy) for cx, cy in ln.coords)]})

    # Substations with no mapped wires: assume fed from the nearest wired substation.
    wired = [t for t in terminals if t.osm in tadj and t.kind == "substation"]
    if wired:
        wtree = STRtree([t.pt for t in wired])
        for t in terminals:
            if t.osm in tadj or t.kind != "substation":
                continue
            i = wtree.nearest(t.pt)
            g = wired[int(i)]
            d = g.pt.distance(t.pt)
            if d <= DIST_LINK_KM * 1000:
                edges.append({"from": g, "to": t, "type": "power", "kind": "distribution_link",
                              "directed": True, "voltageKv": None,
                              "basis": f"no wires mapped; nearest wired substation, {d / 1000:.1f} km"})

    substations = [t for t in terminals if t.kind == "substation"]
    if not substations:
        return edges
    stree = STRtree([t.pt for t in substations])

    # Plants with no mapped wires: assume they connect at the nearest substation.
    for t in terminals:
        if t.kind != "plant" or t.osm in tadj:
            continue
        s = substations[int(stree.nearest(t.pt))]
        d = s.pt.distance(t.pt)
        if d <= PLANT_TIE_KM * 1000:
            edges.append({"from": t, "to": s, "type": "power", "kind": "distribution_link",
                          "directed": True, "voltageKv": None,
                          "basis": f"no wires mapped; plant tied to nearest substation, {d / 1000:.1f} km"})

    # Wired groups with no power source of their own (usually an OSM gap cut them off):
    # join each to the nearest substation that is in a powered group.
    adj = defaultdict(set)
    for e in edges:
        adj[id(e["from"])].add(id(e["to"]))
        adj[id(e["to"])].add(id(e["from"]))
    by_pyid = {id(t): t for t in terminals}
    group: dict[int, int] = {}
    members: dict[int, list] = {}
    for start in adj:
        if start in group:
            continue
        gid, q = len(members), deque([start])
        group[start], members[gid] = gid, []
        while q:
            u = q.popleft()
            members[gid].append(by_pyid[u])
            for v in adj[u]:
                if v not in group:
                    group[v] = gid
                    q.append(v)
    powered = {gid for gid, ms in members.items() if any(m.source for m in ms)}
    hubs = [t for t in substations if group.get(id(t)) in powered]
    if hubs:
        htree = STRtree([t.pt for t in hubs])
        for gid, ms in members.items():
            if gid in powered:
                continue
            best = None
            for m in ms:
                if m.kind != "substation":
                    continue
                h = hubs[int(htree.nearest(m.pt))]
                d = h.pt.distance(m.pt)
                if best is None or d < best[0]:
                    best = (d, h, m)
            if best and best[0] <= ISLAND_LINK_KM * 1000:
                d, h, m = best
                edges.append({"from": h, "to": m, "type": "power", "kind": "distribution_link",
                              "directed": True, "voltageKv": None,
                              "basis": f"no mapped connection to a power source; nearest powered substation, {d / 1000:.1f} km"})
    return edges


# --------------------------------------------------------------------------------------
# 3. Cross-sector dependencies
# --------------------------------------------------------------------------------------

def build_dependencies(assets: list[Asset]) -> list[dict]:
    by_kind = defaultdict(list)
    for a in assets:
        by_kind[a.kind].append(a)
    trees = {k: STRtree([a.pt for a in v]) for k, v in by_kind.items()}

    def nearest(kind: str, consumer: Asset):
        """Nearest asset of `kind` and its distance in metres (no rule maps a kind onto itself)."""
        if kind not in trees:
            return None, None
        sup = by_kind[kind][int(trees[kind].nearest(consumer.pt))]
        return sup, sup.pt.distance(consumer.pt)

    edges = []
    for consumer in assets:
        for dep_type, options in DEPENDENCY_RULES.get(consumer.kind, []):
            for sk, max_km, *flags in options:
                sup, d = nearest(sk, consumer)
                if sup is None or d > max_km * 1000:
                    continue
                basis = f"nearest {sk.replace('_', ' ')}, {d / 1000:.1f} km"
                if flags and flags[0] is FALLBACK:
                    basis += " (fallback: nothing closer in OSM)"
                edges.append({"from": sup, "to": consumer, "type": dep_type,
                              "kind": "service", "directed": True, "voltageKv": None, "basis": basis})
                break
    return edges


# --------------------------------------------------------------------------------------
# 4. Assemble + write
# --------------------------------------------------------------------------------------

def utm_for(lat: float, lon: float) -> CRS:
    zone = int((lon + 180) // 6) + 1
    return CRS.from_epsg((32600 if lat >= 0 else 32700) + zone)


def build(paths: list[str], center: tuple[float, float], radius_mi: float, region: str,
          min_source_mw: float = MIN_SOURCE_MW, min_tie_kv: float = MIN_TIE_KV) -> dict:
    lat0, lon0 = center
    utm = utm_for(lat0, lon0)
    fwd = Transformer.from_crs("EPSG:4326", utm, always_xy=True)
    inv = Transformer.from_crs(utm, "EPSG:4326", always_xy=True)
    to_m = fwd.transform
    to_ll = inv.transform

    radius_m = radius_mi * MI_TO_M
    cx, cy = to_m(lon0, lat0)
    core_center = Point(cx, cy)
    core = core_center.buffer(radius_m)
    pad = radius_m * 1.3
    xs, ys = [cx - pad, cx + pad], [cy - pad, cy + pad]
    lons, lats = zip(*(to_ll(x, y) for x in xs for y in ys))
    bbox = (min(lons), min(lats), max(lons), max(lats))

    assets, lines = read_osm(paths, fwd, core_center, radius_m, bbox)
    grid_edges = build_grid(assets, lines, core, to_ll, min_source_mw, min_tie_kv)
    dep_edges = build_dependencies(assets)

    # Keep assets that take part in at least one link, plus power sources. An asset with no
    # link can't fail in a cascade or cause one, so it would only be a lone dot on the map.
    used = {id(e["from"]) for e in grid_edges + dep_edges} | {id(e["to"]) for e in grid_edges + dep_edges}
    keep = [a for a in assets if id(a) in used or a.source]
    dropped = Counter(a.kind for a in assets if not (id(a) in used or a.source))
    keep.sort(key=lambda a: (a.sector, a.kind, a.osm))
    for i, a in enumerate(keep):
        a.id = f"n{i}"

    nodes = []
    for a in keep:
        lon, lat = to_ll(a.pt.x, a.pt.y)
        node = {"id": a.id, "osm": a.osm, "name": label(a), "sector": a.sector, "kind": a.kind,
                "lon": round(lon, 5), "lat": round(lat, 5)}
        if a.voltage_kv:
            node["voltageKv"] = a.voltage_kv
        if a.kind == "plant" and plant_mw(a.tags) is not None:
            node["outputMw"] = plant_mw(a.tags)
        if a.source:
            node["source"] = a.source
        if a.kind in BACKUP:
            node["backup"] = BACKUP[a.kind]
        nodes.append(node)

    edges = []
    for e in grid_edges + dep_edges:
        if not e["from"].id or not e["to"].id:
            continue
        out = {"id": f"e{len(edges)}", "from": e["from"].id, "to": e["to"].id,
               "type": e["type"], "kind": e["kind"], "basis": e["basis"]}
        if not e["directed"]:
            out["directed"] = False
        if e.get("voltageKv"):
            out["voltageKv"] = e["voltageKv"]
        if e.get("coords"):
            out["coords"] = e["coords"]
        edges.append(out)

    counts = defaultdict(int)
    for n in nodes:
        counts[n["sector"]] += 1
    return {
        "meta": {
            "region": region,
            "center": {"lat": lat0, "lon": lon0},
            "radiusMi": radius_mi,
            "generatedUtc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "inputs": [Path(p).name for p in paths],
            "source": "OpenStreetMap",
            "attribution": "© OpenStreetMap contributors",
            "license": "ODbL 1.0",
            "inferred": True,
            "sample": False,
            "notes": "Dependencies are inferred from geometry and tags (see each edge's basis), "
                     "not taken from utility records.",
            "nodeCounts": dict(sorted(counts.items())),
            "edgeCount": len(edges),
            "powerSources": sum(1 for n in nodes if n.get("source")),
            "wireSegments": len(lines),
            "droppedUnlinked": dict(sorted(dropped.items())),
        },
        "nodes": nodes,
        "edges": edges,
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("inputs", nargs="+", help=".osm.pbf or .osm files")
    ap.add_argument("--center", default="37.5407,-77.4360", help="lat,lon (default: Richmond, VA)")
    ap.add_argument("--radius-mi", type=float, default=70)
    ap.add_argument("--region", default="Richmond, VA")
    ap.add_argument("--min-source-mw", type=float, default=MIN_SOURCE_MW,
                    help="smallest plant that counts as a power source (default %(default)s MW)")
    ap.add_argument("--min-tie-kv", type=float, default=MIN_TIE_KV,
                    help="lowest line voltage that counts as an import across the area edge (default %(default)s kV)")
    ap.add_argument("--sample", action="store_true", help="mark output as synthetic sample data")
    ap.add_argument("-o", "--output", default="build/ci-graph.json")
    args = ap.parse_args(argv)

    lat, lon = (float(v) for v in args.center.split(","))
    graph = build(args.inputs, (lat, lon), args.radius_mi, args.region, args.min_source_mw, args.min_tie_kv)
    if args.sample:
        graph["meta"].update(sample=True, source="Synthetic sample (not real infrastructure)",
                             attribution="Synthetic demo data", license="CC0")

    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(graph, separators=(",", ":"), ensure_ascii=False))
    m = graph["meta"]
    print(f"{out}: {sum(m['nodeCounts'].values())} nodes {m['nodeCounts']}, "
          f"{m['edgeCount']} edges, {m['wireSegments']} wire segments, "
          f"{out.stat().st_size / 1024:.0f} KiB", file=sys.stderr)
    if not graph["nodes"]:
        print("No infrastructure found - check --center/--radius-mi against the input extract.",
              file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
