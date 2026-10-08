// Live hazards that can be used as the cascade area instead of a hand-drawn shape.
//   NWS active alerts (api.weather.gov): warnings, watches and advisories for the graph's states.
//     Storm-based warnings carry a polygon; county/zone-based ones (hurricane, flood, winter...)
//     have geometry null and list affectedZones, whose outlines are fetched on demand.
//   NHC forecast cones via Esri Living Atlas "Active Hurricanes" (layer 4, public, no key).
// Everything is fetched by the browser; nothing runs on the backend.

const NWS = "https://api.weather.gov";
// Esri Living Atlas "Active Hurricanes" layers: 0 forecast positions, 1 observed positions,
// 2 forecast track, 3 observed track, 4 forecast cone.
const HURRICANES = "https://services9.arcgis.com/RHVPKKiFTONKtxq3/arcgis/rest/services/Active_Hurricanes_v1/FeatureServer";
const hurricaneLayer = (id, fields = "*") =>
  `${HURRICANES}/${id}/query?where=1%3D1&outFields=${fields}&outSR=4326&f=geojson`;
const CONES = hurricaneLayer(4, "STORMNAME,STORMTYPE,MAX_LABEL,ADVISNUM,ADVDATE,BASIN");

const STATE_CODES = {
  "virginia": "VA", "maryland": "MD", "district of columbia": "DC", "north carolina": "NC",
  "delaware": "DE", "west virginia": "WV", "pennsylvania": "PA",
  "florida": "FL", "texas": "TX", "louisiana": "LA", "georgia": "GA", "alabama": "AL",
  "south carolina": "SC", "mississippi": "MS",
};
const DEFAULT_STATES = ["VA", "MD", "DC", "NC"];
const SEVERITY_RANK = { Extreme: 0, Severe: 1, Moderate: 2, Minor: 3, Unknown: 4 };
const MAX_VERTICES = 3000;          // the API accepts up to 5,000; leave headroom

/** State codes for the alert query, from the graph's study area. */
export function statesFor(meta) {
  const names = meta?.area?.type === "states" ? meta.area.names : null;
  const codes = (names ?? []).map((n) => STATE_CODES[n.toLowerCase()]).filter(Boolean);
  return codes.length ? codes : DEFAULT_STATES;
}

/** "Warning" | "Watch" | "Advisory" | "Statement" from the event name, for colour and sorting. */
export function alertLevel(event = "") {
  if (/warning/i.test(event)) return "Warning";
  if (/watch/i.test(event)) return "Watch";
  if (/advisory/i.test(event)) return "Advisory";
  return "Statement";
}

export async function fetchAlerts(states, signal) {
  // status=actual drops system tests and exercises (NWS sends "Test Message" products regularly).
  const res = await fetch(`${NWS}/alerts/active?status=actual&area=${states.join(",")}`, {
    signal, headers: { Accept: "application/geo+json" },
  });
  if (!res.ok) throw new Error(`NWS alerts: HTTP ${res.status}`);
  const json = await res.json();
  return (json.features ?? [])
    .filter((f) => (f.properties?.status ?? "Actual") === "Actual" && !/^test\b/i.test(f.properties?.event ?? ""))
    .map((f) => {
      const p = f.properties ?? {};
      return {
        id: p.id ?? f.id,
        source: "nws",
        event: p.event ?? "Alert",
        level: alertLevel(p.event),
        headline: p.headline ?? "",
        severity: p.severity ?? "Unknown",
        areaDesc: p.areaDesc ?? "",
        ends: p.ends ?? p.expires ?? null,
        geometry: f.geometry ?? null,
        zones: p.affectedZones ?? [],
        link: p["@id"] ?? f.id,
      };
    })
    .sort((a, b) =>
      (SEVERITY_RANK[a.severity] ?? 9) - (SEVERITY_RANK[b.severity] ?? 9) ||
      ["Warning", "Watch", "Advisory", "Statement"].indexOf(a.level) -
        ["Warning", "Watch", "Advisory", "Statement"].indexOf(b.level));
}

async function getGeoJson(url, signal, what) {
  const res = await fetch(url, { signal });
  if (!res.ok) throw new Error(`${what}: HTTP ${res.status}`);
  const json = await res.json();
  if (json.error) throw new Error(`${what}: ${json.error.message ?? "service error"}`);
  return json.features ?? [];
}

const stormKey = (p = {}) => `${p.BASIN ?? ""}|${String(p.STORMNAME ?? "").toUpperCase()}`;
const LINE_TYPES = new Set(["LineString", "MultiLineString"]);

// NHC development labels on forecast points.
const DEVELOPMENT = {
  D: "Tropical depression", S: "Tropical storm", H: "Hurricane", M: "Major hurricane",
  L: "Post-tropical low", X: "Extratropical", E: "Extratropical", P: "Potential tropical cyclone",
};

/**
 * Forecast track (line), past track (line) and forecast positions (points) per storm, keyed like
 * the cones. Optional extras: if any of these layers fails, the cones still show.
 */
async function fetchTracks(signal) {
  const [points, line, past] = await Promise.allSettled([
    getGeoJson(hurricaneLayer(0), signal, "Forecast positions"),
    getGeoJson(hurricaneLayer(2), signal, "Forecast track"),
    getGeoJson(hurricaneLayer(3), signal, "Past track"),
  ]);
  const tracks = new Map();
  const get = (p) => {
    const k = stormKey(p);
    if (!tracks.has(k)) tracks.set(k, { line: [], past: [], points: [] });
    return tracks.get(k);
  };
  const lines = (r, field) => {
    if (r.status !== "fulfilled") return;
    for (const f of r.value) {
      if (LINE_TYPES.has(f.geometry?.type)) get(f.properties)[field].push(f.geometry);
    }
  };
  lines(line, "line");
  lines(past, "past");
  if (points.status === "fulfilled") {
    for (const f of points.value) {
      if (f.geometry?.type !== "Point") continue;
      const p = f.properties ?? {};
      const label = String(p.DVLBL ?? "").toUpperCase();
      const knots = Number(p.MAXWIND);
      get(p).points.push({
        lon: f.geometry.coordinates[0],
        lat: f.geometry.coordinates[1],
        label,
        kind: p.TCDVLP || DEVELOPMENT[label] || "Forecast position",
        tau: Number.isFinite(Number(p.TAU)) ? Number(p.TAU) : null,
        when: p.FLDATELBL || p.DATELBL || "",
        short: p.DATELBL || "",
        windMph: Number.isFinite(knots) && knots > 0 ? Math.round(knots * 1.15078 / 5) * 5 : null,
        gustMph: Number(p.GUST) > 0 ? Math.round(Number(p.GUST) * 1.15078 / 5) * 5 : null,
        pressureMb: Number(p.MSLP) > 0 && Number(p.MSLP) < 1100 ? Number(p.MSLP) : null,
      });
    }
  }
  for (const t of tracks.values()) t.points.sort((a, b) => (a.tau ?? 0) - (b.tau ?? 0));
  return tracks;
}

export async function fetchCones(signal) {
  const [features, tracks] = await Promise.all([
    getGeoJson(CONES, signal, "Hurricane cones"),
    fetchTracks(signal).catch(() => new Map()),
  ]);
  return features
    .filter((f) => f.geometry)
    .map((f, i) => {
      const p = f.properties ?? {};
      const name = [p.STORMTYPE, p.STORMNAME].filter(Boolean).join(" ") || "Tropical cyclone";
      return {
        id: `cone-${p.BASIN ?? ""}-${p.STORMNAME ?? i}-${p.ADVISNUM ?? ""}`,
        source: "nhc",
        event: `${name}: forecast cone`,
        level: "Cone",
        headline: [p.MAX_LABEL, p.ADVISNUM && `advisory ${p.ADVISNUM}`].filter(Boolean).join(", "),
        severity: "Severe",
        areaDesc: p.BASIN ? `${p.BASIN} basin` : "",
        ends: null,
        issued: p.ADVDATE ? new Date(p.ADVDATE).toISOString() : null,
        geometry: f.geometry,
        track: tracks.get(stormKey(p)) ?? null,
        zones: [],
        link: "https://www.nhc.noaa.gov/",
      };
    });
}

/** Does the hazard's geometry come within `marginDeg` of the bounding box? */
export function nearBox(geometry, [w, s, e, n], marginDeg = 1.5) {
  for (const poly of polygonsOf(geometry)) {
    for (const [x, y] of poly[0]) {
      if (x >= w - marginDeg && x <= e + marginDeg && y >= s - marginDeg && y <= n + marginDeg) return true;
    }
  }
  return false;
}

// ---------------------------------------------------------------- area for the cascade ---

const zoneCache = new Map();

async function zoneGeometry(url, signal) {
  if (!zoneCache.has(url)) {
    zoneCache.set(url, fetch(url, { signal, headers: { Accept: "application/geo+json" } })
      .then((r) => (r.ok ? r.json() : null))
      .then((j) => j?.geometry ?? null)
      .catch(() => { zoneCache.delete(url); return null; }));
  }
  return zoneCache.get(url);
}

/**
 * GeoJSON (Polygon or MultiPolygon) to send to /api/infrastructure/impact.
 * Zone-based alerts are assembled from their zone outlines; everything is simplified so the
 * API's vertex limit holds and the cascade stays fast.
 */
export async function hazardArea(hazard, signal) {
  let polygons = polygonsOf(hazard.geometry);
  if (!polygons.length && hazard.zones.length) {
    const geoms = [];
    for (let i = 0; i < hazard.zones.length; i += 6) {          // 6 requests at a time
      geoms.push(...await Promise.all(hazard.zones.slice(i, i + 6).map((z) => zoneGeometry(z, signal))));
    }
    polygons = geoms.flatMap(polygonsOf);
  }
  if (!polygons.length) throw new Error("This alert has no outline to use.");
  const simplified = simplifyPolygons(polygons, MAX_VERTICES);
  return simplified.length === 1
    ? { type: "Polygon", coordinates: simplified[0] }
    : { type: "MultiPolygon", coordinates: simplified };
}

/** Polygon / MultiPolygon / GeometryCollection -> array of polygons (arrays of rings). */
export function polygonsOf(g) {
  if (!g) return [];
  if (g.type === "Polygon") return [g.coordinates];
  if (g.type === "MultiPolygon") return g.coordinates;
  if (g.type === "GeometryCollection") return (g.geometries ?? []).flatMap(polygonsOf);
  return [];
}

function simplifyPolygons(polygons, maxVertices) {
  const count = (ps) => ps.reduce((n, p) => n + p.reduce((m, r) => m + r.length, 0), 0);
  let tol = 0.0003;                                             // ~30 m, doubled until it fits
  let out = polygons;
  while (count(out) > maxVertices && tol < 0.2) {
    out = polygons
      .map((p) => p.map((ring) => simplifyRing(ring, tol)).filter((ring) => ring.length >= 4))
      .filter((p) => p.length);
    tol *= 2;
  }
  const r = (v) => Math.round(v * 1e5) / 1e5;
  return out.map((p) => p.map((ring) => ring.map(([x, y]) => [r(x), r(y)])));
}

/** Douglas-Peucker on a closed ring (first point == last point). */
function simplifyRing(ring, tol) {
  if (ring.length <= 5) return ring;
  const keep = new Uint8Array(ring.length);
  keep[0] = keep[ring.length - 1] = 1;
  const stack = [[0, ring.length - 1]];
  while (stack.length) {
    const [a, b] = stack.pop();
    let max = 0;
    let idx = -1;
    for (let i = a + 1; i < b; i++) {
      const d = segDist(ring[i], ring[a], ring[b]);
      if (d > max) { max = d; idx = i; }
    }
    if (max > tol && idx > 0) {
      keep[idx] = 1;
      stack.push([a, idx], [idx, b]);
    }
  }
  const out = ring.filter((_, i) => keep[i]);
  return out.length >= 4 ? out : ring;
}

function segDist([px, py], [ax, ay], [bx, by]) {
  const dx = bx - ax;
  const dy = by - ay;
  const len = dx * dx + dy * dy;
  const t = len ? Math.max(0, Math.min(1, ((px - ax) * dx + (py - ay) * dy) / len)) : 0;
  return Math.hypot(px - (ax + t * dx), py - (ay + t * dy));
}
