import pytest

from geo_mcp.crash_tools import build_where


def test_node_must_be_numeric():
    where, p = build_where(None, dict(node="536154, 536155"))
    assert "node = ANY" in where and p["nodes"] == ["536154", "536155"]
    with pytest.raises(ValueError, match="numeric NODE id"):
        build_where(None, dict(node="intersection"))


def test_place_that_names_two_roads_points_to_find_intersection(monkeypatch):
    from geo_mcp import crash_tools

    class Srv:
        def resolve_place(self, name):
            raise ValueError("unknown place")

    monkeypatch.setattr(crash_tools, "resolve_juris", lambda srv, name: [])   # not a county/city either
    with pytest.raises(ValueError, match="find_intersection"):
        build_where(Srv(), dict(place="Middle Road and Prince George Drive"))


def test_map_footer_names_aurora_on_aws(monkeypatch):
    from geo_mcp import mapgen
    layer = {"name": "x", "geojson": {"type": "FeatureCollection", "features": []}}
    monkeypatch.setenv("DB_IAM_AUTH", "1")
    page = mapgen.render_map("t", [dict(layer)], basemap="osm")
    assert "Aurora PostgreSQL in AWS" in page and "enclave PostGIS" not in page
    monkeypatch.delenv("DB_IAM_AUTH")
    assert "the enclave PostGIS" in mapgen.render_map("t", [dict(layer)], basemap="osm")


def test_node_as_number():
    # Nova sends node=536154 (a number); the tools accept it like '536154'
    _, p = build_where(None, dict(node=536154))
    assert p["nodes"] == ["536154"]
