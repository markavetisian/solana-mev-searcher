// Server-side proxy to the trading API. The read token lives only in the server environment
// (TA_API_READ_TOKEN) and is never sent to the browser. Control POSTs require the operator to supply
// the admin token per request (X-Admin-Token), which is forwarded as a bearer token and never stored.
import { NextRequest, NextResponse } from "next/server";

const API = process.env.TA_API_URL ?? "http://127.0.0.1:8080";
const READ = process.env.TA_API_READ_TOKEN ?? "";
const ALLOWED_GET = /^(health|system\/status|tokens(\/[1-9A-HJ-NP-Za-km-z]{32,44})?|opportunities|positions(\/[\w-]{1,64})?|portfolio|trades|risk|metrics|decisions|research\/experiments|backtests)$/;
const ALLOWED_POST = /^(system\/(pause|resume|kill)|paper\/reset)$/;

async function forward(req: NextRequest, path: string, init: RequestInit) {
  const url = `${API}/${path}${req.nextUrl.search}`;
  try {
    const r = await fetch(url, { ...init, cache: "no-store", signal: AbortSignal.timeout(8000) });
    const body = await r.text();
    return new NextResponse(body, { status: r.status, headers: { "content-type": r.headers.get("content-type") ?? "application/json" } });
  } catch {
    return NextResponse.json({ detail: "API unreachable" }, { status: 502 });
  }
}

export async function GET(req: NextRequest, ctx: { params: Promise<{ path: string[] }> }) {
  const path = (await ctx.params).path.join("/");
  if (!ALLOWED_GET.test(path)) return NextResponse.json({ detail: "not found" }, { status: 404 });
  return forward(req, path, { headers: { Authorization: `Bearer ${READ}` } });
}

export async function POST(req: NextRequest, ctx: { params: Promise<{ path: string[] }> }) {
  const path = (await ctx.params).path.join("/");
  if (!ALLOWED_POST.test(path)) return NextResponse.json({ detail: "not found" }, { status: 404 });
  const admin = req.headers.get("x-admin-token") ?? "";
  if (!admin) return NextResponse.json({ detail: "admin token required" }, { status: 401 });
  const body = await req.text();
  if (body.length > 2000) return NextResponse.json({ detail: "body too large" }, { status: 413 });
  return forward(req, path, { method: "POST", body, headers: { Authorization: `Bearer ${admin}`, "content-type": "application/json" } });
}
