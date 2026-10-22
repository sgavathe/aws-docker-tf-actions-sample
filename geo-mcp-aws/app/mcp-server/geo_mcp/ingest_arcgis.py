"""Daily pull of VDOT crash records from the public ArcGIS FeatureServer into S3, then a load job.

Runs as the "ingest" Lambda (outside the VPC: it needs the internet; it never touches the database).
  1. Read the layer's dataLastEditDate. Unchanged since the last completed run -> nothing to do.
  2. Page through every record by OBJECTID (keyset paging: OBJECTID > last, ORDER BY OBJECTID,
     maxRecordCount per request), as GeoJSON in WGS84 with all fields. Coded-value domains are
     decoded to their labels (PHYSICAL_JURIS 74 -> "074. Prince George County"), as in VDOT's file
     downloads; CRASH_SEVERITY keeps its K/A/B/C/O code, which the analysis tools use.
  3. Write parts of PART_SIZE features to s3://<bucket>/import/arcgis/<run>/part-00001.geojson ...
  4. Near Lambda's time limit, save the cursor and continue in a fresh invocation.
  5. When done, start ONE loader job (jobs Lambda, inside the VPC) for the whole run folder; it
     upserts by DOCUMENT_NBR, so re-runs never duplicate rows.

Event: {} (scheduled), {"force": true} (pull even if unchanged), {"continue": "<run id>"} (internal).
Records VDOT deletes upstream are not deleted here (upsert only).
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

LAYER_URL = os.environ.get("CRASH_SOURCE_URL",
                           "https://services.arcgis.com/p5v98VHDX9Atv3l7/arcgis/rest/services/Full_Crash/FeatureServer/0")
BUCKET = os.environ.get("IMPORT_BUCKET", "")
PREFIX = "import/arcgis/"
STATE_KEY = PREFIX + "state.json"
PAGE = int(os.environ.get("CRASH_PAGE_SIZE", "2000"))        # service maxRecordCount
PART_SIZE = int(os.environ.get("CRASH_PART_SIZE", "50000"))  # features per S3 part file
JOBS_FUNCTION = os.environ.get("JOBS_FUNCTION", "")
SAFETY_MS = 120_000                                          # stop paging with 2 min left
USER_AGENT = "spatialenable-geo-mcp-ingest/1.0 (daily crash sync)"
KEEP_CODES = {"CRASH_SEVERITY"}   # analysis filters on the letter; the label would be "K. Fatal Injury"
FORMAT = 2                        # bump when the part format changes: forces one full re-sync

_s3 = _lambda = None


def s3():
    global _s3
    if _s3 is None:
        import boto3
        _s3 = boto3.client("s3")
    return _s3


def lam():
    global _lambda
    if _lambda is None:
        import boto3
        _lambda = boto3.client("lambda")
    return _lambda


def http_json(url: str, params: dict, tries: int = 5) -> dict:
    full = url + "?" + urllib.parse.urlencode(params)
    for attempt in range(1, tries + 1):
        try:
            req = urllib.request.Request(full, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=60) as r:
                data = json.loads(r.read())
            if isinstance(data, dict) and "error" in data:     # ArcGIS reports errors with HTTP 200
                raise RuntimeError(f"ArcGIS error: {data['error']}")
            return data
        except (urllib.error.URLError, TimeoutError, RuntimeError, ValueError) as exc:
            if attempt == tries:
                raise
            print(f"  retry {attempt} after {type(exc).__name__}: {str(exc)[:120]}", flush=True)
            time.sleep(min(30, 2 ** attempt))
    raise RuntimeError("unreachable")


def load_state() -> dict:
    try:
        return json.loads(s3().get_object(Bucket=BUCKET, Key=STATE_KEY)["Body"].read())
    except Exception:
        return {}


def save_state(state: dict) -> None:
    s3().put_object(Bucket=BUCKET, Key=STATE_KEY, Body=json.dumps(state).encode(),
                    ContentType="application/json", ServerSideEncryption="AES256")


def write_part(run: str, n: int, features: list[dict]) -> str:
    key = f"{PREFIX}{run}/part-{n:05d}.geojson"
    body = json.dumps({"type": "FeatureCollection", "features": features}, separators=(",", ":"))
    s3().put_object(Bucket=BUCKET, Key=key, Body=body.encode(), ContentType="application/geo+json",
                    ServerSideEncryption="AES256")
    print(f"  wrote s3://{BUCKET}/{key} ({len(features):,} features)", flush=True)
    return key


def domains(meta: dict) -> dict[str, dict[str, str]]:
    """field -> {code: label} for every coded-value domain on the layer (except KEEP_CODES)."""
    out = {}
    for f in meta.get("fields") or []:
        d = f.get("domain") or {}
        if d.get("type") == "codedValue" and f.get("name") not in KEEP_CODES:
            out[f["name"]] = {str(c["code"]): c["name"] for c in d.get("codedValues") or [] if "code" in c}
    return out


def decode(features: list[dict], doms: dict[str, dict[str, str]]) -> None:
    for feat in features:
        p = feat.get("properties") or {}
        for field, labels in doms.items():
            v = p.get(field)
            if v is not None and str(v) in labels:
                p[field] = labels[str(v)]


def page(after_oid: int) -> list[dict]:
    data = http_json(LAYER_URL + "/query", {
        "where": f"OBJECTID > {int(after_oid)}", "outFields": "*", "orderByFields": "OBJECTID ASC",
        "resultRecordCount": PAGE, "returnGeometry": "true", "outSR": 4326, "f": "geojson"})
    return data.get("features") or []


def handler(event, context):
    event = event or {}
    if not BUCKET:
        raise RuntimeError("IMPORT_BUCKET is not set")
    state = load_state()
    remaining = (lambda: context.get_remaining_time_in_millis()) if context else (lambda: 10**9)

    meta = http_json(LAYER_URL, {"f": "json"})
    doms = domains(meta)
    if event.get("continue"):
        run = state.get("run")
        if run != event["continue"]:
            return {"ok": False, "error": "stale continuation", "run": run}
    else:
        edit = (meta.get("editingInfo") or {}).get("dataLastEditDate") or (meta.get("editingInfo") or {}).get("lastEditDate")
        if state.get("run") and time.time() - state.get("started", 0) < 6 * 3600 and not event.get("force"):
            return {"ok": True, "skipped": "a run is already in progress", "run": state["run"]}
        # a run older than 6 h never finished (e.g. a failed invocation): start over
        if (edit and edit == state.get("completed_edit") and state.get("format") == FORMAT
                and not event.get("force")):
            print(f"source unchanged since {state.get('completed_at')}; nothing to do", flush=True)
            return {"ok": True, "skipped": "source unchanged"}
        run = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        state.update(run=run, edit=edit, cursor=0, part=0, fetched=0, started=time.time())
        save_state(state)
        print(f"run {run}: source last edited {edit}", flush=True)

    buf: list[dict] = []
    cursor, part, fetched = int(state["cursor"]), int(state["part"]), int(state["fetched"])
    done = False
    while True:
        if remaining() < SAFETY_MS:
            break
        feats = page(cursor)
        if not feats:
            done = True
            break
        oids = [f.get("id") or (f.get("properties") or {}).get("OBJECTID") for f in feats]
        cursor = max(int(o) for o in oids if o is not None)
        decode(feats, doms)
        buf.extend(feats)
        fetched += len(feats)
        if len(buf) >= PART_SIZE:
            part += 1
            write_part(run, part, buf)
            buf = []
            state.update(cursor=cursor, part=part, fetched=fetched)
            save_state(state)   # resume point is always the end of the last written part
        time.sleep(0.2)         # be polite to the public service

    if buf:
        part += 1
        write_part(run, part, buf)
    state.update(cursor=cursor, part=part, fetched=fetched)

    if not done:   # out of time: continue in a fresh invocation from the saved cursor
        save_state(state)
        lam().invoke(FunctionName=os.environ["AWS_LAMBDA_FUNCTION_NAME"], InvocationType="Event",
                     Payload=json.dumps({"continue": run}).encode())
        print(f"run {run}: {fetched:,} so far, continuing in a new invocation", flush=True)
        return {"ok": True, "run": run, "fetched": fetched, "continued": True}

    prefix = f"s3://{BUCKET}/{PREFIX}{run}/"
    if JOBS_FUNCTION and part:
        lam().invoke(FunctionName=JOBS_FUNCTION, InvocationType="Event",
                     Payload=json.dumps({"job": "load_crashes", "s3_prefix": prefix}).encode())
    state = {"completed_edit": state.get("edit"), "completed_at": datetime.now(timezone.utc).isoformat(),
             "last_run": run, "last_count": fetched, "last_parts": part, "format": FORMAT}
    save_state(state)
    print(f"run {run}: {fetched:,} crashes in {part} parts ({len(doms)} coded fields decoded) "
          f"-> load job started for {prefix}", flush=True)
    return {"ok": True, "run": run, "fetched": fetched, "parts": part, "load": prefix}
