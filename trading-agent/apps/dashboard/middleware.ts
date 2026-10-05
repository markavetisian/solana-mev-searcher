// HTTP basic auth for the whole dashboard (DASHBOARD_BASIC_AUTH="user:password").
// Fails closed: if the variable is unset the dashboard refuses to serve unless DASHBOARD_ALLOW_NO_AUTH=1
// (local development only). Put TLS in front of this in any non-local deployment.
import { NextRequest, NextResponse } from "next/server";

export function middleware(req: NextRequest) {
  const expected = process.env.DASHBOARD_BASIC_AUTH;
  if (!expected) {
    if (process.env.DASHBOARD_ALLOW_NO_AUTH === "1") return NextResponse.next();
    return new NextResponse("Dashboard auth not configured (set DASHBOARD_BASIC_AUTH)", { status: 503 });
  }
  const header = req.headers.get("authorization") ?? "";
  const given = header.startsWith("Basic ") ? atob(header.slice(6)) : "";
  if (given.length === expected.length && timingSafeEqual(given, expected)) return NextResponse.next();
  return new NextResponse("Authentication required", { status: 401, headers: { "WWW-Authenticate": 'Basic realm="trading-agent"' } });
}

function timingSafeEqual(a: string, b: string): boolean {
  let diff = 0;
  for (let i = 0; i < a.length; i++) diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return diff === 0;
}

export const config = { matcher: ["/((?!_next/static|_next/image|favicon.ico).*)"] };
