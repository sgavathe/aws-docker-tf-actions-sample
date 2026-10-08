// Popup detail for every asset, built once from the graph the map already loaded:
// what it depends on, what depends on it, where it is, and a link to the OSM source.

const TYPE_WORDS = { power: "Power", water: "Water", comms: "Comms" };
const MAX_LISTED = 8;

export const esc = (s) =>
  String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);

/** Great-circle distance in miles. */
export function milesBetween(a, b) {
  const rad = (d) => (d * Math.PI) / 180;
  const dLat = rad(b.lat - a.lat);
  const dLon = rad(b.lon - a.lon);
  const h = Math.sin(dLat / 2) ** 2 + Math.cos(rad(a.lat)) * Math.cos(rad(b.lat)) * Math.sin(dLon / 2) ** 2;
  return 2 * 3958.8 * Math.asin(Math.sqrt(h));
}

// Pipeline reasons are written in metric ("nearest substation, 7.1 km", "... 450 m").
// The popup already shows the distance in miles, so a trailing distance is dropped and
// any other metric distance is converted to miles.
const KM_PER_MI = 1.609344;
const toMiles = (km) => {
  const mi = km / KM_PER_MI;
  return mi >= 10 ? `${Math.round(mi)} mi` : `${mi.toFixed(1)} mi`;
};
const reason = (basis) =>
  String(basis ?? "")
    .replace(/,\s*\d+(?:\.\d+)?\s*(?:km|m)\s*$/i, "")
    .replace(/(\d+(?:\.\d+)?)\s*km\b/gi, (_, n) => toMiles(Number(n)))
    .replace(/(\d+(?:\.\d+)?)\s*m\b/g, (_, n) => toMiles(Number(n) / 1000));

const fmtMi = (mi) => (mi >= 10 ? `${Math.round(mi)} mi` : `${mi.toFixed(1)} mi`);

/** "123 Main St, Richmond, VA 23219" from addr:* tags, or "" */
function addressOf(t) {
  const street = [t["addr:housenumber"], t["addr:street"]].filter(Boolean).join(" ");
  const place = [t["addr:city"], [t["addr:state"], t["addr:postcode"]].filter(Boolean).join(" ")].filter(Boolean).join(", ");
  return [street, place].filter(Boolean).join(", ");
}

/** Tag value as HTML; website-like values become links. */
function linkify(key, value) {
  if (/^(website|url|contact:website)$/.test(key) && /^https?:\/\//i.test(value)) {
    return `<a href="${esc(value)}" target="_blank" rel="noopener noreferrer">${esc(value)}</a>`;
  }
  if (key === "wikipedia" && /^[a-z-]+:.+/.test(value)) {
    const [lang, title] = [value.slice(0, value.indexOf(":")), value.slice(value.indexOf(":") + 1)];
    return `<a href="https://${esc(lang)}.wikipedia.org/wiki/${encodeURIComponent(title.replaceAll(" ", "_"))}" target="_blank" rel="noopener noreferrer">${esc(value)}</a>`;
  }
  return esc(value);
}

/** "w123" -> https://www.openstreetmap.org/way/123 */
export function osmUrl(osm) {
  const m = /^([nwr])(\d+)$/.exec(osm ?? "");
  if (!m) return null;
  return `https://www.openstreetmap.org/${{ n: "node", w: "way", r: "relation" }[m[1]]}/${m[2]}`;
}

/**
 * Popup detail for each asset ("<br/>..." sections), built on first use and cached:
 * a state-wide graph has ~15k assets, most of which are never clicked.
 *   const details = describeAssets(graph); details.get(nodeId) -> HTML string
 */
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
  const cache = new Map();

  function render(n) {
    const parts = [];

    const facts = [
      n.voltageKv ? `${n.voltageKv} kV` : "",
      n.outputMw ? `${n.outputMw} MW` : "",
    ].filter(Boolean);
    if (facts.length) parts.push(facts.join(" · "));
    const tags = n.tags ?? {};
    const operator = tags.operator || tags.owner;
    if (operator) parts.push(`<b>Operator:</b> ${esc(operator)}`);
    const fuel = tags["plant:source"] || tags["generator:source"];
    if (fuel) parts.push(`<b>Fuel:</b> ${esc(fuel.replaceAll(";", ", "))}`);
    if (tags.substation) parts.push(`<b>Substation type:</b> ${esc(tags.substation)}`);
    const address = addressOf(tags);
    if (address) parts.push(`<b>Address:</b> ${esc(address)}`);
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

    const tagRows = Object.entries(tags);
    if (tagRows.length) {
      parts.push(
        `<details><summary>All OpenStreetMap tags (${tagRows.length})</summary>` +
        `<table style="font-size:12px;border-collapse:collapse">` +
        tagRows.map(([k, v]) =>
          `<tr><td style="opacity:.7;padding:1px 8px 1px 0;vertical-align:top">${esc(k)}</td>` +
          `<td style="padding:1px 0;word-break:break-word">${linkify(k, v)}</td></tr>`).join("") +
        `</table></details>`);
    }

    return parts.map((p) => `<br/>${p}`).join("");
  }

  return {
    get(id) {
      if (!cache.has(id)) {
        const n = byId.get(id);
        cache.set(id, n ? render(n) : "");
      }
      return cache.get(id);
    },
  };
}
