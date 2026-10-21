"""Lambda entry point for one-off jobs that need the private database (in the VPC).

    aws lambda invoke --function-name <prefix>-jobs \
      --payload '{"job": "load_crashes", "s3_uri": "s3://<bucket>/import/CrashData.geojson", "replace": false}' out.json

Only fixed jobs with validated arguments run here: no SQL or shell text comes from the event.
"""

from __future__ import annotations

import os
import re

S3_URI = re.compile(r"^s3://[a-z0-9.\-]{3,63}/import/[\w.\-/ ]{1,200}\.(geo)?json$")


def handler(event, context):
    job = (event or {}).get("job")
    if job != "load_crashes":
        return {"ok": False, "error": "unknown job; supported: load_crashes"}
    uri = str(event.get("s3_uri", ""))
    bucket = os.environ.get("IMPORT_BUCKET", "")
    if not S3_URI.match(uri) or not uri.startswith(f"s3://{bucket}/import/"):
        return {"ok": False, "error": f"s3_uri must be s3://{bucket}/import/<file>.geojson"}
    argv = [uri] + (["--replace"] if event.get("replace") else [])
    if event.get("srid"):
        argv += ["--srid", str(int(event["srid"]))]
    from .load_crashes import main
    code = main(argv)   # progress lines go to CloudWatch Logs
    return {"ok": code == 0, "see": "CloudWatch log group of this function for row counts"}
