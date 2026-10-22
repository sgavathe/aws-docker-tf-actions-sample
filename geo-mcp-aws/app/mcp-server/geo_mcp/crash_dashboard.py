"""Crash safety dashboard page: map + hot spots + network-critical intersections + factors +
time patterns + recommendations, from real crash records. Static HTML and inline SVG charts
(no scripts of its own); the interactive map is the regular generated map page, framed.
Every value is escaped; nothing from the data is ever inserted as markup.
"""

from __future__ import annotations

import html
import re
from urllib.parse import quote

E = html.escape
DOW = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

# FHWA Proven Safety Countermeasures matched to what the numbers show. Each rule fires only when
# the county's share is meaningfully above the statewide share for the same years (or, for the
# intersection rule, when hot spots exist), and the card cites the numbers that triggered it.
RULES = [
    ("angle", "Angle crashes at intersections",
     "Roundabouts; dedicated left-turn lanes; backplates with retroreflective borders on signals"),
    ("rear end", "Rear-end crashes",
     "Yellow change interval and signal timing review; corridor access management"),
    ("pedestrian", "Pedestrian crashes",
     "Crosswalk visibility enhancements; pedestrian refuge islands; RRFB or pedestrian hybrid beacons; leading pedestrian intervals"),
    ("bike", "Bicycle crashes", "Bicycle lanes; reduced conflict at intersections"),
    ("dark_unlit", "Crashes in darkness on unlit roads", "Lighting at intersections and curves"),
    ("speed", "Speed-related crashes", "Appropriate speed limits for all road users; speed safety cameras"),
    ("road_departure", "Road departure (fixed object / off road)",
     "Enhanced delineation for curves; longitudinal rumble strips; SafetyEdge; wider edge lines"),
    ("alcohol", "Alcohol-related crashes", "High-visibility enforcement (behavioral, with engineering above)"),
    ("deer", "Deer and animal crashes", "Wildlife warning signs and fencing on the worst corridors"),
]


def recommendations(factors: dict[str, dict], hotspots: list[dict]) -> list[dict]:
    """factors: key -> {label, n, share, state_share} (shares 0..1)."""
    out = []
    for key, title, measures in RULES:
        f = factors.get(key)
        if not f or f["n"] < 5:
            continue
        here, state = f["share"], f.get("state_share") or 0
        if state and here >= state * 1.15 and here - state >= 0.01:
            out.append({"title": title, "measures": measures,
                        "why": f"{f['n']:,} crashes, {here:.1%} of all here vs {state:.1%} statewide"})
    if hotspots:
        h = hotspots[0]
        out.append({"title": "Stop-controlled and signalized hot spots",
                    "measures": "Systemic low-cost countermeasures at stop-controlled intersections; road safety audit at the top hot spots",
                    "why": f"top intersection: {h['location']} with {h['crashes']:,} crashes"})
    return out


def _hbars(rows: list[dict], value="share", ref="state_share", fmt="{:.0%}", color="#2563eb") -> str:
    """Horizontal bars (county) with a tick for the statewide share when given."""
    if not rows:
        return '<p class="muted">No data</p>'
    top = max(max(r[value], r.get(ref) or 0) for r in rows) or 1
    h, out = 26, []
    for i, r in enumerate(rows):
        y, w = i * h, 230 * r[value] / top
        out.append(f'<text x="0" y="{y + 15}" class="lbl">{E(str(r["label"]))[:34]}</text>'
                   f'<rect x="200" y="{y + 4}" width="{w:.1f}" height="14" rx="3" fill="{color}"/>'
                   f'<text x="{205 + w:.1f}" y="{y + 15}" class="val">{E(fmt.format(r[value]))}</text>')
        if r.get(ref):
            x = 200 + 230 * r[ref] / top
            out.append(f'<line x1="{x:.1f}" x2="{x:.1f}" y1="{y + 1}" y2="{y + 21}" stroke="#111827" stroke-width="2">'
                       f'<title>statewide {E(fmt.format(r[ref]))}</title></line>')
    return f'<svg viewBox="0 0 520 {len(rows) * h}" role="img">{"".join(out)}</svg>'


def _columns(values: list[int], labels: list[str], color="#f97316", every=1) -> str:
    top = max(values, default=0) or 1
    n, w = len(values), 520 / max(1, len(values))
    out = []
    for i, v in enumerate(values):
        hgt = 120 * v / top
        out.append(f'<rect x="{i * w + 2:.1f}" y="{130 - hgt:.1f}" width="{w - 4:.1f}" height="{hgt:.1f}" rx="2" fill="{color}">'
                   f'<title>{E(labels[i])}: {v:,}</title></rect>')
        if i % every == 0:
            out.append(f'<text x="{i * w + w / 2:.1f}" y="146" class="tick">{E(labels[i])}</text>')
    return f'<svg viewBox="0 0 520 150" role="img">{"".join(out)}</svg>'


def zoom_link(d: dict, row: dict, text: str) -> str:
    """Link that flies the framed map to the row's intersection (target = the map iframe)."""
    try:
        lon, lat = float(row["lon"]), float(row["lat"])
    except (KeyError, TypeError, ValueError):
        return E(text)
    if not (-180 <= lon <= 180 and -90 <= lat <= 90):
        return E(text)
    href = f'{d["map_file"]}#zoom={lon:.5f},{lat:.5f},17&label={quote(str(text)[:120], safe="")}'
    return f'<a href="{E(href)}" target="dashmap" title="Zoom the map here">{E(text)}</a>'


def render_dashboard(d: dict) -> str:
    k = d["kpis"]
    tiles = [("Crashes", f"{k['crashes']:,}"), ("Fatal (K)", f"{k['k']:,}"), ("Serious (A)", f"{k['a']:,}"),
             ("People injured", f"{k.get('injured') or 0:,}"),
             ("At intersections", f"{k['at_intersections'] / max(1, k['crashes']):.0%}")]
    hot = "".join(
        f'<li><b>{i + 1}. {zoom_link(d, h, str(h["location"]))}</b><span>{h["crashes"]:,} crashes · {h.get("k", 0)} K · {h.get("a", 0)} A</span></li>'
        for i, h in enumerate(d["hotspots"][:3])) or '<li class="muted">No intersection hot spots</li>'
    net = d.get("network") or {}
    crit = "".join(
        f'<li><b>{i + 1}. {zoom_link(d, c, str(c["location"])[:80])}</b><span>{c["crashes"]:,} crashes · through-traffic rank '
        f'{c["betweenness"]:.2f} · score {c["critical_and_dangerous"]:.2f}</span></li>'
        for i, c in enumerate(net.get("top", [])[:5])) or '<li class="muted">Too few connected intersections</li>'
    pats = "".join(f"<li>{E(p)}</li>" for p in d["patterns"])
    recs = "".join(f'<li><b>{E(r["title"])}</b><div>{E(r["measures"])}</div><span>{E(r["why"])}</span></li>'
                   for r in d["recommendations"]) or '<li class="muted">No factor stands out against the statewide shares</li>'
    factors = _hbars(d["factor_rows"])
    coll = _hbars([{"label": re.sub(r"^\s*\d+\.\s*", "", c["label"]), "share": c["share"], "state_share": c.get("state_share")}
                   for c in d["collisions"]], color="#7c3aed")
    hours = _columns(d["by_hour"], [f"{h}" for h in range(24)], every=3)
    days = _columns(d["by_dow"], DOW, color="#0891b2")
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{E(d["title"])}</title>
<style>
 :root{{--ink:#111827;--muted:#6b7280;--line:#e5e7eb;--bg:#f6f7f9;--card:#fff}}
 *{{box-sizing:border-box}} body{{margin:0;font:14px/1.45 system-ui,sans-serif;color:var(--ink);background:var(--bg)}}
 header{{padding:16px 20px 8px}} h1{{font-size:20px;margin:0}} .scope{{color:var(--muted);margin-top:2px}}
 .tiles{{display:grid;grid-template-columns:repeat(5,1fr);gap:10px;padding:8px 20px}}
 .tile{{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:10px 12px}}
 .tile b{{display:block;font-size:22px}} .tile span{{color:var(--muted);font-size:12px}}
 .main{{display:grid;grid-template-columns:minmax(0,1.6fr) minmax(0,1fr);gap:12px;padding:8px 20px}}
 .grid3{{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px;padding:8px 20px}}
 .grid2{{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px;padding:8px 20px 20px}}
 .card{{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px 14px;min-width:0}}
 .card h2{{font-size:14px;margin:0 0 8px}}
 li b a{{color:inherit;text-decoration:none;border-bottom:1px dashed #9ca3af}} li b a:hover{{color:#1d4ed8;border-color:#1d4ed8}}
 li b a::after{{content:" \\2197";color:#6b7280;font-weight:400}} iframe{{width:100%;height:560px;border:0;border-radius:8px}}
 ol,ul{{margin:0;padding-left:0;list-style:none}} li{{padding:6px 0;border-top:1px solid var(--line)}} li:first-child{{border-top:0}}
 li span{{display:block;color:var(--muted);font-size:12px}} li div{{font-size:13px}}
 .muted{{color:var(--muted)}} svg{{width:100%;height:auto}} .lbl,.val,.tick{{font-size:11px;fill:#374151}}
 .tick{{text-anchor:middle}} .note{{color:var(--muted);font-size:12px;margin-top:6px}}
 @media (max-width:900px){{.tiles{{grid-template-columns:repeat(2,1fr)}}.main,.grid3,.grid2{{grid-template-columns:1fr}}}}
 header{{display:flex;justify-content:space-between;align-items:flex-start;gap:16px;flex-wrap:wrap}}
 .toolbar{{display:flex;gap:8px}} .toolbar button{{font:600 13px system-ui,sans-serif;padding:7px 12px;border-radius:8px;cursor:pointer;
   border:1px solid #c8d6e8;background:#fff;color:#1f4f86}} .toolbar button:hover{{background:#e4edf7}}
 .print-map{{display:none;width:100%;border-radius:8px}}
 @page{{size:letter landscape;margin:9mm}}
 @media print{{
   *{{-webkit-print-color-adjust:exact;print-color-adjust:exact}}
   body{{background:#fff;font-size:11px}} header{{padding:0 0 6px}} h1{{font-size:18px}}
   .toolbar,.screen-only{{display:none!important}} iframe{{display:none!important}} .print-map{{display:block}}
   .tiles,.main,.grid3,.grid2{{padding:4px 0}} .card{{break-inside:avoid;page-break-inside:avoid}}
   .main{{grid-template-columns:minmax(0,1.6fr) minmax(0,1fr)}} .grid3{{grid-template-columns:repeat(3,minmax(0,1fr))}}
   .grid2{{grid-template-columns:repeat(2,minmax(0,1fr))}} .tiles{{grid-template-columns:repeat(5,1fr)}}
   li b a{{border:0;color:inherit}} li b a::after{{content:""}}
 }}
</style></head><body>
<header><div><h1>{E(d["title"])}</h1><div class="scope">{E(d["scope"])}</div></div>
 <div class="toolbar"><button type="button" id="dlpng">Download map (PNG)</button>
 <button type="button" id="printpdf">Print / Save as PDF</button></div></header>
<section class="tiles">{"".join(f'<div class="tile"><b>{E(v)}</b><span>{E(t)}</span></div>' for t, v in tiles)}</section>
<section class="main">
 <div class="card"><h2>Crashes and road network</h2><iframe name="dashmap" src="{E(d["map_file"])}" title="Crash map"></iframe><img class="print-map" alt="Crash map">
  <div class="note">Points by severity; lines are road links between crash-coded intersections, thicker and redder where
  more shortest trips pass (schematic straight lines).<span class="screen-only"> Use the search box to find an address.</span></div></div>
 <div>
  <div class="card"><h2>Accident hot spots (intersections) <span class="muted" style="font-weight:400">· click to zoom</span></h2><ul>{hot}</ul></div>
  <div class="card" style="margin-top:12px"><h2>Critical and dangerous intersections <span class="muted" style="font-weight:400">· click to zoom</span></h2><ul>{crit}</ul>
   <div class="note">Network of {net.get("nodes", 0):,} intersections and {net.get("links", 0):,} links built from VDOT
   node and milepost data. Through-traffic rank = share of shortest trips between intersections that pass through
   (betweenness, from {net.get("sampled_origins", 0):,} origins); score combines it with crash severity (EPDO).
   Ask "what if node … is closed" for the detour cascade.</div></div>
 </div>
</section>
<section class="grid3">
 <div class="card"><h2>Crash factors (share of crashes)</h2>{factors}<div class="note">Bars: here. Black tick: statewide, same years.
  Factors overlap (one crash can be speeding and at night), so they don't add to 100%.</div></div>
 <div class="card"><h2>Collision types</h2>{coll}</div>
 <div class="card"><h2>When crashes happen</h2>{hours}<div class="note">By hour of day</div>{days}<div class="note">By day of week</div></div>
</section>
<section class="grid2">
 <div class="card"><h2>Key patterns</h2><ul>{pats}</ul></div>
 <div class="card"><h2>Recommendations (FHWA Proven Safety Countermeasures)</h2><ul>{recs}</ul>
  <div class="note">Screening suggestions from crash shares, not an engineering study.</div></div>
</section>
<div class="note" style="padding:0 20px 20px">{E(d["source"])}</div>
<script>
// Print and PNG: ask the framed map (same origin) for a snapshot with its legend. Static code only;
// nothing from the data is inserted here.
(() => {{
  const frame = document.querySelector('iframe[name="dashmap"]'), img = document.querySelector('.print-map');
  const snap = () => {{ try {{ return frame.contentWindow.mapSnapshot(); }} catch (e) {{ return null; }} }};
  const fill = () => {{ const u = snap(); if (u) img.src = u; return u; }};
  window.addEventListener('beforeprint', fill);
  document.getElementById('printpdf').addEventListener('click', () => {{ fill(); setTimeout(() => window.print(), 150); }});
  document.getElementById('dlpng').addEventListener('click', () => {{
    const u = snap(); if (!u) return;
    const a = document.createElement('a'); a.href = u;
    a.download = (document.title.replace(/[^\w-]+/g, '-').replace(/^-+|-+$/g, '') || 'dashboard') + '-map.png';
    document.body.append(a); a.click(); a.remove();
  }});
}})();
</script>
</body></html>"""
