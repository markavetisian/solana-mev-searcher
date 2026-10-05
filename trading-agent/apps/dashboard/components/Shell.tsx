"use client";
import Link from "next/link";
import { usePoll } from "@/lib/api";
import { ago } from "@/lib/format";

/** Mode banner on every page: PAPER / SHADOW / LIVE is impossible to miss. */
export function Shell({ children }: { children: React.ReactNode }) {
  const { data, error } = usePoll<any>("system/status", 3000);
  const mode: string = data?.mode ?? "UNKNOWN";
  const worker: string = data?.worker ?? (error ? "API UNREACHABLE" : "…");
  const killed = data?.kill_switch?.killed || data?.kill_switch_persisted?.killed;
  const reason = data?.kill_switch?.reason ?? data?.kill_switch_persisted?.reason;
  return (
    <>
      <div className={`banner ${mode}`} role="status">
        {mode === "LIVE" ? "LIVE — REAL FUNDS" : mode} MODE · worker {worker}
        {data?.heartbeat_age_s != null ? ` · heartbeat ${data.heartbeat_age_s.toFixed(1)}s` : ""}
      </div>
      {killed && <div className="killed" role="alert">TRADING HALTED (kill switch latched): {reason}</div>}
      {data?.kill_switch?.paused && <div className="killed" style={{ background: "#5a4300" }}>ENTRIES PAUSED: {data.kill_switch.pause_reason}</div>}
      <nav className="top">
        <strong>Trading Agent</strong>
        <Link href="/">Overview</Link>
        <Link href="/opportunities">Opportunities</Link>
        <Link href="/positions">Positions</Link>
        <Link href="/risk">Risk &amp; Controls</Link>
        <Link href="/research">Research</Link>
        <span className="muted" style={{ marginLeft: "auto" }}>regime {data?.regime?.regime ?? "—"} · started {ago(data?.started_at)}</span>
      </nav>
      <main>{children}</main>
    </>
  );
}
