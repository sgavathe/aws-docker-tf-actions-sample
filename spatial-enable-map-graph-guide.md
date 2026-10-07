# Grid Cascade: User and Technical Guide

Grid Cascade answers one question: if everything inside an area you draw goes down, which power, water, communications and emergency assets fail next, and how far does the failure travel? It runs on a dependency graph built from OpenStreetMap, served by a .NET API on AWS Lambda behind CloudFront at map.spatialenable.com.

## Using the tool

Draw an area, and within a second the panel, map and graph show what fails and why. The page opens on a worked example: a 1.25-mile square around the center of the study area.

1. **Draw an area.** Pick **Polygon**, **Rectangle** or **Freehand** and draw on the map. Everything inside is treated as down, and any mapped power line crossing it is treated as cut. **Run the example** redraws the starter square; **Clear** removes it.
2. **Read the Impact numbers.** **Failed** = assets that stop working. **On backup** = assets that lost a service but keep running on a generator, stored water or radio. **Hops** = the longest chain of dependency links the failure travelled. **Reach** = the farthest affected asset from the drawn area, in miles. The line under them shows the area drawn, how many assets were inside and how many power lines were cut.
3. **Scan the sector table.** Each row is a sector (Energy, Water, Communications, Data centers, Healthcare, Emergency services) with hit / total. Red is failed, amber is on backup.
4. **Look at the map.** Failed assets are filled with their sector color and ringed in red; on-backup assets are white with an amber ring. Cut power lines turn thick red. Arrows colored by service (orange power, blue water, purple comms) show which asset took down which, dashed when the receiving asset is on backup. Grey grid wires carry repeating arrows in the inferred direction of power flow; dashed grey means the direction is unknown.
5. **Use the cascade graph.** Columns are hops: *In the area*, then Hop 1, 2, 3 and so on. Rows are sectors. Each bubble counts the assets in that sector at that hop, and the curves show what fed what (orange power, blue water, purple comms). **Hide** collapses it.
6. **Click any asset.** The popup lists what it depends on (with the distance in miles and the reason the link was inferred), what it supplies, its coordinates and a link to the original OpenStreetMap feature.

**Group nearby assets when zoomed out** clusters points into numbered bubbles below roughly 1:60,000 scale. Untick it to see every asset; the setting is remembered in your browser.

The tag under Draw an area says which graph is loaded. **SYNTHETIC SAMPLE** means the bundled made-up Richmond network (91 assets); otherwise it shows the real region, for example *Richmond, VA (100 mi)*.

## What happens after you draw

One POST carries the polygon to the API, the analyzer runs in memory against the cached graph, and one JSON response drives the panel, map and graph. Nothing is precomputed per area.

&#91;embedded content: request flow · browser to Lambda and back, graph refresh from S3\]

The browser only ever talks to CloudFront. The graph reaches Lambda from S3, never from the browser, and the pipeline that builds it runs on GitHub Actions.

**On page load (once):** the browser calls `GET /api/infrastructure/graph` and receives the whole graph (nodes, edges, wire geometry). The response is gzip/brotli compressed and carries an ETag, so a reload with an unchanged graph costs a 304. The map draws assets and wires from it, and `assetDetails.js` precomputes every popup.

**When you finish drawing:**

1. **Capture the shape** (`CascadeMap.jsx`). The ArcGIS `SketchViewModel` fires `create` with state `complete`. The polygon is converted from Web Mercator to longitude/latitude, rounded, and handed up as a GeoJSON Polygon.
2. **Send it** (`CascadeApp.jsx` → `api.js`). `runImpact` cancels any request still in flight, then `api.impact(area)` posts `{"area": <GeoJSON>}` to `/api/infrastructure/impact`. `api.js` adds an `x-amz-content-sha256` header (SHA-256 of the body), which CloudFront's origin access control needs to sign a POST to the Lambda Function URL.
3. **Route it** (CloudFront). `/api/*` goes to the Lambda Function URL; everything else is the React build in the S3 site bucket.
4. **Parse the area** (`InfrastructureEndpoints.cs` → `AreaPolygon.cs`). Accepts a Polygon, MultiPolygon or Feature. Coordinates are projected to kilometres on a local equirectangular grid so the containment, crossing and distance tests are cheap.
5. **Get the graph** (`InfrastructureGraphProvider.cs`). Already in memory. Every 15 minutes it checks S3 `data/ci-graph.json` with a conditional (ETag) GET and swaps in a new version only if the file changed. With no S3 file it falls back to a local path, then to the bundled synthetic sample.
6. **Run the cascade** (`ImpactAnalyzer.cs`). A pure function of graph and area: the four stages in the next section.
7. **Return the result.** A summary (directly hit, failed, degraded, cut lines, max hops, reach, area, per-sector and per-hop counts), one row per affected asset (status, hop, cause, which asset it failed *via*, which services it lost, distance from the area) and the IDs of the cut lines.
8. **Draw it** (`CascadeApp.jsx`, `CascadeMap.jsx`, `CascadeGraph.jsx`). The panel converts km to miles, the map adds the cut-wire and cascade-arrow layers, and the graph lays the affected assets out by hop and sector.

## The cascade algorithm

The analyzer runs four stages in order. The grid is judged by connectivity to a power source, not by "everything downstream fails", so a substation with a second route stays up.

1. **Direct hits (hop 0).** Every asset whose point lies inside the drawn area is marked *Failed*, cause "Inside the drawn area".
2. **Cut wires.** Every mapped grid link whose route has a segment touching or crossing the area is marked severed.
3. **Grid islands.** A breadth-first search runs twice from every power source (a large plant or a high-voltage tie leaving the study area) over intact wires: once on the untouched graph, once with the hit assets and cut wires removed. Any energy asset energized before but not after has lost every path to a source and is marked *Failed*. Its hop is its distance in grid links from the nearest hit asset or cut wire.
4. **Service cascade.** Starting from every failed asset, lowest hop first, each dependent is rechecked. It loses a service (power, water or comms) only when **every** supplier of that service has failed. If it has backup for that service it becomes *Degraded* and keeps working, so the cascade stops there. If not, it becomes *Failed* at hop = supplier's hop + 1, and its own dependents are checked next.

**Backup by facility type** (losing a listed service makes the asset Degraded instead of Failed):

| Facility | Rides out loss of |
| --- | --- |
| Hospital | power, water, comms |
| Fire station | power, water, comms |
| Police, ambulance station | power, comms |
| Data center, telecom exchange | power |
| Water treatment plant | power |

Everything else (substations, pumping stations, water towers, cell towers, wastewater plants) has no backup in the model.

**The numbers on screen:** *Failed* and *On backup* count assets in each state. *Hops* is the largest hop. *Reach* is the largest distance from the drawn area to any affected asset (0 for assets inside it). Each asset's *cause* names the supplier that took it down, and *via* draws the arrow.

## The data

Every asset and wire comes from OpenStreetMap (© OpenStreetMap contributors, ODbL 1.0) via Geofabrik's state extracts. Every dependency between them is **inferred** from location and tags by `pipeline/build_ci_graph.py`; none comes from utility records. Each link's popup line says why it was inferred.

### What is read from OSM

| Sector | Kind | OSM tag |
| --- | --- | --- |
| Energy | Power plant | `power=plant` |
| Energy | Substation | `power=substation` |
| Energy | Wires | `power=line`, `cable`, `minor_line` |
| Water | Treatment plant | `man_made=water_works` |
| Water | Wastewater plant | `man_made=wastewater_plant` |
| Water | Pumping station | `man_made=pumping_station` (water); sewage/storm ones kept separately, gas/oil dropped |
| Water | Water tower | `man_made=water_tower` |
| Communications | Telecom exchange | `telecom=exchange` |
| Communications | Cell/comm tower | `man_made=mast` or `tower` with `tower:type=communication` |
| IT | Data center | `telecom=data_center` or `building=data_center` |
| Health | Hospital | `amenity=hospital` |
| Emergency | Fire, police, ambulance | `amenity=fire_station`, `amenity=police`, `emergency=ambulance_station` |

### How the pipeline builds the graph

1. **Read** the `.osm.pbf` extracts with pyosmium, keeping only the tags above inside the study circle (center + radius in miles). Assets that appear in two neighbouring state extracts are counted once; a point sitting on a polygon of the same kind within 150 m is merged into it.
2. **Project** everything to the local UTM zone so all distances are in metres.
3. **Wire the grid.** Wire vertices are merged on a 2 m grid so shared towers join. A wire *ending* within 60 m of a substation or plant connects to it; one *passing* within 10 m (inside the fence) does too. A breadth-first walk along the wires from each substation finds the next substation or plant, and each pair found becomes a grid link that follows the real wire route.
4. **Find power sources.** A plant counts if it is at least 20 MW, or untagged but dispatchable (gas, coal, nuclear, oil, diesel, hydro, biomass, biogas, waste). Rooftop and small solar do not count. A substation counts if a wire of 115 kV or more leaves the study area from it: a tie to the outside grid.
5. **Set flow direction.** Higher voltage feeds lower. When voltages match or are missing, the side fewer links from a source feeds the other. If neither works, the link is undirected and drawn dashed.
6. **Feed unwired substations.** A substation with no mapped wires is fed from the nearest wired substation within 15 km.
7. **Add cross-sector dependencies** with the rules below: each consumer links to the nearest supplier of the first kind found within range.
8. **Write** `ci-graph.json`, keeping all energy assets plus any asset in at least one link. Wire routes are simplified to 20 m.

### Dependency rules

| Consumer | Power from | Water from | Comms from |
| --- | --- | --- | --- |
| Water treatment, wastewater plant, sewage pump | substation ≤ 8 km | — | — |
| Water pumping station | substation ≤ 8 km | treatment plant ≤ 40 km | — |
| Water tower | — | pumping station or treatment plant ≤ 15 km | — |
| Telecom exchange | substation ≤ 8 km | — | — |
| Cell tower, data center, police, ambulance | substation ≤ 8 km | — | exchange ≤ 25 km |
| Hospital, fire station | substation ≤ 8 km | water tower, pumping station or treatment plant ≤ 10 km | exchange ≤ 25 km |

The distances are in km because the pipeline works in metres; the app shows them in miles (8 km ≈ 5 mi, 25 km ≈ 15.5 mi, 40 km ≈ 25 mi).

### The file

`ci-graph.json` has three parts. `meta`: region, center, radius, build time, inputs, license, asset counts by sector, number of power sources and wire segments, and `sample: true|false`. `nodes`: id, OSM id, name, sector, kind, lon/lat, plus voltage, MW output, source reason and backup where they apply. `edges`: from, to, type (power, water, comms), kind (grid, distribution or service link), the inference reason, voltage, and the route coordinates for wired links.

## Limits

Treat the results as a plausible what-if built from public map data, not a utility-grade model. The main gaps:

- **Coverage depends on OSM mappers.** Missing substations, unnamed facilities and untagged voltages are common; an asset that is not mapped cannot fail.
- **Distribution is invisible.** OSM rarely maps the feeders from substations to buildings, so "nearest substation within 8 km" stands in for the real feed.
- **Water and comms networks are not mapped at all.** Mains, fibre and cell backhaul are replaced by nearest-supplier rules.
- **No load or capacity.** The grid test is pure connectivity: a surviving path counts as fully powered, however small the line.
- **Backup lasts forever.** A degraded asset never runs out of fuel or stored water, and no time dimension is modelled.
- **One supplier per service.** Each consumer gets the single nearest supplier; real redundancy (dual feeds, multiple carriers) is not captured, which can overstate impact.
- **Area = total loss.** Everything inside the drawing fails completely; there is no partial damage.

## Code map

All paths are in `sgavathe/aws-docker-tf-actions-sample`.

| File | What it does |
| --- | --- |
| `pipeline/build_ci_graph.py` | OSM extracts → `ci-graph.json` (classify, wire the grid, find sources, set direction, infer dependencies) |
| `pipeline/fixtures/make_sample_osm.py` | Builds the synthetic Richmond network used by the tests and the bundled sample |
| `pipeline/tests/test_build_ci_graph.py` | 13 pipeline tests (pass-through wires, mid-span taps, sources, direction, dedupe) |
| `backend/Endpoints/InfrastructureEndpoints.cs` | `GET /api/infrastructure`, `GET /api/infrastructure/graph`, `POST /api/infrastructure/impact` |
| `backend/Services/InfrastructureGraphProvider.cs` | Loads the graph from S3 (or a local path, or the sample) and refreshes it every 15 minutes |
| `backend/Services/InfrastructureGraph.cs` | Parses the JSON and builds the lookup indexes (grid neighbours, service in/out) |
| `backend/Services/AreaPolygon.cs` | GeoJSON parsing, point-in-polygon, segment crossing, distance and area |
| `backend/Services/ImpactAnalyzer.cs` | The four-stage cascade |
| `backend/Data/ci-graph.sample.json` | Bundled synthetic sample (91 assets, 151 links) |
| `tests/Backend.Tests/` | 28 analyzer and polygon tests |
| `frontend/src/cascade/CascadeApp.jsx` | Panel: draw tools, KPIs in miles, sector table, impact list |
| `frontend/src/cascade/CascadeMap.jsx` | ArcGIS map: wires with flow arrows, clustered assets, cut wires, cascade arrows, sketch tool |
| `frontend/src/cascade/CascadeGraph.jsx` | The hop-by-sector cascade graph |
| `frontend/src/cascade/assetDetails.js` | Popup text: depends on, supplies, coordinates, OSM link |
| `frontend/src/api.js` | API calls, including the signed-POST header for CloudFront |
| `serverless/lambda/Dockerfile` | Lambda image: .NET 8 API + Lambda Web Adapter, cross-compiled for arm64 or x86 |
| `serverless/terraform/` | S3 site bucket, CloudFront, Lambda Function URL, ECR, IAM (incl. read access to `data/*`) |
| `.github/workflows/ci-graph.yml` | Builds and publishes the graph weekly or on demand |

## Refreshing the data

The API serves whatever is at `s3://geo-devops-demo-sls-site-390744232980/data/ci-graph.json` and picks up a new file within 15 minutes. No redeploy is needed.

- **Weekly (automatic).** `ci-graph.yml` runs Mondays 07:17 UTC and publishes when `DEPLOY_TARGET` is `serverless`. It uses the workflow's defaults: **Virginia only, 70 miles**. Left as is, it will replace the 100-mile graph each Monday; change the `EXTRACTS` and `RADIUS_MI` defaults if you want 100 miles kept.
- **On demand (GitHub):**

```bash
gh workflow run ci-graph.yml \
  -f extracts="north-america/us/virginia north-america/us/north-carolina north-america/us/maryland north-america/us/district-of-columbia" \
  -f radius_mi=100 -f region="Richmond, VA (100 mi)" -f publish=true
```

- **From your Mac:**

```bash
python pipeline/build_ci_graph.py osm/*.osm.pbf --center 37.5407,-77.4360 --radius-mi 100 --region "Richmond, VA (100 mi)" -o build/ci-graph.json
AWS_PROFILE=sgavathe-udemy aws s3 cp build/ci-graph.json s3://geo-devops-demo-sls-site-390744232980/data/ci-graph.json --content-type application/json
```

- **Back to the sample:** delete the S3 file (`aws s3 rm …/data/ci-graph.json`); within 15 minutes the API serves the bundled synthetic graph again.

## Sources

- [OpenStreetMap](https://www.openstreetmap.org/copyright) — asset and wire data, ODbL 1.0
- [Geofabrik downloads](https://download.geofabrik.de/north-america/us.html) — state `.osm.pbf` extracts
- [OpenInfraMap](https://openinframap.org/) — a live view of the same OSM power, telecom and water features
