-- Crash records (VDOT CrashData format). Created by the loader (python -m geo_mcp.load_crashes),
-- so existing databases need no re-init. Idempotent.
CREATE SCHEMA IF NOT EXISTS crash;

CREATE TABLE IF NOT EXISTS crash.crashes (
  document_nbr   text PRIMARY KEY,
  crash_year     int,
  crash_dt       date,
  crash_hour     int,
  severity       text,          -- K fatal, A serious, B minor, C possible, O property damage only
  k_people       int,
  a_people       int,
  persons_injured int,
  collision_type text,
  intersection   boolean,
  node           text,          -- VDOT intersection node id (null when not at an intersection)
  rte_nm         text,
  route_label    text,          -- e.g. US-460, VA-156, Rt 625
  rns_mp         double precision,
  juris          text,
  night          boolean,
  alcohol        boolean,
  speed          boolean,
  distracted     boolean,
  pedestrian     boolean,
  bike           boolean,
  weather        text,
  surface        text,
  props          jsonb,         -- every original attribute, untouched
  geom           geometry(Point, 4326)
);

CREATE INDEX IF NOT EXISTS crashes_geom_idx ON crash.crashes USING gist (geom);
CREATE INDEX IF NOT EXISTS crashes_node_idx ON crash.crashes (node);
CREATE INDEX IF NOT EXISTS crashes_year_idx ON crash.crashes (crash_year);
CREATE INDEX IF NOT EXISTS crashes_route_idx ON crash.crashes (route_label);
CREATE INDEX IF NOT EXISTS crashes_juris_idx ON crash.crashes (juris, crash_year);   -- county/city filters

GRANT USAGE ON SCHEMA crash TO mcp_reader, mcp_writer;
GRANT SELECT ON ALL TABLES IN SCHEMA crash TO mcp_reader, mcp_writer;
