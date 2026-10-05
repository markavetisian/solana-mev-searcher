"use client";
import { useMemo, useState } from "react";
import { useWidth } from "@/lib/useWidth";

export type Series = { name: string; color: string; points: { x: number; y: number }[] };

/** Single-axis line chart with a snapping crosshair and one tooltip listing every series at that X. */
export function LineChart({ series, height = 220, fmtX, fmtY, baseline }: {
  series: Series[]; height?: number; fmtX: (x: number) => string; fmtY: (y: number) => string; baseline?: number;
}) {
  const [ref, W, box] = useWidth<HTMLDivElement>();
  const H = height, L = 64, R = 12, T = 10, B = 26;
  const [hover, setHover] = useState<number | null>(null);
  const all = series.flatMap((s) => s.points);
  const xs = useMemo(() => Array.from(new Set(all.map((p) => p.x))).sort((a, b) => a - b), [series]); // eslint-disable-line
  if (!all.length) return <div className="muted">No data yet.</div>;
  const minX = xs[0], maxX = xs[xs.length - 1];
  let minY = Math.min(...all.map((p) => p.y)), maxY = Math.max(...all.map((p) => p.y));
  if (baseline != null) { minY = Math.min(minY, baseline); maxY = Math.max(maxY, baseline); }
  if (minY === maxY) { minY -= Math.abs(minY) * 0.05 || 1; maxY += Math.abs(maxY) * 0.05 || 1; }
  const pad = (maxY - minY) * 0.06; minY -= pad; maxY += pad;
  const sx = (x: number) => L + ((x - minX) / (maxX - minX || 1)) * (W - L - R);
  const sy = (y: number) => T + (1 - (y - minY) / (maxY - minY)) * (H - T - B);
  const ticks = [0, 0.25, 0.5, 0.75, 1].map((f) => minY + f * (maxY - minY));
  const onMove = (e: React.PointerEvent) => {
    const rect = box.current!.getBoundingClientRect();
    const px = ((e.clientX - rect.left) / rect.width) * W;
    let best = xs[0];
    for (const x of xs) if (Math.abs(sx(x) - px) < Math.abs(sx(best) - px)) best = x;
    setHover(best);
  };
  const hx = hover != null ? sx(hover) : 0;
  return (
    <div className="chart" ref={ref} onPointerMove={onMove} onPointerLeave={() => setHover(null)}>
      {series.length > 1 && (
        <div className="legend">{series.map((s) => <span key={s.name}><i style={{ background: s.color, height: 2 }} />{s.name}</span>)}</div>
      )}
      <svg width={W} height={H} viewBox={`0 0 ${W} ${H}`} role="img" aria-label={series.map((s) => s.name).join(", ")}>
        {ticks.map((t, i) => (
          <g key={i}>
            <line x1={L} x2={W - R} y1={sy(t)} y2={sy(t)} stroke="var(--grid)" strokeWidth={1} />
            <text x={L - 6} y={sy(t) + 4} textAnchor="end" fontSize={12} fill="var(--text-muted)">{fmtY(t)}</text>
          </g>
        ))}
        {baseline != null && <line x1={L} x2={W - R} y1={sy(baseline)} y2={sy(baseline)} stroke="var(--text-muted)" strokeDasharray="3 3" />}
        <text x={L} y={H - 6} fontSize={12} fill="var(--text-muted)">{fmtX(minX)}</text>
        <text x={W - R} y={H - 6} fontSize={12} fill="var(--text-muted)" textAnchor="end">{fmtX(maxX)}</text>
        {series.map((s) => (
          <polyline key={s.name} fill="none" stroke={s.color} strokeWidth={2} strokeLinejoin="round" strokeLinecap="round"
            points={s.points.map((p) => `${sx(p.x)},${sy(p.y)}`).join(" ")} />
        ))}
        {hover != null && <line x1={hx} x2={hx} y1={T} y2={H - B} stroke="var(--text-secondary)" strokeWidth={1} />}
        {hover != null && series.map((s) => {
          const p = s.points.find((q) => q.x === hover);
          return p ? <circle key={s.name} cx={sx(p.x)} cy={sy(p.y)} r={4} fill={s.color} stroke="var(--surface-1)" strokeWidth={2} /> : null;
        })}
      </svg>
      {hover != null && (
        <div className="tooltip" style={{ left: `${Math.min(80, (hx / W) * 100)}%`, top: 8 }}>
          <div className="k">{fmtX(hover)}</div>
          {series.map((s) => {
            const p = s.points.find((q) => q.x === hover);
            return p ? <div key={s.name}><span style={{ display: "inline-block", width: 10, height: 2, background: s.color, marginRight: 6, verticalAlign: 3 }} /><span className="v">{fmtY(p.y)}</span> <span className="k">{series.length > 1 ? s.name : ""}</span></div> : null;
          })}
        </div>
      )}
    </div>
  );
}
