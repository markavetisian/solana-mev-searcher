"use client";
import { useState } from "react";
import { postCommand, usePoll } from "@/lib/api";
import { time } from "@/lib/format";

export default function Risk() {
  const { data } = usePoll<any>("risk", 3000);
  const [token, setToken] = useState(""); // admin token: kept in memory only, never persisted
  const [reason, setReason] = useState("");
  const [confirm, setConfirm] = useState("");
  const [msg, setMsg] = useState("");
  const run = async (path: string) => {
    if (path === "system/kill" && !window.confirm("Activate the kill switch? All strategy orders stop and a manual reset is required.")) return;
    const r = await postCommand(path, token, { reason, operator: "dashboard", confirm });
    setMsg(`${path}: ${r.status} ${r.body}`);
  };
  const ks = data?.kill_switch;
  return (
    <>
      <h1>Risk &amp; Controls</h1>
      <div className="grid two">
        <div className="card">
          <h2>Kill switch</h2>
          <p>{ks?.killed ? <span className="status critical">KILLED — {ks.reason}</span> : <span className="status good">armed (not triggered)</span>}</p>
          <div className="row" style={{ marginTop: 8 }}>
            <input type="password" placeholder="admin token" value={token} onChange={(e) => setToken(e.target.value)} autoComplete="off" />
            <input placeholder="reason" value={reason} onChange={(e) => setReason(e.target.value)} maxLength={300} />
          </div>
          <div className="row" style={{ marginTop: 8 }}>
            <button onClick={() => run("system/pause")}>Pause entries</button>
            <button onClick={() => run("system/resume")}>Resume</button>
            <button className="danger" onClick={() => run("system/kill")}>KILL</button>
            <button onClick={() => run("paper/reset")}>Reset paper</button>
          </div>
          <div className="row" style={{ marginTop: 8 }}>
            <input placeholder="reset phrase (to resume after kill)" value={confirm} onChange={(e) => setConfirm(e.target.value)} />
          </div>
          <p className="muted">Resuming after a kill requires the phrase RESET_KILL_SWITCH. No endpoint can sign or send transactions, change limits, or reveal keys.</p>
          {msg && <p className="secondary mono" style={{ wordBreak: "break-all" }}>{msg}</p>}
        </div>
        <div className="card">
          <h2>Decisions in the last hour</h2>
          <table><tbody>{Object.entries(data?.decisions_last_hour ?? {}).map(([k, v]) => <tr key={k}><td>{k}</td><td className="num">{String(v)}</td></tr>)}</tbody></table>
          <h2 style={{ marginTop: 12 }}>Risk limits (read-only)</h2>
          <table><tbody>{Object.entries(data?.limits ?? {}).map(([k, v]) => <tr key={k}><td className="secondary">{k}</td><td className="num">{String(v)}</td></tr>)}</tbody></table>
        </div>
        <div className="card scroll" style={{ gridColumn: "1 / -1" }}>
          <h2>Risk events</h2>
          <table><thead><tr><th>Time</th><th>Kind</th><th>Severity</th><th>Detail</th></tr></thead>
            <tbody>{(data?.risk_events ?? []).map((e: any, i: number) => <tr key={i}><td>{time(e.t)}</td><td>{e.kind}</td>
              <td><span className={`status ${e.severity === "CRITICAL" ? "critical" : "warning"}`}>{e.severity}</span></td>
              <td className="mono muted" style={{ whiteSpace: "normal" }}>{JSON.stringify(e.detail).slice(0, 240)}</td></tr>)}</tbody></table>
        </div>
      </div>
    </>
  );
}
