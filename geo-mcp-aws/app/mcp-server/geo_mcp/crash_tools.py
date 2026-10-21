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
                      place: str | None = None, radius_miles: float | None = None, node: str | None = None) -> dict:
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
        ids = ",".join(str(r["node"]) for r in rows[:3])
        yrs = "".join(f", {k}={v}" for k, v in (("year_from", year_from), ("year_to", year_to)) if v)
        return {"scope": scope(srv, f), "road_a": la, "road_b": lb, "intersections": rows,
                "total_crashes": sum(int(r["crashes"]) for r in rows),
                "next": f"to map them call map_crashes(node='{ids}'{yrs}, style='points'); "
                        f"details: crash_profile(node='{rows[0]['node']}'{yrs}). "
                        "Pass year_from here too when the question names a start year."}

    @mcp.tool(annotations=RO)
    @audited(READER)
    def crash_profile(node: str | None = None, route: str | None = None, place: str | None = None,
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

    def crash_layers(top: int, rank_by: str, f: dict, max_points: int = 20_000) -> list[dict]:
        where, p = build_where(srv, f)
        p["cap"] = max_points
        def pts(name, color, sev):
            sql = (f"SELECT document_nbr, crash_dt, severity, collision_type, route_label, geom FROM crash.crashes "
                   f"WHERE {where} AND severity = ANY(%(s)s) LIMIT %(cap)s")
            rows = fetch(f"SELECT ST_AsGeoJSON(t.*)::json AS f FROM ({sql}) t", {**p, "s": sev})
            return {"name": name, "color": color, "radius": 4,
                    "geojson": {"type": "FeatureCollection", "features": [r["f"] for r in rows]}}
        layers = [pts("Property damage only", "#9aa5b1", ["O"]), pts("Injury (B, C)", "#ff9f1c", ["B", "C"]),
                  pts("Fatal or serious (K, A)", "#d62728", ["K", "A"])]
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
                    node: str | None = None) -> dict:
        """Web map of crashes. node: one intersection node (from find_intersection or crash_hotspots),
        or several comma-separated. style:
        - 'points': crashes colored by severity + top intersections sized by count (best for a county or less)
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
