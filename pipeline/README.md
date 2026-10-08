# Infrastructure dependency graph (OpenStreetMap)

`build_ci_graph.py` turns an OpenStreetMap extract into the graph behind **Grid Cascade**:
power, water, communications, healthcare and emergency-services assets, and the dependency
links between them.

```
Geofabrik .osm.pbf ──> build_ci_graph.py ──> ci-graph.json ──> S3 data/ ──> API (/api/infrastructure/*) ──> map + cascade graph
                       (weekly GitHub Action: .github/workflows/ci-graph.yml)
```

## Run it locally

```bash
pip install -r pipeline/requirements.txt
curl -LO https://download.geofabrik.de/north-america/us/virginia-latest.osm.pbf

python pipeline/build_ci_graph.py virginia-latest.osm.pbf \
  --center 37.5407,-77.4360 --radius-mi 70 --region "Richmond, VA" -o build/ci-graph.json

# Point the API at it (otherwise it uses the bundled synthetic sample)
CiGraph__Path=$PWD/build/ci-graph.json dotnet run --project backend
```

For DC, pass several extracts (`virginia-latest.osm.pbf district-of-columbia-latest.osm.pbf
maryland-latest.osm.pbf`) and `--center 38.9,-77.04`.

Tests use a synthetic Richmond-shaped network (`fixtures/make_sample_osm.py`), so they need no download:

```bash
python -m pytest -q pipeline/tests
```

The same fixture produces `backend/Data/ci-graph.sample.json`, the graph the API uses when nothing
is published:

```bash
python pipeline/fixtures/make_sample_osm.py /tmp/sample.osm
python pipeline/build_ci_graph.py /tmp/sample.osm --radius-mi 30 \
  --region "Richmond, VA (synthetic sample)" --sample -o backend/Data/ci-graph.sample.json
```

## How the graph is built

| Step | What happens |
|---|---|
| Read | `power=plant/substation/line/cable/minor_line`, `man_made=water_works/wastewater_plant/pumping_station/water_tower`, `telecom=exchange/data_center`, communication masts and towers, `amenity=hospital/fire_station/police`, `emergency=ambulance_station`. Points sitting on a polygon of the same kind are merged. |
| Grid | Every wire vertex becomes a graph vertex (shared towers merge), wires are snapped to the substations and plants they end in (60 m) or pass through (10 m), then collapsed into substation-to-substation links that keep the real route. |
| Sources | Plants, and substations wired to anything outside the study area (imports over transmission ties). |
| Direction | Higher voltage feeds lower; otherwise the side nearer a source feeds the other; otherwise marked undirected. |
| Unwired substations | Linked to the nearest wired substation within 15 km (OSM often maps the substation but not its local lines). |
| Cross-sector | Rules in `DEPENDENCY_RULES`, e.g. a hospital takes power from its nearest substation (8 km), water from the nearest tower or pumping station (10 km), comms from the nearest exchange (25 km). |
| Backup | `BACKUP` lists what each kind can ride out (hospital: power, water, comms; telephone exchange: power). Losing a backed-up service makes the asset "on backup" rather than failed. |

Every edge carries a `basis` string saying why it exists.

## Limits

- **Dependencies are inferred, not known.** OSM records where things are, not which feeder serves
  which building. Nearest-supplier links are a stand-in and will be wrong for individual assets.
- **Coverage follows the mappers.** Rural lines, distribution feeders and voltage tags are often
  missing, which makes parts of the grid look less connected (or less redundant) than they are.
- **The grid model is connectivity only.** No load flow, capacity or protection settings; an asset
  counts as powered while any intact path to a source exists.
- **Sensitivity.** The underlying data is public, but a polished "what fails if this area goes down"
  view of a real city deserves some thought before it goes on a public URL.

Data © OpenStreetMap contributors, ODbL 1.0.

## Checking a built graph

```bash
python pipeline/graph_report.py build/ci-graph.json --csv build/gaps.csv
```

Reports isolated assets, grid groups cut off from the rest, energy assets with no path to a power
source, consumers missing a service, and links with unknown flow direction. The CSV lists each one with
its OpenStreetMap link. Keep it in `build/` (git-ignored): it is built from real data.
