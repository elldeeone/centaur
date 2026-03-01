/** Proxy POST /api/agent/execute -> FastAPI /agent/execute */

import { resilientFetch, API_URL, ApiError } from "@/lib/api-client";

export const dynamic = "force-dynamic";
export const fetchCache = "force-no-store";

export async function POST(request: Request) {
  const body = await request.json().catch(() => ({}));
  const slackThreadKey = String(body.slack_thread_key ?? "").trim();
  const message = String(body.message ?? "").trim();
  const harness = typeof body.harness === "string" ? body.harness.trim() : "";

  if (!slackThreadKey || !message) {
    return Response.json(
      { error: "Missing slack_thread_key or message" },
      { status: 400, headers: { "Cache-Control": "no-store" } },
    );
  }

  try {
    const upstream = await resilientFetch(`${API_URL}/agent/execute`, {
      method: "POST",
      body: JSON.stringify({
        slack_thread_key: slackThreadKey,
        message,
        ...(harness ? { harness } : {}),
        source: "thread_ui",
      }),
      signal: request.signal,
    });

    const text = await upstream.text();
    if (!upstream.ok) {
      return Response.json(
        { error: `Execute failed: ${upstream.status}`, detail: text.slice(0, 500) },
        { status: upstream.status, headers: { "Cache-Control": "no-store" } },
      );
    }

    try {
      return Response.json(JSON.parse(text), { headers: { "Cache-Control": "no-store" } });
    } catch {
      return Response.json({ status: "ok" }, { headers: { "Cache-Control": "no-store" } });
    }
  } catch (err) {
    const status = err instanceof ApiError ? (err.status ?? 502) : 502;
    return Response.json(
      { error: err instanceof Error ? err.message : "API unreachable" },
      { status, headers: { "Cache-Control": "no-store" } },
    );
  }
}
