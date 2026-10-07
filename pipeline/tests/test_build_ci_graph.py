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
