// Resolve the API base URL: container runtime config -> Vite env var -> local default.
const runtime = window.__env?.apiBase;
const API_BASE =
  (runtime && !runtime.includes("${") ? runtime : "") ||
  import.meta.env.VITE_API_BASE ||
  "http://localhost:8080";

/** Fetch JSON and return { data, requestId }. Throws with the API's ProblemDetails message. */
async function getJson(path, { signal } = {}) {
  const res = await fetch(`${API_BASE}${path}`, { signal });
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
};

export const TYPE_COLORS = {
  Pollution: "#2F8F5B",
  Collision: "#C8102E",
  Grounding: "#B8860B",
  Fire: "#E8671B",
  Machinery: "#5B5F97",
  MedEvac: "#1F7AC9",
};
