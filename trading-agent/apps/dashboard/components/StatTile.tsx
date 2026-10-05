export function StatTile({ label, value, sub, status }: { label: string; value: React.ReactNode; sub?: React.ReactNode; status?: "good" | "warning" | "serious" | "critical" }) {
  return (
    <div className="card tile">
      <div className="label">{label}</div>
      <div className="value">{status ? <span className={`status ${status}`}>{value}</span> : value}</div>
      {sub != null && <div className="sub">{sub}</div>}
    </div>
  );
}
