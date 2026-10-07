import { forwardRef, useEffect, useImperativeHandle, useRef, useState } from "react";
import { LINK_TYPES, SECTORS, STATUS, kindLabel, rgba, sectorColor, sectorLabel } from "./sectors.js";

const ASSET_POPUP = {
  title: "{name}",
  content: "{kindLabel} ({sectorLabel}){detail}",
};

const IMPACT_POPUP = {
  title: "{name}",
  content: "<b>{statusLabel}</b>, hop {hop}<br/>{cause}<br/>{kindLabel} ({sectorLabel})",
};

const DRAW_SYMBOL = {
  type: "simple-fill",
  color: [200, 16, 46, 0.12],
  outline: { color: [200, 16, 46, 0.95], width: 2, style: "dash" },
};

/**
 * Map for the cascade view (ArcGIS Maps SDK for JavaScript).
 *   wires    mapped power lines (width by voltage)           FeatureLayer, client-side
 *   assets   every facility, colored by sector               FeatureLayer, client-side
 *   area     the drawn polygon (SketchViewModel draws here)  GraphicsLayer
 *   cascade  cut wires, failure links, impacted assets       GraphicsLayer
 *
 * Parent drives drawing through the ref: draw("polygon" | "rectangle" | "freehandPolygon"), cancel().
 */
const CascadeMap = forwardRef(function CascadeMap(
  { graph, area, impact, selected, focus, onArea, onDrawingChange },
  ref
) {
  const containerRef = useRef(null);
  const esri = useRef(null);
  const onAreaRef = useRef(onArea);
  const onDrawingRef = useRef(onDrawingChange);
  const [ready, setReady] = useState(false);

  onAreaRef.current = onArea;
  onDrawingRef.current = onDrawingChange;

  useImperativeHandle(ref, () => ({
    draw(tool) {
      const sketch = esri.current?.sketch;
      if (!sketch) return;
      sketch.cancel();
      sketch.create(tool);
      onDrawingRef.current?.(true);
    },
    cancel() {
      esri.current?.sketch.cancel();
      onDrawingRef.current?.(false);
    },
  }));

  // 1) Map, view, layers and sketch tool: created once.
  useEffect(() => {
    let destroyed = false;
    let view;

    (async () => {
      const [
        { default: Map },
        { default: MapView },
        { default: GraphicsLayer },
        { default: FeatureLayer },
        { default: Graphic },
        { default: SketchViewModel },
        webMercatorUtils,
      ] = await Promise.all([
        import("@arcgis/core/Map"),
        import("@arcgis/core/views/MapView"),
        import("@arcgis/core/layers/GraphicsLayer"),
        import("@arcgis/core/layers/FeatureLayer"),
        import("@arcgis/core/Graphic"),
        import("@arcgis/core/widgets/Sketch/SketchViewModel"),
        import("@arcgis/core/geometry/support/webMercatorUtils"),
      ]);
      if (destroyed) return;

      const area = new GraphicsLayer({ title: "Drawn area" });
      const cascade = new GraphicsLayer({ title: "Cascade" });

      view = new MapView({
        container: containerRef.current,
        // Classic Esri basemap from public tile services, no API key (same as Harbor Watch).
        map: new Map({ basemap: "gray-vector", layers: [area, cascade] }),
        center: [-77.436, 37.54],
        zoom: 9,
        spatialReference: { wkid: 3857 },
        constraints: { snapToZoom: false },
        popup: { dockEnabled: false },
      });

      const sketch = new SketchViewModel({
        view,
        layer: area,
        polygonSymbol: DRAW_SYMBOL,
        updateOnGraphicClick: false,
        defaultCreateOptions: { hasZ: false },
      });
      sketch.on("create", (event) => {
        if (event.state === "cancel") onDrawingRef.current?.(false);
        if (event.state !== "complete") return;
        onDrawingRef.current?.(false);
        const geo = webMercatorUtils.webMercatorToGeographic(event.graphic.geometry);
        const rings = geo.rings.map((ring) => ring.map(([x, y]) => [round(x), round(y)]));
        onAreaRef.current?.({ type: "Polygon", coordinates: rings });
      });

      esri.current = { view, Graphic, FeatureLayer, sketch, layers: { area, cascade } };
      setReady(true);
    })();

    return () => {
      destroyed = true;
      view?.destroy();
    };
  }, []);

  // 2) Base layers from the graph: wires and assets.
  useEffect(() => {
    if (!ready || !graph) return;
    const { view, Graphic, FeatureLayer } = esri.current;

    const wires = new FeatureLayer({
      title: "Power lines",
      source: graph.edges
        .filter((e) => e.coords?.length > 1)
        .map((e, i) => new Graphic({
          geometry: { type: "polyline", paths: [e.coords], spatialReference: { wkid: 4326 } },
          attributes: { oid: i + 1, kv: e.voltageKv ?? 0 },
        })),
      objectIdField: "oid",
      fields: [{ name: "oid", type: "oid" }, { name: "kv", type: "double" }],
      geometryType: "polyline",
      spatialReference: { wkid: 4326 },
      popupEnabled: false,
      renderer: {
        type: "simple",
        symbol: { type: "simple-line", color: [70, 82, 96, 0.55], width: 1 },
        visualVariables: [{
          type: "size", field: "kv",
          stops: [{ value: 0, size: 0.75 }, { value: 115, size: 1 }, { value: 230, size: 1.75 }, { value: 500, size: 3 }],
        }],
      },
    });

    const assets = new FeatureLayer({
      title: "Infrastructure",
      source: graph.nodes.map((n, i) => new Graphic({
        geometry: { type: "point", longitude: n.lon, latitude: n.lat },
        attributes: {
          oid: i + 1,
          id: n.id,
          name: n.name,
          sector: n.sector,
          sectorLabel: sectorLabel(n.sector),
          kindLabel: kindLabel(n.kind),
          detail: [
            n.voltageKv ? `<br/>${n.voltageKv} kV` : "",
            n.source ? `<br/>Power source: ${n.source}` : "",
            n.backup?.length ? `<br/>Backup for: ${n.backup.join(", ")}` : "",
          ].join(""),
        },
      })),
      objectIdField: "oid",
      fields: [
        { name: "oid", type: "oid" },
        ...["id", "name", "sector", "sectorLabel", "kindLabel", "detail"].map((name) => ({ name, type: "string" })),
      ],
      geometryType: "point",
      spatialReference: { wkid: 4326 },
      outFields: ["*"],
      popupTemplate: ASSET_POPUP,
      renderer: {
        type: "unique-value",
        field: "sector",
        defaultSymbol: dot("#8a97a3", 6),
        uniqueValueInfos: Object.entries(SECTORS).map(([value, s]) => ({
          value, label: s.label, symbol: dot(s.color, 6),
        })),
      },
    });

    view.map.addMany([wires, assets], 0);
    esri.current.base = { wires, assets };

    const c = graph.meta?.center;
    if (c) view.goTo({ center: [c.lon, c.lat], zoom: 10 }, { animate: false }).catch(() => {});

    return () => {
      view.map.removeMany([wires, assets]);
      wires.destroy();
      assets.destroy();
    };
  }, [ready, graph]);

  // 3) The drawn area.
  useEffect(() => {
    if (!ready) return;
    const { Graphic, layers } = esri.current;
    layers.area.removeAll();
    if (!area) return;
    layers.area.add(new Graphic({
      geometry: { type: "polygon", rings: area.coordinates, spatialReference: { wkid: 4326 } },
      symbol: DRAW_SYMBOL,
    }));
  }, [ready, area]);

  // 4) Cascade: cut wires, failure links (cause -> effect) and impacted assets.
  useEffect(() => {
    if (!ready || !graph) return;
    const { Graphic, layers, base } = esri.current;
    layers.cascade.removeAll();
    if (base) base.assets.opacity = impact ? 0.4 : 1;
    if (!impact) return;

    const nodes = new Map(graph.nodes.map((n) => [n.id, n]));
    const edges = new Map(graph.edges.map((e) => [e.id, e]));
    const wireBetween = new Map();
    for (const e of graph.edges) {
      if (e.coords?.length > 1) {
        wireBetween.set(`${e.from}>${e.to}`, e.coords);
        wireBetween.set(`${e.to}>${e.from}`, [...e.coords].reverse());
      }
    }
    const selectedSet = new Set(selected ?? []);
    const graphics = [];

    for (const id of impact.severedEdges) {
      const e = edges.get(id);
      if (!e?.coords) continue;
      graphics.push(new Graphic({
        geometry: { type: "polyline", paths: [e.coords], spatialReference: { wkid: 4326 } },
        symbol: { type: "simple-line", color: rgba(STATUS.Failed.color, 0.95), width: 3.5 },
      }));
    }

    for (const i of impact.impacts) {
      if (!i.via) continue;
      const from = nodes.get(i.via);
      const to = nodes.get(i.id);
      if (!from || !to) continue;
      const path = wireBetween.get(`${i.via}>${i.id}`) ?? [[from.lon, from.lat], [to.lon, to.lat]];
      const color = LINK_TYPES[i.viaType]?.color ?? "#8a97a3";
      graphics.push(new Graphic({
        geometry: { type: "polyline", paths: [path], spatialReference: { wkid: 4326 } },
        symbol: { type: "simple-line", color: rgba(color, 0.85), width: 1.75,
                  style: i.status === "Failed" ? "solid" : "short-dash" },
      }));
    }

    for (const i of impact.impacts) {
      const n = nodes.get(i.id);
      if (!n) continue;
      const failed = i.status === "Failed";
      const isSelected = selectedSet.has(i.id);
      graphics.push(new Graphic({
        geometry: { type: "point", longitude: n.lon, latitude: n.lat },
        attributes: {
          name: i.name, hop: i.hop, cause: i.cause,
          statusLabel: STATUS[i.status].label,
          kindLabel: kindLabel(i.kind), sectorLabel: sectorLabel(i.sector),
        },
        popupTemplate: IMPACT_POPUP,
        symbol: {
          type: "simple-marker",
          size: (i.hop === 0 ? 13 : 10) + (isSelected ? 5 : 0),
          color: failed ? rgba(sectorColor(i.sector)) : [255, 255, 255, 0.95],
          outline: {
            color: isSelected ? [15, 45, 70, 1] : rgba(STATUS[i.status].color),
            width: isSelected ? 3.5 : failed ? 2.5 : 3,
          },
        },
      }));
    }
    layers.cascade.addMany(graphics);
  }, [ready, graph, impact, selected]);

  // 5) Fly to a list selection or graph cell.
  useEffect(() => {
    if (!ready || !focus?.length || !graph) return;
    const byId = new Map(graph.nodes.map((n) => [n.id, n]));
    const pts = focus.map((id) => byId.get(id)).filter(Boolean);
    if (!pts.length) return;
    const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    const target = pts.length === 1
      ? { center: [pts[0].lon, pts[0].lat], zoom: Math.max(esri.current.view.zoom, 13) }
      : pts.map((p) => ({ type: "point", longitude: p.lon, latitude: p.lat }));
    esri.current.view.goTo(target, { animate: !reduceMotion, duration: 700 }).catch(() => {});
  }, [ready, focus, graph]);

  return <div ref={containerRef} className="map" aria-label="Map of infrastructure and the drawn area" />;
});

function dot(color, size) {
  return { type: "simple-marker", size, color: rgba(color), outline: { color: [255, 255, 255, 0.9], width: 0.75 } };
}

const round = (v) => Math.round(v * 1e6) / 1e6;

export default CascadeMap;
