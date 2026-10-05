"use client";
import { usePoll } from "@/lib/api";
import { StatTile } from "@/components/StatTile";
import { LineChart } from "@/components/LineChart";
import { pct, sol, signedSol, time, num } from "@/lib/format";
import Link from "next/link";

export default function Overview() {
  const status = usePoll<any>("system/status", 2000);
  const pf = usePoll<any>("portfolio", 3000);
  const metrics = usePoll<any>("metrics", 5000);
  const s = status.data, snap = pf.data?.snapshot;
  const wallet = metrics.data?.metrics?.gauges?.["wallet.balance_sol"];
  const feed = s?.feed;
  const scanner = s?.worker !== "RUNNING" ? ["critical", s?.worker ?? "DOWN"] : feed?.connected === false ? ["serious", "FEED DOWN"]
    : (s?.market?.data_staleness_s ?? 0) > 10 ? ["warning", "STALE"] : ["good", "HEALTHY"];
  const ddStatus = (snap?.drawdown ?? 0) > 0.1 ? "critical" : (snap?.drawdown ?? 0) > 0.05 ? "warning" : undefined;
  const curve = (pf.data?.equity_curve ?? []).map((p: any) => ({ x: p.t, y: p.equity_sol }));
  return (
    <>
      <h1>Overview</h1>
      <div className="grid tiles">
        <StatTile label="System mode" value={s?.mode ?? "—"} sub={`config ${s?.configuration_version ?? "—"}`} />
        <StatTile label="Scanner health" value={scanner[1]} status={scanner[0] as any} sub={`${num(s?.market?.trades_per_min, 0)} trades/min · ${s?.tracked_tokens ?? 0} tokens`} />
        <StatTile label="Wallet balance" value={wallet != null ? sol(wallet, 3) : "—"} sub={s?.wallet ? `wallet ${s.wallet.slice(0, 6)}…` : "no wallet (paper)"} />
        <StatTile label={`${s?.mode === "LIVE" ? "Live" : "Paper"} equity`} value={sol(snap?.equity_sol, 3)} sub={`start ${sol(snap?.starting_equity_sol, 2)}`} />
        <StatTile label="Realized P/L" value={signedSol(snap?.realized_pnl_sol, 3)} />
        <StatTile label="Unrealized P/L" value={signedSol(snap?.unrealized_pnl_sol, 3)} sub="at executable (sell-quote) value" />
        <StatTile label="Daily P/L" value={signedSol(snap?.daily_pnl_sol, 3)} />
        <StatTile label="Drawdown" value={pct(snap?.drawdown)} status={ddStatus as any} sub={`max ${pct(snap?.max_drawdown)}`} />
        <StatTile label="Open positions" value={snap?.open_positions ?? 0} sub={<Link href="/positions">view</Link>} />
        <StatTile label="Calibration samples" value={s?.outcome_samples ?? 0} sub={`${s?.calibration_positions ?? 0} tracking now`} />
      </div>
      <div className="grid two" style={{ marginTop: 12 }}>
        <div className={`card ${pf.loading && curve.length ? "" : ""}`}>
          <h2>Equity ({s?.mode ?? "—"})</h2>
          <LineChart series={[{ name: "Equity", color: "var(--series-1)", points: curve }]} fmtX={time} fmtY={(v) => `${v.toFixed(3)}`} baseline={snap?.starting_equity_sol} />
        </div>
        <div className="card">
          <h2>Market regime: {s?.regime?.regime ?? "—"}</h2>
          <ul className="reasons">{(s?.regime?.reasons ?? []).map((r: string) => <li key={r}>{r}</li>)}</ul>
          <table style={{ marginTop: 8 }}><tbody>
            {Object.entries(s?.regime?.stats ?? {}).map(([k, v]) => <tr key={k}><td className="secondary">{k}</td><td className="num">{typeof v === "number" ? num(v, 3) : String(v)}</td></tr>)}
          </tbody></table>
        </div>
      </div>
      {(status.error || pf.error) && <p className="err">{status.error ?? pf.error}</p>}
    </>
  );
}
