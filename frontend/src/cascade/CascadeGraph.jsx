import { useMemo } from "react";
import { LINK_TYPES, SECTOR_ORDER, STATUS, sectorColor, sectorLabel } from "./sectors.js";

const COL_W = 118;
const ROW_H = 54;
const LEFT = 196;
const TOP = 34;
const MAX_R = 19;

/**
 * Cascade graph: one row per sector, one column per hop away from the drawn area.
 * Each bubble is the set of assets in that sector that failed (solid) or went onto
 * backup (ring) at that hop. Links are the dependencies the failure travelled over,
 * colored by what they carry (power, water, comms) and weighted by how many.
 */
export default function CascadeGraph({ impact, selectedCell, onSelectCell }) {
  const model = useMemo(() => buildModel(impact), [impact]);

  if (!impact) return null;
  if (!model.cells.length) {
    return <p className="graph-empty">Nothing in this area depends on anything else in the network. Try a larger area or one that covers a substation.</p>;
  }

  const { rows, maxHop, cells, links, maxCount } = model;
  const width = LEFT + (maxHop + 1) * COL_W + 12;
  const height = TOP + rows.length * ROW_H + 8;
  const cx = (hop) => LEFT + hop * COL_W + COL_W / 2;
  const cy = (sector) => TOP + rows.indexOf(sector) * ROW_H + ROW_H / 2;
  const radius = (count) => 5 + (MAX_R - 5) * Math.sqrt(count / maxCount);
  const key = (c) => `${c.sector}:${c.hop}`;
  const sel = selectedCell ? `${selectedCell.sector}:${selectedCell.hop}` : null;
  const cellBy = new Map(cells.map((c) => [key(c), c]));

  return (
    <div className="graph-scroll">
      <svg viewBox={`0 0 ${width} ${height}`} width={width} height={height} role="img"
           aria-label="Cascade graph: sectors by hops from the drawn area">
        <defs>
          {Object.entries(LINK_TYPES).map(([t, l]) => (
            <marker key={t} id={`arrow-${t}`} viewBox="0 0 8 8" refX="7" refY="4" markerUnits="userSpaceOnUse"
                    markerWidth="9" markerHeight="9" orient="auto-start-reverse">
              <path d="M0,0 L8,4 L0,8 z" fill={l.color} />
            </marker>
          ))}
        </defs>

        {/* hop columns */}
        {Array.from({ length: maxHop + 1 }, (_, h) => (
          <g key={h}>
            <rect x={LEFT + h * COL_W + 2} y={TOP - 4} width={COL_W - 4} height={rows.length * ROW_H + 4}
                  className={h === 0 ? "hop-col hop-zero" : "hop-col"} rx="4" />
            <text x={cx(h)} y={TOP - 12} className="hop-label" textAnchor="middle">
              {h === 0 ? "In the area" : `Hop ${h}`}
            </text>
          </g>
        ))}

        {/* sector rows */}
        {rows.map((s) => {
          const t = model.rowTotals.get(s);
          return (
            <g key={s}>
              <circle cx={14} cy={cy(s)} r={5} fill={sectorColor(s)} />
              <text x={26} y={cy(s) - 3} className="row-label">{sectorLabel(s)}</text>
              <text x={26} y={cy(s) + 12} className="row-sub">
                {t.failed} failed{t.degraded ? `, ${t.degraded} on backup` : ""}
              </text>
            </g>
          );
        })}

        {/* links between cells */}
        {links.map((l) => {
          const a = cellBy.get(l.from);
          const b = cellBy.get(l.to);
          const x1 = cx(a.hop) + radius(a.count) + 2;
          const y1 = cy(a.sector);
          const x2 = cx(b.hop) - radius(b.count) - 4;
          const y2 = cy(b.sector);
          const mx = (x1 + x2) / 2;
          const color = LINK_TYPES[l.type]?.color ?? "#8a97a3";
          return (
            <path key={`${l.from}>${l.to}:${l.type}`}
                  d={`M${x1},${y1} C${mx},${y1} ${mx},${y2} ${x2},${y2}`}
                  stroke={color} strokeWidth={1 + 2.2 * Math.log2(1 + l.count)} fill="none"
                  strokeOpacity="0.7" markerEnd={`url(#arrow-${l.type})`}
                  strokeDasharray={l.degradedOnly ? "4 3" : undefined}>
              <title>{`${l.count} ${LINK_TYPES[l.type]?.label ?? l.type} link${l.count === 1 ? "" : "s"}: ${sectorLabel(a.sector)} hop ${a.hop} → ${sectorLabel(b.sector)} hop ${b.hop}`}</title>
            </path>
          );
        })}

        {/* cells */}
        {cells.map((c) => {
          const r = radius(c.count);
          const rf = c.failed ? radius(c.failed) : 0;
          const color = sectorColor(c.sector);
          const isSel = sel === key(c);
          const label = `${sectorLabel(c.sector)}, ${c.hop === 0 ? "inside the area" : `hop ${c.hop}`}: ${c.failed} failed, ${c.degraded} on backup`;
          return (
            <g key={key(c)} className={`cell${isSel ? " is-selected" : ""}`} role="button" tabIndex={0}
               aria-pressed={isSel} aria-label={label}
               onClick={() => onSelectCell(isSel ? null : { sector: c.sector, hop: c.hop })}
               onKeyDown={(e) => {
                 if (e.key === "Enter" || e.key === " ") {
                   e.preventDefault();
                   onSelectCell(isSel ? null : { sector: c.sector, hop: c.hop });
                 }
               }}>
              <title>{label}</title>
              <circle cx={cx(c.hop)} cy={cy(c.sector)} r={r + 6} className="cell-hit" />
              {c.degraded > 0 && (
                <circle cx={cx(c.hop)} cy={cy(c.sector)} r={r} fill="none"
                        stroke={STATUS.Degraded.color} strokeWidth="2.5" />
              )}
              {c.failed > 0 && (
                <circle cx={cx(c.hop)} cy={cy(c.sector)} r={rf} fill={color}
                        stroke={STATUS.Failed.color} strokeWidth="2" />
              )}
              <text x={cx(c.hop) + r + 5} y={cy(c.sector) - r + 9} className="cell-count">{c.count}</text>
            </g>
          );
        })}
      </svg>
    </div>
  );
}

function buildModel(impact) {
  const cells = new Map();
  const byId = new Map();
  const rowTotals = new Map();
  let maxHop = 0;

  for (const i of impact?.impacts ?? []) {
    byId.set(i.id, i);
    const k = `${i.sector}:${i.hop}`;
    const c = cells.get(k) ?? { sector: i.sector, hop: i.hop, count: 0, failed: 0, degraded: 0 };
    c.count++;
    if (i.status === "Failed") c.failed++;
    else c.degraded++;
    cells.set(k, c);
    const t = rowTotals.get(i.sector) ?? { failed: 0, degraded: 0 };
    if (i.status === "Failed") t.failed++;
    else t.degraded++;
    rowTotals.set(i.sector, t);
    maxHop = Math.max(maxHop, i.hop);
  }

  const links = new Map();
  for (const i of impact?.impacts ?? []) {
    const v = i.via && byId.get(i.via);
    if (!v) continue;
    const from = `${v.sector}:${v.hop}`;
    const to = `${i.sector}:${i.hop}`;
    const lk = `${from}>${to}:${i.viaType}`;
    const l = links.get(lk) ?? { from, to, type: i.viaType, count: 0, degradedOnly: true };
    l.count++;
    if (i.status === "Failed") l.degradedOnly = false;
    links.set(lk, l);
  }

  const present = new Set([...cells.values()].map((c) => c.sector));
  const rows = [...SECTOR_ORDER.filter((s) => present.has(s)), ...[...present].filter((s) => !SECTOR_ORDER.includes(s))];
  const list = [...cells.values()];
  return {
    rows,
    maxHop,
    cells: list,
    links: [...links.values()],
    rowTotals,
    maxCount: Math.max(1, ...list.map((c) => c.count)),
  };
}
