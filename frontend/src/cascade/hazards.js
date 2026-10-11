// Live hazards that can be used as the cascade area instead of a hand-drawn shape.
//   NWS active alerts (api.weather.gov): warnings, watches and advisories for the graph's states.
//     Storm-based warnings carry a polygon; county/zone-based ones (hurricane, flood, winter...)
//     have geometry null and list affectedZones, whose outlines are fetched on demand.
//     Tornado and severe thunderstorm warnings also carry storm motion and tornado detection,
//     which drive the motion arrow and the "+30 min" projected area.
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
// Corners in a hazard area sent to the cascade. The API accepts 5,000, but its cost grows with
// corners x power-line segments: a county-based watch at full detail (jagged coastlines) can run
// past the live API's 15 s timeout, while ~600 corners (about 1-2 km of simplification at county
// scale) runs as fast as a hand-drawn shape and changes almost nothing in the result.
const MAX_VERTICES = 600;

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
  return sortHazards((json.features ?? [])
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
        storm: stormInfo(p.event, p),
      };
    }));
}

const LEVEL_ORDER = ["Warning", "Watch", "Advisory", "Statement"];

// ---------------------------------------------------------------- hazard categories ---

/**
 * Hazard type, for colour and filtering. First match on the event name wins, so the order
 * matters ("Tropical Storm Warning" is tropical, "Coastal Flood Warning" coastal, "Flash Flood" flood).
 * Colours are RGB; the level (warning, watch, advisory) sets line style and fill, not colour.
 */
export const CATEGORIES = [
  { id: "tornado", label: "Tornado", color: [180, 0, 90], match: /tornado/i },
  { id: "tropical", label: "Hurricane & tropical", color: [120, 60, 200], match: /hurricane|tropical|storm surge|typhoon/i },
  { id: "severe", label: "Severe storms", color: [225, 105, 0], match: /severe thunderstorm|extreme wind|special weather|special marine/i },
  { id: "coastal", label: "Coastal & marine", color: [0, 125, 155], match: /coastal|marine|rip current|surf|gale|small craft|beach|tsunami|lakeshore|seas\b/i },
  { id: "flood", label: "Flood", color: [25, 135, 65], match: /flood|hydrologic/i },
  { id: "winter", label: "Winter & cold", color: [55, 105, 205], match: /winter|snow|ice|freez|frost|blizzard|wind chill|cold|sleet/i },
  { id: "heat", label: "Heat, fire & air", color: [195, 45, 30], match: /heat|fire|red flag|smoke|dust|air quality/i },
  { id: "other", label: "Other", color: [110, 120, 135], match: /.*/ },
];
const CATEGORY_BY_ID = Object.fromEntries(CATEGORIES.map((c) => [c.id, c]));

export function categoryOf(h) {
  if (h.source === "nhc") return CATEGORY_BY_ID.tropical;
  if (h.storm?.isTornado) return CATEGORY_BY_ID.tornado;
  return CATEGORIES.find((c) => c.match.test(h.event ?? "")) ?? CATEGORY_BY_ID.other;
}

/** Category counts for the filter chips, in CATEGORIES order, empty ones left out. */
export function categoryCounts(list) {
  const n = new Map();
  for (const h of list) n.set(categoryOf(h).id, (n.get(categoryOf(h).id) ?? 0) + 1);
  return CATEGORIES.filter((c) => n.has(c.id)).map((c) => ({ ...c, count: n.get(c.id) }));
}

// Zone-based alerts worth drawing even before they're run: watches and warnings for tornado,
// severe storms and tropical systems (county or forecast-zone lists, no polygon of their own).
const OUTLINE_CATEGORIES = new Set(["tornado", "severe", "tropical"]);
const OUTLINE_ZONE_BUDGET = 200;      // zone outlines fetched per refresh, at most
const OUTLINE_VERTICES = 1500;        // per alert, for drawing

/**
 * Adds `outline` (Polygon/MultiPolygon) to zone-based tornado, severe-storm and tropical alerts so
 * the map can draw them. Zone outlines are cached for the session, so later refreshes are cheap.
 * Returns a new array; alerts that already have geometry, or that run past the budget, are unchanged.
 */
export async function attachOutlines(list, signal) {
  let budget = OUTLINE_ZONE_BUDGET;
  const out = [];
  for (const h of list) {
    if (h.geometry || !h.zones?.length || !OUTLINE_CATEGORIES.has(categoryOf(h).id) ||
        !["Warning", "Watch"].includes(h.level) || h.zones.length > budget) {
      out.push(h);
      continue;
    }
    budget -= h.zones.length;
    const geoms = [];
    for (let i = 0; i < h.zones.length; i += 6) {
      if (signal?.aborted) return list;
      geoms.push(...await Promise.all(h.zones.slice(i, i + 6).map((z) => zoneGeometry(z, signal))));
    }
    const polygons = geoms.flatMap(polygonsOf);
    if (!polygons.length) { out.push(h); continue; }
    const simplified = simplifyPolygons(polygons, OUTLINE_VERTICES);
    out.push({
      ...h,
      outline: simplified.length === 1
        ? { type: "Polygon", coordinates: simplified[0] }
        : { type: "MultiPolygon", coordinates: simplified },
    });
  }
  return out;
}

/** Tornado warnings first, then tornado watches, then by severity and level. */
export function sortHazards(list) {
  const tornadoRank = (h) => (!h.storm?.isTornado ? 2 : h.level === "Warning" ? 0 : 1);
  return [...list].sort((a, b) =>
    tornadoRank(a) - tornadoRank(b) ||
    (SEVERITY_RANK[a.severity] ?? 9) - (SEVERITY_RANK[b.severity] ?? 9) ||
    LEVEL_ORDER.indexOf(a.level) - LEVEL_ORDER.indexOf(b.level));
}

// ---------------------------------------------------------------- tornadoes and storm motion ---

// NWS alert parameters arrive as arrays of strings: { tornadoDetection: ["RADAR INDICATED"], ... }.
const param = (p, key) => {
  const v = p?.parameters?.[key];
  return (Array.isArray(v) ? v[0] : v) ?? null;
};
const sentence = (s) => (s ? s.charAt(0).toUpperCase() + s.slice(1).toLowerCase() : s);

const COMPASS = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE", "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"];
export const compass = (deg) => COMPASS[Math.round((((deg % 360) + 360) % 360) / 22.5) % 16];

/**
 * Storm motion from the CAP "eventMotionDescription" parameter, e.g.
 *   "2026-05-07T20:40:00-05:00...storm...240DEG...35KT...35.28,-97.53"
 * DEG is the direction the storm is moving FROM (000-359); speed is knots; positions are
 * lat,lon pairs (one point, or several along a line of storms).
 */
export function parseMotion(text) {
  if (!text) return null;
  const m = /(\d{1,3})DEG\.\.\.(\d{1,3})KT(?:\.\.\.(.*))?/.exec(text);
  if (!m) return null;
  const fromDeg = Number(m[1]) % 360;
  const knots = Number(m[2]);
  const points = [...(m[3] ?? "").matchAll(/(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)/g)]
    .map(([, lat, lon]) => [Number(lon), Number(lat)])
    .filter(([lon, lat]) => Math.abs(lat) <= 90 && Math.abs(lon) <= 180);
  const toDeg = (fromDeg + 180) % 360;
  return {
    fromDeg, toDeg, knots,
    mph: Math.round(knots * 1.15078),
    heading: compass(toDeg),
    points,
    time: /^(\S+?)\.\.\./.exec(text)?.[1] ?? null,
  };
}

/** Tornado detection, damage threat, hail and motion; null for alerts without any of them. */
function stormInfo(event = "", p = {}) {
  const isTornado = /tornado/i.test(event);
  const detection = param(p, "tornadoDetection");
  const motion = parseMotion(param(p, "eventMotionDescription"));
  if (!isTornado && !detection && !motion) return null;
  const threat = param(p, "tornadoDamageThreat");
  return {
    isTornado,
    detection: detection ? sentence(detection) : null,      // "Radar indicated" | "Observed" | "Possible"
    damageThreat: threat ? sentence(threat) : null,          // "Considerable" (PDS) | "Catastrophic" (emergency)
    hail: param(p, "maxHailSize"),                           // inches, e.g. "1.00"
    motion,
  };
}

const KM_PER_DEG_LAT = 110.574;
const kmPerDegLon = (lat) => 111.32 * Math.cos((lat * Math.PI) / 180);

/** Move a [lon, lat] point `km` toward compass bearing `deg` (flat-earth, fine for < 100 km). */
export function offset([lon, lat], km, deg) {
  const rad = (deg * Math.PI) / 180;
  return [lon + (km * Math.sin(rad)) / kmPerDegLon(lat), lat + (km * Math.cos(rad)) / KM_PER_DEG_LAT];
}

/** Where the storm is now: the first motion point, else the centre of the warning polygon. */
export function stormStart(hazard) {
  const mo = hazard.storm?.motion;
  if (mo?.points?.length) return mo.points[0];
  const pts = polygonsOf(hazard.geometry ?? hazard.outline).flatMap((p) => p[0]);
  if (!pts.length) return null;
  return [pts.reduce((s, [x]) => s + x, 0) / pts.length, pts.reduce((s, [, y]) => s + y, 0) / pts.length];
}

/** LineString from the storm's position to where it would be in `minutes` at its current motion. */
export function motionLine(hazard, minutes = 30) {
  const mo = hazard.storm?.motion;
  const start = stormStart(hazard);
  if (!mo || !start || !mo.knots) return null;
  return { type: "LineString", coordinates: [start, offset(start, mo.knots * 1.852 * (minutes / 60), mo.toDeg)] };
}

/**
 * The warning polygon swept `minutes` further along the storm's motion: the convex hull of the
 * polygon and a copy moved downstream. An estimate for "what's next", not an NWS product.
 */
export function projectPath(hazard, minutes = 30) {
  const mo = hazard.storm?.motion;
  const pts = polygonsOf(hazard.geometry).flatMap((p) => p[0]);
  if (!mo || !mo.knots || !pts.length) throw new Error("This alert has no storm motion to project.");
  const km = mo.knots * 1.852 * (minutes / 60);
  const moved = pts.map((pt) => offset(pt, km, mo.toDeg));
  const ring = convexHull([...pts, ...moved]);
  const r = (v) => Math.round(v * 1e5) / 1e5;
  return { type: "Polygon", coordinates: [ring.map(([x, y]) => [r(x), r(y)])] };
}

/** Andrew's monotone chain; returns a closed counter-clockwise ring. */
function convexHull(points) {
  const p = [...new Map(points.map((q) => [`${q[0]},${q[1]}`, q])).values()]
    .sort((a, b) => a[0] - b[0] || a[1] - b[1]);
  if (p.length < 3) throw new Error("Not enough points for an area.");
  const cross = (o, a, b) => (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0]);
  const half = (pts) => {
    const out = [];
    for (const q of pts) {
      while (out.length >= 2 && cross(out[out.length - 2], out[out.length - 1], q) <= 0) out.pop();
      out.push(q);
    }
    out.pop();
    return out;
  };
  const hull = [...half(p), ...half([...p].reverse())];
  return [...hull, hull[0]];
}

/**
 * A made-up tornado warning near `center` ({ lat, lon }), shaped like a real storm-based warning:
 * a box that widens downstream, storm moving from the WSW at 35 kt. For trying the feature when
 * no real tornado warnings are active. Always labelled as a demo.
 */
export function demoTornado(center, regionName = "", now = Date.now()) {
  const fromDeg = 240;
  const toDeg = (fromDeg + 180) % 360;
  const c = [center.lon, center.lat];
  const start = offset(c, 12, fromDeg);              // storm 12 km upstream of the region centre
  const front = offset(start, 30, toDeg);            // ~30 km of path ahead of it
  const side = (pt, km, sign) => offset(pt, km, toDeg + sign * 90);
  const r = (v) => Math.round(v * 1e5) / 1e5;
  const ring = [side(start, 3, -1), side(front, 7, -1), side(front, 7, 1), side(start, 3, 1)]
    .map(([x, y]) => [r(x), r(y)]);
  ring.push(ring[0]);
  const knots = 35;
  return {
    id: "demo-tornado",
    source: "demo",
    event: "Tornado Warning (demo)",
    level: "Warning",
    headline: "Demo only: not a real warning",
    severity: "Extreme",
    areaDesc: regionName ? `Made-up storm near ${regionName}` : "Made-up storm",
    ends: new Date(now + 45 * 60 * 1000).toISOString(),
    geometry: { type: "Polygon", coordinates: [ring] },
    zones: [],
    link: null,
    demo: true,
    storm: {
      isTornado: true,
      detection: "Radar indicated",
      damageThreat: null,
      hail: "1.00",
      motion: { fromDeg, toDeg, knots, mph: Math.round(knots * 1.15078), heading: compass(toDeg), points: [start.map(r)], time: null },
    },
  };
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
  let polygons = polygonsOf(hazard.geometry ?? hazard.outline);
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
  const extent = (ring) => {
    let w = Infinity, s = Infinity, e = -Infinity, n = -Infinity;
    for (const [x, y] of ring) { if (x < w) w = x; if (x > e) e = x; if (y < s) s = y; if (y > n) n = y; }
    return Math.max(e - w, n - s);
  };
  const largest = polygons.reduce((best, p) => (extent(p[0]) > extent(best[0]) ? p : best), polygons[0]);
  let tol = 0.0003;                                             // ~30 m, doubled until it fits
  let out = polygons;
  while (count(out) > maxVertices && tol < 0.2) {
    // Pieces smaller than a few tolerances (marsh islands, sandbars) go first, so they don't
    // force the tolerance up until the real outlines lose their shape. The largest always stays.
    out = polygons
      .filter((p) => p === largest || extent(p[0]) >= tol * 4)
      .map((p) => p.map((ring) => simplifyRing(ring, tol)).filter((ring) => ring.length >= 4))
      .filter((p) => p.length);
    tol *= 2;
  }
  // Still over: drop holes, then the smallest pieces, keeping the largest until it fits.
  if (count(out) > maxVertices) {
    const kept = [];
    let n = 0;
    for (const p of [...out].sort((a, b) => extent(b[0]) - extent(a[0]))) {
      if (kept.length && n + p[0].length > maxVertices) continue;
      kept.push([p[0]]);
      n += p[0].length;
    }
    out = kept;
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
