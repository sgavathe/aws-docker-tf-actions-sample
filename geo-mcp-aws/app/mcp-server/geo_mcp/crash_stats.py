"""Statewide crash shares, precomputed per year so dashboards don't scan 1.16M rows.

refresh(cur) rebuilds two small tables after each load (the loader calls it once per run):
  crash.year_stats       one row per year: total, at_intersections, and one count per factor
  crash.year_collisions  per year and collision type: count
Dashboards sum the rows for the requested years. If the tables are missing (an older load),
the tools fall back to a live scan.
"""

from __future__ import annotations

# factor key -> (label, SQL condition). Keys double as column names in crash.year_stats.
FACTORS: dict[str, tuple[str, str]] = {
    "speed": ("Speeding", "speed"),
    "alcohol": ("Alcohol", "alcohol"),
    "distracted": ("Distracted", "distracted"),
    "night": ("Night", "night"),
    "dark_unlit": ("Dark, unlit road", "props->>'LIGHT_CONDITION' ILIKE '%%not lighted%%'"),
    "young": ("Young driver", "props->>'YOUNG_NOTYOUNG' = 'Yes'"),
    "senior": ("Senior driver", "props->>'SENIOR_NOTSENIOR' = 'Yes'"),
    "pedestrian": ("Pedestrian", "pedestrian"),
    "bike": ("Bicycle", "bike"),
    "angle": ("Angle", "collision_type ILIKE '%%angle%%'"),
    "rear_end": ("Rear end", "collision_type ILIKE '%%rear end%%'"),
    "road_departure": ("Road departure", "collision_type ILIKE '%%fixed object%%'"),
    "deer": ("Deer / animal", "(collision_type ILIKE '%%deer%%' OR collision_type ILIKE '%%animal%%')"),
}


def factor_columns() -> str:
    """SELECT list counting every factor (for psycopg with parameters: '%%' stays escaped)."""
    return ", ".join(f"count(*) FILTER (WHERE {sql}) AS {key}" for key, (_, sql) in FACTORS.items())


def refresh(cur) -> None:
    cols = ", ".join(f"{k} int" for k in FACTORS)
    cur.execute(f"CREATE TABLE IF NOT EXISTS crash.year_stats (crash_year int PRIMARY KEY, total int, "
                f"at_intersections int, {cols})")
    for k in FACTORS:   # tables created by an older version gain new factor columns
        cur.execute(f"ALTER TABLE crash.year_stats ADD COLUMN IF NOT EXISTS {k} int")
    cur.execute("CREATE TABLE IF NOT EXISTS crash.year_collisions (crash_year int, collision_type text, n int, "
                "PRIMARY KEY (crash_year, collision_type))")
    cur.execute("TRUNCATE crash.year_stats, crash.year_collisions")
    # no parameters here, so a literal % is fine; factor_columns() escapes for the parameterized path
    cur.execute(f"INSERT INTO crash.year_stats (crash_year, total, at_intersections, {', '.join(FACTORS)}) "
                f"SELECT crash_year, count(*), count(*) FILTER (WHERE node IS NOT NULL), "
                f"{factor_columns().replace('%%', '%')} FROM crash.crashes WHERE crash_year IS NOT NULL GROUP BY crash_year")
    cur.execute("INSERT INTO crash.year_collisions SELECT crash_year, collision_type, count(*) FROM crash.crashes "
                "WHERE crash_year IS NOT NULL AND coalesce(collision_type, '') NOT IN ('', 'Not Provided') "
                "GROUP BY 1, 2")
    cur.execute("GRANT SELECT ON crash.year_stats, crash.year_collisions TO mcp_reader, mcp_writer")
