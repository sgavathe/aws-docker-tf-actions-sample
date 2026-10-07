// Resolve the API base URL: container runtime config -> Vite env var -> local default.
const runtime = window.__env?.apiBase;
const API_BASE =
  (runtime && !runtime.includes("${") ? runtime : "") ||
  import.meta.env.VITE_API_BASE ||
  "http://localhost:8080";

/** Fetch JSON and return { data, requestId }. Throws with the API's ProblemDetails message. */
async function getJson(path, { signal, init } = {}) {
  const res = await fetch(`${API_BASE}${path}`, { ...init, signal });
  const requestId = res.headers.get("X-Correlation-ID");
  const body = await res.json().catch(() => null);

  if (!res.ok) {
    const fieldErrors = body?.errors ? Object.values(body.errors).flat().join(" ") : "";
    const message = fieldErrors || body?.detail || body?.title || `Request failed (${res.status})`;
    const err = new Error(message);
    err.requestId = requestId;
    throw err;
  }
  return { data: body, requestId };
}

/** Hex SHA-256 of a string (Web Crypto; available on https and localhost). */
async function sha256Hex(text) {
  if (!globalThis.crypto?.subtle) return null;
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(text));
  return [...new Uint8Array(digest)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

/**
 * POST JSON. In the serverless stack CloudFront signs requests to the Lambda Function URL
 * (Origin Access Control), and Lambda only accepts signed POST bodies when the viewer
 * sends the body's SHA-256 in x-amz-content-sha256. Harmless everywhere else.
 */
async function postJson(path, body, { signal } = {}) {
  const text = JSON.stringify(body);
  const headers = { "Content-Type": "application/json" };
  const hash = await sha256Hex(text);
  if (hash) headers["x-amz-content-sha256"] = hash;
  return getJson(path, { signal, init: { method: "POST", headers, body: text } });
}

const qs = (params) =>
  new URLSearchParams(
    Object.entries(params).filter(([, v]) => v !== undefined && v !== null && v !== "")
  ).toString();

export const api = {
  baseUrl: API_BASE,
  ports: () => getJson("/api/ports"),
  incidents: (type) => getJson(`/api/incidents?${qs({ type })}`),
  nearby: ({ lat, lon, radiusNm, type }, opts) =>
    getJson(`/api/incidents/nearby?${qs({ lat, lon, radiusNm, type })}`, opts),
  hotspots: (k) => getJson(`/api/hotspots?${qs({ k })}`),
  weather: ({ lat, lon }, opts) => getJson(`/api/weather?${qs({ lat, lon })}`, opts),

  // Critical-infrastructure dependency graph (see pipeline/ and InfrastructureEndpoints.cs)
  infraGraph: (opts) => getJson("/api/infrastructure/graph", opts),
  impact: (area, opts) => postJson("/api/infrastructure/impact", { area }, opts),
};

export const TYPE_COLORS = {
  Pollution: "#2F8F5B",
  Collision: "#C8102E",
  Grounding: "#B8860B",
  Fire: "#E8671B",
  Machinery: "#5B5F97",
  MedEvac: "#1F7AC9",
};
