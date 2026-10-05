"use client";
import { useEffect, useRef, useState } from "react";

export async function getJSON<T = any>(path: string): Promise<T> {
  const r = await fetch(`/api/proxy/${path}`, { cache: "no-store" });
  if (!r.ok) throw new Error(`${r.status} ${await r.text()}`);
  return r.json();
}

export async function postCommand(path: string, adminToken: string, body: Record<string, string>) {
  const r = await fetch(`/api/proxy/${path}`, {
    method: "POST",
    headers: { "content-type": "application/json", "x-admin-token": adminToken },
    body: JSON.stringify(body),
  });
  return { ok: r.ok, status: r.status, body: await r.text() };
}

/** Poll an API path. Keeps the previous data while refetching (no flash), exposes staleness. */
export function usePoll<T = any>(path: string | null, ms = 2000) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const alive = useRef(true);
  useEffect(() => {
    alive.current = true;
    if (!path) return;
    let timer: ReturnType<typeof setTimeout>;
    const tick = async () => {
      setLoading(true);
      try {
        const d = await getJSON<T>(path);
        if (alive.current) { setData(d); setError(null); }
      } catch (e: any) {
        if (alive.current) setError(String(e.message ?? e));
      } finally {
        if (alive.current) { setLoading(false); timer = setTimeout(tick, ms); }
      }
    };
    tick();
    return () => { alive.current = false; clearTimeout(timer); };
  }, [path, ms]);
  return { data, error, loading };
}
