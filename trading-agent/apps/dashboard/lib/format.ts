export const sol = (x: number | null | undefined, d = 4) => (x == null || Number.isNaN(x) ? "—" : `${x.toFixed(d)} SOL`);
export const signedSol = (x: number | null | undefined, d = 4) => (x == null ? "—" : `${x >= 0 ? "+" : "−"}${Math.abs(x).toFixed(d)} SOL`);
export const pct = (x: number | null | undefined, d = 1) => (x == null || Number.isNaN(x) ? "—" : `${(x * 100).toFixed(d)}%`);
export const signedPct = (x: number | null | undefined, d = 1) => (x == null ? "—" : `${x >= 0 ? "+" : "−"}${Math.abs(x * 100).toFixed(d)}%`);
export const num = (x: number | null | undefined, d = 2) => (x == null || Number.isNaN(x) ? "—" : x.toLocaleString(undefined, { maximumFractionDigits: d }));
export const ago = (t: number | null | undefined) => {
  if (!t) return "—";
  const s = Date.now() / 1000 - t;
  if (s < 60) return `${s.toFixed(0)}s ago`;
  if (s < 3600) return `${(s / 60).toFixed(0)}m ago`;
  return `${(s / 3600).toFixed(1)}h ago`;
};
export const time = (t: number) => new Date(t * 1000).toLocaleTimeString([], { hour12: false });
export const short = (k?: string | null) => (k ? `${k.slice(0, 4)}…${k.slice(-4)}` : "—");
