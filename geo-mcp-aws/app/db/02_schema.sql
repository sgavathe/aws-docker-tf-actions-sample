-- Critical-infrastructure sample schema. All data is synthetic.
CREATE TABLE infra.substations (
  id text PRIMARY KEY, name text NOT NULL, voltage_kv int, owner text,
  geom geometry(Point, 4326) NOT NULL);
CREATE TABLE infra.water_plants (
  id text PRIMARY KEY, name text NOT NULL, capacity_mgd numeric, plant_type text,
  geom geometry(Point, 4326) NOT NULL);
CREATE TABLE infra.cell_towers (
  id text PRIMARY KEY, name text NOT NULL, carrier text, backup_hours int,
  geom geometry(Point, 4326) NOT NULL);
CREATE TABLE infra.hospitals (
  id text PRIMARY KEY, name text NOT NULL, beds int, trauma_level text, backup_hours int,
  geom geometry(Point, 4326) NOT NULL);
-- Which asset depends on which (power -> water/comms/hospitals, comms -> hospitals).
CREATE TABLE infra.dependencies (
  src_id text NOT NULL, dst_id text NOT NULL, dep_type text NOT NULL,
  PRIMARY KEY (src_id, dst_id));

CREATE INDEX ON infra.substations USING gist (geom);
CREATE INDEX ON infra.water_plants USING gist (geom);
CREATE INDEX ON infra.cell_towers USING gist (geom);
CREATE INDEX ON infra.hospitals USING gist (geom);

-- One view across all assets keeps the tools simple.
CREATE VIEW infra.assets AS
  SELECT id, name, 'substation' AS asset_type, geom FROM infra.substations
  UNION ALL SELECT id, name, 'water_plant', geom FROM infra.water_plants
  UNION ALL SELECT id, name, 'cell_tower', geom FROM infra.cell_towers
  UNION ALL SELECT id, name, 'hospital', geom FROM infra.hospitals;

GRANT SELECT ON ALL TABLES IN SCHEMA infra TO mcp_reader, mcp_writer;
