"use client";
import Link from "next/link";
import { usePoll } from "@/lib/api";
import { num, pct, sol, short, ago } from "@/lib/format";

export default function Opportunities() {
  const { data, error, loading } = usePoll<any>("opportunities", 2000);
  const rows: any[] = data?.rows ?? [];
  return (
    <>
      <h1>Opportunities <span className="muted" style={{ fontSize: 13 }}>updated {ago(data?.updated_at)}</span></h1>
      <p className="muted">Ranked by quant score. A high score is not an entry: every deterministic gate must pass, including
        expected value after costs from recorded outcomes (INSUFFICIENT DATA = no trade).</p>
      <div className={`card scroll ${loading && rows.length ? "" : ""}`}>
        <table>
          <thead><tr>
            <th className="num">Rank</th><th>Token</th><th className="num">Score</th><th className="num">Market cap</th>
            <th className="num">Liquidity</th><th className="num">Volume 5m</th><th className="num">Buy/Sell 60s</th>
            <th className="num">Holder growth</th><th>Risk</th><th className="num">Expected value</th>
            <th className="num">Proposed size</th><th>Decision</th>
          </tr></thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.mint}>
                <td className="num">{r.rank}</td>
                <td><Link href={`/tokens/${r.mint}`}>{r.symbol ? <>{r.symbol} <span className="muted mono">{short(r.mint)}</span></> : <span className="mono">{short(r.mint)}</span>}</Link></td>
                <td className="num"><strong>{num(r.score, 0)}</strong></td>
                <td className="num">{sol(r.market_cap_sol, 1)}</td>
                <td className="num">{sol(r.liquidity_sol, 2)}</td>
                <td className="num">{sol(r.volume_sol_5m, 2)}</td>
                <td className="num">{r.buy_sell_imbalance_60s == null ? "—" : `${r.buy_sell_imbalance_60s >= 0 ? "+" : "−"}${Math.abs(r.buy_sell_imbalance_60s).toFixed(2)}`}</td>
                <td className="num">{pct(r.holder_growth_60s)}</td>
                <td title={(r.risk_reasons ?? []).join("\n")}><span className={`status ${r.risk === "PASS" ? "good" : "serious"}`}>{r.risk}</span></td>
                <td className="num">{r.ev_status === "OK" ? pct(r.expected_value, 2) : <span className="muted">{r.ev_status ?? "—"}</span>}</td>
                <td className="num">{r.proposed_size_sol ? sol(r.proposed_size_sol, 3) : "—"}</td>
                <td title={r.top_rejection ?? ""}><span className="pill">{r.outcome}</span></td>
              </tr>
            ))}
            {!rows.length && <tr><td colSpan={12} className="muted">No evaluated candidates yet.</td></tr>}
          </tbody>
        </table>
      </div>
      {error && <p className="err">{error}</p>}
    </>
  );
}
