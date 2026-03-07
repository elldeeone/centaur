/** /api/threads/detail?key=... — proxy to FastAPI backend + enrich with pipe status */

import { resilientFetch, API_URL } from "@/lib/api-client";
import type { Harness, ThreadDetail, ThreadState } from "@/lib/types";

export const dynamic = "force-dynamic";
export const fetchCache = "force-no-store";

type PipeStatus = {
  thread_key: string;
  status: string;
  container_id?: string;
  harness?: string;
  engine?: string;
  started_at?: number;
};

export async function GET(request: Request) {
  const { searchParams } = new URL(request.url);
  const key = searchParams.get("key") || "";
  if (!key) {
    return Response.json({ error: "Missing thread key" }, { status: 400 });
  }

  try {
    const res = await resilientFetch(
      `${API_URL}/threads/detail?key=${encodeURIComponent(key)}`,
      { timeoutMs: 10_000, signal: request.signal },
    );

    if (!res.ok) {
      const data = await res.json().catch(() => ({}));
      return Response.json(data, {
        status: res.status,
        headers: { "Cache-Control": "no-store" },
      });
    }

    const detail = (await res.json()) as ThreadDetail;

    // Enrich with live pipe status (best-effort)
    try {
      const pipeRes = await resilientFetch(
        `${API_URL}/pipe/status?key=${encodeURIComponent(key)}`,
        { timeoutMs: 3000, signal: request.signal },
      );
      if (pipeRes.ok) {
        const pipeStatus = (await pipeRes.json()) as PipeStatus;
        const isRunning = pipeStatus.status === "running";
        detail.state = (isRunning ? "running" : "idle") as ThreadState;
        detail.harness = (pipeStatus.harness as Harness) ?? detail.harness;
      }
    } catch {
      // Pipe server unreachable — keep idle state from API
    }

    return Response.json(detail, { headers: { "Cache-Control": "no-store" } });
  } catch (err) {
    return Response.json(
      { error: err instanceof Error ? err.message : "API unreachable" },
      { status: 502, headers: { "Cache-Control": "no-store" } },
    );
  }
}
