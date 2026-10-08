"""End-to-end tests: synthetic .osm fixture -> dependency graph JSON."""
import json
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
PIPELINE = HERE.parent
sys.path.insert(0, str(PIPELINE))
sys.path.insert(0, str(PIPELINE / "fixtures"))

import build_ci_graph  # noqa: E402
import make_sample_osm  # noqa: E402


@pytest.fixture(scope="module")
def graph(tmp_path_factory):
    osm = tmp_path_factory.mktemp("osm") / "sample.osm"
    make_sample_osm.main(str(osm))
    return build_ci_graph.build([str(osm)], (37.5407, -77.4360), 30, "test")


def by_name(graph):
    return {n["name"]: n for n in graph["nodes"]}


def links(graph, kind="grid_link"):
    names = {n["id"]: n["name"] for n in graph["nodes"]}
    return {(names[e["from"]], names[e["to"]]) for e in graph["edges"] if e["kind"] == kind}


def test_every_edge_points_at_real_nodes(graph):
    ids = {n["id"] for n in graph["nodes"]}
    assert len(ids) == len(graph["nodes"])
    for e in graph["edges"]:
        assert e["from"] in ids and e["to"] in ids and e["from"] != e["to"]
        assert e["type"] in {"power", "water", "comms"}
        assert e["basis"]


def test_wire_passing_through_a_substation_connects_it(graph):
    grid = links(graph)
    assert ("Sample Downtown Substation", "Sample Westover Substation") in grid
    assert ("Sample Westover Substation", "Sample Millwood Substation") in grid
    assert ("Sample Downtown Substation", "Sample Millwood Substation") not in grid


def test_mid_span_tap_is_connected(graph):
    grid = links(graph)
    assert any("Sample Church Hill Substation" in pair for pair in grid)


def test_sources_are_plants_and_ties_leaving_the_area(graph):
    sources = {n["name"] for n in graph["nodes"] if n.get("source")}
    assert sources == {
        "Sample James Bend Generating Station", "Sample Hanover Solar Farm",
        "Sample North Tie 500 kV Substation", "Sample South Tie 500 kV Substation",
    }


def test_small_plants_are_not_sources():
    assert build_ci_graph.plant_source({"plant:output:electricity": "350 kW", "plant:source": "solar"}, 20) is None
    assert build_ci_graph.plant_source({"plant:output:electricity": "80 MW"}, 20) == "power plant, 80 MW"
    assert build_ci_graph.plant_source({"plant:output:electricity": "1.2 GW"}, 20) == "power plant, 1200 MW"
    assert build_ci_graph.plant_source({"plant:source": "gas;oil"}, 20) == "power plant (gas;oil)"
    assert build_ci_graph.plant_source({"plant:source": "solar"}, 20) is None
    assert build_ci_graph.plant_source({}, 20) is None


def test_power_flows_from_higher_to_lower_voltage(graph):
    grid = links(graph)
    assert ("Sample North Tie 500 kV Substation", "Sample Westbrook Substation") in grid
    assert ("Sample Westbrook Substation", "Sample Hanover Road Substation") in grid


def test_wired_links_follow_the_route(graph):
    for e in graph["edges"]:
        if e["kind"] == "grid_link":
            assert len(e["coords"]) >= 2
        else:
            assert "coords" not in e


def test_unwired_distribution_substations_get_a_feed(graph):
    fed = {to for _, to in links(graph, "distribution_link")}
    assert sum(1 for n in fed if n.startswith("Sample Distribution Substation")) >= 8


def test_hospitals_depend_on_power_water_and_comms(graph):
    nodes = by_name(graph)
    hosp = nodes["Sample Regional Medical Center"]
    types = {e["type"] for e in graph["edges"] if e["to"] == hosp["id"]}
    assert types == {"power", "water", "comms"}
    assert hosp["backup"] == ["power", "water", "comms"]


def test_water_towers_need_no_power(graph):
    towers = {n["id"] for n in graph["nodes"] if n["kind"] == "water_tower"}
    assert towers
    assert not [e for e in graph["edges"] if e["to"] in towers and e["type"] == "power"]


def test_cli_writes_compact_json(tmp_path):
    osm = tmp_path / "sample.osm"
    make_sample_osm.main(str(osm))
    out = tmp_path / "g.json"
    subprocess.run([sys.executable, str(PIPELINE / "build_ci_graph.py"), str(osm),
                    "--radius-mi", "30", "--sample", "-o", str(out)], check=True)
    g = json.loads(out.read_text())
    assert g["meta"]["sample"] is True
    assert out.stat().st_size < 200_000


def test_empty_area_fails_loudly(tmp_path):
    osm = tmp_path / "sample.osm"
    make_sample_osm.main(str(osm))
    rc = build_ci_graph.main([str(osm), "--center", "40.0,-100.0", "--radius-mi", "5",
                              "-o", str(tmp_path / "x.json")])
    assert rc == 1


def test_overlapping_extracts_are_not_double_counted(tmp_path):
    osm = tmp_path / "sample.osm"
    make_sample_osm.main(str(osm))
    once = build_ci_graph.build([str(osm)], (37.5407, -77.4360), 30, "t")
    twice = build_ci_graph.build([str(osm), str(osm)], (37.5407, -77.4360), 30, "t")
    assert len(twice["nodes"]) == len(once["nodes"])
    assert len(twice["edges"]) == len(once["edges"])


def test_unwired_plant_is_tied_to_nearest_substation(graph):
    edges = links(graph, "distribution_link")
    assert ("Sample Eastgate Community Solar", "Sample Eastgate Substation") in edges


def test_island_without_a_source_joins_the_powered_grid(graph):
    names = {n["id"]: n["name"] for n in graph["nodes"]}
    joins = [e for e in graph["edges"] if "nearest powered substation" in e["basis"]]
    assert any(names[e["to"]] in {"Sample Bottoms Bridge Substation", "Sample Providence Forge Substation"}
               for e in joins)
    # every wired energy asset can now reach a power source
    adj = {}
    for e in graph["edges"]:
        if e["kind"] in ("grid_link", "distribution_link"):
            adj.setdefault(e["from"], set()).add(e["to"])
            adj.setdefault(e["to"], set()).add(e["from"])
    on = [n["id"] for n in graph["nodes"] if n.get("source")]
    seen = set(on)
    while on:
        for v in adj.get(on.pop(), ()):
            if v not in seen:
                seen.add(v)
                on.append(v)
    assert set(adj) <= seen


def test_assets_with_no_link_are_dropped(graph):
    assert "Sample Grocery Rooftop Solar" not in by_name(graph)          # 350 kW, nothing within 3 km
    assert graph["meta"]["droppedUnlinked"].get("plant", 0) >= 1
    linked = {e["from"] for e in graph["edges"]} | {e["to"] for e in graph["edges"]}
    assert all(n["id"] in linked or n.get("source") for n in graph["nodes"])


def test_fallback_links_are_labelled():
    assert any(opt[-1] is build_ci_graph.FALLBACK for _, opts in build_ci_graph.DEPENDENCY_RULES["hospital"]
               for opt in opts if len(opt) == 3)


def test_sewer_stations_are_classified_by_name_when_untagged():
    c = build_ci_graph.classify
    assert c({"man_made": "pumping_station", "name": "Four Mile Creek Sewer Pumping Station"}) == ("water", "sewage_pumping")
    assert c({"man_made": "pumping_station", "name": "Oak Hill Lift Station"}) == ("water", "sewage_pumping")
    assert c({"man_made": "pumping_station", "name": "Elm St Stormwater Pump"}) == ("water", "sewage_pumping")
    assert c({"man_made": "pumping_station", "name": "Northside Booster Pump Station"}) == ("water", "pumping_station")
    assert c({"man_made": "pumping_station"}) == ("water", "pumping_station")
    assert c({"man_made": "pumping_station", "substance": "gas"}) is None
