"use client";
import { use } from "react";
import Link from "next/link";
import { usePoll } from "@/lib/api";
import { pct, sol, short, time } from "@/lib/format";

export default function PositionDetail({ params }: { params: Promise<{ id: string }> }) {
  const { id } = use(params);
  const { data, error } = usePoll<any>(`positions/${id}`, 3000);
  const p = data?.position ?? data?.events?.at(-1)?.detail?.position;
  const tr = data?.trade;
  if (!p && !tr) return <p className="muted">{error ?? "Loading…"}</p>;
  const x = p ?? {};
  return (
    <>
      <h1>Position {x.symbol ?? short(x.mint ?? tr?.mint)} <span className="pill">{data?.open ? "OPEN" : "CLOSED"}</span></h1>
      <div className="grid two">
        <div className="card"><h2>Entry &amp; execution</h2><table><tbody>
          {[["Token", <Link key="t" href={`/tokens/${x.mint}`}>{x.mint}</Link>], ["Venue", x.venue], ["Opened", x.opened_at ? time(x.opened_at) : "—"],
            ["Entry (effective)", `${Number(x.entry_effective_price ?? 0).toExponential(3)} lamports/unit`], ["Entry spot", `${Number(x.entry_spot_price ?? 0).toExponential(3)}`],
            ["Size", sol(x.initial_cost_sol)], ["Expected slippage", pct(x.expected_entry_slippage, 2)], ["Actual slippage", pct(x.actual_entry_slippage, 2)],
            ["Platform fees", sol(x.platform_fees_sol, 6)], ["Network fees", sol(x.network_fees_sol, 6)], ["Entry execution", <span key="e" className="mono">{x.entry_execution_id}</span>],
            ["Decision", <span key="d" className="mono">{x.decision_id}</span>]]
            .map(([k, v]) => <tr key={k as string}><td className="secondary">{k}</td><td>{v as any}</td></tr>)}
        </tbody></table></div>
        <div className="card"><h2>Risk levels &amp; P/L</h2><table><tbody>
          {[["Current value (executable)", sol(x.value_sol)], ["Total return", pct(x.total_return ?? tr?.net_return, 2)], ["Peak return", pct(x.peak_return, 2)],
            ["Hard stop", `−${pct(x.hard_stop_frac, 0)}`], ["Take-profit ladder", (x.take_profits ?? []).map((t: number[]) => `+${(t[0] * 100).toFixed(0)}% → sell ${(t[1] * 100).toFixed(0)}%`).join(", ")],
            ["TP hits", x.tp_hits], ["Trailing stop", x.trailing_stop_level == null ? `inactive (activates at +${pct(x.trailing_stop_frac, 0)} peak)` : pct(x.trailing_stop_level, 1)],
            ["Exit reason", x.exit_reason ?? tr?.exit_reason ?? "—"], ["Regime at entry", x.regime], ["Score at entry", x.score?.toFixed?.(1)]]
            .map(([k, v]) => <tr key={k as string}><td className="secondary">{k}</td><td>{v as any}</td></tr>)}
        </tbody></table></div>
        <div className="card"><h2>Original thesis</h2><pre className="mono secondary" style={{ whiteSpace: "pre-wrap" }}>{JSON.stringify(x.thesis, null, 2)}</pre></div>
        <div className="card"><h2>Current thesis</h2><pre className="mono secondary" style={{ whiteSpace: "pre-wrap" }}>{JSON.stringify(x.current_thesis, null, 2)}</pre></div>
        <div className="card"><h2>Exits</h2><table><thead><tr><th>Time</th><th className="num">Tokens</th><th className="num">Received</th><th>Reason</th></tr></thead>
          <tbody>{(x.exits ?? []).map((f: any, i: number) => <tr key={i}><td>{time(f.t)}</td><td className="num">{f.tokens}</td><td className="num">{sol(f.lamports / 1e9)}</td><td>{f.reason}</td></tr>)}</tbody></table></div>
        <div className="card"><h2>Versions (audit)</h2><table><tbody>{Object.entries(x.versions ?? tr?.versions ?? {}).map(([k, v]) => <tr key={k}><td className="secondary">{k}</td><td className="mono">{String(v)}</td></tr>)}</tbody></table></div>
      </div>
    </>
  );
}
