"""Road network from VDOT's own crash records, for criticality and closure cascades.

Every crash at an intersection carries VDOT's intersection NODE id, the route name (RTE_NM) and
the route milepost (RNS_MP). Ordering a route's nodes by milepost gives the road links between
consecutive intersections, with the milepost difference as the link length. No external road
layer is needed, so this works in the air-gapped enclave as well as in AWS.

Limits, stated in every result: only intersections that have had at least one crash become
nodes, so quiet intersections are skipped (a link may span several of them), and links are
drawn as straight lines between intersections (schematic, not the road geometry).

Pure functions (no database): the MCP tools in crash_tools.py fetch the rows and call these.
"""

from __future__ import annotations

import math
import random

import networkx as nx

MAX_LINK_MILES = 3.0     # consecutive nodes further apart than this are not joined (data gap)
SAMPLE_SOURCES = 400     # betweenness from this many sampled origins on large graphs (fixed seed)


def build_graph(nodes: list[dict], links: list[dict]) -> nx.Graph:
    """nodes: node, lon, lat, crashes, epdo, k, a, routes, streets.
    links: node, nnode, miles, rte (route label)."""
    g = nx.Graph()
    for n in nodes:
        g.add_node(str(n["node"]), **{k: v for k, v in n.items() if k != "node"})
    for e in links:
        a, b, miles = str(e["node"]), str(e["nnode"]), float(e["miles"] or 0)
        if a == b or a not in g or b not in g or not (0 <= miles <= MAX_LINK_MILES):
            continue
        miles = max(miles, 0.01)
        if g.has_edge(a, b) and g[a][b]["miles"] <= miles:
            continue
        g.add_edge(a, b, miles=miles, route=e.get("rte") or "")
    g.remove_nodes_from([n for n in list(g.nodes) if g.degree(n) == 0])
    return g


def _sources(g: nx.Graph, exclude: str | None = None) -> list[str]:
    pool = sorted(n for n in g.nodes if n != exclude)
    if len(pool) <= SAMPLE_SOURCES:
        return pool
    return random.Random(42).sample(pool, SAMPLE_SOURCES)


def edge_load(g: nx.Graph, sources: list[str]) -> dict[tuple[str, str], float]:
    """Share of shortest trips between the sampled intersections that use each link."""
    eb = nx.edge_betweenness_centrality_subset(g, sources=sources, targets=sources, weight="miles",
                                               normalized=False)
    return {tuple(sorted(k)): v for k, v in eb.items()}


def criticality(g: nx.Graph, top: int = 10) -> dict:
    """Rank intersections by how many shortest trips pass through them (betweenness) and by
    their crash severity (EPDO); 'critical and dangerous' = both high (geometric mean)."""
    src = _sources(g)
    bc = nx.betweenness_centrality_subset(g, sources=src, targets=src, weight="miles", normalized=False)
    bmax = max(bc.values(), default=0) or 1
    emax = max((float(g.nodes[n].get("epdo") or 0) for n in g.nodes), default=0) or 1
    rows = []
    for n, b in bc.items():
        d = g.nodes[n]
        bn, rn = b / bmax, float(d.get("epdo") or 0) / emax
        rows.append({"node": n, "location": d.get("streets") or d.get("routes") or n,
                     "crashes": int(d.get("crashes") or 0), "k": int(d.get("k") or 0), "a": int(d.get("a") or 0),
                     "betweenness": round(bn, 3), "risk": round(rn, 3),
                     "critical_and_dangerous": round(math.sqrt(bn * rn), 3),
                     "links": g.degree(n), "lon": d.get("lon"), "lat": d.get("lat")})
    rows.sort(key=lambda r: r["critical_and_dangerous"], reverse=True)
    eload = edge_load(g, src)
    emax_load = max(eload.values(), default=0) or 1
    return {"nodes": g.number_of_nodes(), "links": g.number_of_edges(),
            "components": nx.number_connected_components(g),
            "sampled_origins": len(src), "top": rows[:top], "all": rows,
            "edge_load": {k: v / emax_load for k, v in eload.items()}}


def closure_cascade(g: nx.Graph, closed: str, top: int = 10) -> dict:
    """Close one intersection: which links and intersections take the rerouted trips, and which
    intersections are cut off. Same sampled origins before and after, so the change is the
    closure's effect, not sampling noise."""
    if closed not in g:
        raise ValueError(f"node {closed} is not in this road network (no crash-coded links there)")
    src = _sources(g, exclude=closed)
    before = edge_load(g, src)
    h = g.copy()
    h.remove_node(closed)
    after = edge_load(h, src)
    nb = nx.betweenness_centrality_subset(g, sources=src, targets=src, weight="miles", normalized=False)
    na = nx.betweenness_centrality_subset(h, sources=src, targets=src, weight="miles", normalized=False)
    gain = {e: after.get(e, 0) - before.get(e, 0) for e in after}
    gmax = max(gain.values(), default=0) or 1
    links = sorted(({"a": a, "b": b, "route": h[a][b].get("route", ""), "miles": round(h[a][b]["miles"], 2),
                     "extra_trips": round(v), "increase": round(v / gmax, 3)}
                    for (a, b), v in gain.items() if v > 0), key=lambda r: r["extra_trips"], reverse=True)
    absorb = []
    for n, v in na.items():
        d = v - nb.get(n, 0)
        if d > 0:
            nd = h.nodes[n]
            absorb.append({"node": n, "location": nd.get("streets") or nd.get("routes") or n,
                           "extra_trips": round(d), "crashes": int(nd.get("crashes") or 0),
                           "k": int(nd.get("k") or 0), "a": int(nd.get("a") or 0),
                           "lon": nd.get("lon"), "lat": nd.get("lat")})
    absorb.sort(key=lambda r: r["extra_trips"], reverse=True)
    # Intersections that could reach each other before but end up off the largest remaining piece
    pieces = list(nx.connected_components(h.subgraph(nx.node_connected_component(g, closed) - {closed})))
    main = max(pieces, key=len) if pieces else set()
    stranded = sorted(n for c in pieces if c is not main for n in c)
    return {"closed": closed, "closed_location": g.nodes[closed].get("streets") or g.nodes[closed].get("routes"),
            "sampled_origins": len(src), "links_with_more_trips": len(links),
            "top_links": links[:top], "absorbing_intersections": absorb[:top],
            "cut_off_intersections": stranded[:50], "cut_off_count": len(stranded),
            "_gain": {(r["a"], r["b"]): r["increase"] for r in links}}


def link_features(g: nx.Graph, weights: dict[tuple[str, str], float], field: str) -> list[dict]:
    """GeoJSON lines between intersections (schematic), with weights[field] in 0..1."""
    feats = []
    for a, b, d in g.edges(data=True):
        w = weights.get(tuple(sorted((a, b))), weights.get((a, b), 0.0))
        pa, pb = g.nodes[a], g.nodes[b]
        feats.append({"type": "Feature", "geometry": {"type": "LineString", "coordinates": [
            [float(pa["lon"]), float(pa["lat"])], [float(pb["lon"]), float(pb["lat"])]]},
            "properties": {field: round(float(w), 3), "route": d.get("route", ""), "miles": round(d["miles"], 2)}})
    feats.sort(key=lambda f: f["properties"][field])   # heaviest drawn last (on top)
    return feats
