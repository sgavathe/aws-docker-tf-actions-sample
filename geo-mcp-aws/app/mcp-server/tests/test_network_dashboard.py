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
    assert "<script>alert(1)" not in page and 'onload="alert' not in page   # payload escaped; the page's own script is static
    assert page.count("&lt;script&gt;") >= 8


def test_dashboard_zoom_links_target_the_map_frame():
    page = dash.render_dashboard({
        "title": "t", "scope": "s", "map_file": "pg-map-1.html",
        "kpis": {"crashes": 1, "k": 0, "a": 0, "injured": 0, "at_intersections": 1},
        "hotspots": [{"location": "TEMPLE AVE & PUDDLEDOCK RD", "crashes": 38, "lon": -77.33, "lat": 37.21},
                     {"location": "NO COORDS", "crashes": 2}],
        "network": {"top": [{"location": "X & Y", "crashes": 1, "betweenness": .1, "critical_and_dangerous": .1,
                             "lon": 999, "lat": 0}], "nodes": 1, "links": 0, "sampled_origins": 1},
        "factor_rows": [], "collisions": [], "by_hour": [0] * 24, "by_dow": [0] * 7, "patterns": [],
        "recommendations": [], "source": ""})
    assert 'name="dashmap"' in page
    assert ('href="pg-map-1.html#zoom=-77.33000,37.21000,17&amp;label=TEMPLE%20AVE%20%26%20PUDDLEDOCK%20RD" '
            'target="dashmap"') in page
    assert page.count('target="dashmap"') == 1          # no link without valid coordinates


def test_trend_labels_use_full_years_and_refuse_small_numbers():
    years = [2020, 2021, 2022, 2023, 2024, 2025]
    assert dash.trend({2020: 14, 2021: 12, 2024: 4, 2025: 3}, years)["label"] == "fewer"
    assert dash.trend({2020: 3, 2021: 4, 2024: 11, 2025: 13}, years)["label"] == "more"
    assert dash.trend({2020: 7, 2021: 7, 2024: 7, 2025: 8}, years)["label"] == "about the same"
    assert dash.trend({2020: 1, 2025: 1}, years)["label"] == "too few to tell"
    t = dash.trend({2020: 10, 2021: 10, 2026: 1}, [2020, 2021])     # partial 2026 left out by the caller
    assert t["label"] == "about the same" and "2020 vs 10/yr in 2021" in t["detail"]


def test_dashboard_year_section_marks_partial_year_and_escapes():
    evil = "<b>x</b>"
    page = dash.render_dashboard({
        "title": "t", "scope": "s", "map_file": "m.html",
        "kpis": {"crashes": 1, "k": 0, "a": 0, "injured": 0, "at_intersections": 1},
        "hotspots": [], "network": {}, "factor_rows": [], "collisions": [], "by_hour": [0] * 24,
        "by_dow": [0] * 7, "patterns": [], "recommendations": [], "source": "",
        "years": {"by_year": [{"year": 2025, "crashes": 70, "k": 1, "a": 2, "injured": 3, "same_period": 50},
                              {"year": 2026, "crashes": 43, "k": 0, "a": 1, "injured": 2, "same_period": 43}],
                  "partial": {"year": 2026, "through": "2026-09-27", "through_label": "Sep 27"},
                  "area_trend": {"label": "fewer", "detail": "x"}, "ka_trend": None,
                  "hotspot_trend": [{"node": "1", "location": evil, "lon": -77.3, "lat": 37.2,
                                     "by_year": {"2025": 13, "2026": 9}, "trend": {"label": "more", "detail": "d"}}],
                  "top3_by_year": {"2026": [{"node": "1", "location": evil, "crashes": 9}]},
                  "new_in_top3": [evil]}})
    assert "2026*" in page and "Jan 1 - Sep 27 only" in page and "data through Sep 27" in page
    assert evil not in page and "&lt;b&gt;x&lt;/b&gt;" in page
    assert "Fatal crashes (K)" in page
