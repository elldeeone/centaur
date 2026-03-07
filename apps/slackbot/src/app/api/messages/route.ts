/** GET /api/messages?key={thread_key} — proxy to FastAPI backend */

import { NextRequest } from "next/server";
import { safeValidateUIMessages } from "ai";
import { dataPartSchemas } from "@/lib/data-part-schemas";
import { resilientFetch, API_URL } from "@/lib/api-client";

export const dynamic = "force-dynamic";
export const fetchCache = "force-no-store";

export async function GET(request: NextRequest) {
  const threadKey = request.nextUrl.searchParams.get("key");
  if (!threadKey) {
    return Response.json({ error: "Missing key parameter" }, { status: 400 });
  }

  try {
    const res = await resilientFetch(
      `${API_URL}/threads/messages?key=${encodeURIComponent(threadKey)}`,
      { timeoutMs: 10_000 },
    );

    if (!res.ok) {
      const data = await res.json().catch(() => ({}));
      return Response.json(data, {
        status: res.status,
        headers: { "Cache-Control": "no-store" },
      });
    }

    const rawMessages = await res.json();

    const validated = await safeValidateUIMessages({
      messages: rawMessages,
      dataSchemas: dataPartSchemas,
    });

    return Response.json(
      validated.success ? validated.data : rawMessages,
      { headers: { "Cache-Control": "no-store" } },
    );
  } catch (err) {
    console.error("Failed to fetch messages from API:", err);
    return Response.json(
      { error: err instanceof Error ? err.message : "API unreachable" },
      { status: 502, headers: { "Cache-Control": "no-store" } },
    );
  }
}
