"use client";
import { usePoll } from "@/lib/api";
import { num, pct } from "@/lib/format";

export default function Research() {
  const exps = usePoll<any>("research/experiments?limit=5", 15000);
  const bts = usePoll<any>("backtests?limit=10", 15000);
  const latest = exps.data?.experiments?.[0];
  return (
    <>
      <h1>Research</h1>
      <p className="muted">Experiments are stored separately from the production strategy. A significant correlation is a hypothesis,
        not a rule: it must survive walk-forward out-of-sample testing after costs, and its placebo must be clean.</p>
      <div className="card scroll">
        <h2>Latest research run {latest ? `#${latest.id}` : ""} {latest?.results?.synthetic_data ? <span className="status serious">SYNTHETIC DATA</span> : null}</h2>
        <table><thead><tr><th>Experiment</th><th className="num">n (non-overlapping)</th><th className="num">tokens</th><th className="num">IC</th>
          <th className="num">95% CI</th><th className="num">p (BH-adj.)</th><th className="num">placebo p</th><th className="num">top-quintile mean</th><th>Interpretation</th></tr></thead>
          <tbody>{(latest?.results?.experiments ?? []).map((e: any) => (
            <tr key={e.name}><td title={e.question}>{e.name}</td><td className="num">{e.n}</td><td className="num">{e.n_tokens}</td>
              <td className="num">{e.ic == null ? "—" : num(e.ic, 3)}</td><td className="num">{e.ic_ci95 ? `${num(e.ic_ci95[0], 3)} … ${num(e.ic_ci95[1], 3)}` : "—"}</td>
              <td className="num">{e.p_adjusted == null ? "—" : num(e.p_adjusted, 4)}</td><td className="num">{e.placebo_p == null ? "—" : num(e.placebo_p, 3)}</td>
              <td className="num">{e.quintile_mean_label?.length ? pct(e.quintile_mean_label[4], 2) : "—"}</td>
              <td className="secondary" style={{ whiteSpace: "normal" }}>{e.interpretation}</td></tr>))}</tbody></table>
      </div>
      <div className="card scroll" style={{ marginTop: 12 }}>
        <h2>Backtest &amp; walk-forward runs</h2>
        <table><thead><tr><th>#</th><th>Kind</th><th>Verdict / trades</th><th className="num">Expectancy</th><th className="num">Profit factor</th><th className="num">Max DD</th></tr></thead>
          <tbody>{(bts.data?.runs ?? []).map((r: any) => {
            const m = r.results?.out_of_sample ?? r.results?.metrics ?? {};
            return <tr key={r.id}><td>{r.id}</td><td>{r.kind}{r.results?.synthetic_data ? " (synthetic)" : ""}</td>
              <td>{r.results?.verdict?.verdict ?? `${m.trades ?? 0} trades`}</td><td className="num">{pct(m.expectancy_per_trade, 2)}</td>
              <td className="num">{m.profit_factor == null ? "—" : Number(m.profit_factor).toFixed(2)}</td><td className="num">{pct(m.max_drawdown)}</td></tr>;
          })}</tbody></table>
      </div>
    </>
  );
}
