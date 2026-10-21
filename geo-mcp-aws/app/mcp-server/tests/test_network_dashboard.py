"""Road network from crash nodes (criticality, closure cascades) and the dashboard page."""

import pytest

nx = pytest.importorskip("networkx")
from geo_mcp import crash_dashboard as dash
from geo_mcp import crash_network as cn


def ladder():
    """Two parallel routes (A: 1-2-3, B: 4-5-6) joined by rungs 1-4, 2-5, 3-6, plus a spur 3-7."""
    nodes = [{"node": str(i), "lon": -77 + i * .01, "lat": 37, "crashes": 1, "epdo": 1} for i in range(1, 8)]
    nodes[1]["epdo"] = 100   # node 2 is dangerous
    links = [{"node": "1", "nnode": "2", "miles": 1, "rte": "A"}, {"node": "2", "nnode": "3", "miles": 1, "rte": "A"},
             {"node": "4", "nnode": "5", "miles": 1, "rte": "B"}, {"node": "5", "nnode": "6", "miles": 1, "rte": "B"},
             {"node": "1", "nnode": "4", "miles": 1, "rte": "C"}, {"node": "2", "nnode": "5", "miles": 1, "rte": "C"},
             {"node": "3", "nnode": "6", "miles": 1, "rte": "C"}, {"node": "3", "nnode": "7", "miles": 1, "rte": "D"},
             {"node": "1", "nnode": "6", "miles": 9, "rte": "gap"},       # too far apart: data gap, not a link
             {"node": "2", "nnode": "2", "miles": 0, "rte": "self"}]
    return cn.build_graph(nodes, links)


def test_graph_skips_gaps_and_self_links():
    g = ladder()
    assert g.number_of_nodes() == 7 and g.number_of_edges() == 8 and not g.has_edge("1", "6")


def test_criticality_needs_both_through_traffic_and_danger():
    res = cn.criticality(ladder(), top=3)
    assert res["top"][0]["node"] == "2"                       # central and dangerous
    by = {r["node"]: r for r in res["all"]}
    assert by["3"]["betweenness"] > by["2"]["betweenness"] * 0.9   # 3 carries the spur traffic...
    assert by["3"]["critical_and_dangerous"] < by["2"]["critical_and_dangerous"]   # ...but is not dangerous


def test_closure_reroutes_and_finds_cut_off_intersections():
    g = ladder()
    c = cn.closure_cascade(g, "2")
    assert c["cut_off_count"] == 0
    assert {r["node"] for r in c["absorbing_intersections"]} >= {"5"}   # the parallel route takes the trips
    c3 = cn.closure_cascade(g, "3")
    assert c3["cut_off_intersections"] == ["7"]                # the spur is stranded
    with pytest.raises(ValueError, match="not in this road network"):
        cn.closure_cascade(g, "999")


def test_link_features_are_schematic_lines_with_weights():
    g = ladder()
    res = cn.criticality(g)
    feats = cn.link_features(g, res["edge_load"], "load")
    assert len(feats) == 8 and all(f["geometry"]["type"] == "LineString" for f in feats)
    assert max(f["properties"]["load"] for f in feats) == 1.0


def test_recommendations_fire_only_above_statewide():
    fx = {"pedestrian": {"label": "Pedestrian", "n": 40, "share": 0.08, "state_share": 0.03},
          "speed": {"label": "Speeding", "n": 50, "share": 0.10, "state_share": 0.11},
          "deer": {"label": "Deer", "n": 3, "share": 0.30, "state_share": 0.05}}       # too few to matter
    recs = dash.recommendations(fx, [])
    assert [r["title"] for r in recs] == ["Pedestrian crashes"]
    assert "8.0% of all here vs 3.0% statewide" in recs[0]["why"]


def test_dashboard_escapes_everything():
    evil = '<script>alert(1)</script>'
    page = dash.render_dashboard({
        "title": evil, "scope": evil, "map_file": 'x.html" onload="alert(1)',
        "kpis": {"crashes": 10, "k": 1, "a": 2, "injured": 3, "at_intersections": 5},
        "hotspots": [{"location": evil, "crashes": 4, "k": 0, "a": 1}],
        "network": {"top": [{"location": evil, "crashes": 4, "betweenness": .5, "critical_and_dangerous": .4}],
                    "nodes": 3, "links": 2, "sampled_origins": 3},
        "factor_rows": [{"label": evil, "n": 1, "share": .1, "state_share": .05}],
        "collisions": [{"label": "1. " + evil, "n": 2, "share": .2, "state_share": .1}],
        "by_hour": [1] * 24, "by_dow": [1] * 7, "patterns": [evil],
        "recommendations": [{"title": evil, "measures": evil, "why": evil}], "source": evil})
    assert "<script>" not in page and 'onload="alert' not in page
    assert page.count("&lt;script&gt;") >= 8
