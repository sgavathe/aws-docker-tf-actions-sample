import { useEffect, useRef, useState } from "react";
import { TYPE_COLORS } from "../api.js";

const INCIDENT_POPUP = {
  title: "{type}: {vesselName}",
  content:
    "{summary}<br/>Severity {severity} of 5<br/>Port area {portId}<br/>Reported {reportedUtc}",
};

const HOTSPOT_POPUP = {
  title: "Hotspot, risk score {riskScore}",
  content:
    "{incidentCount} incidents, average severity {avgSeverity}<br/>Most common: {dominantType}<br/>Spread about {radiusNm} nm",
};

/**
 * ArcGIS Maps SDK for JavaScript (@arcgis/core) inside React.
 * The map is created once; later prop changes only redraw the graphics layers.
 */
export default function MapPanel({ incidents, hitIds, hotspots, showHotspots, search, focus, onMapClick }) {
  const containerRef = useRef(null);
  const esri = useRef(null); // { view, Graphic, Circle, layers }
  const clickRef = useRef(onMapClick);
  const [ready, setReady] = useState(false);

  clickRef.current = onMapClick; // always call the latest handler

  // 1) Create the map and view once (modules loaded lazily to keep the first bundle small).
  useEffect(() => {
    let destroyed = false;
    let view;

    (async () => {
      const [
        { default: Map },
        { default: MapView },
        { default: GraphicsLayer },
        { default: Graphic },
        { default: Circle },
      ] = await Promise.all([
        import("@arcgis/core/Map"),
        import("@arcgis/core/views/MapView"),
        import("@arcgis/core/layers/GraphicsLayer"),
        import("@arcgis/core/Graphic"),
        import("@arcgis/core/geometry/Circle"),
      ]);
      if (destroyed) return;

      const layers = {
        hotspots: new GraphicsLayer({ title: "ML hotspots" }),
        search: new GraphicsLayer({ title: "Search area" }),
        incidents: new GraphicsLayer({ title: "Incidents" }),
      };

      view = new MapView({
        container: containerRef.current,
        // "oceans" is a classic Esri basemap served from public tile services (no API key).
        map: new Map({ basemap: "oceans", layers: [layers.hotspots, layers.search, layers.incidents] }),
        center: [-76.33, 36.95],
        zoom: 8,
        // Pin Web Mercator so graphics draw correctly even if the basemap is unreachable.
        spatialReference: { wkid: 3857 },
        constraints: { snapToZoom: false },
      });

      // Click empty water to search there; clicking an incident just opens its popup.
      view.on("click", async (event) => {
        const { results } = await view.hitTest(event, { include: [layers.incidents] });
        if (results.length === 0 && event.mapPoint) {
          clickRef.current?.(event.mapPoint.latitude, event.mapPoint.longitude);
        }
      });

      esri.current = { view, Graphic, Circle, layers };
      setReady(true);
    })();

    return () => {
      destroyed = true;
      view?.destroy();
    };
  }, []);

  // 2) Incidents: all shown small; the ones inside the search radius shown larger.
  useEffect(() => {
    if (!ready) return;
    const { Graphic, layers } = esri.current;
    layers.incidents.removeAll();

    for (const f of incidents?.features ?? []) {
      const p = f.properties;
      const isHit = hitIds.has(p.id);
      layers.incidents.add(
        new Graphic({
          geometry: { type: "point", longitude: f.geometry.coordinates[0], latitude: f.geometry.coordinates[1] },
          attributes: p,
          popupTemplate: INCIDENT_POPUP,
          symbol: {
            type: "simple-marker",
            color: TYPE_COLORS[p.type] ?? "#333",
            size: isHit ? 11 + p.severity : 6,
            outline: { color: isHit ? "#ffffff" : [255, 255, 255, 0.6], width: isHit ? 2 : 0.75 },
          },
        })
      );
    }
  }, [ready, incidents, hitIds]);

  // 3) Search radius as a geodesic circle (true nautical miles on the ground).
  useEffect(() => {
    if (!ready) return;
    const { Graphic, Circle, layers } = esri.current;
    layers.search.removeAll();
    if (!search) return;

    layers.search.add(
      new Graphic({
        geometry: new Circle({
          center: [search.lon, search.lat],
          radius: search.radiusNm,
          radiusUnit: "nautical-miles",
          geodesic: true,
        }),
        symbol: {
          type: "simple-fill",
          color: [15, 45, 70, 0.08],
          outline: { color: [15, 45, 70, 0.9], width: 1.5, style: "dash" },
        },
      })
    );
  }, [ready, search]);

  // 4) ML.NET hotspots: circle sized by cluster spread, opacity by risk.
  useEffect(() => {
    if (!ready) return;
    const { Graphic, Circle, layers } = esri.current;
    layers.hotspots.removeAll();
    if (!showHotspots) return;

    const features = hotspots?.features ?? [];
    const maxRisk = Math.max(1, ...features.map((f) => f.properties.riskScore));
    for (const f of features) {
      const p = f.properties;
      const strength = p.riskScore / maxRisk;
      layers.hotspots.add(
        new Graphic({
          geometry: new Circle({
            center: f.geometry.coordinates,
            radius: Math.max(p.radiusNm, 3),
            radiusUnit: "nautical-miles",
            geodesic: true,
          }),
          attributes: p,
          popupTemplate: HOTSPOT_POPUP,
          symbol: {
            type: "simple-fill",
            color: [200, 16, 46, 0.08 + 0.22 * strength],
            outline: { color: [200, 16, 46, 0.35 + 0.5 * strength], width: 1 },
          },
        })
      );
    }
  }, [ready, hotspots, showHotspots]);

  // 5) Fly to a new focus point (e.g., when a port is chosen).
  useEffect(() => {
    if (!ready || !focus) return;
    const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    esri.current.view
      .goTo({ center: [focus.lon, focus.lat], zoom: focus.zoom ?? 9 }, { animate: !reduceMotion, duration: 800 })
      .catch(() => {}); // goTo rejects if interrupted by another navigation; safe to ignore
  }, [ready, focus]);

  return <div ref={containerRef} className="map" aria-label="Map of maritime incidents" />;
}
