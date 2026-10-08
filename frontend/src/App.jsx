import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import MapPanel from "./components/MapPanel.jsx";
import ViewNav from "./ViewNav.jsx";
import { api, TYPE_COLORS } from "./api.js";

const TYPES = Object.keys(TYPE_COLORS);

export default function App() {
  const [ports, setPorts] = useState([]);
  const [portId, setPortId] = useState("HRO");
  const [radiusNm, setRadiusNm] = useState(15);
  const [type, setType] = useState("");

  const [incidents, setIncidents] = useState(null);   // GeoJSON FeatureCollection
  const [search, setSearch] = useState(null);         // { lat, lon, radiusNm, label }
  const [nearby, setNearby] = useState(null);         // GeoJSON FeatureCollection
  const [weather, setWeather] = useState(null);       // { data } | { error }
  const [focus, setFocus] = useState(null);

  const [showHotspots, setShowHotspots] = useState(true);
  const [k, setK] = useState(8);
  const [hotspots, setHotspots] = useState(null);

  const [error, setError] = useState("");
  const [requestId, setRequestId] = useState("");
  const [busy, setBusy] = useState(false);
  const abortRef = useRef(null);

  useEffect(() => { document.title = "Harbor Watch: harbor incidents, hotspots and weather | SpatialEnable"; }, []);

  const track = (res) => {
    if (res?.requestId) setRequestId(res.requestId);
    return res?.data;
  };

  // Nearby search + forecast for one point. Cancels any search still in flight.
  const runSearch = useCallback(
    async (lat, lon, label) => {
      abortRef.current?.abort();
      const controller = new AbortController();
      abortRef.current = controller;

      setSearch({ lat, lon, radiusNm, label });
      setBusy(true);
      setError("");
      setWeather(null);

      try {
        const [nearbyRes, weatherRes] = await Promise.allSettled([
          api.nearby({ lat, lon, radiusNm, type }, { signal: controller.signal }),
          api.weather({ lat, lon }, { signal: controller.signal }),
        ]);
        if (controller.signal.aborted) return;

        if (nearbyRes.status === "fulfilled") setNearby(track(nearbyRes.value).results);
        else setError(nearbyRes.reason.message);

        setWeather(
          weatherRes.status === "fulfilled"
            ? { data: weatherRes.value.data }
            : { error: "No forecast is available for this point. Forecasts cover US waters only." }
        );
      } finally {
        if (!controller.signal.aborted) setBusy(false);
      }
    },
    [radiusNm, type]
  );

  // Load ports once.
  useEffect(() => {
    api.ports().then((r) => setPorts(track(r))).catch((e) => setError(e.message));
  }, []);

  // Reload the incident layer when the type filter changes.
  useEffect(() => {
    api.incidents(type).then((r) => setIncidents(track(r))).catch((e) => setError(e.message));
  }, [type]);

  // Re-run the current search when radius or type changes (or first load).
  useEffect(() => {
    const port = ports.find((p) => p.id === portId);
    if (search) runSearch(search.lat, search.lon, search.label);
    else if (port) runSearch(port.location.lat, port.location.lon, port.name);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [runSearch, ports]);

  // ML hotspots (retrained server-side per k, then cached).
  useEffect(() => {
    if (!showHotspots) return;
    api.hotspots(k).then((r) => setHotspots(track(r))).catch((e) => setError(e.message));
  }, [k, showHotspots]);

  const choosePort = (id) => {
    setPortId(id);
    const port = ports.find((p) => p.id === id);
    if (!port) return;
    setFocus({ lat: port.location.lat, lon: port.location.lon, zoom: 9 });
    runSearch(port.location.lat, port.location.lon, port.name);
  };

  const handleMapClick = useCallback(
    (lat, lon) => runSearch(lat, lon, `${lat.toFixed(3)}, ${lon.toFixed(3)}`),
    [runSearch]
  );

  const hitIds = useMemo(
    () => new Set((nearby?.features ?? []).map((f) => f.properties.id)),
    [nearby]
  );
  const topHotspots = (hotspots?.features ?? []).slice(0, 3);
  const hits = nearby?.features ?? [];

  return (
    <div className="app">
      <aside className="panel">
        <ViewNav current="harbor" />
        <header className="brand">
          <div className="stripe" aria-hidden="true" />
          <h1>Harbor Watch</h1>
          <p className="lede">Maritime incident explorer on synthetic demo data.</p>
        </header>

        <section className="block" aria-labelledby="search-h">
          <h2 id="search-h">Search an area</h2>
          <label className="field">
            <span>Port</span>
            <select value={portId} onChange={(e) => choosePort(e.target.value)}>
              {ports.map((p) => (
                <option key={p.id} value={p.id}>{p.name}</option>
              ))}
            </select>
          </label>

          <label className="field">
            <span>Radius: {radiusNm} nm</span>
            <input type="range" min="5" max="60" step="5" value={radiusNm}
                   onChange={(e) => setRadiusNm(Number(e.target.value))} />
          </label>

          <label className="field">
            <span>Incident type</span>
            <select value={type} onChange={(e) => setType(e.target.value)}>
              <option value="">All types</option>
              {TYPES.map((t) => <option key={t} value={t}>{t}</option>)}
            </select>
          </label>
          <p className="hint">Or click open water on the map to search there.</p>
        </section>

        <section className="block" aria-live="polite" aria-labelledby="results-h">
          <h2 id="results-h">
            {busy ? "Searching…" : `${hits.length} incident${hits.length === 1 ? "" : "s"} within ${search?.radiusNm ?? radiusNm} nm`}
          </h2>
          {search && <p className="where">Around {search.label}</p>}

          {weather?.data && (
            <p className="weather">
              <strong>{weather.data.period}:</strong> {weather.data.temperature}°{weather.data.temperatureUnit},
              wind {weather.data.windDirection} {weather.data.windSpeed}. {weather.data.shortForecast}.
            </p>
          )}
          {weather?.error && <p className="weather muted">{weather.error}</p>}

          {!busy && hits.length === 0 && search && (
            <p className="muted">Nothing reported here. Widen the radius or pick another port.</p>
          )}
          <ul className="hits">
            {hits.map((f) => {
              const p = f.properties;
              return (
                <li key={p.id}>
                  <button type="button" className="hit"
                          onClick={() => setFocus({ lat: f.geometry.coordinates[1], lon: f.geometry.coordinates[0], zoom: 12 })}>
                    <span className="dot" style={{ background: TYPE_COLORS[p.type] }} />
                    <span className="hit-main">
                      <span className="hit-title">{p.type}, severity {p.severity}</span>
                      <span className="hit-sub">{p.vesselName}. {p.summary}</span>
                    </span>
                    <span className="hit-dist">{p.distanceNm} nm</span>
                  </button>
                </li>
              );
            })}
          </ul>
        </section>

        <section className="block" aria-labelledby="ml-h">
          <h2 id="ml-h">Hotspots</h2>
          <label className="check">
            <input type="checkbox" checked={showHotspots} onChange={(e) => setShowHotspots(e.target.checked)} />
            <span>Show clusters found by K-Means (ML.NET)</span>
          </label>
          <label className="field">
            <span>Number of clusters: {k}</span>
            <input type="range" min="2" max="12" value={k} disabled={!showHotspots}
                   onChange={(e) => setK(Number(e.target.value))} />
          </label>
          {showHotspots && topHotspots.length > 0 && (
            <ol className="risk">
              {topHotspots.map((f) => (
                <li key={f.properties.clusterId}>
                  <button type="button" className="risk-row"
                          onClick={() => setFocus({ lat: f.geometry.coordinates[1], lon: f.geometry.coordinates[0], zoom: 9 })}>
                    <span>{f.properties.incidentCount} incidents, mostly {f.properties.dominantType}</span>
                    <span className="score">risk {f.properties.riskScore}</span>
                  </button>
                </li>
              ))}
            </ol>
          )}
        </section>

        {error && <p className="error" role="alert">{error}</p>}

        <footer className="foot">
          <a href={`${api.baseUrl}/api/swagger`} target="_blank" rel="noreferrer">API documentation</a>
          {requestId && <span>Last request ID {requestId}</span>}
        </footer>
      </aside>

      <main className="stage">
        <MapPanel
          incidents={incidents}
          hitIds={hitIds}
          hotspots={hotspots}
          showHotspots={showHotspots}
          search={search}
          focus={focus}
          onMapClick={handleMapClick}
        />
        <div className="legend" aria-label="Legend">
          {TYPES.map((t) => (
            <span key={t}><i style={{ background: TYPE_COLORS[t] }} />{t}</span>
          ))}
        </div>
      </main>
    </div>
  );
}
