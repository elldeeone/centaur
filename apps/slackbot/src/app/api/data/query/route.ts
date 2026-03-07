/** Proxy /api/data/query -> FastAPI /api/data/query */

import { apiPost, ApiError } from "@/lib/api-client";

export const dynamic = "force-dynamic";
export const fetchCache = "force-no-store";

export async function POST(request: Request) {
  try {
    const body = await request.json();
    const data = await apiPost("/api/data/query", body, {
      signal: request.signal,
      timeoutMs: 30_000,
    });
    return Response.json(data, { headers: { "Cache-Control": "no-store" } });
  } catch (err) {
    const status = err instanceof ApiError ? (err.status ?? 502) : 502;
    return Response.json(
      { error: err instanceof Error ? err.message : "API unreachable" },
      { status, headers: { "Cache-Control": "no-store" } },
    );
  }
}
