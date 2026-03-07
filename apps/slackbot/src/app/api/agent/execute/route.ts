/**
 * POST /api/agent/execute
 *
 * Accepts { slack_thread_key, message, harness? } from the client.
 * Calls the Python pipe server, reads raw harness SSE events, converts them
 * to AI SDK v6 UIMessageChunk objects server-side, and returns a proper
 * AI SDK UIMessage stream response.
 *
 * The client can consume this with DefaultChatTransport / HttpChatTransport
 * — no custom SSE parsing needed on the client side.
 */

import { z } from "zod";
import {
  createUIMessageStreamResponse,
  createUIMessageStream,
  createIdGenerator,
  parseJsonEventStream,
} from "ai";
import type { UIMessage } from "ai";
import { resilientFetch, API_URL, ApiError } from "@/lib/api-client";
import {
  harnessEventToUiChunks,
  createConversionState,
} from "@/lib/harness-to-ui-chunks";

const generateMessageId = createIdGenerator({ prefix: "msg", size: 16 });

export const dynamic = "force-dynamic";
export const fetchCache = "force-no-store";
export const maxDuration = 300;

const rawEventSchema = z.record(z.string(), z.unknown());

export async function POST(request: Request) {
  const body = await request.json().catch(() => ({}));
  const slackThreadKey = String(body.slack_thread_key ?? "").trim();
  const message = String(body.message ?? "").trim();
  const harness =
    typeof body.harness === "string" && body.harness.trim().length > 0
      ? body.harness.trim()
      : "amp";
  const originalMessages: UIMessage[] = Array.isArray(body.messages) ? body.messages : [];

  if (!slackThreadKey || !message) {
    return Response.json(
      { error: "Missing slack_thread_key or message" },
      { status: 400, headers: { "Cache-Control": "no-store" } },
    );
  }

  let upstream: Response;
  try {
    upstream = await resilientFetch(`${API_URL}/pipe/execute`, {
      method: "POST",
      body: JSON.stringify({
        thread_key: slackThreadKey,
        message,
        harness,
      }),
      stream: true,
    });
  } catch (err) {
    const status = err instanceof ApiError ? (err.status ?? 502) : 502;
    return Response.json(
      { error: err instanceof Error ? err.message : "API unreachable" },
      { status, headers: { "Cache-Control": "no-store" } },
    );
  }

  if (!upstream.ok) {
    const text = await upstream.text().catch(() => "");
    return Response.json(
      { error: `Execute failed: ${upstream.status}`, detail: text.slice(0, 500) },
      { status: upstream.status, headers: { "Cache-Control": "no-store" } },
    );
  }

  if (!upstream.body) {
    return Response.json(
      { error: "No response body from pipe server" },
      { status: 502, headers: { "Cache-Control": "no-store" } },
    );
  }

  // Parse the raw SSE from the pipe server using the AI SDK's built-in parser
  const rawEvents = parseJsonEventStream({
    stream: upstream.body,
    schema: rawEventSchema,
  });

  // Convert raw harness events → AI SDK UIMessageChunks
  let eventIndex = 0;
  const conversionState = createConversionState();

  const uiChunkStream = rawEvents.pipeThrough(
    new TransformStream({
      transform(parseResult, controller) {
        if (!parseResult.success) {
          // Skip malformed events — keep stream alive
          return;
        }
        const rawEvent = parseResult.value;
        const chunks = harnessEventToUiChunks(
          harness,
          rawEvent,
          0,
          eventIndex,
          conversionState,
        );
        eventIndex += 1;
        for (const chunk of chunks) {
          controller.enqueue(chunk);
        }
      },
    }),
  );

  // Return a proper AI SDK UIMessage stream response
  return createUIMessageStreamResponse({
    stream: createUIMessageStream({
      originalMessages,
      generateId: generateMessageId,
      execute: async ({ writer }) => {
        writer.merge(uiChunkStream);
      },
      onFinish: async ({ messages }) => {
        try {
          await resilientFetch(`${API_URL}/threads/messages`, {
            method: "POST",
            body: JSON.stringify({
              thread_key: slackThreadKey,
              messages: messages.map((msg) => ({
                id: msg.id,
                role: msg.role,
                parts: msg.parts,
                metadata: msg.metadata || {},
              })),
            }),
            maxAttempts: 2,
            timeoutMs: 10_000,
          });
        } catch {
          // Best-effort persistence — don't block stream
        }
      },
    }),
  });
}
