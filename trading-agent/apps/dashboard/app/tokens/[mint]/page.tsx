"use client";
import { use } from "react";
import { usePoll } from "@/lib/api";
import { LineChart } from "@/components/LineChart";
import { BarChart, HBars } from "@/components/BarChart";
import { num, pct, sol, short, time } from "@/lib/format";

export default function TokenDetail({ params }: { params: Promise<{ mint: string }> }) {
  const { mint } = use(params);
  const { data, error } = usePoll<any>(`tokens/${mint}?window_s=3600`, 4000);
  const t = data?.token ?? {};
  const trades: any[] = data?.trades ?? [];
  const snaps: any[] = data?.snapshots ?? [];
  const decisions: any[] = data?.decisions ?? [];
  const latest = decisions[0];
  const priceSol = trades.filter((x) => x.price).map((x) => ({ x: x.t, y: (x.price * 1e6) / 1e9 }));
  // 30s buckets of buy (+) and sell (−) volume
  const buckets = new Map<number, { buy: number; sell: number }>();
  for (const x of trades) { const k = Math.floor(x.t / 30) * 30; const b = buckets.get(k) ?? { buy: 0, sell: 0 }; if (x.side === "BUY") b.buy += x.sol; else b.sell += x.sol; buckets.set(k, b); }
  const vol = [...buckets.entries()].sort((a, b) => a[0] - b[0]).slice(-60).flatMap(([k, b]) => [
    { label: time(k), value: b.buy, color: "var(--series-1)", detail: "buy SOL" },
    { label: time(k), value: -b.sell, color: "var(--series-2)", detail: "sell SOL" },
  ]);
  const components = Object.entries((data?.score_components ?? {}) as Record<string, { score: number; weight: number; missing: string[] }>);
  const ai = data?.ai_analyses?.[0];
  const f = data?.latest_features ?? {};
  return (
    <>
      <h1>{t.symbol ? <>{t.symbol} <span className="muted">(untrusted name: {t.name})</span></> : "Token"} <span className="mono muted" style={{ fontSize: 13 }}>{mint}</span></h1>
      <div className="grid tiles">
        <div className="card tile"><div className="label">State</div><div className="value" style={{ fontSize: 16 }}>{t.market_state ?? "—"}</div><div className="sub">graduation {t.graduation_state ?? "—"}</div></div>
        <div className="card tile"><div className="label">Market cap</div><div className="value">{sol(f.mcap_sol, 1)}</div></div>
        <div className="card tile"><div className="label">Liquidity</div><div className="value">{sol(f.liquidity_sol, 2)}</div><div className="sub">exit cost {pct(f.exit_impact_frac, 2)} at ref size</div></div>
        <div className="card tile"><div className="label">Holders</div><div className="value">{num(f.holders, 0)}</div><div className="sub">top-10 {pct(f.top10_share)}</div></div>
        <div className="card tile"><div className="label">Creator</div><div className="value mono" style={{ fontSize: 14 }}>{short(t.creator)}</div><div className="sub">holds {pct(f.creator_holding, 2)} · sold {pct(f.creator_sold_frac)} · prior launches {num(f.creator_prev_launches, 0)}</div></div>
        <div className="card tile"><div className="label">Creator suspicion</div><div className="value">{pct(f.creator_suspicion, 0)}</div><div className="sub">confidence {pct(f.creator_suspicion_confidence, 0)}</div></div>
      </div>
      <div className="grid two" style={{ marginTop: 12 }}>
        <div className="card"><h2>Price (SOL per token, last hour)</h2>
          <LineChart series={[{ name: "Price", color: "var(--series-1)", points: priceSol }]} fmtX={time} fmtY={(v) => v.toExponential(2)} /></div>
        <div className="card"><h2>Volume per 30s (buys up, sells down)</h2>
          <BarChart bars={vol} fmt={(v) => `${Math.abs(v).toFixed(2)}`} legend={[{ name: "Buy SOL", color: "var(--series-1)" }, { name: "Sell SOL", color: "var(--series-2)" }]} /></div>
        <div className="card"><h2>Liquidity &amp; market cap (snapshots)</h2><div className="legend">Liquidity (SOL)</div>
          <LineChart series={[{ name: "Liquidity SOL", color: "var(--series-1)", points: snaps.map((s) => ({ x: s.t, y: s.liquidity_sol ?? 0 })) }]} fmtX={time} fmtY={(v) => v.toFixed(1)} />
          <div className="legend" style={{ marginTop: 8 }}>Market cap (SOL)</div><LineChart height={140} series={[{ name: "Market cap SOL", color: "var(--series-1)", points: snaps.map((s) => ({ x: s.t, y: s.market_cap_sol ?? 0 })) }]} fmtX={time} fmtY={(v) => v.toFixed(0)} /></div>
        <div className="card"><h2>Holder distribution (trade-derived, top 20, % of supply)</h2>
          <HBars rows={(data?.holder_distribution_window ?? []).map((h: any) => ({ label: short(h.wallet), value: h.share }))} max={Math.max(0.01, ...(data?.holder_distribution_window ?? []).map((h: any) => h.share))} fmt={(v) => pct(v, 2)} /></div>
        <div className="card"><h2>Score components (latest decision)</h2>
          {components.length ? <>
            <p className="secondary">Total <strong>{num(data?.score_total, 1)}</strong> / 100 — weights are configurable defaults, not tuned optima.</p>
            <HBars rows={components.sort((a, b) => b[1].weight - a[1].weight).map(([k, v]) => ({ label: `${k} (w ${v.weight})`, value: v.score, note: v.missing?.length ? `missing: ${v.missing.join(", ")}` : undefined }))} max={100} fmt={(v) => v.toFixed(0)} />
          </> : <p className="muted">No score recorded yet.</p>}</div>
        <div className="card"><h2>Trade decision &amp; rejection reasons</h2>
          {latest ? <>
            <p><span className="pill">{latest.outcome}</span> score {num(latest.score, 1)} · regime {latest.regime} · {time(latest.t)}</p>
            <ul className="reasons">{(latest.rejection_reasons ?? []).map((r: string, i: number) => <li key={i}>{r}</li>)}</ul>
            {latest.ev && <p className="secondary">EV: {latest.ev.status === "OK" ? `${pct(latest.ev.ev_conservative, 2)} conservative (P(win) ${pct(latest.ev.p_win)}, n=${latest.ev.n})` : `INSUFFICIENT DATA (n=${latest.ev.n})`}</p>}
          </> : <p className="muted">—</p>}</div>
        <div className="card"><h2>AI analysis (advisory, veto-only)</h2>
          {ai ? <>
            <p><span className="pill">{ai.status}</span> {ai.model} · {num(ai.latency_ms, 0)} ms</p>
            {ai.assessment && <>
              <p><strong>{ai.assessment.assessment}</strong> · confidence {pct(ai.assessment.confidence, 0)}</p>
              <p className="secondary">{ai.assessment.thesis}</p>
              <ul className="reasons">{[...ai.assessment.risk_flags.map((x: string) => `risk: ${x}`), ...ai.assessment.contradictions.map((x: string) => `contradiction: ${x}`), ...ai.assessment.observations].map((x: string, i: number) => <li key={i}>{x}</li>)}</ul>
            </>}
            {ai.error && <p className="err">{ai.error}</p>}
          </> : <p className="muted">No AI analysis (disabled or not reached).</p>}</div>
        <div className="card scroll"><h2>Transaction flow (latest 100)</h2>
          <table><thead><tr><th>Time</th><th>Side</th><th className="num">SOL</th><th>Wallet</th><th>Venue</th></tr></thead>
            <tbody>{trades.slice(-100).reverse().map((x, i) => (
              <tr key={i}><td>{time(x.t)}</td><td style={{ color: x.side === "BUY" ? "var(--series-1)" : "var(--series-2)" }}>{x.side}</td>
                <td className="num">{x.sol.toFixed(3)}</td><td className="mono">{short(x.user)}</td><td className="muted">{x.venue}</td></tr>))}</tbody></table></div>
      </div>
      {error && <p className="err">{error}</p>}
    </>
  );
}
