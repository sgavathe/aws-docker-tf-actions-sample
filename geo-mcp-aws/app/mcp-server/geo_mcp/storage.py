"""Where generated maps and GeoJSON exports go.

Local / air-gapped: files under MAPS_DIR (served by the map-viewer nginx at MAP_VIEWER_URL/maps/...).
AWS: objects in a private S3 bucket (MAPS_BUCKET) under the same relative keys, e.g.
     maps/crash-heatmap-123.html and maps/exports/hospitals.geojson. They are served only to
     signed-in users, through the chat app, never straight from the bucket.
"""

from __future__ import annotations

import fnmatch
import os
from pathlib import Path

MAPS_DIR = Path(os.environ.get("MAPS_DIR", "/maps"))
BUCKET = os.environ.get("MAPS_BUCKET", "")
_s3 = None

TYPES = {".html": "text/html; charset=utf-8", ".geojson": "application/geo+json", ".json": "application/json"}


def _client():
    global _s3
    if _s3 is None:
        import boto3  # only needed in AWS
        _s3 = boto3.client("s3")
    return _s3


def _key(rel: str) -> str:
    rel = rel.lstrip("/")
    if ".." in Path(rel).parts:
        raise ValueError("bad path")
    return f"maps/{rel}"


def put_text(rel: str, text: str) -> None:
    """Write `rel` (relative to the maps root, e.g. 'x.html' or 'exports/y.geojson')."""
    if BUCKET:
        _client().put_object(Bucket=BUCKET, Key=_key(rel), Body=text.encode("utf-8"),
                             ContentType=TYPES.get(Path(rel).suffix, "application/octet-stream"),
                             ServerSideEncryption="AES256")
        return
    path = MAPS_DIR / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def delete_matching(folder: str, pattern: str) -> None:
    """Delete files in `folder` whose names match a glob pattern, e.g. ('exports', 'crash_*')."""
    if BUCKET:
        prefix = _key(folder.rstrip("/") + "/")
        s3 = _client()
        for page in s3.get_paginator("list_objects_v2").paginate(Bucket=BUCKET, Prefix=prefix):
            for obj in page.get("Contents", []):
                if fnmatch.fnmatch(obj["Key"][len(prefix):], pattern):
                    s3.delete_object(Bucket=BUCKET, Key=obj["Key"])
        return
    for p in (MAPS_DIR / folder).glob(pattern):
        p.unlink(missing_ok=True)
