import pytest

from geo_mcp.crash_tools import build_where


def test_node_must_be_numeric():
    where, p = build_where(None, dict(node="536154, 536155"))
    assert "node = ANY" in where and p["nodes"] == ["536154", "536155"]
    with pytest.raises(ValueError, match="numeric NODE id"):
        build_where(None, dict(node="intersection"))
