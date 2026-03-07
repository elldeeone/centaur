/** GET /api/threads — proxy to FastAPI backend */

import { resilientFetch, API_URL } from "@/lib/api-client";

export const dynamic = "force-dynamic";
export const fetchCache = "force-no-store";

export async function GET() {
  try {
    const res = await resilientFetch(`${API_URL}/threads`, {
      timeoutMs: 10_000,
    });
    const data = await res.json();
    return Response.json(data, {
      status: res.status,
      headers: { "Cache-Control": "no-store" },
    });
  } catch (err) {
    console.error("Failed to fetch threads from API:", err);
    return Response.json(
      { error: err instanceof Error ? err.message : "API unreachable" },
      { status: 502, headers: { "Cache-Control": "no-store" } },
    );
  }
}
