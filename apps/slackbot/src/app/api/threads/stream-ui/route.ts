/** Proxy /api/threads/stream-ui?key=... -> FastAPI /api/threads/stream-ui?key=... as SSE */

import { resilientFetch, API_URL, ApiError } from "@/lib/api-client";

export const dynamic = "force-dynamic";
export const fetchCache = "force-no-store";
export const maxDuration = 300;

export async function GET(request: Request) {
  const { searchParams } = new URL(request.url);
  const key = searchParams.get("key") || "";
  const liveOnly = searchParams.get("live_only") || "";
  if (!key) {
    return Response.json({ error: "Missing thread key" }, { status: 400 });
  }

  const upstreamParams = new URLSearchParams({ key });
  if (liveOnly) {
    upstreamParams.set("live_only", liveOnly);
  }

  try {
    const upstream = await resilientFetch(
      `${API_URL}/api/threads/stream-ui?${upstreamParams.toString()}`,
      { stream: true, signal: request.signal },
    );

    if (!upstream.ok || !upstream.body) {
      return Response.json(
        { error: `Stream not available: ${key}` },
        { status: upstream.status },
      );
    }

    return new Response(upstream.body, {
      headers: {
        "Content-Type": "text/event-stream",
        "Cache-Control": "no-cache, no-transform",
        Connection: "keep-alive",
        "X-Accel-Buffering": "no",
        "x-vercel-ai-ui-message-stream": "v1",
      },
    });
  } catch (err) {
    const status = err instanceof ApiError ? (err.status ?? 502) : 502;
    return Response.json(
      { error: err instanceof Error ? err.message : "API unreachable" },
      { status },
    );
  }
}
