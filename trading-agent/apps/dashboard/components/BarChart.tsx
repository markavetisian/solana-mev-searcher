"use client";
import { useState } from "react";
import { useWidth } from "@/lib/useWidth";

export type Bar = { label: string; value: number; color: string; detail?: string };

/** Vertical bars from a zero baseline (supports negatives). Each bar is its own hover target. */
export function BarChart({ bars, height = 180, fmt, legend }: { bars: Bar[]; height?: number; fmt: (v: number) => string; legend?: { name: string; color: string }[] }) {
  const [ref, W, box] = useWidth<HTMLDivElement>();
  const H = height, L = 56, R = 8, T = 8, B = 20;
  const [hover, setHover] = useState<number | null>(null);
  if (!bars.length) return <div className="muted">No data yet.</div>;
  const maxV = Math.max(0, ...bars.map((b) => b.value)), minV = Math.min(0, ...bars.map((b) => b.value));
  const span = maxV - minV || 1;
  const sy = (v: number) => T + (1 - (v - minV) / span) * (H - T - B);
  const slot = (W - L - R) / bars.length, bw = Math.max(1, slot - 2);
  return (
    <div className="chart" ref={ref} onPointerLeave={() => setHover(null)}>
      {legend && <div className="legend">{legend.map((l) => <span key={l.name}><i style={{ background: l.color }} />{l.name}</span>)}</div>}
      <svg width={W} height={H} viewBox={`0 0 ${W} ${H}`} role="img">
        {[maxV, (maxV + minV) / 2, minV].map((t, i) => (
          <g key={i}>
            <line x1={L} x2={W - R} y1={sy(t)} y2={sy(t)} stroke="var(--grid)" />
            <text x={L - 6} y={sy(t) + 4} textAnchor="end" fontSize={12} fill="var(--text-muted)">{fmt(t)}</text>
          </g>
        ))}
        {bars.map((b, i) => {
          const y0 = sy(0), y1 = sy(b.value);
          const y = Math.min(y0, y1), h = Math.max(1, Math.abs(y1 - y0));
          return (
            <g key={i} onPointerEnter={() => setHover(i)} onFocus={() => setHover(i)} tabIndex={0}>
              <rect x={L + i * slot} y={T} width={slot} height={H - T - B} fill="transparent" />
              <rect x={L + i * slot + 1} y={y} width={bw} height={h} rx={Math.min(4, bw / 2)} fill={b.color} opacity={hover === null || hover === i ? 1 : 0.55} />
            </g>
          );
        })}
        <line x1={L} x2={W - R} y1={sy(0)} y2={sy(0)} stroke="var(--text-muted)" />
      </svg>
      {hover != null && (
        <div className="tooltip" style={{ left: `${Math.min(78, ((L + hover * slot) / W) * 100)}%`, top: 8 }}>
          <div className="v">{fmt(bars[hover].value)}</div>
          <div className="k">{bars[hover].label}{bars[hover].detail ? ` · ${bars[hover].detail}` : ""}</div>
        </div>
      )}
    </div>
  );
}

/** Horizontal bars with direct value labels (score components, holder distribution). */
export function HBars({ rows, max, fmt, color = "var(--series-1)" }: { rows: { label: string; value: number | null; note?: string }[]; max: number; fmt: (v: number) => string; color?: string }) {
  return (
    <table>
      <tbody>
        {rows.map((r) => (
          <tr key={r.label} title={r.note ?? ""}>
            <td style={{ width: "34%" }} className="secondary">{r.label}</td>
            <td style={{ width: "50%" }}>
              <svg viewBox="0 0 100 10" preserveAspectRatio="none" style={{ width: "100%", height: 10 }}>
                <rect x={0} y={0} width={100} height={10} rx={2} fill="var(--surface-2)" />
                {r.value != null && <rect x={0} y={0} width={Math.max(0.5, Math.min(100, (r.value / max) * 100))} height={10} rx={2} fill={color} />}
              </svg>
            </td>
            <td className="num">{r.value == null ? <span className="muted">n/a</span> : fmt(r.value)}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}
