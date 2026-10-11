import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  attachOutlines, categoryCounts, categoryOf, demoTornado, fetchAlerts, fetchCones,
  hazardArea, nearBox, projectPath, statesFor,
} from "./hazards.js";

const REFRESH_MS = 5 * 60 * 1000;
const TORNADO_REFRESH_MS = 60 * 1000;   // while a tornado warning is active
const PROJECT_MIN = 30;                 // "+30 min" projection along storm motion
const SHOWN = 6;

const rgb = (c) => `rgb(${c.join(",")})`;

const until = (iso) => {
  if (!iso) return "";
  const d = new Date(iso);
  return `until ${d.toLocaleString("en-US", { weekday: "short", hour: "numeric", minute: "2-digit" })}`;
};

const isTornadoWarning = (h) => h.storm?.isTornado && h.level === "Warning";

/** Tag text: the level, except tornado warnings, which say so (and emergencies louder). */
function tagText(h) {
  if (isTornadoWarning(h)) return h.storm.damageThreat === "Catastrophic" ? "Tornado emergency" : "Tornado";
  return h.level;
}

/** "Radar indicated · moving NE at 40 mph · considerable damage threat · hail 1.00 in" */
function stormLine(h) {
  const s = h.storm;
  if (!s) return "";
  return [
    s.detection,
    s.motion?.knots ? `moving ${s.motion.heading} at ${s.motion.mph} mph` : "",
    s.damageThreat ? `${s.damageThreat.toLowerCase()} damage threat` : "",
    s.hail && Number(s.hail) > 0 ? `hail ${s.hail} in` : "",
  ].filter(Boolean).join(" · ");
}

/**
 * "Live hazards": current NWS alerts and hurricane forecast cones for the selected region's states.
 * Each can be run as the cascade area, and outlines go to the map, colour-coded by hazard type
 * (tornado, tropical, severe storms, flood...) with the level shown by line style.
 * Tornado and severe-storm watches are county lists with no polygon; their county outlines are
 * fetched in the background so they show on the map too.
 */
export default function HazardsPanel({ graph, busy, onRun, onHazards }) {
  const [items, setItems] = useState(null);       // null = loading
  const [elsewhere, setElsewhere] = useState(0);  // storms outside the study area
  const [error, setError] = useState("");
  const [updated, setUpdated] = useState(null);
  const [all, setAll] = useState(false);
  const [running, setRunning] = useState(null);
  const [runError, setRunError] = useState("");
  const [demo, setDemo] = useState(false);
  const [lastRun, setLastRun] = useState(null);   // id of the hazard whose cascade is running/shown
  const [category, setCategory] = useState("all");
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

  // A demo warning near the region centre, rebuilt when the region changes.
  const demoHazard = useMemo(() => {
    const c = graph?.meta?.center;
    return demo && c ? demoTornado(c, graph.meta.region) : null;
  }, [demo, graph]);

  const load = useCallback(async () => {
    abort.current?.abort();
    const ctrl = new AbortController();
    abort.current = ctrl;
    setError("");
    const [alerts, cones] = await Promise.allSettled([fetchAlerts(states, ctrl.signal), fetchCones(ctrl.signal)]);
    if (ctrl.signal.aborted) return;
    const near = cones.status === "fulfilled" ? cones.value.filter((c) => !box || nearBox(c.geometry, box)) : [];
    setElsewhere(cones.status === "fulfilled" ? cones.value.length - near.length : 0);
    const nws = alerts.status === "fulfilled" ? alerts.value : [];
    // Tornado alerts (already sorted first by fetchAlerts) above the hurricane cones, then the rest.
    const tornado = nws.filter((h) => h.storm?.isTornado);
    const list = [...tornado, ...near, ...nws.filter((h) => !h.storm?.isTornado)];
    setItems(list);
    const failed = [alerts, cones].filter((r) => r.status === "rejected").map((r) => r.reason?.message);
    if (failed.length) setError(failed.join(" · "));
    setUpdated(new Date());
    // Then, in the background: county/zone outlines for watches, so they appear on the map.
    try {
      const withOutlines = await attachOutlines(list, ctrl.signal);
      if (!ctrl.signal.aborted && withOutlines !== list) setItems(withOutlines);
    } catch { /* outlines are a nice-to-have; the list already shows */ }
  }, [states, box]);

  const list = useMemo(
    () => (items ? (demoHazard ? [demoHazard, ...items] : items) : demoHazard ? [demoHazard] : null),
    [items, demoHazard]
  );
  const counts = useMemo(() => categoryCounts(list ?? []), [list]);
  // If the chosen category empties out on a refresh, fall back to everything.
  const activeCategory = counts.some((c) => c.id === category) ? category : "all";
  const filtered = useMemo(
    () => (list && activeCategory !== "all" ? list.filter((h) => categoryOf(h).id === activeCategory) : list),
    [list, activeCategory]
  );
  const tornadoActive = useMemo(() => (items ?? []).some(isTornadoWarning), [items]);

  // Outlines on the map follow the checkbox and the category filter.
  useEffect(() => {
    onHazards?.(outlines && filtered ? filtered.filter((h) => h.geometry || h.outline) : []);
    try { localStorage.setItem("cascade.hazardOutlines", outlines ? "on" : "off"); } catch { /* private mode */ }
  }, [filtered, outlines, onHazards]);

  // New region: clear the old list and filter, and load once.
  useEffect(() => {
    if (!graph) return undefined;
    setItems(null);
    setCategory("all");
    load();
    return () => abort.current?.abort();
  }, [graph, load]);

  // Poll every 5 minutes, or every minute while a real tornado warning is active.
  useEffect(() => {
    if (!graph) return undefined;
    const t = setInterval(load, tornadoActive ? TORNADO_REFRESH_MS : REFRESH_MS);
    return () => clearInterval(t);
  }, [graph, load, tornadoActive]);

  const run = async (h, minutes = 0) => {
    const key = `${h.id}|${minutes}`;
    setRunning(key);
    setRunError("");
    try {
      const area = minutes ? projectPath(h, minutes) : await hazardArea(h);
      const what = minutes ? `${h.event}, extended ${minutes} min along its path (estimate)` : h.event;
      setLastRun(h.id);
      onRun(area, `${what}${h.areaDesc ? `: ${h.areaDesc}` : ""}`);
    } catch (e) {
      setRunError(`${h.event}: ${e.message}`);
    } finally {
      setRunning(null);
    }
  };

  const shown = filtered && (all ? filtered : filtered.slice(0, SHOWN));
  const realCount = items?.length ?? 0;
  const regionName = graph?.meta?.region ?? states.join(", ");

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
      {tornadoActive && (
        <p className="hz-alert" role="status">
          Tornado warning in effect for {states.join(", ")}. This list now updates every minute.
        </p>
      )}

      {counts.length > 1 && (
        <div className="hz-filters" role="group" aria-label="Filter hazards by type">
          <button type="button" className="hz-chip" aria-pressed={activeCategory === "all"}
                  onClick={() => { setCategory("all"); setAll(false); }}>
            All <span className="hz-count">{list.length}</span>
          </button>
          {counts.map((c) => (
            <button key={c.id} type="button" className="hz-chip" aria-pressed={activeCategory === c.id}
                    style={{ "--cat": rgb(c.color) }}
                    onClick={() => { setCategory(activeCategory === c.id ? "all" : c.id); setAll(false); }}>
              <i className="hz-swatch" aria-hidden="true" />{c.label} <span className="hz-count">{c.count}</span>
            </button>
          ))}
        </div>
      )}

      {items === null && <p className="muted">Checking the National Weather Service and hurricane feeds for {regionName}…</p>}
      {items && !realCount && (
        <p className="muted">No active weather alerts for {states.join(", ")}{elsewhere ? `; ${elsewhere} storm cone${elsewhere === 1 ? "" : "s"} active elsewhere` : ""}.</p>
      )}
      {shown?.length > 0 && (
        <ul className="hazard-list">
          {shown.map((h) => {
            const cat = categoryOf(h);
            const storm = stormLine(h);
            const canProject = Boolean(h.storm?.motion?.knots && h.geometry);
            const level = h.level.toLowerCase();
            const rowClass = [h.demo ? "is-demo" : "", isTornadoWarning(h) ? "is-tornado" : ""].filter(Boolean).join(" ");
            return (
              <li key={h.id} className={rowClass} style={{ "--cat": rgb(cat.color) }}>
                <span className={`hz-tag lvl-${level}${isTornadoWarning(h) ? " hz-tornado" : ""}${tagText(h) === "Tornado emergency" ? " hz-emergency" : ""}`}
                      title={`${cat.label}: ${h.level}`}>
                  {tagText(h)}
                </span>
                <span className="hz-main">
                  <span className="hz-title">{h.event}</span>
                  {storm && <span className="hz-storm">{storm}</span>}
                  <span className="hz-sub" title={h.areaDesc}>
                    {[h.headline && (h.source === "nhc" || h.demo) ? h.headline : "", h.areaDesc, until(h.ends)].filter(Boolean).join(" · ")}
                  </span>
                </span>
                <span className="hz-actions">
                  <button type="button" className="hz-run" onClick={() => run(h)}
                          disabled={busy || running !== null}
                          aria-label={`Run the cascade on ${h.event}`}>
                    {running === `${h.id}|0` ? "Loading…" : busy && lastRun === h.id ? "Running…" : "Run cascade"}
                  </button>
                  {canProject && (
                    <button type="button" className="hz-run hz-project" onClick={() => run(h, PROJECT_MIN)}
                            disabled={busy || running !== null}
                            title={`The warning area extended ${PROJECT_MIN} minutes along the storm's motion (an estimate, not an NWS product)`}
                            aria-label={`Run the cascade on ${h.event} extended ${PROJECT_MIN} minutes along its path`}>
                      {running === `${h.id}|${PROJECT_MIN}` ? "Loading…" : `+${PROJECT_MIN} min path`}
                    </button>
                  )}
                </span>
              </li>
            );
          })}
        </ul>
      )}
      {filtered && filtered.length > SHOWN && (
        <button type="button" className="link-btn" onClick={() => setAll(!all)}>
          {all ? "Show fewer" : `Show all ${filtered.length}`}
        </button>
      )}
      <div className="tool-row">
        <button type="button" className="link-btn" onClick={() => setDemo(!demo)} disabled={!graph?.meta?.center}
                aria-pressed={demo}>
          {demo ? "Remove demo tornado" : "Try a demo tornado warning"}
        </button>
      </div>
      {demo && (
        <p className="muted">
          The demo is a made-up tornado warning near the middle of the region, moving east-northeast, so you can
          see how a real one works. It is not an alert.
        </p>
      )}
      {runError && <p className="error" role="alert">{runError}</p>}
      {error && <p className="muted">Feed problem: {error}</p>}
      <p className="muted hz-credit">
        Alerts: <a href="https://www.weather.gov/" target="_blank" rel="noreferrer">National Weather Service</a> ·
        cones: <a href="https://www.nhc.noaa.gov/" target="_blank" rel="noreferrer">NHC</a> via Esri Living Atlas ·
        radar: <a href="https://radar.weather.gov/" target="_blank" rel="noreferrer">NWS</a>
        {updated ? ` · checked ${updated.toLocaleTimeString("en-US", { hour: "numeric", minute: "2-digit" })}` : ""}
      </p>
    </section>
  );
}
