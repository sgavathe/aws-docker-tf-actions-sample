import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { fetchAlerts, fetchCones, hazardArea, nearBox, statesFor } from "./hazards.js";

const REFRESH_MS = 5 * 60 * 1000;
const SHOWN = 6;

const until = (iso) => {
  if (!iso) return "";
  const d = new Date(iso);
  return `until ${d.toLocaleString("en-US", { weekday: "short", hour: "numeric", minute: "2-digit" })}`;
};

/**
 * "Live hazards": current NWS alerts and hurricane forecast cones for the study area.
 * Each can be run as the cascade area. Outlines that come with the feed go to the map.
 */
export default function HazardsPanel({ graph, busy, onRun, onHazards }) {
  const [items, setItems] = useState(null);       // null = loading
  const [elsewhere, setElsewhere] = useState(0);  // storms outside the study area
  const [error, setError] = useState("");
  const [updated, setUpdated] = useState(null);
  const [all, setAll] = useState(false);
  const [running, setRunning] = useState(null);
  const [runError, setRunError] = useState("");
  const [outlines, setOutlines] = useState(() => {
    try { return localStorage.getItem("cascade.hazardOutlines") !== "off"; } catch { return true; }
  });
  const abort = useRef(null);

  const states = useMemo(() => statesFor(graph?.meta), [graph]);
  const box = useMemo(() => {
    if (!graph?.nodes?.length) return null;
    let w = 180, s = 90, e = -180, n = -90;
    for (const { lon, lat } of graph.nodes) {
      if (lon < w) w = lon; if (lon > e) e = lon; if (lat < s) s = lat; if (lat > n) n = lat;
    }
    return [w, s, e, n];
  }, [graph]);

  const load = useCallback(async () => {
    abort.current?.abort();
    const ctrl = new AbortController();
    abort.current = ctrl;
    setError("");
    const [alerts, cones] = await Promise.allSettled([fetchAlerts(states, ctrl.signal), fetchCones(ctrl.signal)]);
    if (ctrl.signal.aborted) return;
    const near = cones.status === "fulfilled" ? cones.value.filter((c) => !box || nearBox(c.geometry, box)) : [];
    setElsewhere(cones.status === "fulfilled" ? cones.value.length - near.length : 0);
    const list = [...near, ...(alerts.status === "fulfilled" ? alerts.value : [])];
    setItems(list);
    const failed = [alerts, cones].filter((r) => r.status === "rejected").map((r) => r.reason?.message);
    if (failed.length) setError(failed.join(" · "));
    setUpdated(new Date());
  }, [states, box, onHazards]);

  // Outlines on the map follow the checkbox; the list itself always shows every hazard.
  useEffect(() => {
    onHazards?.(outlines && items ? items.filter((h) => h.geometry) : []);
    try { localStorage.setItem("cascade.hazardOutlines", outlines ? "on" : "off"); } catch { /* private mode */ }
  }, [items, outlines, onHazards]);

  useEffect(() => {
    if (!graph) return undefined;
    setItems(null);                                   // new region: don't show the old one's list
    load();
    const t = setInterval(load, REFRESH_MS);
    return () => { clearInterval(t); abort.current?.abort(); };
  }, [graph, load]);

  const run = async (h) => {
    setRunning(h.id);
    setRunError("");
    try {
      const area = await hazardArea(h);
      onRun(area, `${h.event}${h.areaDesc ? `: ${h.areaDesc}` : ""}`);
    } catch (e) {
      setRunError(`${h.event}: ${e.message}`);
    } finally {
      setRunning(null);
    }
  };

  const shown = items && (all ? items : items.slice(0, SHOWN));

  return (
    <section className="block hazards" aria-labelledby="hazards-h">
      <div className="hazards-head">
        <h2 id="hazards-h">Live hazards</h2>
        <button type="button" className="link-btn" onClick={load} disabled={!graph}>Refresh</button>
      </div>
      <label className="check">
        <input id="hazard-outlines" type="checkbox" checked={outlines} onChange={(e) => setOutlines(e.target.checked)} />
        <span>Show hazard outlines on map</span>
      </label>
      {items === null && <p className="muted">Checking the National Weather Service and hurricane feeds…</p>}
      {items && !items.length && (
        <p className="muted">No active weather alerts for {states.join(", ")}{elsewhere ? `; ${elsewhere} storm cone${elsewhere === 1 ? "" : "s"} active elsewhere` : ""}.</p>
      )}
      {shown?.length > 0 && (
        <ul className="hazard-list">
          {shown.map((h) => (
            <li key={h.id}>
              <span className={`hz-tag hz-${h.level.toLowerCase()}`}>{h.level}</span>
              <span className="hz-main">
                <span className="hz-title">{h.event}</span>
                <span className="hz-sub" title={h.areaDesc}>
                  {[h.headline && h.source === "nhc" ? h.headline : "", h.areaDesc, until(h.ends)].filter(Boolean).join(" · ")}
                </span>
              </span>
              <button type="button" className="hz-run" onClick={() => run(h)}
                      disabled={busy || running !== null}
                      aria-label={`Run the cascade on ${h.event}`}>
                {running === h.id ? "Loading…" : "Run cascade"}
              </button>
            </li>
          ))}
        </ul>
      )}
      {items && items.length > SHOWN && (
        <button type="button" className="link-btn" onClick={() => setAll(!all)}>
          {all ? "Show fewer" : `Show all ${items.length}`}
        </button>
      )}
      {runError && <p className="error" role="alert">{runError}</p>}
      {error && <p className="muted">Feed problem: {error}</p>}
      <p className="muted hz-credit">
        Alerts: <a href="https://www.weather.gov/" target="_blank" rel="noreferrer">National Weather Service</a> ·
        cones: <a href="https://www.nhc.noaa.gov/" target="_blank" rel="noreferrer">NHC</a> via Esri Living Atlas
        {updated ? ` · checked ${updated.toLocaleTimeString("en-US", { hour: "numeric", minute: "2-digit" })}` : ""}
      </p>
    </section>
  );
}
