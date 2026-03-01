/** Proxy /api/threads -> FastAPI /api/threads */

import { apiGet, ApiError } from "@/lib/api-client";

export const dynamic = "force-dynamic";
export const fetchCache = "force-no-store";

export async function GET(request: Request) {
  try {
    const res = await apiGet("/api/threads", undefined, { signal: request.signal });

    if (!res.ok) {
      return Response.json(
        { error: `Failed to fetch threads: ${res.status}` },
        { status: res.status, headers: { "Cache-Control": "no-store" } }
      );
    }

    const data = await res.json();
    return Response.json(data, { headers: { "Cache-Control": "no-store" } });
  } catch (err) {
    const status = err instanceof ApiError ? (err.status ?? 502) : 502;
    return Response.json(
      { error: err instanceof Error ? err.message : "API unreachable" },
      { status, headers: { "Cache-Control": "no-store" } },
    );
  }
}
