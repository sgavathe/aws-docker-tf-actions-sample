"""Lambda entry point for one-off jobs that need the private database (in the VPC).

    aws lambda invoke --function-name <prefix>-jobs \
      --payload '{"job": "load_crashes", "s3_uri": "s3://<bucket>/import/CrashData.geojson", "replace": false}' out.json

Only fixed jobs with validated arguments run here: no SQL or shell text comes from the event.
"""

from __future__ import annotations

import os
import re

S3_URI = re.compile(r"^s3://[a-z0-9.\-]{3,63}/import/[\w.\-/ ]{1,200}\.(geo)?json$")
S3_PREFIX = re.compile(r"^s3://[a-z0-9.\-]{3,63}/import/[\w.\-/]{1,200}/$")


def handler(event, context):
    job = (event or {}).get("job")
    if job == "refresh_stats":
        from .load_crashes import refresh_stats
        return {"ok": refresh_stats() == 0}
    if job != "load_crashes":
        return {"ok": False, "error": "unknown job; supported: load_crashes, refresh_stats"}
    bucket = os.environ.get("IMPORT_BUCKET", "")
    if event.get("s3_prefix"):   # every .geojson part in a folder, in name order (daily ArcGIS sync)
        prefix = str(event["s3_prefix"])
        if not S3_PREFIX.match(prefix) or ".." in prefix or not prefix.startswith(f"s3://{bucket}/import/"):
            return {"ok": False, "error": f"s3_prefix must be s3://{bucket}/import/<folder>/"}
        import boto3
        key_prefix = prefix[len(f"s3://{bucket}/"):]
        pages = boto3.client("s3").get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=key_prefix)
        keys = sorted(o["Key"] for p in pages for o in p.get("Contents", []) if o["Key"].endswith(".geojson"))
        from .load_crashes import main
        failed = [k for k in keys if main([f"s3://{bucket}/{k}", "--no-stats"]) != 0]
        if keys:   # statewide per-year stats once for the whole run, not once per part
            from .load_crashes import refresh_stats
            refresh_stats()
        return {"ok": not failed and bool(keys), "parts": len(keys), "failed": failed}
    uri = str(event.get("s3_uri", ""))
    if not S3_URI.match(uri) or ".." in uri or not uri.startswith(f"s3://{bucket}/import/"):
        return {"ok": False, "error": f"s3_uri must be s3://{bucket}/import/<file>.geojson"}
    argv = [uri] + (["--replace"] if event.get("replace") else [])
    if event.get("srid"):
        argv += ["--srid", str(int(event["srid"]))]
    from .load_crashes import main
    code = main(argv)   # progress lines go to CloudWatch Logs
    return {"ok": code == 0, "see": "CloudWatch log group of this function for row counts"}
