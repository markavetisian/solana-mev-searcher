"use client";
import Link from "next/link";
import { usePoll } from "@/lib/api";
import { pct, sol, signedSol, short, time } from "@/lib/format";

export default function Positions() {
  const pos = usePoll<any>("positions", 2000);
  const trades = usePoll<any>("trades?limit=200", 10000);
  const m = trades.data?.metrics ?? {};
  return (
    <>
      <h1>Positions <span className="pill">{pos.data?.mode ?? "—"}</span></h1>
      <div className="card scroll">
        <table><thead><tr><th>Token</th><th>Venue</th><th>Opened</th><th className="num">Cost</th><th className="num">Value (exec.)</th>
          <th className="num">Return</th><th className="num">Stop</th><th>TP hits</th><th className="num">Trailing level</th><th>Status</th></tr></thead>
          <tbody>
            {(pos.data?.positions ?? []).map((p: any) => (
              <tr key={p.position_id}>
                <td><Link href={`/positions/${p.position_id}`}>{p.symbol ?? short(p.mint)}</Link></td>
                <td>{p.venue}</td><td>{time(p.opened_at)}</td>
                <td className="num">{sol(p.initial_cost_sol)}</td><td className="num">{sol(p.value_sol)}</td>
                <td className="num"><span className={`status ${p.total_return >= 0 ? "good" : "critical"}`}>{pct(p.total_return, 2)}</span></td>
                <td className="num">−{pct(p.hard_stop_frac, 0)}</td><td>{p.tp_hits}/{p.take_profits.length}</td>
                <td className="num">{p.trailing_stop_level == null ? "inactive" : pct(p.trailing_stop_level)}</td>
                <td><span className="pill">{p.status}</span>{p.stuck_reason ? <span className="muted"> {p.stuck_reason}</span> : null}</td>
              </tr>))}
            {!(pos.data?.positions ?? []).length && <tr><td colSpan={10} className="muted">No open positions.</td></tr>}
          </tbody></table>
      </div>
      <h2 style={{ marginTop: 18 }}>Closed trades — not just win rate</h2>
      <div className="grid tiles">
        {[["Trades", m.trades ?? 0], ["Win rate", pct(m.win_rate)], ["Avg winner", pct(m.avg_winner, 2)], ["Avg loser", pct(m.avg_loser, 2)],
          ["Expectancy / trade", pct(m.expectancy_per_trade, 2)], ["Profit factor", m.profit_factor == null ? "—" : Number(m.profit_factor).toFixed(2)],
          ["Median hold", m.median_hold_s == null ? "—" : `${m.median_hold_s.toFixed(0)}s`], ["Tail loss (CVaR 5%)", pct(m.cvar_5, 1)],
          ["Fees paid", sol(m.fees_sol, 3)], ["Expectancy 95% CI", m.expectancy_ci95 ? `${pct(m.expectancy_ci95[0], 2)} … ${pct(m.expectancy_ci95[1], 2)}` : "n<5"]]
          .map(([k, v]) => <div key={k as string} className="card tile"><div className="label">{k}</div><div className="value" style={{ fontSize: 18 }}>{v as any}</div></div>)}
      </div>
      <div className="card scroll" style={{ marginTop: 12 }}>
        <table><thead><tr><th>Closed</th><th>Token</th><th className="num">P/L</th><th className="num">Return</th><th className="num">Held</th><th>Exit reason</th><th>Strategy</th></tr></thead>
          <tbody>{(trades.data?.trades ?? []).map((t: any) => (
            <tr key={t.position_id}><td>{time(t.closed_at)}</td><td><Link href={`/positions/${t.position_id}`}>{t.symbol ?? short(t.mint)}</Link></td>
              <td className="num">{signedSol(t.pnl_lamports / 1e9)}</td><td className="num">{pct(t.net_return, 2)}</td><td className="num">{t.hold_s.toFixed(0)}s</td>
              <td>{t.exit_reason}</td><td className="muted">{t.versions?.strategy_version}</td></tr>))}</tbody></table>
      </div>
    </>
  );
}
