"""Render a self-contained web map (no internet: MapLibre is served from /vendor by map-viewer)."""

from __future__ import annotations

import html
import json
import os
import re

PALETTE = ["#d62728", "#1f77b4", "#2ca02c", "#9467bd", "#ff7f0e", "#17becf", "#8c564b"]


def _bounds(layers: list[dict]) -> list[float] | None:
    xs, ys = [], []

    def walk(c):
        if isinstance(c, (list, tuple)) and c and isinstance(c[0], (int, float)):
            xs.append(c[0])
            ys.append(c[1])
        elif isinstance(c, (list, tuple)):
            for x in c:
                walk(x)

    for lyr in layers:
        for f in lyr["geojson"].get("features", []):
            if f.get("geometry"):
                walk(f["geometry"]["coordinates"])
    if not xs:
        return None
    return [min(xs), min(ys), max(xs), max(ys)]


def data_source() -> str:
    """Footer wording for where the analysis data lives (server config, never user input)."""
    label = os.environ.get("DATA_SOURCE_LABEL") or (
        "Aurora PostgreSQL in AWS" if os.environ.get("DB_IAM_AUTH") == "1" else "the enclave PostGIS")
    return re.sub(r"[^\w ().,/-]", "", label)[:80]


def render_map(title: str, layers: list[dict], basemap: str = "offline", subtitle: str = "") -> str:
    """basemap: offline (local PMTiles if present, else blank) | osm (offline first, else online
    OpenStreetMap tiles) | none."""
    for i, lyr in enumerate(layers):
        lyr.setdefault("color", PALETTE[i % len(PALETTE)])
    geocode = os.environ.get("MAP_GEOCODE_PATH", "")
    geocode = geocode if re.fullmatch(r"/[\w/-]{1,60}", geocode) else ""   # same-origin path only
    payload = json.dumps({"layers": layers, "bounds": _bounds(layers), "basemap": basemap, "geocode": geocode})
    def swatch(l):
        if l.get("kind") == "heatmap":
            return '<span style="width:28px;border-radius:3px;background:linear-gradient(90deg,#fde68a,#f97316,#b91c1c)"></span>'
        if l.get("ramp"):
            stops = ",".join(html.escape(c) for _, c in l["ramp"])
            return f'<span style="width:28px;border-radius:3px;background:linear-gradient(90deg,{stops})"></span>'
        feats = l["geojson"].get("features", [])
        is_line = bool(feats) and "Line" in (feats[0].get("geometry") or {}).get("type", "")
        shape = "width:16px;height:3px;border-radius:0" if is_line else ""
        return f'<span style="background:{html.escape(l["color"])};{shape}"></span>'

    legend = "".join(
        f'<div>{swatch(l)}{html.escape(l["name"])}'
        + (f' ({len(l["geojson"].get("features", []))})' if not l.get("legend_note") else "")
        + (f'<div class="note" style="margin:0 0 4px 18px">{html.escape(l["legend_note"])}</div>' if l.get("legend_note") else "")
        + '</div>'
        for l in layers
    )
    return _render(title, layers, payload, legend, subtitle).replace("the enclave PostGIS", data_source())


def _render(title, layers, payload, legend, subtitle) -> str:
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(title)}</title>
<link rel="stylesheet" href="/vendor/maplibre-gl.css">
<script src="/vendor/maplibre-gl.js"></script>
<script src="/vendor/pmtiles.js"></script>
<script src="/vendor/basemaps.js"></script>
<style>
 html,body,#map{{margin:0;height:100%;font:14px system-ui,sans-serif}}
 #panel{{position:absolute;top:12px;left:12px;background:#fff;padding:10px 14px;border-radius:8px;
   box-shadow:0 1px 6px rgba(0,0,0,.25);max-width:320px}}
 #panel h1{{font-size:16px;margin:0 0 6px}}
 #panel span{{display:inline-block;width:12px;height:12px;border-radius:50%;margin-right:6px;vertical-align:middle}}
 .note{{color:#666;font-size:12px;margin-top:6px}}
 #search{{position:absolute;top:12px;right:52px;width:280px;background:#fff;border-radius:8px;
   box-shadow:0 1px 6px rgba(0,0,0,.25);font-size:13px}}
 #search input{{box-sizing:border-box;width:100%;border:0;padding:9px 12px;border-radius:8px;font:inherit}}
 #search ul{{list-style:none;margin:0;padding:0;border-top:1px solid #e5e7eb}}
 #search li{{padding:7px 12px;cursor:pointer}} #search li:hover{{background:#f3f4f6}}
 #search .msg{{padding:7px 12px;color:#666}}
 @media (max-width:760px){{#search{{top:auto;right:auto;bottom:30px;left:12px;width:min(280px,calc(100% - 24px))}}
   #panel{{max-width:calc(100% - 70px)}}}}
</style></head>
<body><div id="map"></div>
<div id="panel"><h1>{html.escape(title)}</h1>{('<div class="note" style="margin:-4px 0 8px;font-weight:600">' + html.escape(subtitle) + "</div>") if subtitle else ""}{legend}
<div class="note" id="basenote">Loading basemap…</div></div>
<script>
const DATA = {payload};
function circleRadius(l) {{
  if (!l.size_field) return l.radius || 7;
  const v = l.geojson.features.map(f => Number(f.properties[l.size_field]) || 0);
  const lo = Math.min(...v), hi = Math.max(...v);
  if (!v.length || hi === lo) return 14;
  return ['interpolate', ['linear'], ['to-number', ['get', l.size_field]], lo, 9, hi, 26];
}}
const BLANK = {{version: 8, sources: {{}}, layers: [{{id: 'bg', type: 'background', paint: {{'background-color': '#eef1f4'}}}}]}};
async function baseStyle() {{
  if (DATA.basemap !== 'none' && window.pmtiles && window.basemaps) {{
    try {{
      const head = await fetch('/basemap/region.pmtiles', {{method: 'HEAD'}});
      if (head.ok) {{
        const protocol = new pmtiles.Protocol();
        maplibregl.addProtocol('pmtiles', protocol.tile);
        const ok = async (u) => {{ try {{ return (await fetch(u, {{method: 'HEAD'}})).ok; }} catch (e) {{ return false; }} }};
        const hasFonts = await ok('/basemap/fonts/Noto%20Sans%20Regular/0-255.pbf');
        const hasSprite = await ok('/basemap/sprites/v4/light.json');
        let layers = basemaps.layers('protomaps', basemaps.namedFlavor('light'), {{lang: 'en'}});
        if (!hasFonts) layers = layers.filter(l => l.type !== 'symbol');  // no glyphs: keep the map, drop labels
        const style = {{
          version: 8,
          sources: {{protomaps: {{type: 'vector', url: 'pmtiles://' + location.origin + '/basemap/region.pmtiles',
            attribution: '&copy; OpenStreetMap contributors, Protomaps'}}}},
          layers: layers
        }};
        if (hasFonts) style.glyphs = location.origin + '/basemap/fonts/{{fontstack}}/{{range}}.pbf';
        if (hasSprite) style.sprite = location.origin + '/basemap/sprites/v4/light';
        return [style, 'Offline basemap: OpenStreetMap data served from the enclave' + (hasFonts ? '' : ' (no labels)') +
          '. All analysis data from the enclave PostGIS.'];
      }}
    }} catch (e) {{ /* fall through */ }}
  }}
  if (DATA.basemap === 'osm') {{
    return [{{
      version: 8,
      sources: {{osm: {{type: 'raster', tiles: ['https://tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png'], tileSize: 256, maxzoom: 19,
        attribution: '&copy; OpenStreetMap contributors'}}}},
      layers: [{{id: 'osm', type: 'raster', source: 'osm', paint: {{'raster-saturation': -0.6, 'raster-opacity': 0.85}}}}]
    }}, 'Online basemap: OpenStreetMap tiles (connected demo only). Analysis data from the enclave PostGIS.'];
  }}
  return [BLANK, 'No basemap: all data from the enclave PostGIS.'];
}}
// Address search: same-origin POST to the chat app (signed-in users only), which asks the
// geocoder server-side. Results are inserted as text nodes, never HTML.
function addressSearch(map) {{
  const box = document.createElement('div'); box.id = 'search';
  const form = document.createElement('form');
  const input = document.createElement('input');
  input.type = 'search'; input.placeholder = 'Search an address or place'; input.maxLength = 200;
  input.setAttribute('aria-label', 'Search an address or place');
  const list = document.createElement('ul');
  form.append(input); box.append(form, list); document.body.append(box);
  let marker = null;
  const show = (r) => {{
    list.replaceChildren();
    if (marker) marker.remove();
    marker = new maplibregl.Marker({{color: '#111827'}}).setLngLat([r.lon, r.lat])
      .setPopup(new maplibregl.Popup({{offset: 24}}).setText(r.label)).addTo(map);
    marker.togglePopup();
    map.flyTo({{center: [r.lon, r.lat], zoom: Math.max(map.getZoom(), 16)}});
  }};
  form.addEventListener('submit', async (e) => {{
    e.preventDefault();
    const q = input.value.trim();
    if (q.length < 2) return;
    const c = map.getCenter();
    const body = JSON.stringify({{q: q, lon: c.lng, lat: c.lat}});
    const hash = Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256', new TextEncoder().encode(body))))
      .map(b => b.toString(16).padStart(2, '0')).join('');
    list.replaceChildren(Object.assign(document.createElement('li'), {{className: 'msg', textContent: 'Searching…'}}));
    try {{
      const r = await fetch(DATA.geocode, {{method: 'POST', credentials: 'same-origin', body: body,
        headers: {{'Content-Type': 'application/json', 'x-amz-content-sha256': hash}}}});
      const data = r.ok ? await r.json() : {{results: []}};
      list.replaceChildren();
      if (!data.results.length) {{
        list.append(Object.assign(document.createElement('li'), {{className: 'msg',
          textContent: r.status === 401 ? 'Sign in again to search'
            : r.status === 429 ? "Today's demo budget is used up" : 'No match'}}));
        return;
      }}
      if (data.results.length === 1) return show(data.results[0]);
      data.results.forEach((res) => {{
        const li = document.createElement('li'); li.textContent = res.label;
        li.addEventListener('click', () => show(res)); list.append(li);
      }});
    }} catch (err) {{
      list.replaceChildren(Object.assign(document.createElement('li'), {{className: 'msg', textContent: 'Search failed'}}));
    }}
  }});
}}
(async () => {{
const [style, note] = await baseStyle();
document.getElementById('basenote').textContent = note;
const map = new maplibregl.Map({{
  container: 'map',
  style: style,
  center: [-77.45, 37.55], zoom: 10
}});
map.addControl(new maplibregl.NavigationControl());
if (DATA.geocode) addressSearch(map);
map.on('load', () => {{
  const popup = (layerId) => map.on('click', layerId, e => {{
    const p = e.features[0].properties;
    const box = document.createElement('div');
    Object.entries(p).forEach(([k, v]) => {{
      const row = document.createElement('div');
      const b = document.createElement('b'); b.textContent = k + ': ';
      row.append(b, String(v)); box.append(row);
    }});
    new maplibregl.Popup().setLngLat(e.lngLat).setDOMContent(box).addTo(map);
  }});
  DATA.layers.forEach((l, i) => {{
    const id = 'l' + i;
    map.addSource(id, {{type: 'geojson', data: l.geojson}});
    if (l.kind === 'heatmap') {{
      // Weighted heat from pre-aggregated cells. Weight is scaled so the 98th-percentile cell = 1, and
      // intensity is divided by how many cells overlap one kernel at each zoom, so dense statewide
      // grids don't saturate into a solid block.
      const w = l.weight_field;
      const vals = l.geojson.features.map(f => Number(f.properties[w]) || 0).sort((a, b) => a - b);
      const top = Math.max(1, vals[Math.floor(vals.length * 0.98)] || 1);
      const lat = DATA.bounds ? (DATA.bounds[1] + DATA.bounds[3]) / 2 : 37.5;
      const meanW = vals.reduce((a, v) => a + Math.min(1.5, v / top), 0) / Math.max(1, vals.length);
      const R = 18, cellM = l.cell_m || 200, stops = [];
      // share of the extent's cells that hold crashes (sqrt: crashes bunch along roads, so local
      // occupancy is higher than the global share); keeps sparse layers from washing out
      const b = DATA.bounds || [0, 0, 1, 1];
      const extentCells = Math.max(1, ((b[2] - b[0]) * 111320 * Math.cos(lat * Math.PI / 180) / cellM) * ((b[3] - b[1]) * 110574 / cellM));
      const occ = Math.sqrt(Math.min(1, vals.length / extentCells));
      for (let z = 4; z <= 16; z += 2) {{
        // cells under one kernel x average kernel value (~0.22) x average weight -> aim for ~0.9 density
        const px = cellM / (156543.03 * Math.cos(lat * Math.PI / 180) / 2 ** z);
        const under = Math.max(1, Math.PI * R * R / (px * px) * occ);
        stops.push(z, Math.min(3, 0.9 / (under * 0.22 * Math.max(meanW, 0.02))));
      }}
      map.addLayer({{id: id + '-heat', type: 'heatmap', source: id, paint: {{
        'heatmap-weight': ['min', 1.5, ['/', ['to-number', ['get', w]], top]],
        'heatmap-intensity': ['interpolate', ['linear'], ['zoom'], ...stops],
        'heatmap-radius': R,
        'heatmap-opacity': 0.8,
        'heatmap-color': ['interpolate', ['linear'], ['heatmap-density'], 0, 'rgba(253,230,138,0)',
          0.2, '#fde68a', 0.45, '#fdba74', 0.7, '#f97316', 1, '#b91c1c']}}}});
      return;
    }}
    const fill = l.ramp && l.color_field
      ? ['interpolate', ['linear'], ['to-number', ['get', l.color_field]], ...l.ramp.flat()]
      : l.color;
    map.addLayer({{id: id + '-fill', type: 'fill', source: id, filter: ['==', '$type', 'Polygon'],
      paint: {{'fill-color': fill, 'fill-opacity': l.ramp ? 0.7 : 0.15, 'fill-outline-color': l.ramp ? 'rgba(255,255,255,0.4)' : l.color}}}});
    if (l.lines) {{
      // network links: color and width from a 0..1 field (through-trips or detour increase)
      const wf = l.width_field;
      map.addLayer({{id: id + '-line', type: 'line', source: id, filter: ['==', '$type', 'LineString'],
        layout: {{'line-cap': 'round'}},
        paint: {{'line-color': fill, 'line-opacity': 0.85,
                 'line-width': wf ? ['interpolate', ['linear'], ['to-number', ['get', wf]], 0, 1, 1, 8] : 2.5}}}});
      popup(id + '-line');
    }} else if (!l.ramp) map.addLayer({{id: id + '-line', type: 'line', source: id,
      paint: {{'line-color': l.color, 'line-width': 2.5, 'line-opacity': 0.8}}}});
    map.addLayer({{id: id + '-pt', type: 'circle', source: id, filter: ['==', '$type', 'Point'],
      paint: {{'circle-color': fill, 'circle-radius': circleRadius(l), 'circle-opacity': l.size_field ? 0.6 : 1,
               'circle-stroke-color': '#fff', 'circle-stroke-width': l.radius && l.radius < 6 ? 0.5 : 1.5}}}});
    popup(id + '-pt');
    if (l.ramp) popup(id + '-fill');
  }});
  if (DATA.bounds) map.fitBounds([[DATA.bounds[0], DATA.bounds[1]], [DATA.bounds[2], DATA.bounds[3]]], {{padding: 60}});
}});
}})();
</script></body></html>
"""
