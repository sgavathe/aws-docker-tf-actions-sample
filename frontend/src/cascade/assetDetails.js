// Popup detail for every asset, built once from the graph the map already loaded:
// what it depends on, what depends on it, where it is, and a link to the OSM source.

const TYPE_WORDS = { power: "Power", water: "Water", comms: "Comms" };
const MAX_LISTED = 8;

const esc = (s) =>
  String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);

/** Great-circle distance in miles. */
export function milesBetween(a, b) {
  const rad = (d) => (d * Math.PI) / 180;
  const dLat = rad(b.lat - a.lat);
  const dLon = rad(b.lon - a.lon);
  const h = Math.sin(dLat / 2) ** 2 + Math.cos(rad(a.lat)) * Math.cos(rad(b.lat)) * Math.sin(dLon / 2) ** 2;
  return 2 * 3958.8 * Math.asin(Math.sqrt(h));
}

// Pipeline reasons end in ", 7.1 km"; the popup already shows the distance in miles.
const reason = (basis) => String(basis ?? "").replace(/,\s*\d+(?:\.\d+)?\s*km\s*$/, "");

const fmtMi = (mi) => (mi >= 10 ? `${Math.round(mi)} mi` : `${mi.toFixed(1)} mi`);

/** "w123" -> https://www.openstreetmap.org/way/123 */
export function osmUrl(osm) {
  const m = /^([nwr])(\d+)$/.exec(osm ?? "");
  if (!m) return null;
  return `https://www.openstreetmap.org/${{ n: "node", w: "way", r: "relation" }[m[1]]}/${m[2]}`;
}

/** Map of node id -> HTML snippet appended to the popup ("<br/>..." sections). */
export function describeAssets(graph) {
  const byId = new Map(graph.nodes.map((n) => [n.id, n]));
  const feeds = new Map();     // id -> edges into it (it depends on e.from)
  const supplies = new Map();  // id -> edges out of it (e.to depends on it)
  const wired = new Map();     // id -> undirected grid links (direction unknown)
  const add = (m, k, v) => (m.get(k) ?? m.set(k, []).get(k)).push(v);

  for (const e of graph.edges) {
    if (!byId.has(e.from) || !byId.has(e.to)) continue;
    if (e.directed === false) {
      add(wired, e.from, { other: e.to, e });
      add(wired, e.to, { other: e.from, e });
    } else {
      add(feeds, e.to, e);
      add(supplies, e.from, e);
    }
  }

  const sample = Boolean(graph.meta?.sample);
  const out = new Map();

  for (const n of graph.nodes) {
    const parts = [];

    const facts = [
      n.voltageKv ? `${n.voltageKv} kV` : "",
      n.outputMw ? `${n.outputMw} MW` : "",
    ].filter(Boolean);
    if (facts.length) parts.push(facts.join(" · "));
    if (n.source) parts.push(`<b>Power source:</b> ${esc(n.source)}`);
    if (n.backup?.length) parts.push(`<b>Has backup for:</b> ${esc(n.backup.join(", "))}`);

    // What it depends on
    const ins = (feeds.get(n.id) ?? [])
      .map((e) => ({ e, src: byId.get(e.from) }))
      .sort((a, b) => a.e.type.localeCompare(b.e.type) || milesBetween(n, a.src) - milesBetween(n, b.src));
    const wires = wired.get(n.id) ?? [];
    if (ins.length || wires.length) {
      const lines = ins.slice(0, MAX_LISTED).map(({ e, src }) =>
        `${TYPE_WORDS[e.type] ?? esc(e.type)} from <b>${esc(src.name)}</b> (${fmtMi(milesBetween(n, src))})` +
        `<br/><span style="opacity:.7">&nbsp;&nbsp;${esc(reason(e.basis))}</span>`);
      for (const { other } of wires.slice(0, Math.max(0, MAX_LISTED - lines.length))) {
        const o = byId.get(other);
        lines.push(`Power line to <b>${esc(o.name)}</b> (${fmtMi(milesBetween(n, o))}), flow direction unknown`);
      }
      const more = ins.length + wires.length - lines.length;
      parts.push(`<b>Depends on</b><br/>${lines.join("<br/>")}${more > 0 ? `<br/>…and ${more} more` : ""}`);
    } else if (n.sector !== "energy" || !n.source) {
      parts.push(`<b>Depends on:</b> nothing found within range`);
    }

    // What depends on it
    const outs = supplies.get(n.id) ?? [];
    if (outs.length) {
      const dependents = new Set(outs.map((e) => e.to));
      const byType = {};
      for (const e of outs) byType[e.type] = (byType[e.type] ?? 0) + 1;
      const breakdown = Object.entries(byType)
        .sort((a, b) => b[1] - a[1])
        .map(([t, c]) => `${c} ${(TYPE_WORDS[t] ?? t).toLowerCase()}`)
        .join(", ");
      parts.push(`<b>Supplies ${dependents.size} asset${dependents.size === 1 ? "" : "s"}:</b> ${breakdown}`);
    }

    parts.push(`${n.lat.toFixed(5)}, ${n.lon.toFixed(5)}`);
    const url = sample ? null : osmUrl(n.osm);
    if (url) parts.push(`<a href="${url}" target="_blank" rel="noopener noreferrer">View on OpenStreetMap</a>`);
    else if (sample) parts.push(`<span style="opacity:.7">Synthetic sample asset</span>`);

    out.set(n.id, parts.map((p) => `<br/>${p}`).join(""));
  }
  return out;
}
