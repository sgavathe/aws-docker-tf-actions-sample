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


class Places:
    METERS_PER_MILE = 1609.344

    def resolve_place(self, name):
        if name == "Richmond International Airport":
            return name, -77.32, 37.50
        raise ValueError(f"unknown place {name!r}")

    def fetch(self, sql, p=None):
        return [{"juris": j} for j in ("074. Prince George County", "043. Richmond County", "127. City of Richmond",
                                       "029. Fairfax County", "107. City of Fairfax", "076. Prince William County")]


@pytest.fixture
def places(monkeypatch):
    from geo_mcp import crash_tools
    monkeypatch.setattr(crash_tools, "_JURIS", [])
    return Places()


def test_county_repeated_as_place_and_zero_radius(places):
    where, p = build_where(places, dict(juris="Prince George County", place="Prince George County", radius_miles=0))
    assert p["juris_names"] == ["074. Prince George County"] and "ST_DWithin" not in where
    where, p = build_where(places, dict(place="Prince George"))            # county named as a place
    assert p["juris_names"] == ["074. Prince George County"]
    where, p = build_where(places, dict(juris="Prince George",
                                        place="Richmond International Airport", radius_miles=3))
    assert "ST_DWithin" in where                                           # a real landmark is kept


def test_ambiguous_names_raise_unless_kind_given(places):
    from geo_mcp.crash_tools import resolve_juris
    with pytest.raises(ValueError, match="more than one jurisdiction"):
        resolve_juris(places, "Richmond")
    assert resolve_juris(places, "City of Richmond") == ["127. City of Richmond"]
    assert resolve_juris(places, "Richmond County") == ["043. Richmond County"]
    assert resolve_juris(places, "fairfax city") == ["107. City of Fairfax"]
    assert resolve_juris(places, "prince grorge") == ["074. Prince George County"]
    with pytest.raises(ValueError, match="Prince George County, Prince William County"):
        resolve_juris(places, "prince")
