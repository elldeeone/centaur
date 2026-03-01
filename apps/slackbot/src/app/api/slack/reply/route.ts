/** Proxy POST /api/slack/reply -> FastAPI /slack/reply */

import { resilientFetch, API_URL, ApiError } from "@/lib/api-client";

export const dynamic = "force-dynamic";
export const fetchCache = "force-no-store";

type ReplyAttachment = { url: string; name: string };

function normalizeAttachments(value: unknown): ReplyAttachment[] {
  if (!Array.isArray(value)) return [];
  return value
    .map((item) => {
      const raw = item as { url?: unknown; name?: unknown };
      const url = typeof raw.url === "string" ? raw.url.trim() : "";
      const name = typeof raw.name === "string" ? raw.name.trim() : "";
      return { url, name };
    })
    .filter((item) => item.url && item.name);
}

export async function POST(request: Request) {
  const body = await request.json().catch(() => ({}));
  const threadKey = String(body.thread_key ?? "").trim();
  const reply = String(body.reply ?? "").trim();
  const attachments = normalizeAttachments((body as { attachments?: unknown }).attachments);
  const source = typeof body.source === "string" ? body.source.trim() : "";
  const userId = typeof body.user_id === "string" ? body.user_id.trim() : "";
  const messageId = typeof body.message_id === "string" ? body.message_id.trim() : "";

  if (!threadKey || !reply) {
    return Response.json(
      { error: "Missing thread_key or reply" },
      { status: 400, headers: { "Cache-Control": "no-store" } },
    );
  }

  try {
    const upstream = await resilientFetch(`${API_URL}/slack/reply`, {
      method: "POST",
      body: JSON.stringify({
        thread_key: threadKey,
        reply,
        ...(attachments.length > 0 ? { attachments } : {}),
        ...(source ? { source } : {}),
        ...(userId ? { user_id: userId } : {}),
        ...(messageId ? { message_id: messageId } : {}),
      }),
      timeoutMs: 30_000,
      signal: request.signal,
    });

    const text = await upstream.text();
    if (!upstream.ok) {
      return Response.json(
        { error: `Reply failed: ${upstream.status}`, detail: text.slice(0, 500) },
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
