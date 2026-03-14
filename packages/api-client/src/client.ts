import type { CanonicalEvent } from "@centaur/harness-events";
import { EventSourceParserStream, type EventSourceMessage } from "eventsource-parser/stream";
import axios, { type AxiosInstance } from "axios";

export type InputContentBlock =
  | { type: "text"; text: string }
  | { type: "image"; source: { type: "base64"; media_type: string; data: string } }
  | { type: "document"; source: { type: "base64"; media_type: string; data: string } };

export interface MessageOptions {
  threadKey: string;
  parts: InputContentBlock[];
  userId?: string;
  metadata?: Record<string, unknown>;
}

export interface ExecuteOptions {
  threadKey: string;
  message: string;
  harness?: string;
  platform?: string;
  userId?: string;
  signal?: AbortSignal;
}

/**
 * Centaur API client. Two-step protocol:
 *
 *   1. client.message()  — POST /agent/messages  (persist user message + attachments)
 *   2. client.execute()  — POST /agent/execute   (run the turn, stream SSE response)
 */
export class CentaurClient {
  readonly http: AxiosInstance;
  private log?: { info: Function; warn: Function; error: Function };

  constructor(opts: {
    apiUrl: string;
    apiKey: string;
    logger?: { info: Function; warn: Function; error: Function };
  }) {
    this.log = opts.logger;
    this.http = axios.create({
      baseURL: opts.apiUrl,
      headers: { Authorization: `Bearer ${opts.apiKey}` },
      timeout: 30_000,
    });
  }

  /** Step 1: Buffer a user message into chat_messages. Always call before execute(). */
  async message(opts: MessageOptions): Promise<void> {
    await this.http.post("/agent/messages", {
      thread_key: opts.threadKey,
      role: "user",
      parts: opts.parts,
      user_id: opts.userId,
      metadata: opts.metadata,
    });
  }

  /** Step 2: Execute a turn. Streams CanonicalEvents via SSE. */
  async *execute(opts: ExecuteOptions): AsyncGenerator<CanonicalEvent, void, undefined> {
    const { threadKey, message, harness, platform, userId, signal } = opts;

    this.log?.info("sse_connect", { thread_key: threadKey, harness });

    const body: Record<string, unknown> = { thread_key: threadKey, message };
    if (harness) body.harness = harness;
    if (platform) body.platform = platform;
    if (userId) body.user_id = userId;

    const res = await fetch(`${this.http.defaults.baseURL}/agent/execute`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Authorization: (this.http.defaults.headers["Authorization"] ?? this.http.defaults.headers.common?.["Authorization"]) as string,
        "X-Trace-Id": threadKey,
      },
      body: JSON.stringify(body),
      signal,
    });

    if (!res.ok) {
      const text = await res.text().catch(() => "");
      let parsed: Record<string, unknown> | undefined;
      try { parsed = JSON.parse(text); } catch {}
      const code = parsed?.code as string | undefined;
      throw new Error(
        code
          ? `${code}: ${(parsed?.detail as string) ?? text.slice(0, 300)}`
          : `/agent/execute failed (${res.status}): ${text.slice(0, 300)}`,
      );
    }

    this.log?.info("sse_streaming", { thread_key: threadKey });
    if (!res.body) return;
    const stream = (res.body as ReadableStream<Uint8Array>)
      .pipeThrough(new TextDecoderStream() as unknown as TransformStream<Uint8Array, string>)
      .pipeThrough(new EventSourceParserStream());
    for await (const event of stream as unknown as AsyncIterable<EventSourceMessage>) {
      if (event.data === "[DONE]") return;
      try { yield JSON.parse(event.data) as CanonicalEvent; } catch {}
    }
  }

  /** Check session status (used for recovery on expired streams). */
  async getStatus(threadKey: string) {
    const { data } = await this.http.get("/agent/status", { params: { key: threadKey } });
    return data as Record<string, unknown>;
  }
}
