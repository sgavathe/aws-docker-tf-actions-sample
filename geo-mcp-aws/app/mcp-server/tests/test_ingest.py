"""Daily ArcGIS crash sync: paging, part files, skip-when-unchanged, continuation, load job.
A local fake FeatureServer answers like ArcGIS (keyset paging by OBJECTID, maxRecordCount)."""

import json
import os
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
for k in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"):
    os.environ.setdefault(k, "testing")
boto3 = pytest.importorskip("boto3")
moto = pytest.importorskip("moto")

N = 5_500
EDIT = {"value": 1788370645601}


class FakeLayer(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = dict(urllib.parse.parse_qsl(u.query))
        if u.path.endswith("/0"):
            body = {"editingInfo": {"dataLastEditDate": EDIT["value"]}, "maxRecordCount": 1000, "fields": [
                {"name": "PHYSICAL_JURIS", "domain": {"type": "codedValue", "codedValues": [
                    {"name": "074. Prince George County", "code": "74"}]}},
                {"name": "CRASH_SEVERITY", "domain": {"type": "codedValue", "codedValues": [
                    {"name": "K. Fatal Injury", "code": "K"}]}},
                {"name": "NIGHT", "domain": {"type": "codedValue", "codedValues": [
                    {"name": "No", "code": "0"}, {"name": "Yes", "code": "1"}]}}]}
        else:
            after = int(q["where"].split(">")[1])
            assert q["orderByFields"] == "OBJECTID ASC" and q["outSR"] == "4326" and q["f"] == "geojson"
            ids = range(after + 1, min(after + int(q["resultRecordCount"]), N) + 1)
            body = {"type": "FeatureCollection", "features": [
                {"type": "Feature", "id": i, "geometry": {"type": "Point", "coordinates": [-77.3, 37.2]},
                 "properties": {"OBJECTID": i, "DOCUMENT_NBR": 100000 + i, "CRASH_DT": 1700000000000,
                                "ROUTE_OR_STREET_NM": "MIDDLE RD", "PHYSICAL_JURIS": "74",
                                "CRASH_SEVERITY": "K", "NIGHT": "1"}} for i in ids]}
        data = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(data)


class Ctx:
    def __init__(self, ms):
        self.ms = ms

    def get_remaining_time_in_millis(self):
        return self.ms


@pytest.fixture
def ingest(monkeypatch):
    srv = HTTPServer(("127.0.0.1", 0), FakeLayer)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    with moto.mock_aws():
        boto3.client("s3").create_bucket(Bucket="data")
        monkeypatch.setenv("IMPORT_BUCKET", "data")
        monkeypatch.setenv("CRASH_SOURCE_URL", f"http://127.0.0.1:{srv.server_port}/FeatureServer/0")
        monkeypatch.setenv("CRASH_PAGE_SIZE", "1000")
        monkeypatch.setenv("CRASH_PART_SIZE", "2000")
        monkeypatch.setenv("JOBS_FUNCTION", "jobs-fn")
        monkeypatch.setenv("AWS_LAMBDA_FUNCTION_NAME", "ingest-fn")
        import importlib
        from geo_mcp import ingest_arcgis as m
        importlib.reload(m)
        monkeypatch.setattr(m.time, "sleep", lambda s: None)
        invokes = []

        class FakeLambda:
            def invoke(self, **kw):
                invokes.append((kw["FunctionName"], json.loads(kw["Payload"])))

        monkeypatch.setattr(m, "_lambda", FakeLambda())
        yield m, invokes
    srv.shutdown()


def keys():
    return sorted(o["Key"] for o in boto3.client("s3").list_objects_v2(Bucket="data").get("Contents", []))


def test_full_run_pages_writes_parts_and_starts_one_load(ingest):
    m, invokes = ingest
    out = m.handler({}, Ctx(10**9))
    assert out["fetched"] == N and out["parts"] == 3            # 2000 + 2000 + 1500
    parts = [k for k in keys() if k.endswith(".geojson")]
    assert len(parts) == 3
    feats = [f for k in parts for f in json.loads(boto3.client("s3").get_object(Bucket="data", Key=k)["Body"].read())["features"]]
    assert [f["id"] for f in feats] == list(range(1, N + 1))   # every record exactly once, in order
    p = feats[0]["properties"]
    assert (p["PHYSICAL_JURIS"], p["CRASH_SEVERITY"], p["NIGHT"]) == ("074. Prince George County", "K", "Yes")
    assert invokes == [("jobs-fn", {"job": "load_crashes", "s3_prefix": f"s3://data/import/arcgis/{out['run']}/"})]
    # unchanged source: nothing fetched, nothing loaded
    assert m.handler({}, Ctx(10**9)) == {"ok": True, "skipped": "source unchanged"}
    assert len(invokes) == 1


def test_continues_in_new_invocation_near_time_limit(ingest, monkeypatch):
    m, invokes = ingest
    budget = {"left": 3}

    class Shrinking:
        def get_remaining_time_in_millis(self):
            budget["left"] -= 1
            return 10**9 if budget["left"] > 0 else 0

    first = m.handler({}, Shrinking())
    assert first["continued"] and invokes[-1][0] == "ingest-fn"
    run = invokes[-1][1]["continue"]
    out = m.handler({"continue": run}, Ctx(10**9))
    assert out["fetched"] == N and out["run"] == run
    parts = [k for k in keys() if k.endswith(".geojson")]
    ids = [f["id"] for k in parts for f in json.loads(boto3.client("s3").get_object(Bucket="data", Key=k)["Body"].read())["features"]]
    assert ids == list(range(1, N + 1))                         # no gaps, no duplicates across invocations
    assert invokes[-1][0] == "jobs-fn"


def test_new_edit_date_triggers_a_new_run(ingest):
    m, invokes = ingest
    m.handler({}, Ctx(10**9))
    EDIT["value"] += 1
    try:
        assert m.handler({}, Ctx(10**9))["fetched"] == N
    finally:
        EDIT["value"] -= 1
    assert sum(1 for f, _ in invokes if f == "jobs-fn") == 2


def test_old_format_state_forces_one_resync(ingest):
    m, invokes = ingest
    m.handler({}, Ctx(10**9))
    st = m.load_state(); st.pop("format"); m.save_state(st)    # a run from before domain decoding
    assert m.handler({}, Ctx(10**9))["fetched"] == N
    assert m.handler({}, Ctx(10**9)) == {"ok": True, "skipped": "source unchanged"}


def test_severity_from_code_or_label():
    from geo_mcp.load_crashes import severity
    assert [severity(v) for v in ("K", "K. Fatal Injury", "PDO. Property Damage Only", "O", "", None)] == \
        ["K", "K", "O", "O", None, None]


@pytest.mark.parametrize("event", [
    {"job": "load_crashes", "s3_prefix": "s3://other/import/arcgis/x/"},
    {"job": "load_crashes", "s3_prefix": "s3://imports/maps/"},
    {"job": "load_crashes", "s3_prefix": "s3://imports/import/../x/"},
])
def test_load_job_rejects_bad_prefix(event, monkeypatch):
    monkeypatch.setenv("IMPORT_BUCKET", "imports")
    from geo_mcp import lambda_jobs
    assert lambda_jobs.handler(event, None)["ok"] is False
