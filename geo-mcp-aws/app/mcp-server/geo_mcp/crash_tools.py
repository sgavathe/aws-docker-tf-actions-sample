"""Crash analysis tools (VDOT CrashData loaded into crash.crashes by load_crashes.py).

All filters are passed as SQL parameters; tool arguments never become SQL text.
"""

from __future__ import annotations

import difflib
import json
import math
import re
import os
import time
from pathlib import Path

SEV_ORDER = ["K", "A", "B", "C", "O"]
SEV_LABEL = {"K": "fatal", "A": "serious injury", "B": "minor injury", "C": "possible injury", "O": "property damage only"}


def epdo_weights() -> dict[str, float]:
    """Severity weights for 'equivalent property damage only' ranking. The defaults are
    illustrative; set CRASH_EPDO_WEIGHTS (e.g. 'K=1500,A=100,B=25,C=10,O=1') from VDOT/FHWA guidance."""
    raw = os.environ.get("CRASH_EPDO_WEIGHTS", "K=1500,A=100,B=25,C=10,O=1")
    return {k.strip(): float(v) for k, v in (p.split("=") for p in raw.split(",") if "=" in p)}


def build_where(srv, f: dict) -> tuple[str, dict]:
    """Translate tool filters into a parameterized WHERE clause."""
    clauses, p = ["TRUE"], {}
    if f.get("year_from"):
        clauses.append("crash_year >= %(year_from)s"); p["year_from"] = int(f["year_from"])
    if f.get("year_to"):
        clauses.append("crash_year <= %(year_to)s"); p["year_to"] = int(f["year_to"])
    sev = f.get("severity")
    if sev:
        if isinstance(sev, str):
            sev = [s.strip().upper()[:1] for s in sev.replace(";", ",").split(",") if s.strip()]
        bad = set(sev) - set(SEV_ORDER)
        if bad:
            raise ValueError(f"severity must be from {SEV_ORDER} (K fatal, A serious, B minor, C possible, O PDO)")
        clauses.append("severity = ANY(%(severity)s)"); p["severity"] = list(sev)
    if f.get("place") and not f.get("juris"):
        try:
            srv.resolve_place(f["place"])
        except ValueError:   # not a gazetteer point: treat it as a county/city name ("Prince George County")
            if not resolve_juris(srv, f["place"]):
                if re.search(r"\s(and|&|at)\s|/", f["place"], re.I):
                    raise ValueError(f"{f['place']!r} looks like two roads: call find_intersection(road_a, road_b) "
                                     "and pass its NODE id as node='...' instead of place")
                raise ValueError(f"{f['place']!r} is neither a known place nor a jurisdiction in the crash data. "
                                 f"For a county or city use juris='Henrico'; known places: Richmond, RIC airport, West End")
            f = {**f, "juris": f["place"], "place": None}
    if f.get("juris"):
        names = resolve_juris(srv, f["juris"])
        if not names:
            raise ValueError(f"no jurisdiction matches {f['juris']!r}")
        clauses.append("juris = ANY(%(juris_names)s)"); p["juris_names"] = names
    for key, col in (("collision", "collision_type"), ("route", "route_label"),
                     ("weather", "weather"), ("surface", "surface")):
        if f.get(key):
            clauses.append(f"{col} ILIKE %({key})s"); p[key] = f"%{f[key]}%"
    for key in ("night", "alcohol", "speed", "distracted", "pedestrian", "bike"):
        if f.get(key) is not None:
            clauses.append(f"{key} = %({key})s"); p[key] = bool(f[key])
    if f.get("node"):
        nodes = f["node"] if isinstance(f["node"], list) else [n.strip() for n in str(f["node"]).split(",") if n.strip()]
        bad = [n for n in nodes if not str(n).isdigit()]
        if bad:
            raise ValueError(f"node must be the numeric NODE id from find_intersection or crash_hotspots "
                             f"(e.g. node='536154'), not {bad[0]!r}")
        clauses.append("node = ANY(%(nodes)s)"); p["nodes"] = [str(n) for n in nodes]
    if f.get("intersection_only"):
        clauses.append("node IS NOT NULL")
    if f.get("place") or (f.get("lon") is not None and f.get("lat") is not None):
        lon, lat = f.get("lon"), f.get("lat")
        if f.get("place"):
            _, lon, lat = srv.resolve_place(f["place"])
        miles = float(f.get("radius_miles") or 5)
        clauses.append("ST_DWithin(geom::geography, ST_MakePoint(%(lon)s, %(lat)s)::geography, %(r)s)")
        p.update(lon=float(lon), lat=float(lat), r=miles * srv.METERS_PER_MILE)
    return " AND ".join(clauses), p


_JURIS: list[str] = []


def _plain(name: str) -> str:
    """'074. Prince George County' -> 'prince george'"""
    n = re.sub(r"^\s*\d+\.?\s*", "", name.lower())
    n = re.sub(r"\b(county|city|town|of|va|virginia)\b", " ", n)
    return re.sub(r"[^a-z]+", " ", n).strip()


def resolve_juris(srv, text: str) -> list[str]:
    """Jurisdiction names in the data that match free text, tolerating case, numbering, the words
    County/City, and typos ('prince grorge' -> '074. Prince George County')."""
    global _JURIS
    if not _JURIS:
        _JURIS = [r["juris"] for r in srv.fetch("SELECT DISTINCT juris FROM crash.crashes WHERE juris IS NOT NULL")]
    want = _plain(text)
    if not want:
        return []
    plain = {j: _plain(j) for j in _JURIS}
    exact = [j for j, n in plain.items() if n == want]
    if exact:
        return exact
    sub = [j for j, n in plain.items() if want in n]
    if sub:
        return sub
    close = difflib.get_close_matches(want, list(plain.values()), n=1, cutoff=0.75)
    return [j for j, n in plain.items() if close and n == close[0]]


def scope(srv, f: dict) -> str:
    """Plain statement of what the filters actually selected, returned with every result so a
    statewide answer is never mistaken for a county one."""
    _, p = build_where(srv, f)
    parts = []
    if f.get("node"):
        parts.append(f"intersection node {f['node']}")
    if p.get("juris_names"):
        parts.append(", ".join(re.sub(r"^\s*\d+\.?\s*", "", j) for j in p["juris_names"]))
    elif f.get("place"):
        parts.append(f"within {float(f.get('radius_miles') or 5):g} mi of {srv.resolve_place(f['place'])[0]}")
    elif not f.get("node"):
        parts.append("ALL jurisdictions in the data (no area filter)")
    if f.get("year_from") or f.get("year_to"):
        parts.append(f"years {f.get('year_from') or 'start'}-{f.get('year_to') or 'latest'}")
    else:
        parts.append("ALL years in the data (no year filter; pass year_from for 'since <year>')")
    for k in ("severity", "collision", "route", "weather", "surface"):
        if f.get(k):
            parts.append(f"{k} {f[k] if not isinstance(f[k], list) else ','.join(f[k])}")
    for k in ("night", "alcohol", "speed", "distracted", "pedestrian", "bike"):
        if f.get(k) is not None:
            parts.append(f"{k}={'yes' if f[k] else 'no'}")
    return " · ".join(parts)


ROAD_NAMES_FILE = Path(os.environ.get("CRASH_ROAD_NAMES", Path(__file__).with_name("road_names.json")))


def road_names() -> dict[str, str]:
    """Local street names -> VDOT route labels, maintained by the analyst (road_names.json).
    The crash data carries route numbers only, never street names."""
    try:
        raw = json.loads(ROAD_NAMES_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {_road_key(k): v for k, v in raw.items() if not k.startswith("_")}


def _road_key(name: str) -> str:
    n = re.sub(r"[^a-z0-9 ]+", " ", name.lower())
    n = re.sub(r"\b(road|rd|drive|dr|street|st|avenue|ave|boulevard|blvd|highway|hwy|lane|ln|parkway|pkwy)\b", " ", n)
    return re.sub(r"\s+", " ", n).strip()


def route_pattern(road: str) -> tuple[str, str, str]:
    """A road as the user names it -> (column, regex, label) for matching crash records:
      'VA-156', 'SR 156', 'Route 630', '630', 'US 460', 'I-95'  -> route_label
      names listed in road_names.json                           -> route_label of that route
      any other name ('Middle Road', 'Prince George Drive')     -> the street name VDOT records
                                                                   (ROUTE_OR_STREET_NM, in the daily
                                                                   ArcGIS sync), whole words, any case"""
    label = road_names().get(_road_key(road), road)
    m = re.search(r"(?i)\b(i|us|va|sr|rt|route|state route|sc)?[\s-]*0*(\d{1,5})\b", label)
    if m:
        prefix, num = (m.group(1) or "").lower(), m.group(2)
        pre = {"i": "I-", "us": "US-", "va": "VA-", "sr": "VA-", "state route": "VA-", "rt": "Rt ", "route": None,
               "sc": "Rt "}.get(prefix)
        return "route_label", (f"^{re.escape(pre)}{num}$" if pre else f"(^|[^0-9]){num}$"), label
    words = _road_key(label).split()
    if not words or not all(re.fullmatch(r"[a-z0-9]{1,30}", w) for w in words):
        raise ValueError(f"{road!r} is not a route number or a street name")
    # whole words in order: "middle" matches "MIDDLE RD" but not "MIDDLETON"
    return "street", r"\m" + r"\s+".join(words) + r"\M", label


def road_match_sql(kind: str, param: str) -> str:
    if kind == "street":
        return f"(props->>'ROUTE_OR_STREET_NM') ~* %({param})s"
    return f"route_label ~ %({param})s"


def sev_cols() -> str:
    return ", ".join(f"count(*) FILTER (WHERE severity = '{s}') AS {s.lower()}" for s in SEV_ORDER)


def epdo_expr(w: dict) -> str:
    return " + ".join(f"{float(w.get(s, 0))} * count(*) FILTER (WHERE severity = '{s}')" for s in SEV_ORDER)


GI_BINS = [(-3, "#2166ac", "Cold spot, 99% confidence"), (-2, "#67a9cf", "Cold spot, 95%"),
           (-1, "#d1e5f0", "Cold spot, 90%"), (0, "#f2f2f2", "Not significant"),
           (1, "#fddbc7", "Hot spot, 90%"), (2, "#ef8a62", "Hot spot, 95%"), (3, "#b2182b", "Hot spot, 99% confidence")]


def gi_star(cells: dict[tuple[int, int], float]) -> dict[tuple[int, int], tuple[float, int]]:
    """Getis-Ord Gi* on a regular grid of crash counts (or EPDO).

    Neighbours: the 3x3 block around each cell (itself included). The study area is the cells that
    have at least one crash, so empty fields and water don't make every road look "hot". Confidence
    bins are corrected for multiple testing with Benjamini-Hochberg FDR, as ArcGIS's Optimized Hot
    Spot Analysis does. Returns {(gx, gy): (z, bin)} with bin in -3..3."""
    n = len(cells)
    if n < 3:
        return {k: (0.0, 0) for k in cells}
    vals = list(cells.values())
    mean = sum(vals) / n
    s = math.sqrt(max(sum(v * v for v in vals) / n - mean * mean, 1e-12))
    z = {}
    for (gx, gy) in cells:
        nb = [cells[(gx + dx, gy + dy)] for dx in (-1, 0, 1) for dy in (-1, 0, 1) if (gx + dx, gy + dy) in cells]
        w = len(nb)
        denom = s * math.sqrt(max((n * w - w * w) / (n - 1), 1e-12))
        z[(gx, gy)] = (sum(nb) - mean * w) / denom
    # two-sided p-values, then the largest FDR-passing p-value at each confidence level
    p = {k: math.erfc(abs(v) / math.sqrt(2)) for k, v in z.items()}
    ranked = sorted(p.values())
    def cutoff(alpha):
        best = 0.0
        for i, pv in enumerate(ranked, 1):
            if pv <= alpha * i / n:
                best = pv
        return best
    c99, c95, c90 = cutoff(0.01), cutoff(0.05), cutoff(0.10)
    out = {}
    for k, v in z.items():
        level = 3 if p[k] <= c99 and c99 else 2 if p[k] <= c95 and c95 else 1 if p[k] <= c90 and c90 else 0
        out[k] = (round(v, 2), level if v > 0 else -level)
    return out


def register(mcp, srv) -> None:
    fetch, audited, READER, WRITER = srv.fetch, srv.audited, srv.READER, srv.WRITER
    RO, RW = srv.RO, srv.RW

    def ensure_loaded():
        if not fetch("SELECT to_regclass('crash.crashes') IS NOT NULL AS ok")[0]["ok"]:
            raise ValueError("no crash data loaded yet; run: docker compose run --rm crash-loader /import/<file>.geojson")

    def filters(**kw):
        return {k: v for k, v in kw.items() if v not in (None, "", [])}

    def hotspots(top: int, rank_by: str, group: str, cluster_m: float, f: dict) -> list[dict]:
        where, p = build_where(srv, f)
        w = epdo_weights()
        order = "epdo DESC, crashes DESC" if rank_by == "severity" else "crashes DESC, epdo DESC"
        p["top"] = max(1, min(int(top), 100))
        if group == "cluster":
            lat = fetch(f"SELECT avg(ST_Y(geom)) AS lat FROM crash.crashes WHERE {where}", p)[0]["lat"] or 37.5
            p["eps"] = float(cluster_m) / math.cos(math.radians(lat))   # Web Mercator meters at this latitude
            src = (f"SELECT *, ST_ClusterDBSCAN(ST_Transform(geom, 3857), eps => %(eps)s, minpoints => 3) "
                   f"OVER () AS grp FROM crash.crashes WHERE {where}")
            key = "grp::text"
        else:
            src = f"SELECT *, node AS grp FROM crash.crashes WHERE {where} AND node IS NOT NULL"
            key = "grp"
        rows = fetch(f"""
            SELECT {key} AS id,
                   string_agg(DISTINCT route_label, ' & ') AS location,
                   count(*) AS crashes, {sev_cols()}, round(({epdo_expr(w)})::numeric, 0) AS epdo,
                   mode() WITHIN GROUP (ORDER BY collision_type) AS top_collision,
                   min(crash_year) || '-' || max(crash_year) AS years,
                   round(ST_X(ST_Centroid(ST_Collect(geom)))::numeric, 5) AS lon,
                   round(ST_Y(ST_Centroid(ST_Collect(geom)))::numeric, 5) AS lat
            FROM ({src}) s WHERE grp IS NOT NULL
            GROUP BY grp ORDER BY {order} LIMIT %(top)s""", p)
        names = {}
        if group != "cluster" and rows:
            # Name each intersection by every route seen at that node, not just the filtered crashes.
            names = {n["node"]: n["location"] for n in fetch(
                "SELECT node, string_agg(DISTINCT route_label, ' & ') AS location FROM crash.crashes "
                "WHERE node = ANY(%(nodes)s) GROUP BY node", {"nodes": [r["id"] for r in rows]})}
        for i, r in enumerate(rows, 1):
            r["rank"] = i
            if group != "cluster":
                r["node"] = r.pop("id")
                r["location"] = names.get(r["node"]) or r["location"]
            else:
                r.pop("id")
        first = ["rank", "location", "crashes", "epdo", "k", "a", "b", "c", "o"]
        return [{**{k: r[k] for k in first if k in r}, **{k: v for k, v in r.items() if k not in first}} for r in rows]


    @mcp.tool(annotations=RO)
    @audited(READER)
    def crash_summary(year_from: int | None = None, year_to: int | None = None, severity: list[str] | str | None = None,
                      collision: str | None = None, route: str | None = None, juris: str | None = None,
                      night: bool | None = None, alcohol: bool | None = None, pedestrian: bool | None = None,
                      place: str | None = None, radius_miles: float | None = None, node: str | int | None = None) -> dict:
        """Overview of loaded crash data: totals, crashes by year and severity, top collision types,
        share at intersections, top routes. Call first for general crash questions.
        Filters (all optional): year_from, year_to, severity (e.g. ["K","A"]: K fatal, A serious,
        B minor, C possible injury, O property damage), collision (text, e.g. 'rear end', 'angle'),
        route (e.g. 'US-460'), juris, weather, surface, night, alcohol, speed, distracted, pedestrian,
        bike (true/false), place + radius_miles (e.g. place='Richmond center'), node (NODE id(s) from
        find_intersection, e.g. '536154', to count crashes at one intersection)."""
        ensure_loaded()
        f = filters(year_from=year_from, year_to=year_to, severity=severity, collision=collision, route=route,
                    juris=juris, night=night, alcohol=alcohol, pedestrian=pedestrian, place=place,
                    radius_miles=radius_miles, node=node)
        where, p = build_where(srv, f)
        tot = fetch(f"SELECT count(*) AS crashes, count(*) FILTER (WHERE node IS NOT NULL) AS at_intersections, "
                    f"{sev_cols()}, min(crash_year) AS first_year, max(crash_year) AS last_year "
                    f"FROM crash.crashes WHERE {where}", p)[0]
        by_year = fetch(f"SELECT crash_year AS year, count(*) AS crashes, {sev_cols()} FROM crash.crashes "
                        f"WHERE {where} GROUP BY 1 ORDER BY 1", p)
        coll = fetch(f"SELECT collision_type, count(*) AS crashes FROM crash.crashes WHERE {where} "
                     f"GROUP BY 1 ORDER BY 2 DESC LIMIT 8", p)
        routes = fetch(f"SELECT route_label AS route, count(*) AS crashes FROM crash.crashes WHERE {where} "
                       f"GROUP BY 1 ORDER BY 2 DESC LIMIT 8", p)
        return {"scope": scope(srv, f), "totals": tot, "by_year": by_year, "top_collision_types": coll, "top_routes": routes,
                "severity_codes": SEV_LABEL}

    @mcp.tool(annotations=RO)
    @audited(READER)
    def crash_hotspots(top: int = 10, rank_by: str = "count", group: str = "intersection", cluster_m: float = 60,
                       year_from: int | None = None, year_to: int | None = None, severity: list[str] | str | None = None,
                       collision: str | None = None, route: str | None = None, juris: str | None = None,
                       night: bool | None = None, alcohol: bool | None = None, pedestrian: bool | None = None,
                       place: str | None = None, radius_miles: float | None = None) -> dict:
        """Worst crash locations. Use for 'which intersection has the most crashes'.
        rank_by: 'count' (most crashes) or 'severity' (EPDO-weighted, fatal and serious count more).
        group: 'intersection' (VDOT intersection nodes, named by the crossing routes) or 'cluster'
        (groups crashes within cluster_m meters, works for crashes not tied to a node).
        Filters (all optional): year_from, year_to, severity (e.g. ["K","A"]: K fatal, A serious,
        B minor, C possible injury, O property damage), collision (text, e.g. 'rear end', 'angle'),
        route (e.g. 'US-460'), juris, weather, surface, night, alcohol, speed, distracted, pedestrian,
        bike (true/false), place + radius_miles (e.g. place='Richmond center')."""
        ensure_loaded()
        if rank_by not in ("count", "severity") or group not in ("intersection", "cluster"):
            raise ValueError("rank_by is 'count' or 'severity'; group is 'intersection' or 'cluster'")
        f = filters(year_from=year_from, year_to=year_to, severity=severity, collision=collision,
                    route=route, juris=juris, night=night, alcohol=alcohol, pedestrian=pedestrian,
                    place=place, radius_miles=radius_miles)
        rows = hotspots(top, rank_by, group, cluster_m, f)
        return {"scope": scope(srv, f), "hotspots": rows, "ranked_by": rank_by, "grouped_by": group,
                "epdo_weights": epdo_weights(), "note": "EPDO weights are configurable; defaults are illustrative"}

    @mcp.tool(annotations=RO)
    @audited(READER)
    def find_intersection(road_a: str, road_b: str, juris: str | None = None, year_from: int | None = None,
                          year_to: int | None = None) -> dict:
        """Find where two roads meet and count the crashes there. road_a/road_b: route numbers
        ('VA-156', 'Rt 630', 'US-460', '630') or street names ('Middle Road', 'Prince George Drive').
        year_from: ALWAYS pass it when the question says 'since <year>' (e.g. year_from=2020).
        The result also has crashes by year (by_year) for the returned intersections.
        juris narrows to a county/city. Returns candidate
        intersection nodes (the same pair can meet more than once) with crash counts; pass node to
        map_crashes or crash_profile for a map or details."""
        ensure_loaded()
        ka, ra, la = route_pattern(road_a)
        kb, rb, lb = route_pattern(road_b)
        f = filters(juris=juris, year_from=year_from, year_to=year_to)
        where, p = build_where(srv, f)
        jw, jp = build_where(srv, filters(juris=juris))
        p.update({f"j_{k}": v for k, v in jp.items()}, ra=ra, rb=rb)
        jw = re.sub(r"%\((\w+)\)s", r"%(j_\1)s", jw)
        rows = fetch(f"""
            WITH n AS (SELECT node FROM crash.crashes WHERE node IS NOT NULL AND {jw}
                       GROUP BY node
                       HAVING bool_or({road_match_sql(ka, "ra")}) AND bool_or({road_match_sql(kb, "rb")}))
            SELECT c.node, string_agg(DISTINCT c.route_label, ' & ') AS routes,
                   string_agg(DISTINCT c.props->>'ROUTE_OR_STREET_NM', ' & ') AS streets, count(*) AS crashes,
                   {sev_cols()}, min(c.crash_year) || '-' || max(c.crash_year) AS years,
                   round(ST_X(ST_Centroid(ST_Collect(c.geom)))::numeric, 5) AS lon,
                   round(ST_Y(ST_Centroid(ST_Collect(c.geom)))::numeric, 5) AS lat
            FROM crash.crashes c JOIN n USING (node) WHERE {where.replace("crash_year", "c.crash_year")}
            GROUP BY c.node ORDER BY crashes DESC LIMIT 10""", p)
        if not rows:
            raise ValueError(f"no crash-coded intersection of {la} and {lb}"
                             + (f" in {juris}" if juris else "") + " in the selected years. Check the names or "
                             "route numbers (street names come from the daily ArcGIS sync).")
        by_year = {str(r["crash_year"]): int(r["crashes"]) for r in fetch(
            f"SELECT crash_year, count(*) AS crashes FROM crash.crashes c WHERE "
            f"{where.replace('crash_year', 'c.crash_year')} AND c.node = ANY(%(ids)s) "
            f"GROUP BY crash_year ORDER BY crash_year", {**p, "ids": [str(r["node"]) for r in rows]})}
        ids = ",".join(str(r["node"]) for r in rows[:3])
        yrs = "".join(f", {k}={v}" for k, v in (("year_from", year_from), ("year_to", year_to)) if v)
        return {"scope": scope(srv, f), "road_a": la, "road_b": lb, "intersections": rows,
                "total_crashes": sum(int(r["crashes"]) for r in rows), "by_year": by_year,
                "next": f"to map them call map_crashes(node='{ids}'{yrs}, style='points'); "
                        f"details: crash_profile(node='{rows[0]['node']}'{yrs}). "
                        "Pass year_from here too when the question names a start year."}

    @mcp.tool(annotations=RO)
    @audited(READER)
    def crash_profile(node: str | int | None = None, route: str | None = None, place: str | None = None,
                      radius_miles: float | None = None, year_from: int | None = None,
                      year_to: int | None = None) -> dict:
        """Detailed profile of one location: give node (from crash_hotspots) or a route or place.
        Returns severity, collision types, hour of day, light/weather/surface and contributing factors."""
        ensure_loaded()
        f = filters(route=route, place=place, radius_miles=radius_miles, year_from=year_from, year_to=year_to)
        where, p = build_where(srv, f)
        if node:
            if not str(node).strip().isdigit():
                raise ValueError(f"node must be one numeric NODE id from find_intersection (e.g. '536154'), not {node!r}")
            where += " AND node = %(node)s"; p["node"] = str(node).strip()
        if not (node or f):
            raise ValueError("give node, route or place")
        base = f"FROM crash.crashes WHERE {where}"
        def top(col, n=6):
            return fetch(f"SELECT {col} AS value, count(*) AS crashes {base} GROUP BY 1 ORDER BY 2 DESC LIMIT {n}", p)
        totals = fetch(f"SELECT count(*) AS crashes, {sev_cols()}, string_agg(DISTINCT route_label, ' & ') AS routes, "
                       f"min(crash_year) || '-' || max(crash_year) AS years {base}", p)[0]
        factors = fetch(f"""SELECT count(*) FILTER (WHERE night) AS night, count(*) FILTER (WHERE alcohol) AS alcohol,
                   count(*) FILTER (WHERE speed) AS speed, count(*) FILTER (WHERE distracted) AS distracted,
                   count(*) FILTER (WHERE pedestrian) AS pedestrian, count(*) FILTER (WHERE bike) AS bike {base}""", p)[0]
        return {"totals": totals, "collision_types": top("collision_type"),
                "by_hour": fetch(f"SELECT crash_hour AS hour, count(*) AS crashes {base} GROUP BY 1 ORDER BY 1", p),
                "weather": top("weather", 4), "surface": top("surface", 4), "factors": factors}

    @mcp.tool(annotations=RO)
    @audited(READER)
    def worst_segments(top: int = 10, segment_miles: float = 1.0, rank_by: str = "count", route: str | None = None,
                       year_from: int | None = None, year_to: int | None = None, severity: list[str] | str | None = None,
                       juris: str | None = None) -> list[dict]:
        """Worst road stretches (between intersections too): crashes per segment_miles along each route
        and direction, using VDOT route milepost (RNS_MP)."""
        ensure_loaded()
        where, p = build_where(srv, filters(route=route, year_from=year_from, year_to=year_to,
                                            severity=severity, juris=juris))
        p.update(seg=max(0.1, float(segment_miles)), top=max(1, min(int(top), 100)))
        order = "epdo DESC" if rank_by == "severity" else "crashes DESC"
        return fetch(f"""
            SELECT route_label AS route, right(rte_nm, 2) AS dir,
                   round((floor(rns_mp / %(seg)s) * %(seg)s)::numeric, 2) AS from_mp,
                   round(((floor(rns_mp / %(seg)s) + 1) * %(seg)s)::numeric, 2) AS to_mp,
                   count(*) AS crashes, {sev_cols()}, round(({epdo_expr(epdo_weights())})::numeric, 0) AS epdo
            FROM crash.crashes WHERE {where} AND rns_mp IS NOT NULL
            GROUP BY 1, 2, 3, 4 ORDER BY {order} LIMIT %(top)s""", p)

    def grid(f: dict, cell_m: float | None, divisions: int) -> tuple[list[dict], float]:
        """Aggregate crashes into square cells (degrees sized to cell_m at the data's latitude).
        One GROUP BY over the table, no reprojection, so it stays fast at millions of rows."""
        where, p = build_where(srv, f)
        ext = fetch(f"SELECT ST_XMin(e) AS x0, ST_YMin(e) AS y0, ST_XMax(e) AS x1, ST_YMax(e) AS y1, n FROM "
                    f"(SELECT ST_Extent(geom) AS e, count(*) AS n FROM crash.crashes WHERE {where}) q", p)[0]
        if not ext["n"]:
            raise ValueError("no crashes matched the filters")
        lat = (ext["y0"] + ext["y1"]) / 2
        m_per_deg_x = 111_320 * math.cos(math.radians(lat))
        if not cell_m:
            diag = math.hypot((ext["x1"] - ext["x0"]) * m_per_deg_x, (ext["y1"] - ext["y0"]) * 110_574)
            cell_m = max(150.0, diag / divisions)
        p.update(dx=cell_m / m_per_deg_x, dy=cell_m / 110_574)
        rows = fetch(f"""
            SELECT floor(ST_X(geom) / %(dx)s)::int AS gx, floor(ST_Y(geom) / %(dy)s)::int AS gy,
                   count(*) AS crashes, count(*) FILTER (WHERE severity IN ('K','A')) AS fatal_serious,
                   round(({epdo_expr(epdo_weights())})::numeric, 0)::float8 AS epdo
            FROM crash.crashes WHERE {where} GROUP BY 1, 2""", p)
        for r in rows:
            r["_dx"], r["_dy"] = p["dx"], p["dy"]
        return rows, cell_m

    def cell_polygon(r):
        x0, y0 = r["gx"] * r["_dx"], r["gy"] * r["_dy"]
        x1, y1 = x0 + r["_dx"], y0 + r["_dy"]
        return {"type": "Polygon", "coordinates": [[[x0, y0], [x1, y0], [x1, y1], [x0, y1], [x0, y0]]]}

    def props(r, *extra):
        keep = {"crashes": r["crashes"], "fatal_serious": r["fatal_serious"], "epdo": r["epdo"]}
        for k in extra:
            keep[k] = r[k]
        return keep

    def fc(features):
        return {"type": "FeatureCollection", "features": features}

    def ramp(values, colors=("#fff7bc", "#fec44f", "#fe9929", "#d95f0e", "#993404")):
        vals = sorted(v for v in values if v is not None)
        if not vals:
            return [[0, colors[0]], [1, colors[-1]]]
        qs = [vals[min(len(vals) - 1, int(q * (len(vals) - 1)))] for q in (0, 0.5, 0.8, 0.95, 1)]
        stops, last = [], None
        for q, c in zip(qs, colors):
            if last is None or q > last:
                stops.append([q, c]); last = q
        return stops if len(stops) > 1 else [[stops[0][0], colors[0]], [stops[0][0] + 1, colors[-1]]]

    def style_layers(style: str, top: int, rank_by: str, f: dict, cell_m: float | None,
                     weight_by: str) -> tuple[list[dict], str]:
        """Build map layers for one visual style. Returns (layers, note)."""
        weight = "epdo" if weight_by == "severity" else "crashes"
        if style == "heatmap":
            rows, cm = grid(f, cell_m, 250)
            feats = [{"type": "Feature", "properties": props(r),
                      "geometry": {"type": "Point", "coordinates": [(r["gx"] + .5) * r["_dx"], (r["gy"] + .5) * r["_dy"]]}}
                     for r in rows]
            return ([{"name": f"Crash density ({'severity-weighted' if weight == 'epdo' else 'count'})", "kind": "heatmap",
                      "weight_field": weight, "cell_m": cm, "color": "#f97316", "geojson": fc(feats),
                      "legend_note": f"{sum(r['crashes'] for r in rows):,} crashes in {len(rows):,} cells of {cm:,.0f} m"}],
                    f"heatmap from {len(rows):,} cells of {cm:,.0f} m")
        if style == "grid":
            rows, cm = grid(f, cell_m, 80)
            feats = [{"type": "Feature", "properties": props(r), "geometry": cell_polygon(r)} for r in rows]
            return ([{"name": f"Crashes per {cm:,.0f} m cell", "color": "#d95f0e", "color_field": weight,
                      "ramp": ramp([r[weight] for r in rows]), "geojson": fc(feats),
                      "legend_note": f"low to high {weight}, {len(rows):,} cells"}],
                    f"{len(rows):,} cells of {cm:,.0f} m")
        if style == "hotspots":
            rows, cm = grid(f, cell_m, 80)
            gi = gi_star({(r["gx"], r["gy"]): float(r[weight]) for r in rows})
            feats = []
            for r in rows:
                z, b = gi[(r["gx"], r["gy"])]
                r["gi_z"], r["gi_bin"] = z, b
                if b != 0:
                    feats.append({"type": "Feature", "properties": props(r, "gi_z", "gi_bin"), "geometry": cell_polygon(r)})
            hot = sum(1 for x in feats if x["properties"]["gi_bin"] > 0)
            return ([{"name": "Hot spots (Getis-Ord Gi*)", "color": "#b2182b", "color_field": "gi_bin",
                      "ramp": [[b, c] for b, c, _ in GI_BINS], "geojson": fc(feats),
                      "legend_note": f"{hot} hot / {len(feats) - hot} cold of {len(rows):,} cells ({cm:,.0f} m), "
                                     f"90/95/99% confidence; red = hot, blue = cold"}],
                    f"Gi* on {len(rows):,} cells of {cm:,.0f} m (3x3 neighbours, FDR-corrected)")
        if style == "clusters":
            where, p = build_where(srv, f)
            n = fetch(f"SELECT count(*) AS n FROM crash.crashes WHERE {where}", p)[0]["n"]
            if n <= 200_000:
                hs = hotspots(min(int(top) * 10, 500), rank_by, "cluster", cell_m or 60, f)
                how = f"DBSCAN clusters ({cell_m or 60:.0f} m, 3+ crashes)"
            else:  # too many points for DBSCAN within the query limit: use fine grid cells instead
                rows, cm = grid(f, cell_m or 250, 400)
                rows.sort(key=lambda r: r[weight], reverse=True)
                hs = [{"crashes": r["crashes"], "k": None, "a": None, "epdo": r["epdo"], "location": "",
                       "lon": (r["gx"] + .5) * r["_dx"], "lat": (r["gy"] + .5) * r["_dy"]} for r in rows[:min(int(top) * 10, 500)]]
                how = f"densest {cm:,.0f} m cells (dataset too large for DBSCAN; add a place or route filter)"
            feats = [{"type": "Feature", "geometry": {"type": "Point", "coordinates": [float(h["lon"]), float(h["lat"])]},
                      "properties": {k: v for k, v in h.items() if k not in ("lon", "lat") and v not in (None, "")}} for h in hs]
            return ([{"name": "Crash clusters", "color": "#7b2cbf", "size_field": "crashes", "geojson": fc(feats),
                      "legend_note": how}], how)
        # points (default)
        return crash_layers(top, rank_by, f), "crash points by severity, top intersections sized by count"

    COLLISION_COLORS = ["#1f77b4", "#ff7f0e", "#2ca02c", "#d62728", "#9467bd", "#8c564b"]

    def crash_layers(top: int, rank_by: str, f: dict, max_points: int = 20_000,
                     color_by: str = "severity") -> list[dict]:
        where, p = build_where(srv, f)
        p["cap"] = max_points
        def pts(name, color, cond, extra):
            sql = (f"SELECT document_nbr, crash_dt, severity, collision_type, route_label, geom FROM crash.crashes "
                   f"WHERE {where} AND {cond} LIMIT %(cap)s")
            rows = fetch(f"SELECT ST_AsGeoJSON(t.*)::json AS f FROM ({sql}) t", {**p, **extra})
            return {"name": name, "color": color, "radius": 4,
                    "geojson": {"type": "FeatureCollection", "features": [r["f"] for r in rows]}}
        if color_by == "collision":
            kinds = [r["t"] for r in fetch(
                f"SELECT collision_type AS t, count(*) AS n FROM crash.crashes WHERE {where} "
                f"AND coalesce(collision_type, '') NOT IN ('', 'Not Provided') GROUP BY 1 ORDER BY n DESC LIMIT 6", p)]
            layers = [pts(re.sub(r"^\s*\d+\.\s*", "", k), COLLISION_COLORS[i], "collision_type = %(ct)s", {"ct": k})
                      for i, k in enumerate(kinds)]
            layers.append(pts("Other / not provided", "#b0b7bf",
                              "NOT (coalesce(collision_type, '') = ANY(%(cts)s))", {"cts": kinds}))
        else:
            layers = [pts("Property damage only", "#9aa5b1", "severity = ANY(%(s)s)", {"s": ["O"]}),
                      pts("Injury (B, C)", "#ff9f1c", "severity = ANY(%(s)s)", {"s": ["B", "C"]}),
                      pts("Fatal or serious (K, A)", "#d62728", "severity = ANY(%(s)s)", {"s": ["K", "A"]})]
        hs = hotspots(top, rank_by, "intersection", 60, f)
        layers.append({"name": f"Top {len(hs)} intersections", "color": "#3b1f8f", "size_field": "crashes",
                       "geojson": {"type": "FeatureCollection", "features": [
                           {"type": "Feature", "geometry": {"type": "Point", "coordinates": [float(h["lon"]), float(h["lat"])]},
                            "properties": {k: v for k, v in h.items() if k not in ("lon", "lat")}} for h in hs]}})
        return layers

    STYLES = ("points", "heatmap", "grid", "hotspots", "clusters")

    @mcp.tool(annotations=RW)
    @audited(WRITER)
    def map_crashes(title: str = "Crash analysis", style: str = "points", top: int = 10, rank_by: str = "count",
                    weight_by: str = "count", cell_m: float | None = None,
                    year_from: int | None = None, year_to: int | None = None, severity: list[str] | str | None = None,
                    collision: str | None = None, route: str | None = None, juris: str | None = None,
                    night: bool | None = None, place: str | None = None, radius_miles: float | None = None,
                    node: str | int | None = None, color_by: str = "severity") -> dict:
        """Web map of crashes. node: one intersection node (from find_intersection or crash_hotspots),
        or several comma-separated. color_by (points style): 'severity' (default) or 'collision'
        (top 6 collision types, e.g. rear end, angle, each its own color) for 'what type of crashes'. style:
        - 'points': crashes colored by severity (or collision type) + top intersections sized by count (best for a county or less)
        - 'heatmap': crash density heat map (scales to statewide)
        - 'grid': crashes counted per square cell, shaded low to high
        - 'hotspots': statistically significant hot and cold spots (Getis-Ord Gi*)
        - 'clusters': clusters of nearby crashes, sized by count
        weight_by: 'count' or 'severity' (EPDO). cell_m: cell size in meters (auto if omitted).
        Filters: years, severity, collision, route, juris, night, place + radius_miles."""
        ensure_loaded()
        if style not in STYLES:
            raise ValueError(f"style must be one of {STYLES}")
        f = filters(year_from=year_from, year_to=year_to, severity=severity, collision=collision, route=route,
                    juris=juris, night=night, place=place, radius_miles=radius_miles, node=node)
        if color_by not in ("severity", "collision"):
            raise ValueError("color_by must be 'severity' or 'collision'")
        if style == "points" and color_by == "collision":
            layers, note = crash_layers(top, rank_by, f, color_by="collision"), "points colored by collision type"
        else:
            layers, note = style_layers(style, top, rank_by, f, cell_m, weight_by)
        if not any(l["geojson"]["features"] for l in layers):
            raise ValueError("no crashes matched the filters (or none formed a cluster); widen the area or filters")
        sc = scope(srv, f)
        name = f"{srv.slug(title)}-{int(time.time())}.html"
        srv.storage.put_text(name, srv.render_map(title, layers, srv.MAP_BASEMAP, subtitle=sc))
        return {"url": f"{srv.MAP_VIEWER_URL}/maps/{name}", "scope": sc, "style": style, "method": note,
                "layers": {l["name"]: len(l["geojson"]["features"]) for l in layers}}

    @mcp.tool(annotations=RW)
    @audited(WRITER)
    def export_crashes_for_pro(style: str = "points", top: int = 10, rank_by: str = "count",
                               year_from: int | None = None, year_to: int | None = None,
                               severity: list[str] | str | None = None, collision: str | None = None,
                               route: str | None = None, juris: str | None = None,
                               place: str | None = None, radius_miles: float | None = None) -> dict:
        """STEP 1 of an ArcGIS Pro crash map. Pass the returned pro_layers UNCHANGED as layers to
        build_pro_map (STEP 2). style:
        - 'points': crashes by severity + top intersections
        - 'heatmap': crash points drawn with ArcGIS Pro's heat map symbology
        - 'hotspots': ArcGIS Pro runs Optimized Hot Spot Analysis (Getis-Ord Gi*, hexagons)
        - 'clusters': ArcGIS Pro runs Density-based Clustering (DBSCAN)
        Heatmap/hotspots/clusters export individual crashes (up to 250,000), so filter large areas
        by place + radius_miles, route or juris."""
        ensure_loaded()
        if style not in ("points", "heatmap", "hotspots", "clusters"):
            raise ValueError("style must be points, heatmap, hotspots or clusters")
        f = filters(year_from=year_from, year_to=year_to, severity=severity, collision=collision, route=route,
                    juris=juris, place=place, radius_miles=radius_miles)
        if style == "points":
            layers = crash_layers(top, rank_by, f)
        else:
            where, p = build_where(srv, f)
            p["cap"] = 250_000
            n = fetch(f"SELECT count(*) AS n FROM crash.crashes WHERE {where}", p)[0]["n"]
            sql = (f"SELECT document_nbr, crash_year, severity, collision_type, route_label, node, geom "
                   f"FROM crash.crashes WHERE {where} LIMIT %(cap)s")
            rows = fetch(f"SELECT ST_AsGeoJSON(t.*)::json AS f FROM ({sql}) t", p)
            analysis = {"heatmap": "heatmap", "hotspots": "hotspot", "clusters": "clusters"}[style]
            layers = [{"name": {"heatmap": "Crash heat map", "hotspots": "Crash hot spots (Gi*)",
                                "clusters": "Crash clusters (DBSCAN)"}[style],
                       "color": "#d62728", "analysis": analysis, "geojson": fc([r["f"] for r in rows]),
                       "truncated": n > p["cap"]}]
        pro_layers, counts = [], {}
        srv.storage.delete_matching("exports", "crash_*")  # keep only the current crash export, so "the crash layer" is unambiguous
        for l in layers:
            key = "crash_" + srv.slug(l["name"]).replace("-", "_")
            srv.storage.put_text(f"exports/{key}.geojson", json.dumps(l["geojson"], default=str))
            counts[l["name"]] = len(l["geojson"]["features"])
            if l["geojson"]["features"]:
                spec = {"file": f"{key}.geojson", "name": l["name"], "color": l["color"]}
                if l.get("analysis"):
                    spec["analysis"] = l["analysis"]
                srv.storage.put_text(f"exports/{key}.layer.json", json.dumps(spec))  # Pro reads defaults
                pro_layers.append(spec)
        if not pro_layers:
            raise ValueError("no crashes matched the filters")
        result = {"scope": scope(srv, f), "pro_layers": pro_layers, "features": counts,
                  "downloads": [srv.export_url(l["file"]) for l in pro_layers],
                  "next": "call build_pro_map with layers=pro_layers and a title"}
        try:  # matching interactive web map of the same crashes, for the chat's Map tab
            web_layers, _ = style_layers(style, top, rank_by, f, None, "count")
            name = f"crashes-{style}-{int(time.time())}.html"
            srv.storage.put_text(name, srv.render_map(
                f"Crashes: {style} (interactive view of the Pro export)", web_layers, srv.MAP_BASEMAP,
                subtitle=result["scope"]))
            result["url"] = f"{srv.MAP_VIEWER_URL}/maps/{name}"
        except Exception as exc:  # the Pro export is the deliverable; the preview is best effort
            result["web_map_skipped"] = f"{type(exc).__name__}: {str(exc)[:200]}"
        if any(l.get("truncated") for l in layers):
            result["warning"] = "more than 250,000 crashes matched; only the first 250,000 were exported. Add filters."
        return result

    # ------------------------------------------------------------ road network + dashboard
    from . import crash_dashboard as dash, crash_network as cn

    NET_MAX_NODES = 8000
    LOAD_RAMP = [[0, "#cbd5e1"], [0.15, "#fdba74"], [0.4, "#f97316"], [1, "#b91c1c"]]

    def area_filters(juris, place, radius_miles) -> dict:
        area = filters(juris=juris, place=place, radius_miles=radius_miles)
        if not (area.get("juris") or area.get("place")):
            raise ValueError("the road network is built per area: pass juris (county or city, e.g. "
                             "'Prince George') or place + radius_miles")
        return area

    def prefixed(where: str, p: dict, tag: str) -> tuple[str, dict]:
        return re.sub(r"%\((\w+)\)s", rf"%({tag}_\1)s", where), {f"{tag}_{k}": v for k, v in p.items()}

    def node_names(ids: list[str]) -> dict[str, str]:
        """'MIDDLE RD & PRINCE GEORGE DR'-style names: the two commonest street names at each node."""
        if not ids:
            return {}
        return {r["node"]: r["name"] for r in fetch("""
            SELECT node, string_agg(s, ' & ' ORDER BY n DESC) AS name FROM (
              SELECT node, upper(trim(props->>'ROUTE_OR_STREET_NM')) AS s, count(*) AS n,
                     row_number() OVER (PARTITION BY node ORDER BY count(*) DESC) AS rn
              FROM crash.crashes WHERE node = ANY(%(ids)s) AND coalesce(props->>'ROUTE_OR_STREET_NM', '') <> ''
              GROUP BY node, s) t WHERE rn <= 2 GROUP BY node""", {"ids": list(ids)})}

    def network_graph(area: dict, f: dict):
        """Graph for an area: topology from all years, crash counts from the filters in f."""
        aw, ap = prefixed(*build_where(srv, area), "a")
        cw, cp = prefixed(*build_where(srv, f), "c")
        w = epdo_weights()
        sev = "CASE severity " + " ".join(f"WHEN '{s}' THEN {float(w.get(s, 0))}" for s in SEV_ORDER) + " ELSE 0 END"
        nodes = fetch(f"""
            SELECT node, round(ST_X(ST_Centroid(ST_Collect(geom)))::numeric, 6) AS lon,
                   round(ST_Y(ST_Centroid(ST_Collect(geom)))::numeric, 6) AS lat,
                   count(*) FILTER (WHERE {cw}) AS crashes,
                   count(*) FILTER (WHERE {cw} AND severity = 'K') AS k,
                   count(*) FILTER (WHERE {cw} AND severity = 'A') AS a,
                   coalesce(sum({sev}) FILTER (WHERE {cw}), 0) AS epdo,
                   string_agg(DISTINCT route_label, ' & ') AS routes
            FROM crash.crashes WHERE node IS NOT NULL AND {aw} GROUP BY node LIMIT {NET_MAX_NODES + 1}""", {**ap, **cp})
        if len(nodes) > NET_MAX_NODES:
            raise ValueError(f"more than {NET_MAX_NODES:,} intersections in this area; pick a county, city or smaller radius")
        links = fetch(f"""
            WITH n AS (
              SELECT node, regexp_replace(rte_nm, '(NB|SB|EB|WB)$', '') AS rte, min(route_label) AS label,
                     percentile_cont(0.5) WITHIN GROUP (ORDER BY rns_mp) AS mp
              FROM crash.crashes
              WHERE node IS NOT NULL AND rns_mp IS NOT NULL AND rte_nm IS NOT NULL AND {aw}
              GROUP BY 1, 2),
            o AS (SELECT label, node, mp, lead(node) OVER w AS nnode, lead(mp) OVER w AS nmp
                  FROM n WINDOW w AS (PARTITION BY rte ORDER BY mp))
            SELECT node, nnode, (nmp - mp) AS miles, label AS rte FROM o WHERE nnode IS NOT NULL""", ap)
        g = cn.build_graph(nodes, links)
        if g.number_of_nodes() < 3:
            raise ValueError("too few connected intersections here to build a road network (needs route mileposts)")
        return g

    def network_limits() -> str:
        return ("Road network built from VDOT crash records: intersections that have had a crash are nodes, "
                "consecutive ones on a route (by milepost) are linked. Quiet intersections are skipped and "
                "links are drawn as straight lines. Through-traffic is shortest-path betweenness between "
                "intersections, not measured traffic volume.")

    def criticality_result(g, top: int) -> dict:
        res = cn.criticality(g, top)
        names = node_names([r["node"] for r in res["top"]])
        for r in res["top"]:
            r["location"] = names.get(r["node"]) or r["location"]
        return res

    def network_layers(g, res: dict) -> list[dict]:
        top = res["top"]
        return [
            {"name": "Road links (thicker/redder = more through-trips)", "lines": True, "color": "#94a3b8",
             "color_field": "load", "width_field": "load", "ramp": LOAD_RAMP,
             "geojson": fc(cn.link_features(g, res["edge_load"], "load")),
             "legend_note": f"{res['links']:,} links between {res['nodes']:,} intersections"},
            {"name": f"Top {len(top)} critical and dangerous intersections", "color": "#3b1f8f",
             "size_field": "critical_and_dangerous",
             "geojson": fc([{"type": "Feature", "geometry": {"type": "Point", "coordinates": [float(r["lon"]), float(r["lat"])]},
                             "properties": {k: v for k, v in r.items() if k not in ("lon", "lat")}} for r in top])},
        ]

    @mcp.tool(annotations=RW)
    @audited(WRITER)
    def crash_network(juris: str | None = None, place: str | None = None, radius_miles: float | None = None,
                      year_from: int | None = None, year_to: int | None = None, top: int = 10,
                      title: str = "Road network: critical and dangerous intersections") -> dict:
        """Road NETWORK analysis for a county/city (juris) or place + radius_miles: builds the road graph
        from VDOT intersection nodes and route mileposts, then ranks intersections that are both
        CRITICAL (many shortest trips between intersections pass through them: betweenness centrality)
        and DANGEROUS (crash severity, EPDO). Returns the ranking and a map of links colored by
        through-traffic. Use for 'critical / most important intersections', 'network', 'betweenness',
        'chokepoints'. Then road_closure_cascade(node=...) for what happens if one is closed."""
        ensure_loaded()
        area = area_filters(juris, place, radius_miles)
        f = {**area, **filters(year_from=year_from, year_to=year_to)}
        g = network_graph(area, f)
        res = criticality_result(g, max(1, min(int(top), 50)))
        sc = scope(srv, f)
        name = f"{srv.slug(title)}-{int(time.time())}.html"
        srv.storage.put_text(name, srv.render_map(title, network_layers(g, res), srv.MAP_BASEMAP, subtitle=sc))
        return {"scope": sc, "url": f"{srv.MAP_VIEWER_URL}/maps/{name}",
                "network": {k: res[k] for k in ("nodes", "links", "components", "sampled_origins")},
                "critical_and_dangerous": [{k: v for k, v in r.items() if k not in ("lon", "lat")} for r in res["top"]],
                "method": network_limits(),
                "next": "road_closure_cascade(node='<node>') shows where trips detour if that intersection closes"}

    @mcp.tool(annotations=RW)
    @audited(WRITER)
    def road_closure_cascade(node: str | int, juris: str | None = None, place: str | None = None,
                             radius_miles: float | None = None, year_from: int | None = None,
                             year_to: int | None = None, top: int = 10, title: str = "Closure cascade") -> dict:
        """CASCADE: what if intersection `node` is closed (crash, construction, flooding)? Removes it from
        the road network and reroutes the shortest trips: returns the links and intersections that
        absorb the detoured trips, with their crash history (detour risk), and intersections cut off.
        node: NODE id from find_intersection, crash_hotspots or crash_network. juris: the county or
        city network to use (default: the node's own)."""
        ensure_loaded()
        node = str(node).strip()
        if not node.isdigit():
            raise ValueError("node must be a numeric NODE id (from find_intersection, crash_hotspots or crash_network)")
        if not (juris or place):
            hit = fetch("SELECT juris FROM crash.crashes WHERE node = %(n)s AND juris IS NOT NULL LIMIT 1", {"n": node})
            if not hit:
                raise ValueError(f"no crashes recorded at node {node}")
            juris = hit[0]["juris"]
        area = area_filters(juris, place, radius_miles)
        f = {**area, **filters(year_from=year_from, year_to=year_to)}
        g = network_graph(area, f)
        res = cn.closure_cascade(g, node, max(1, min(int(top), 50)))
        names = node_names([node] + [r["node"] for r in res["absorbing_intersections"]])
        for r in res["absorbing_intersections"]:
            r["location"] = names.get(r["node"]) or r["location"]
        closed_name = names.get(node) or res["closed_location"]
        h = g.copy()
        h.remove_node(node)
        cg = g.nodes[node]
        layers = [
            {"name": "Links with detoured trips (redder = more)", "lines": True, "color": "#94a3b8",
             "color_field": "increase", "width_field": "increase", "ramp": LOAD_RAMP,
             "geojson": fc(cn.link_features(h, res.pop("_gain"), "increase")),
             "legend_note": f"{res['links_with_more_trips']:,} links carry more trips"},
            {"name": "Intersections absorbing detours", "color": "#f97316", "size_field": "extra_trips",
             "geojson": fc([{"type": "Feature", "geometry": {"type": "Point", "coordinates": [float(r["lon"]), float(r["lat"])]},
                             "properties": {k: v for k, v in r.items() if k not in ("lon", "lat")}}
                            for r in res["absorbing_intersections"]])},
            {"name": f"Closed: {closed_name}"[:90], "color": "#111827", "radius": 11,
             "geojson": fc([{"type": "Feature", "geometry": {"type": "Point", "coordinates": [float(cg["lon"]), float(cg["lat"])]},
                             "properties": {"node": node, "location": closed_name, "crashes": cg.get("crashes")}}])},
        ]
        sc = scope(srv, f)
        name = f"{srv.slug(title)}-{node}-{int(time.time())}.html"
        srv.storage.put_text(name, srv.render_map(f"{title}: {closed_name}"[:120], layers, srv.MAP_BASEMAP, subtitle=sc))
        for r in res["absorbing_intersections"]:
            r.pop("lon", None), r.pop("lat", None)
        res["closed_location"] = closed_name
        return {"scope": sc, "url": f"{srv.MAP_VIEWER_URL}/maps/{name}", **res, "method": network_limits()}

    FACTOR_SQL = {
        "speed": ("Speeding", "speed"),
        "alcohol": ("Alcohol", "alcohol"),
        "distracted": ("Distracted", "distracted"),
        "night": ("Night", "night"),
        "dark_unlit": ("Dark, unlit road", "props->>'LIGHT_CONDITION' ILIKE '%%not lighted%%'"),
        "young": ("Young driver", "props->>'YOUNG_NOTYOUNG' = 'Yes'"),
        "senior": ("Senior driver", "props->>'SENIOR_NOTSENIOR' = 'Yes'"),
        "pedestrian": ("Pedestrian", "pedestrian"),
        "bike": ("Bicycle", "bike"),
        "angle": ("Angle", "collision_type ILIKE '%%angle%%'"),
        "rear end": ("Rear end", "collision_type ILIKE '%%rear end%%'"),
        "road_departure": ("Road departure", "collision_type ILIKE '%%fixed object%%'"),
        "deer": ("Deer / animal", "(collision_type ILIKE '%%deer%%' OR collision_type ILIKE '%%animal%%')"),
    }

    _state_cache: dict[str, tuple[float, dict]] = {}

    def factor_counts(where: str, p: dict, cache: bool = False) -> dict:
        """Factor counts; statewide ones (a full scan) are cached for an hour per year filter."""
        key = where + repr(sorted(p.items()))
        if cache and key in _state_cache and time.time() - _state_cache[key][0] < 3600:
            return _state_cache[key][1]
        cols = ", ".join(f"count(*) FILTER (WHERE {sql}) AS \"{key}\"" for key, (_, sql) in FACTOR_SQL.items())
        cols += ", count(*) FILTER (WHERE node IS NOT NULL) AS at_intersections"
        row = fetch(f"SELECT count(*) AS total, {cols} FROM crash.crashes WHERE {where}", p)[0]
        if cache:
            _state_cache[key] = (time.time(), row)
        return row

    @mcp.tool(annotations=RW)
    @audited(WRITER)
    def crash_dashboard(juris: str | None = None, place: str | None = None, radius_miles: float | None = None,
                        year_from: int | None = None, year_to: int | None = None, title: str | None = None) -> dict:
        """Safety DASHBOARD page for a county/city (juris) or place + radius_miles: map (crashes by
        severity + road network through-traffic), top 3 hot spot intersections, critical-and-dangerous
        intersections (network), crash factors and collision types compared with statewide, crashes
        by hour and weekday, key patterns, and FHWA countermeasure recommendations. Use for 'dashboard',
        'report', 'overview of crashes in <county>', 'patterns and recommendations'."""
        ensure_loaded()
        area = area_filters(juris, place, radius_miles)
        f = {**area, **filters(year_from=year_from, year_to=year_to)}
        where, p = build_where(srv, f)
        state_f = filters(year_from=year_from, year_to=year_to)
        sw, sp = build_where(srv, state_f)
        sc = scope(srv, f)
        kp = fetch(f"""SELECT count(*) AS crashes, {sev_cols()}, coalesce(sum(persons_injured), 0) AS injured,
                              count(*) FILTER (WHERE node IS NOT NULL) AS at_intersections,
                              count(*) FILTER (WHERE night AND severity IN ('K', 'A')) AS night_ka,
                              count(*) FILTER (WHERE severity IN ('K', 'A')) AS ka,
                              min(crash_year) AS first_year, max(crash_year) AS last_year
                       FROM crash.crashes WHERE {where}""", p)[0]
        if not kp["crashes"]:
            raise ValueError("no crashes matched; check the county name or years")
        here, state = factor_counts(where, p), factor_counts(sw, sp, cache=True)
        fx = {key: {"label": label, "n": int(here[key]), "share": here[key] / max(1, here["total"]),
                    "state_share": state[key] / max(1, state["total"])} for key, (label, _) in FACTOR_SQL.items()}
        coll = fetch(f"""SELECT collision_type AS label, count(*) AS n FROM crash.crashes
                         WHERE {where} AND coalesce(collision_type, '') NOT IN ('', 'Not Provided')
                         GROUP BY 1 ORDER BY n DESC LIMIT 8""", p)
        st = {r["label"]: r["n"] for r in fetch(
            f"SELECT collision_type AS label, count(*) AS n FROM crash.crashes WHERE {sw} AND collision_type = ANY(%(cts)s) GROUP BY 1",
            {**sp, "cts": [c["label"] for c in coll]})}
        for c in coll:
            c["share"], c["state_share"] = c["n"] / kp["crashes"], st.get(c["label"], 0) / max(1, state["total"])
        hours = {r["h"]: r["n"] for r in fetch(
            f"SELECT crash_hour AS h, count(*) AS n FROM crash.crashes WHERE {where} AND crash_hour BETWEEN 0 AND 23 GROUP BY 1", p)}
        dows = {int(r["d"]): r["n"] for r in fetch(
            f"SELECT extract(isodow FROM crash_dt) AS d, count(*) AS n FROM crash.crashes WHERE {where} AND crash_dt IS NOT NULL GROUP BY 1", p)}
        routes = fetch(f"""SELECT route_label AS r, count(*) AS n FROM crash.crashes WHERE {where} AND route_label IS NOT NULL
                           GROUP BY 1 ORDER BY n DESC LIMIT 3""", p)
        hs = hotspots(3, "count", "intersection", 60, f)
        names = node_names([h["node"] for h in hs])
        for h in hs:
            h["location"] = names.get(h["node"]) or h["location"]
        try:
            g = network_graph(area, f)
            net = criticality_result(g, 5)
        except ValueError:
            g, net = None, None

        by_hour = [int(hours.get(h, 0)) for h in range(24)]
        by_dow = [int(dows.get(d, 0)) for d in range(1, 8)]
        n = kp["crashes"]
        peak = max(range(24), key=lambda h: by_hour[h])
        rush = sum(by_hour[h] for h in (7, 8, 16, 17))
        state_int = state["at_intersections"] / max(1, state["total"])
        patterns = [
            f"{kp['at_intersections'] / n:.0%} of crashes are at intersections (statewide {state_int:.0%}).",
            f"Busiest hour: {peak}:00-{peak + 1}:00 ({by_hour[peak]:,} crashes); 7-9 am and 4-6 pm together hold {rush / n:.0%}.",
            f"Night: {fx['night']['share']:.0%} of all crashes but {kp['night_ka'] / max(1, kp['ka']):.0%} of fatal and serious ones.",
        ]
        if coll:
            kind = re.sub(r"^\s*\d+\.\s*", "", coll[0]["label"])
            patterns.append(f"Most common collision: {kind} ({coll[0]['share']:.0%}).")
        if routes:
            patterns.append("Most crashes by route: " + ", ".join(f"{r['r']} ({r['n']:,})" for r in routes) + ".")
        recs = dash.recommendations(fx, hs)
        factor_rows = sorted((v for k, v in fx.items() if k in ("speed", "alcohol", "distracted", "night", "dark_unlit",
                                                                   "young", "senior", "pedestrian", "bike")),
                             key=lambda v: v["share"], reverse=True)

        title = title or f"Crash safety dashboard: {sc.split(' · ')[0]}"
        stamp = int(time.time())
        map_name = f"{srv.slug(title)}-map-{stamp}.html"
        layers = crash_layers(10, "count", f)
        if g is not None:
            layers = network_layers(g, net)[:1] + layers
        srv.storage.put_text(map_name, srv.render_map("Crashes and road network", layers, srv.MAP_BASEMAP, subtitle=sc))
        page = dash.render_dashboard({
            "title": title, "scope": sc, "map_file": map_name,
            "kpis": {**kp, "crashes": n}, "hotspots": hs, "network": net,
            "factor_rows": factor_rows, "collisions": coll, "by_hour": by_hour, "by_dow": by_dow,
            "patterns": patterns, "recommendations": recs,
            "source": "Source: VDOT crash records (public ArcGIS layer, synced daily). " + network_limits()})
        name = f"{srv.slug(title)}-{stamp}.html"
        srv.storage.put_text(name, page)
        return {"scope": sc, "url": f"{srv.MAP_VIEWER_URL}/maps/{name}",
                "totals": {k: kp[k] for k in ("crashes", "k", "a", "b", "c", "o", "injured", "at_intersections")},
                "top_hotspots": [{"node": h["node"], "location": h["location"], "crashes": h["crashes"]} for h in hs],
                "critical_and_dangerous": [{"node": r["node"], "location": r["location"], "crashes": r["crashes"],
                                            "score": r["critical_and_dangerous"]} for r in (net or {}).get("top", [])],
                "patterns": patterns, "recommendations": [r["title"] + ": " + r["measures"] for r in recs]}
