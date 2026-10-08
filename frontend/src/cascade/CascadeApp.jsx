import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api } from "../api.js";
import ViewNav from "../ViewNav.jsx";
import CascadeGraph from "./CascadeGraph.jsx";
import CascadeMap from "./CascadeMap.jsx";
import { LINK_TYPES, STATUS, kindLabel, sectorColor, sectorLabel } from "./sectors.js";
import "./cascade.css";

const LIST_LIMIT = 120;

const TOOLS = [
  { id: "polygon", label: "Polygon", hint: "Click to add corners. Double-click to finish." },
  { id: "rectangle", label: "Rectangle", hint: "Drag a box on the map." },
  { id: "freehandPolygon", label: "Freehand", hint: "Hold and drag to trace an outline." },
];

/** A square of the given size (km) centered on a point, as GeoJSON. */
function squareAround(lat, lon, km) {
  const dLat = km / 2 / 110.574;
  const dLon = km / 2 / (111.32 * Math.cos((lat * Math.PI) / 180));
  const r = (v) => Math.round(v * 1e6) / 1e6;
  return {
    type: "Polygon",
    coordinates: [[
      [r(lon - dLon), r(lat - dLat)], [r(lon + dLon), r(lat - dLat)],
      [r(lon + dLon), r(lat + dLat)], [r(lon - dLon), r(lat + dLat)], [r(lon - dLon), r(lat - dLat)],
    ]],
  };
}

const fmt = new Intl.NumberFormat("en-US");

// The API works in kilometres; the page shows US units.
const KM_PER_MI = 1.609344;
const miles = (km) => {
  const mi = km / KM_PER_MI;
  return mi >= 10 ? Math.round(mi) : Math.round(mi * 10) / 10;
};
const squareMiles = (km2) => {
  const mi2 = km2 / (KM_PER_MI * KM_PER_MI);
  return mi2 >= 10 ? fmt.format(Math.round(mi2)) : Math.round(mi2 * 10) / 10;
};

export default function CascadeApp() {
  const [graph, setGraph] = useState(null);
  const [area, setArea] = useState(null);
  const [impact, setImpact] = useState(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [drawingTool, setDrawingTool] = useState(null);
  const [cell, setCell] = useState(null);           // { sector, hop } picked in the graph
  const [picked, setPicked] = useState(null);       // asset id picked in the list
  const [focus, setFocus] = useState(null);         // ids the map should fly to
  const [showAll, setShowAll] = useState(false);
  const [graphOpen, setGraphOpen] = useState(true);
  const [cluster, setCluster] = useState(() => {
    try { return localStorage.getItem("cascade.cluster") !== "off"; } catch { return true; }
  });
  const toggleCluster = (on) => {
    setCluster(on);
    try { localStorage.setItem("cascade.cluster", on ? "on" : "off"); } catch { /* private mode */ }
  };
  const mapRef = useRef(null);
  const abortRef = useRef(null);

  useEffect(() => { document.title = "Grid Cascade: see how infrastructure failures cascade | SpatialEnable"; }, []);

  const runImpact = useCallback(async (geojson) => {
    abortRef.current?.abort();
    const controller = new AbortController();
    abortRef.current = controller;
    setArea(geojson);
    setBusy(true);
    setError("");
    setCell(null);
    setPicked(null);
    setShowAll(false);
    try {
      const res = await api.impact(geojson, { signal: controller.signal });
      if (!controller.signal.aborted) setImpact(res.data);
    } catch (e) {
      if (!controller.signal.aborted) {
        setImpact(null);
        setError(e.message);
      }
    } finally {
      if (!controller.signal.aborted) setBusy(false);
    }
  }, []);

  const example = useCallback(() => {
    const c = graph?.meta?.center;
    if (c) runImpact(squareAround(c.lat, c.lon, 1.25 * KM_PER_MI));   // 1.25-mile square
  }, [graph, runImpact]);

  // Load the graph once, then open on a worked example so the page never starts empty.
  useEffect(() => {
    api.infraGraph()
      .then((r) => setGraph(r.data))
      .catch((e) => setError(`Couldn't load the infrastructure graph. ${e.message}`));
  }, []);
  useEffect(() => {
    if (graph && !area) example();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [graph]);

  const startDrawing = (tool) => {
    setDrawingTool(tool);
    mapRef.current?.draw(tool);
  };
  const clear = () => {
    abortRef.current?.abort();
    mapRef.current?.cancel();
    setArea(null);
    setImpact(null);
    setCell(null);
    setPicked(null);
    setBusy(false);
  };

  const impacts = impact?.impacts ?? [];
  const listed = useMemo(
    () => (cell ? impacts.filter((i) => i.sector === cell.sector && i.hop === cell.hop) : impacts),
    [impacts, cell]
  );
  const visible = showAll ? listed : listed.slice(0, LIST_LIMIT);
  const selected = useMemo(
    () => (picked ? [picked] : cell ? listed.map((i) => i.id) : null),
    [picked, cell, listed]
  );

  const selectCell = (c) => {
    setCell(c);
    setPicked(null);
    setShowAll(false);
    if (c) setFocus(impacts.filter((i) => i.sector === c.sector && i.hop === c.hop).map((i) => i.id));
  };
  const pickAsset = (id) => {
    setPicked(id === picked ? null : id);
    setFocus([id]);
  };

  const meta = graph?.meta;
  const s = impact?.summary;
  const sectorsHit = s?.sectors.filter((x) => x.failed + x.degraded > 0) ?? [];
  const activeHint = TOOLS.find((t) => t.id === drawingTool)?.hint;

  return (
    <div className="app cascade">
      <aside className="panel">
        <ViewNav current="cascade" />
        <header className="brand">
          <div className="stripe" aria-hidden="true" />
          <h1>Grid Cascade</h1>
          <p className="lede">Draw an area to see which power, water and communications services fail, and how far the failure travels.</p>
        </header>

        <section className="block" aria-labelledby="draw-h">
          <h2 id="draw-h">Draw an area</h2>
          <div className="tools" role="group" aria-label="Drawing tools">
            {TOOLS.map((t) => (
              <button key={t.id} type="button" className={drawingTool === t.id ? "tool is-active" : "tool"}
                      aria-pressed={drawingTool === t.id} onClick={() => startDrawing(t.id)}>
                {t.label}
              </button>
            ))}
          </div>
          <p className="hint">
            {activeHint ?? "Everything inside the area is treated as down, and power lines crossing it as cut."}
          </p>
          <label className="check">
            <input id="cluster-toggle" type="checkbox" checked={cluster} onChange={(e) => toggleCluster(e.target.checked)} />
            <span>Group nearby assets when zoomed out</span>
          </label>
          <div className="tool-row">
            <button type="button" className="link-btn" onClick={example} disabled={!graph}>Run the example</button>
            <button type="button" className="link-btn" onClick={clear} disabled={!area && !drawingTool}>Clear</button>
          </div>
        </section>

        {meta && (
          <section className="block source" aria-label="About the data">
            <p>
              <span className={meta.sample ? "badge badge-sample" : "badge"}>
                {meta.sample ? "Synthetic sample" : "OpenStreetMap"}
              </span>{" "}
              {meta.region} · {fmt.format(graph.nodes.length)} assets · {fmt.format(graph.edges.length)} links
            </p>
            <p className="muted">
              Links are inferred from location and map tags (nearest substation, nearest exchange),
              not from utility records.{meta.generatedUtc && !meta.sample ? ` Built ${meta.generatedUtc.slice(0, 10)}.` : ""}
            </p>
          </section>
        )}

        <section className="block" aria-live="polite" aria-labelledby="impact-h">
          <h2 id="impact-h">{busy ? "Tracing the cascade…" : s ? "Impact" : "No area yet"}</h2>
          {!s && !busy && <p className="muted">Pick a drawing tool, or run the example.</p>}
          {s && (
            <>
              <dl className="kpis">
                <div><dt>Failed</dt><dd className="k-failed">{fmt.format(s.failed)}</dd></div>
                <div><dt>On backup</dt><dd className="k-degraded">{fmt.format(s.degraded)}</dd></div>
                <div><dt>Hops</dt><dd>{s.maxHops}</dd></div>
                <div><dt>Reach</dt><dd>{miles(s.reachKm)}<small> mi</small></dd></div>
              </dl>
              <p className="where">
                {squareMiles(s.areaKm2)} sq mi drawn · {s.directlyHit} inside · {s.severedLines} power line{s.severedLines === 1 ? "" : "s"} cut
              </p>
              {sectorsHit.length === 0 ? (
                <p className="muted">Nothing fails. The area misses every asset and power line, or the grid routes around it.</p>
              ) : (
                <table className="sector-table">
                  <caption className="sr-only">Affected assets by sector</caption>
                  <thead>
                    <tr><th scope="col">Sector</th><th scope="col" className="sr-only">Share</th><th scope="col">Hit / total</th></tr>
                  </thead>
                  <tbody>
                    {s.sectors.map((x) => {
                      const hit = x.failed + x.degraded;
                      return (
                        <tr key={x.sector} className={hit ? "" : "is-quiet"}>
                          <th scope="row"><span className="dot" style={{ background: sectorColor(x.sector) }} />{sectorLabel(x.sector)}</th>
                          <td className="bar-cell" aria-hidden="true">
                            <span className="bar">
                              <span className="bar-failed" style={{ width: `${(100 * x.failed) / x.total}%` }} />
                              <span className="bar-degraded" style={{ width: `${(100 * x.degraded) / x.total}%` }} />
                            </span>
                          </td>
                          <td className="num">{hit} / {x.total}</td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              )}
              {impact.sampleData && <p className="muted">Synthetic sample network: names and locations are made up.</p>}
            </>
          )}
        </section>

        {impacts.length > 0 && (
          <section className="block" aria-labelledby="list-h">
            <h2 id="list-h">
              Affected assets
              {cell && (
                <button type="button" className="chip" onClick={() => selectCell(null)}
                        aria-label={`Show all, clear the ${sectorLabel(cell.sector)} hop ${cell.hop} filter`}>
                  {sectorLabel(cell.sector)}, {cell.hop === 0 ? "in area" : `hop ${cell.hop}`} ×
                </button>
              )}
            </h2>
            <ul className="impact-list">
              {visible.map((i, idx) => (
                <li key={i.id}>
                  {(idx === 0 || visible[idx - 1].hop !== i.hop) && !cell && (
                    <p className="hop-head">{i.hop === 0 ? "Inside the area" : `Hop ${i.hop}`}</p>
                  )}
                  <button type="button" className={picked === i.id ? "hit is-picked" : "hit"}
                          aria-pressed={picked === i.id} onClick={() => pickAsset(i.id)}>
                    <span className={i.status === "Failed" ? "dot dot-failed" : "dot dot-degraded"}
                          style={{ "--c": sectorColor(i.sector) }} />
                    <span className="hit-main">
                      <span className="hit-title">{i.name}</span>
                      <span className="hit-sub">{kindLabel(i.kind)}. {i.cause}.</span>
                    </span>
                    <span className={`pill pill-${i.status.toLowerCase()}`}>{STATUS[i.status].label}</span>
                  </button>
                </li>
              ))}
            </ul>
            {listed.length > visible.length && (
              <button type="button" className="link-btn" onClick={() => setShowAll(true)}>
                Show all {fmt.format(listed.length)}
              </button>
            )}
          </section>
        )}

        {error && <p className="error" role="alert">{error}</p>}

        <footer className="foot">
          <a href={`${api.baseUrl}/api/swagger`} target="_blank" rel="noreferrer">API documentation</a>
          {meta && !meta.sample && <span>Map data {meta.attribution}, {meta.license}</span>}
        </footer>
      </aside>

      <main className="stage stage-split">
        <div className="map-wrap">
          <CascadeMap
            ref={mapRef}
            graph={graph}
            area={area}
            impact={impact}
            selected={selected}
            focus={focus}
            cluster={cluster}
            onArea={(geo) => { setDrawingTool(null); runImpact(geo); }}
            onDrawingChange={(on) => { if (!on) setDrawingTool(null); }}
          />
          {!graph && !error && <p className="map-note">Loading the infrastructure network…</p>}
        </div>

        <section className={graphOpen ? "graph-panel" : "graph-panel is-closed"} aria-labelledby="graph-h">
          <header className="graph-head">
            <h2 id="graph-h">Cascade graph</h2>
            <div className="graph-legend" aria-label="Legend">
              <span><i className="lg-failed" />Failed</span>
              <span><i className="lg-degraded" />On backup</span>
              {Object.entries(LINK_TYPES).map(([t, l]) => (
                <span key={t}><i className="lg-line" style={{ background: l.color }} />{l.label}</span>
              ))}
            </div>
            <button type="button" className="link-btn" aria-expanded={graphOpen}
                    onClick={() => setGraphOpen((o) => !o)}>
              {graphOpen ? "Hide" : "Show"}
            </button>
          </header>
          {graphOpen && (
            impact ? (
              <CascadeGraph impact={impact} selectedCell={cell} onSelectCell={selectCell} />
            ) : (
              <p className="graph-empty">{busy ? "Tracing the cascade…" : "Draw an area to see the cascade, sector by sector, hop by hop."}</p>
            )
          )}
        </section>
      </main>
    </div>
  );
}
