"""Stream a large crash GeoJSON (VDOT CrashData format) into PostGIS.

    docker compose run --rm crash-loader /import/CrashData.geojson [--srid 4326] [--replace]
    python -m geo_mcp.load_crashes s3://bucket/import/CrashData.geojson     (AWS: loader Lambda)

- Streams the file with ijson, so memory stays flat however large the file is.
- Loads in batches with COPY, then upserts on DOCUMENT_NBR: re-running with a newer extract
  updates records instead of duplicating them. --replace empties the table first.
- Keeps every original attribute in a jsonb column, plus typed columns for fast analysis.
- Reads the file's CRS (CRS84 / EPSG:xxxx) from its header; --srid overrides.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path

import ijson

from .db import connect

BATCH = 10_000
COLUMNS = ["document_nbr", "crash_year", "crash_dt", "crash_hour", "severity", "k_people", "a_people",
           "persons_injured", "collision_type", "intersection", "node", "rte_nm", "route_label", "rns_mp",
           "juris", "night", "alcohol", "speed", "distracted", "pedestrian", "bike", "weather", "surface",
           "props", "geom_ewkt"]
ROUTE_RE = re.compile(r"(US|SR|SC|IS)0*(\d+)")
ROUTE_PREFIX = {"US": "US-", "SR": "VA-", "SC": "Rt ", "IS": "I-"}


def _s3_parts(src: str) -> tuple[str, str]:
    bucket, _, key = src[5:].partition("/")
    return bucket, key


def open_input(src: str):
    """Binary stream for a local path or s3://bucket/key (streamed, never fully in memory)."""
    if src.startswith("s3://"):
        import boto3
        b, k = _s3_parts(src)
        return boto3.client("s3").get_object(Bucket=b, Key=k)["Body"]
    return Path(src).open("rb")


def exists(src: str) -> bool:
    if src.startswith("s3://"):
        import boto3
        b, k = _s3_parts(src)
        try:
            boto3.client("s3").head_object(Bucket=b, Key=k)
            return True
        except Exception:
            return False
    return Path(src).exists()


def detect_srid(src) -> int:
    src = str(src)
    if src.startswith("s3://"):
        import boto3
        b, k = _s3_parts(src)
        head = boto3.client("s3").get_object(Bucket=b, Key=k, Range="bytes=0-262143")["Body"].read()
    else:
        with Path(src).open("rb") as fh:
            head = fh.read(262_144)
    head = head.decode("utf-8", "ignore")
    m = re.search(r'"crs"\s*:\s*\{.*?"name"\s*:\s*"([^"]+)"', head, re.S)
    if not m or "CRS84" in m.group(1):
        return 4326
    code = re.search(r"(\d{4,6})\s*$", m.group(1))
    return int(code.group(1)) if code else 4326


def yes(v) -> bool | None:
    if v is None:
        return None
    return str(v).strip().lower() in ("yes", "y", "true", "1")


def severity(v) -> str | None:
    """K/A/B/C/O from a code ('K') or a domain label ('K. Fatal Injury', 'PDO. Property Damage Only')."""
    if v in (None, ""):
        return None
    head = str(v).split(".")[0].strip().upper()
    return "O" if head == "PDO" else head[:1] or None


def as_int(v) -> int | None:
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


def as_date(v) -> str | None:
    if v in (None, ""):
        return None
    if isinstance(v, (int, float)):  # ArcGIS exports: epoch milliseconds
        return dt.datetime.fromtimestamp(v / 1000, dt.timezone.utc).date().isoformat()
    return str(v)[:10]


def route_label(rte: str | None) -> str | None:
    m = ROUTE_RE.search(rte or "")
    if not m:
        return rte or None
    if m.group(2) in ("9999", "99999"):   # VDOT placeholder for unnamed / ramp segments
        return None
    return f"{ROUTE_PREFIX[m.group(1)]}{m.group(2)}"


def row(feature: dict) -> list | None:
    g = feature.get("geometry") or {}
    if g.get("type") != "Point" or not g.get("coordinates"):
        return None
    x, y = g["coordinates"][:2]
    p = feature.get("properties") or {}
    key = p.get("DOCUMENT_NBR") or p.get("OBJECTID")
    if key is None:
        key = hashlib.sha1(json.dumps(feature, sort_keys=True, default=str).encode()).hexdigest()
    hhmm = as_int(p.get("CRASH_MILITARY_TM"))
    node = p.get("NODE")
    ia = str(p.get("INTERSECTION_ANALYSIS") or "")
    return [
        str(key), as_int(p.get("CRASH_YEAR")), as_date(p.get("CRASH_DT")),
        hhmm // 100 if hhmm is not None and 0 <= hhmm <= 2400 else None,
        severity(p.get("CRASH_SEVERITY")), as_int(p.get("K_PEOPLE")), as_int(p.get("A_PEOPLE")),
        as_int(p.get("PERSONS_INJURED")), p.get("COLLISION_TYPE"),
        bool(node) or (ia != "" and not ia.lower().startswith("not")),
        str(node) if node not in (None, "") else None,
        p.get("RTE_NM"), route_label(p.get("RTE_NM")),
        float(p["RNS_MP"]) if isinstance(p.get("RNS_MP"), (int, float)) else None,
        p.get("PHYSICAL_JURIS"), yes(p.get("NIGHT")), yes(p.get("ALCOHOL_NOTALCOHOL")),
        yes(p.get("SPEED_NOTSPEED")), yes(p.get("DISTRACTED_NOTDISTRACTED")),
        yes(p.get("PED_NONPED")), yes(p.get("BIKE_NONBIKE")),
        p.get("WEATHER_CONDITION"), p.get("ROADWAY_SURFACE_COND"),
        json.dumps(p, default=str), f"POINT({float(x)} {float(y)})",
    ]


def flush(cur, rows: list[list], srid: int) -> None:
    cur.execute("TRUNCATE crash_stage")
    with cur.copy(f"COPY crash_stage ({', '.join(COLUMNS)}) FROM STDIN") as cp:
        for r in rows:
            cp.write_row(r)
    cols = [c for c in COLUMNS if c != "geom_ewkt"]
    updates = ", ".join(f"{c} = EXCLUDED.{c}" for c in cols[1:] + ["geom"])
    cur.execute(
        f"""INSERT INTO crash.crashes ({', '.join(cols)}, geom)
            SELECT {', '.join(cols[:-1])}, props::jsonb,
                   ST_Transform(ST_SetSRID(ST_GeomFromText(geom_ewkt), {int(srid)}), 4326)
            FROM crash_stage
            ON CONFLICT (document_nbr) DO UPDATE SET {updates}""")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("file", help="local path or s3://bucket/key")
    ap.add_argument("--srid", type=int, help="source CRS EPSG code (default: read from the file, else 4326)")
    ap.add_argument("--replace", action="store_true", help="empty crash.crashes before loading")
    a = ap.parse_args(argv)
    if not exists(a.file):
        print(f"not found: {a.file}  (put files in ./data/import on the host; they appear as /import/...)")
        return 2
    srid = a.srid or detect_srid(a.file)
    dsn = os.environ["DATABASE_URL_ADMIN"]
    ddl = (Path(__file__).parent / "crash_schema.sql").read_text()
    start, n, skipped, batch = time.time(), 0, 0, []
    with connect(dsn) as conn, conn.cursor() as cur:
        cur.execute(ddl)
        if a.replace:
            cur.execute("TRUNCATE crash.crashes")
        cur.execute("CREATE TEMP TABLE crash_stage (document_nbr text, crash_year int, crash_dt date, crash_hour int, "
                    "severity text, k_people int, a_people int, persons_injured int, collision_type text, "
                    "intersection boolean, node text, rte_nm text, route_label text, rns_mp double precision, "
                    "juris text, night boolean, alcohol boolean, speed boolean, distracted boolean, pedestrian boolean, "
                    "bike boolean, weather text, surface text, props text, geom_ewkt text)")
        print(f"loading {a.file.rsplit('/', 1)[-1]} (source SRID {srid}) ...", flush=True)
        with contextlib.closing(open_input(a.file)) as fh:
            for feat in ijson.items(fh, "features.item", use_float=True):
                r = row(feat)
                if r is None:
                    skipped += 1
                    continue
                batch.append(r)
                if len(batch) >= BATCH:
                    flush(cur, batch, srid)
                    conn.commit()
                    n += len(batch)
                    batch = []
                    print(f"  {n:,} rows  ({n / (time.time() - start):,.0f}/s)", flush=True)
        if batch:
            flush(cur, batch, srid)
            n += len(batch)
        cur.execute("ANALYZE crash.crashes")
        conn.commit()
        cur.execute("SELECT count(*), min(crash_year), max(crash_year), count(*) FILTER (WHERE node IS NOT NULL) "
                    "FROM crash.crashes")
        total, y0, y1, at_nodes = cur.fetchone()
    print(f"done: {n:,} loaded, {skipped:,} skipped (no point geometry), {time.time() - start:.1f}s")
    print(f"crash.crashes now holds {total:,} crashes ({y0}-{y1}); {at_nodes:,} tied to an intersection node")
    return 0


if __name__ == "__main__":
    sys.exit(main())
