import { forwardRef, useEffect, useImperativeHandle, useRef, useState } from "react";
import { LINK_TYPES, SECTORS, STATUS, kindLabel, rgba, sectorColor, sectorLabel } from "./sectors.js";
import { describeAssets } from "./assetDetails.js";

const ASSET_POPUP = {
  title: "{name}",
  content: "{kindLabel} ({sectorLabel}){detail}",
};

const IMPACT_POPUP = {
  title: "{name}",
  content: "<b>{statusLabel}</b>, hop {hop}<br/>{cause}<br/>{kindLabel} ({sectorLabel}){detail}",
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
  { graph, area, impact, selected, focus, cluster = true, onArea, onDrawingChange },
  ref
) {
  const containerRef = useRef(null);
  const esri = useRef(null);
  const onAreaRef = useRef(onArea);
  const onDrawingRef = useRef(onDrawingChange);
  const clusterRef = useRef(cluster);
  const [ready, setReady] = useState(false);
  const [basemap, setBasemap] = useState(() => {
    try { return localStorage.getItem("cascade.basemap") === "satellite" ? "satellite" : "map"; } catch { return "map"; }
  });
  const basemapRef = useRef(basemap);
  basemapRef.current = basemap;

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
        { default: Basemap },
        { default: TileLayer },
        { default: Search },
        { default: LayerSearchSource },
      ] = await Promise.all([
        import("@arcgis/core/Map"),
        import("@arcgis/core/views/MapView"),
        import("@arcgis/core/layers/GraphicsLayer"),
        import("@arcgis/core/layers/FeatureLayer"),
        import("@arcgis/core/Graphic"),
        import("@arcgis/core/widgets/Sketch/SketchViewModel"),
        import("@arcgis/core/geometry/support/webMercatorUtils"),
        import("@arcgis/core/Basemap"),
        import("@arcgis/core/layers/TileLayer"),
        import("@arcgis/core/widgets/Search"),
        import("@arcgis/core/widgets/Search/LayerSearchSource"),
      ]);
      if (destroyed) return;

      // Both basemaps come from Esri's public tile services; neither needs an API key.
      const tiles = (path, opts = {}) =>
        new TileLayer({ url: `https://services.arcgisonline.com/ArcGIS/rest/services/${path}/MapServer`, ...opts });
      const basemaps = {
        map: Basemap.fromId("gray-vector"),
        satellite: new Basemap({
          id: "satellite-labels",
          title: "Satellite",
          // Imagery faded over the view's light background, so assets and lines stay readable.
          baseLayers: [tiles("World_Imagery", { opacity: 0.5 })],
          referenceLayers: [tiles("Reference/World_Transportation"), tiles("Reference/World_Boundaries_and_Places")],
        }),
      };

      const area = new GraphicsLayer({ title: "Drawn area" });
      const cascade = new GraphicsLayer({ title: "Cascade" });

      view = new MapView({
        container: containerRef.current,
        map: new Map({ basemap: basemaps[basemapRef.current], layers: [area, cascade] }),
        center: [-77.436, 37.54],
        zoom: 9,
        spatialReference: { wkid: 3857 },
        constraints: { snapToZoom: false },
        popup: { dockEnabled: false },
        background: { color: [238, 241, 243, 1] },   // shows through the faded imagery
      });

      // Address / place search (Esri World Geocoder, no key) plus the graph's assets by name.
      const search = new Search({
        view,
        includeDefaultSources: true,
        popupEnabled: false,
        resultGraphicEnabled: true,
        allPlaceholder: "Address, place or asset",
      });
      view.ui.add(search, { position: "top-left", index: 0 });

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

      esri.current = { view, Graphic, FeatureLayer, LayerSearchSource, sketch, search, basemaps, layers: { area, cascade } };
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
          // coords run supplier -> dependent, so arrows drawn along the line show power flow
          attributes: { oid: i + 1, kv: e.voltageKv ?? 0, style: wireStyle(e) },
        })),
      objectIdField: "oid",
      fields: [{ name: "oid", type: "oid" }, { name: "kv", type: "double" }, { name: "style", type: "string" }],
      geometryType: "polyline",
      spatialReference: { wkid: 4326 },
      popupEnabled: false,
      renderer: wireRenderer(),
    });

    // Depends on / supplies / coordinates / OSM link, shared with the impact popups.
    const details = describeAssets(graph);
    esri.current.details = details;

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
          detail: details.get(n.id) ?? "",
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
      featureReduction: clusterRef.current ? CLUSTER : null,
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

    const search = esri.current.search;
    const assetSource = new esri.current.LayerSearchSource({
      layer: assets,
      name: "Infrastructure",
      placeholder: "Substation, hospital, pump...",
      searchFields: ["name"],
      displayField: "name",
      exactMatch: false,
      outFields: ["*"],
      maxSuggestions: 6,
      zoomScale: 12000,
    });
    search.sources.add(assetSource, 0);

    const c = graph.meta?.center;
    // Open on the whole study area: zoom 10 for ~30 miles, 9 for ~60, 8 for ~100+.
    const radius = graph.meta?.radiusMi ?? 30;
    const zoom = radius > 80 ? 8 : radius > 45 ? 9 : 10;
    if (c) view.goTo({ center: [c.lon, c.lat], zoom }, { animate: false }).catch(() => {});

    return () => {
      // On unmount the view (and its map) may already be destroyed by effect 1's cleanup.
      if (!view.destroyed && view.map) view.map.removeMany([wires, assets]);
      const src = esri.current?.search?.sources.find((x) => x.layer === assets);
      if (src) esri.current.search.sources.remove(src);
      if (esri.current?.base?.assets === assets) esri.current.base = null;
      wires.destroy();
      assets.destroy();
    };
  }, [ready, graph]);

  // 2a) Switch basemap.
  useEffect(() => {
    try { localStorage.setItem("cascade.basemap", basemap); } catch { /* private mode */ }
    if (!ready) return;
    const { view, basemaps } = esri.current;
    if (view.destroyed) return;
    view.map.basemap = basemaps[basemap];
  }, [ready, basemap]);

  // 2b) Turn point clustering on or off without rebuilding the layer.
  useEffect(() => {
    clusterRef.current = cluster;
    const assets = esri.current?.base?.assets;
    if (assets) assets.featureReduction = cluster ? CLUSTER : null;
  }, [ready, graph, cluster]);

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
        symbol: arrowLine({ color: rgba(color, 0.9), width: 2, dashed: i.status !== "Failed", arrowSize: 11 }),
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
          detail: esri.current.details?.get(i.id) ?? "",
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

  return (
    <>
      <div ref={containerRef} className="map" aria-label="Map of infrastructure and the drawn area" />
      <div className="basemap-switch" role="group" aria-label="Basemap">
        {[["map", "Map"], ["satellite", "Satellite"]].map(([id, label]) => (
          <button key={id} type="button" aria-pressed={basemap === id} onClick={() => setBasemap(id)}>
            {label}
          </button>
        ))}
      </div>
    </>
  );
});

/**
 * CIM line with arrowheads that follow the line's digitized direction.
 *   every: repeat an arrow every N points along the line; otherwise one arrow at mid-line.
 */
function arrowLine({ color, width, dashed = false, every = 0, arrowSize = 9 }) {
  const c = color.length === 4 ? [color[0], color[1], color[2], Math.round(color[3] * 255)] : [...color, 255];
  const stroke = { type: "CIMSolidStroke", enable: true, width, color: c, capStyle: "Round", joinStyle: "Round" };
  if (dashed) stroke.effects = [{ type: "CIMGeometricEffectDashes", dashTemplate: [5, 4], lineDashEnding: "NoConstraint" }];
  return {
    type: "cim",
    data: {
      type: "CIMSymbolReference",
      symbol: {
        type: "CIMLineSymbol",
        symbolLayers: [
          {
            type: "CIMVectorMarker",
            enable: true,
            size: arrowSize,
            anchorPointUnits: "Relative",
            frame: { xmin: -5, ymin: -5, xmax: 5, ymax: 5 },
            markerPlacement: every
              ? { type: "CIMMarkerPlacementAlongLineSameSize", angleToLine: true, placementTemplate: [every] }
              : { type: "CIMMarkerPlacementOnLine", angleToLine: true, relativeTo: "LineMiddle" },
            markerGraphics: [{
              type: "CIMMarkerGraphic",
              geometry: { rings: [[[-5, -4.5], [5, 0], [-5, 4.5], [-2.5, 0], [-5, -4.5]]] },
              symbol: { type: "CIMPolygonSymbol", symbolLayers: [{ type: "CIMSolidFill", enable: true, color: c }] },
            }],
            scaleSymbolsProportionally: true,
            respectFrame: true,
          },
          stroke,
        ],
      },
    },
  };
}

// Cluster when zoomed out (a 100-mile area is thousands of points). Each cluster takes
// the color of its most common sector; clustering turns off at neighborhood scale.
const CLUSTER = {
  type: "cluster",
  clusterRadius: "56px",
  clusterMinSize: "16px",
  clusterMaxSize: "40px",
  maxScale: 60000,
  popupTemplate: {
    title: "{cluster_count} assets",
    content: "Mostly {cluster_type_sector}. Zoom in to see each one.",
  },
  labelingInfo: [{
    deconflictionStrategy: "none",
    labelExpressionInfo: { expression: "Text($feature.cluster_count, '#,###')" },
    symbol: {
      type: "text",
      color: [255, 255, 255, 1],
      haloColor: [20, 32, 44, 0.85],
      haloSize: 1,
      font: { weight: "bold", family: "Arial Unicode MS", size: "11px" },
    },
    labelPlacement: "center-center",
  }],
};

const WIRE_COLOR = [70, 82, 96, 0.65];

/** Width by voltage band; arrows show the inferred flow direction (higher voltage -> lower,
 *  or away from a source). Links whose direction can't be inferred are dashed. */
function wireRenderer() {
  const color = WIRE_COLOR;
  return {
    type: "unique-value",
    field: "style",
    defaultSymbol: { type: "simple-line", color, width: 1, style: "dash" },
    uniqueValueInfos: Object.entries(WIRE_WIDTHS).flatMap(([band, width]) => [
      { value: `flow:${band}`, symbol: arrowLine({ color, width, every: 60, arrowSize: 6 + width * 2 }) },
      { value: `unknown:${band}`, symbol: { type: "simple-line", color, width, style: "dash" } },
    ]),
  };
}
const WIRE_WIDTHS = { low: 1, mid: 1.75, high: 2.75 };   // <200 kV, 200-344 kV, 345 kV+

function wireStyle(edge) {
  const kv = edge.voltageKv ?? 0;
  const band = kv >= 345 ? "high" : kv >= 200 ? "mid" : "low";
  return `${edge.directed === false ? "unknown" : "flow"}:${band}`;
}

function dot(color, size) {
  return { type: "simple-marker", size, color: rgba(color), outline: { color: [255, 255, 255, 0.9], width: 0.75 } };
}

const round = (v) => Math.round(v * 1e6) / 1e6;

export default CascadeMap;
