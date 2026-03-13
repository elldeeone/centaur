import * as crypto from "node:crypto";
import { Chat } from "chat";
import { createSlackAdapter, type SlackAdapter } from "@chat-adapter/slack";
import { createPostgresState } from "@chat-adapter/state-pg";
import { normalizeThreadKey, splitThreadKey } from "@centaur/harness-events";
import {
  resilientFetch as _resilientFetch,
  ApiError,
  type FetchOptions,
} from "@centaur/api-client";
import type { CanonicalEvent } from "@centaur/harness-events";
import type { StreamChunk } from "chat";
import { Pool } from "pg";
import { log } from "@/lib/logger";
import { ProgressTracker } from "./progress-tracker";

// ── Config ──────────────────────────────────────────────────────────────────

const API_URL = process.env.CENTAUR_API_URL || "http://api:8000";
const API_KEY = process.env.SLACKBOT_API_KEY || "";
const SLACK_BOT_USERNAME = process.env.SLACK_BOT_USERNAME || "ai-agent";
const THREAD_VIEWER_URL = process.env.THREAD_VIEWER_URL || "";
const KEEPALIVE_MS = 60_000;

// ── Singletons ──────────────────────────────────────────────────────────────

let _pool: Pool | null = null;
function getPool(): Pool {
  if (!_pool) {
    _pool = new Pool({ connectionString: process.env.DATABASE_URL, max: 10 });
  }
  return _pool;
}

function apiFetch(url: string, opts: FetchOptions = {}): Promise<Response> {
  return _resilientFetch(url, opts, API_KEY, log);
}

// ── Types ───────────────────────────────────────────────────────────────────

type Harness = string;

type ContentBlock =
  | { type: "text"; text: string }
  | { type: "image"; source: { type: "base64"; media_type: string; data: string } }
  | { type: "document"; source: { type: "base64"; media_type: string; data: string } };

type Thread = Parameters<Parameters<Chat["onNewMention"]>[0]>[0];

// ── Flag parsing ────────────────────────────────────────────────────────────

export function extractRunOptions(text: string): {
  harness: Harness;
  cleanedText: string;
  harnessExplicit: boolean;
} {
  let cleaned = text;
  let harness: Harness = "amp";
  let harnessExplicit = false;

  const kvMatch = cleaned.match(/\bharness\s*=\s*([A-Za-z0-9_-]+)\b/i);
  if (kvMatch) {
    harness = kvMatch[1].toLowerCase();
    harnessExplicit = true;
    cleaned = (
      cleaned.slice(0, kvMatch.index) + cleaned.slice(kvMatch.index! + kvMatch[0].length)
    ).trim();
  }

  const engineFlags: Array<{ regex: RegExp; value: string }> = [
    { regex: /(^|\s)--amp(?=\s|$)/gi, value: "amp" },
    { regex: /(^|\s)--claude(?=\s|$)/gi, value: "claude-code" },
    { regex: /(^|\s)--claude-code(?=\s|$)/gi, value: "claude-code" },
    { regex: /(^|\s)--codex(?=\s|$)/gi, value: "codex" },
    { regex: /(^|\s)--pi(?=\s|$)/gi, value: "pi-mono" },
    { regex: /(^|\s)--pi-mono(?=\s|$)/gi, value: "pi-mono" },
  ];
  for (const { regex, value } of engineFlags) {
    const matched = regex.test(cleaned);
    regex.lastIndex = 0;
    if (matched) {
      harness = value;
      harnessExplicit = true;
      cleaned = cleaned.replace(regex, " ");
      regex.lastIndex = 0;
    }
  }

  cleaned = cleaned.replace(/(^|\s)--(engine|model)\s+[A-Za-z0-9._-]+(?=\s|$)/gi, " ");
  cleaned = cleaned.replace(/(^|\s)--(opus|sonnet|haiku)(?=\s|$)/gi, " ");
  cleaned = cleaned.replace(/\bmodel\s*=\s*[A-Za-z0-9._-]+\b/gi, "");

  const knownFlags = new Set([
    "amp", "claude", "claude-code", "codex", "pi", "pi-mono",
    "opus", "sonnet", "haiku", "engine", "model",
  ]);
  const genericFlagRegex = /(^|\s)--([a-z][a-z0-9-]*)(?=\s|$)/gi;
  let genericMatch: RegExpExecArray | null;
  while ((genericMatch = genericFlagRegex.exec(cleaned)) !== null) {
    const flag = genericMatch[2];
    if (knownFlags.has(flag)) continue;
    harness = flag;
    harnessExplicit = true;
  }
  cleaned = cleaned.replace(genericFlagRegex, " ");

  cleaned = cleaned.replace(/\s+/g, " ").trim();
  return { harness, cleanedText: cleaned, harnessExplicit };
}

// ── API helpers ─────────────────────────────────────────────────────────────

async function* readSSEStream(
  res: Response,
): AsyncGenerator<CanonicalEvent, void, undefined> {
  if (!res.body) return;

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buf = "";

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buf += decoder.decode(value, { stream: true });

    while (buf.includes("\n\n")) {
      const boundary = buf.indexOf("\n\n");
      const raw = buf.slice(0, boundary);
      buf = buf.slice(boundary + 2);

      const dataLines = raw
        .split("\n")
        .filter((l) => l.startsWith("data:"))
        .map((l) => l.slice(5).trim());
      if (dataLines.length === 0) continue;
      const payload = dataLines.join("\n");
      if (payload === "[DONE]") return;

      try {
        yield JSON.parse(payload) as CanonicalEvent;
      } catch {
        // skip unparseable
      }
    }
  }
}

async function* executeSSE(
  threadKey: string,
  message: string | ContentBlock[],
  harness: Harness,
  options?: { platform?: string; userId?: string },
): AsyncGenerator<CanonicalEvent, void, undefined> {
  const maxBusyRetries = 4;

  for (let attempt = 1; attempt <= maxBusyRetries; attempt++) {
    log.info("sse_connect", { thread_key: threadKey, harness });

    const body: Record<string, unknown> = { thread_key: threadKey, message, harness };
    if (options?.platform) body.platform = options.platform;
    if (options?.userId) body.user_id = options.userId;

    const res = await apiFetch(`${API_URL}/agent/execute`, {
      method: "POST",
      body: JSON.stringify(body),
      headers: { "X-Trace-Id": threadKey },
      timeoutMs: 10 * 60_000,
      maxAttempts: 1,
      stream: true,
    });

    if (!res.ok) {
      const text = await res.text().catch(() => "");
      const isBusy = text.toLowerCase().includes("already in progress");
      if (isBusy && attempt < maxBusyRetries) {
        await new Promise((r) => setTimeout(r, Math.min(300 * 2 ** (attempt - 1), 2500)));
        continue;
      }
      throw new ApiError(
        `/agent/execute failed (${res.status}): ${text.slice(0, 300)}`,
        res.status,
        res.status >= 500,
      );
    }

    log.info("sse_streaming", { thread_key: threadKey });
    yield* readSSEStream(res);
    return;
  }
}

async function fetchThreadHarness(threadKey: string): Promise<Harness | null> {
  try {
    const res = await apiFetch(
      `${API_URL}/agent/status?key=${encodeURIComponent(threadKey)}`,
      { timeoutMs: 5_000, maxAttempts: 1 },
    );
    if (!res.ok) return null;
    const data = (await res.json()) as Record<string, unknown>;
    return (data.harness as string) || null;
  } catch {
    return null;
  }
}

async function postContextMessage(
  threadKey: string,
  text: string,
  options?: {
    source?: string;
    userId?: string;
    messageId?: string;
    slackTs?: string;
    attachments?: Array<{ url: string; name: string; mimeType?: string }>;
  },
): Promise<void> {
  const metadata: Record<string, unknown> = {};
  if (options?.source) metadata.source = options.source;
  if (options?.userId) metadata.user_id = options.userId;
  if (options?.attachments?.length) metadata.attachments = options.attachments;
  if (options?.messageId) metadata.message_id = options.messageId;
  if (options?.slackTs) metadata.slack_ts = options.slackTs;

  const res = await apiFetch(`${API_URL}/agent/messages`, {
    method: "POST",
    body: JSON.stringify({
      thread_key: threadKey,
      messages: [{ role: "user", parts: [{ type: "text", text }], user_id: options?.userId, metadata }],
    }),
    timeoutMs: 10_000,
  });
  if (!res.ok) {
    const errText = await res.text().catch(() => "");
    throw new ApiError(`/agent/messages failed (${res.status}): ${errText.slice(0, 300)}`, res.status, res.status >= 500);
  }
}

async function pollForLastResult(threadKey: string, maxWaitMs = 5 * 60_000): Promise<string> {
  const deadline = Date.now() + maxWaitMs;
  while (Date.now() < deadline) {
    try {
      const res = await apiFetch(
        `${API_URL}/agent/status?key=${encodeURIComponent(threadKey)}`,
        { timeoutMs: 10_000, maxAttempts: 1 },
      );
      if (res.ok) {
        const data = (await res.json()) as Record<string, unknown>;
        if (!data.busy) {
          const result = data.last_result;
          if (typeof result === "string" && result.trim()) return result.trim();
          return "";
        }
      }
    } catch {
      // keep polling
    }
    await new Promise((r) => setTimeout(r, 3_000));
  }
  return "";
}

// ── Attachments ─────────────────────────────────────────────────────────────

async function resolveAttachments(
  attachments: Array<{ url?: string; name?: string; mimeType?: string; fetchData?: () => Promise<Buffer> }>,
): Promise<ContentBlock[]> {
  const blocks: ContentBlock[] = [];
  for (const att of attachments) {
    if (!att.fetchData || !att.mimeType) continue;
    try {
      const data = await att.fetchData();
      const b64 = data.toString("base64");
      const base = { source: { type: "base64" as const, media_type: att.mimeType, data: b64 } };
      blocks.push(
        att.mimeType.startsWith("image/")
          ? { type: "image", ...base, ...(att.name ? { name: att.name } : {}) } as ContentBlock
          : { type: "document", ...base, ...(att.name ? { name: att.name } : {}) } as ContentBlock,
      );
    } catch (err) {
      log.warn("attachment_fetch_failed", {
        name: att.name || "unknown",
        error: err instanceof Error ? err.message : String(err),
      });
    }
  }
  return blocks;
}

// ── Formatting ──────────────────────────────────────────────────────────────

const LOW_VALUE_RE = [
  /^i('ve| have) (handed off|delegated)/i,
  /^(handing off|delegating)/i,
  /^continuing in/i,
];

function isLowValue(text: string): boolean {
  return !text || LOW_VALUE_RE.some((p) => p.test(text.trim()));
}

function formatFinal(text: string, harness: string, tracker: ProgressTracker, startTime: number): string {
  const dur = (Date.now() - startTime) / 1000;
  const durStr = dur < 10 ? `${dur.toFixed(1)}s` : `${Math.round(dur)}s`;
  const hLabel = tracker.agentThreadId
    ? `[${harness}](https://ampcode.com/threads/${tracker.agentThreadId})`
    : harness;
  const meta = [process.env.APP_NAME || "Centaur", hLabel, durStr].filter(Boolean);
  return `_${meta.join(" · ")}_\n\n${text}`;
}

function formatErrorForSlack(error: unknown, context: string): string {
  if (error instanceof ApiError) {
    if (error.retryable && error.status === null) {
      return `${context}: API is unreachable. The service may be restarting — try again in ~30s.`;
    }
    if (error.status && error.status >= 500) {
      return `${context}: API returned ${error.status}. Try again shortly.`;
    }
    return `${context}: ${error.message}`;
  }
  if (error instanceof Error) return `${context}: ${error.message}`;
  return `${context}: unknown error`;
}

// ── Streaming ───────────────────────────────────────────────────────────────

async function* streamTurn(
  threadKey: string,
  message: string | ContentBlock[],
  harness: Harness,
  tracker: ProgressTracker,
  userId?: string,
): AsyncGenerator<StreamChunk> {
  if (THREAD_VIEWER_URL) {
    yield { type: "markdown_text", text: `[Thread Viewer](${THREAD_VIEWER_URL}/${encodeURIComponent(threadKey)})` };
  }
  yield { type: "task_update", id: "init", title: "Starting…", status: "in_progress" };

  const stream = executeSSE(threadKey, message, harness, { platform: "slack", userId });
  let keepaliveId = 0;

  while (true) {
    const nextP = stream.next();
    const winner = await Promise.race([
      nextP.then((r) => ({ kind: "event" as const, result: r })),
      new Promise<{ kind: "keepalive" }>((resolve) =>
        setTimeout(() => resolve({ kind: "keepalive" }), KEEPALIVE_MS),
      ),
    ]);

    let result: IteratorResult<CanonicalEvent>;
    if (winner.kind === "keepalive") {
      yield { type: "task_update", id: `keepalive-${keepaliveId++}`, title: "Working…", status: "in_progress" };
      result = await nextP;
    } else {
      result = winner.result;
    }

    if (result.done) break;

    if (tracker.update(result.value)) {
      for (const chunk of tracker.pendingChunks()) yield chunk;
    }
  }

  if (!tracker.initCompleted) {
    yield { type: "task_update", id: "init", title: "Started", status: "complete" };
  }
}

// ── Message handler ─────────────────────────────────────────────────────────

async function handleMessage(
  bot: Chat,
  thread: Thread,
  messageText: string,
  isFirstMessage: boolean,
  attachments: Array<{ url?: string; name?: string; mimeType?: string; fetchData?: () => Promise<Buffer> }>,
  userId?: string,
  slackTs?: string,
) {
  const rawThreadId = thread.id;
  const threadKey = normalizeThreadKey(rawThreadId);

  let activeHarness: Harness | null = null;
  if (!isFirstMessage) {
    try {
      activeHarness = await fetchThreadHarness(threadKey);
    } catch (error) {
      log.warn("thread_harness_recovery_failed", {
        thread: threadKey,
        error: error instanceof Error ? error.message : String(error),
      });
    }
  }

  const parsed = extractRunOptions(messageText);
  const harness: Harness = isFirstMessage ? parsed.harness : (activeHarness ?? parsed.harness);

  log.info("message_received", {
    thread_key: threadKey,
    harness,
    is_first_message: isFirstMessage,
    has_attachments: Boolean(attachments.length),
    user_id: userId,
  });

  if (!isFirstMessage && !activeHarness && !parsed.harnessExplicit) {
    await thread.post(
      "I could not recover the active harness for this thread. Please retry with an explicit harness flag (for example `--legal`).",
    );
    return;
  }
  if (!isFirstMessage && activeHarness && parsed.harnessExplicit && parsed.harness !== activeHarness) {
    await thread.post("This thread is already running with a different harness. Start a new thread to switch.");
    return;
  }
  if (!parsed.cleanedText) {
    await thread.post("Please provide a prompt after flags. Example: `--amp build me a dashboard`.");
    return;
  }

  const contentBlocks = await resolveAttachments(attachments);
  const message: string | ContentBlock[] = contentBlocks.length > 0
    ? [{ type: "text" as const, text: parsed.cleanedText }, ...contentBlocks]
    : parsed.cleanedText;

  const tracker = new ProgressTracker();
  const startTime = Date.now();
  log.info("execute_start", { thread_key: threadKey, harness });

  try {
    let sentMessage: Awaited<ReturnType<typeof thread.post>> | null = null;
    try {
      sentMessage = await thread.post(
        streamTurn(threadKey, message, harness, tracker, userId),
      );
    } catch (streamErr) {
      const errMsg = streamErr instanceof Error ? streamErr.message : String(streamErr);
      if (errMsg.includes("message_not_in_streaming_state")) {
        log.warn("slack_stream_expired", { thread_key: threadKey });
        const fallback = await pollForLastResult(threadKey);
        if (fallback && !isLowValue(fallback)) {
          await thread.post({ markdown: fallback });
        } else if (THREAD_VIEWER_URL) {
          await thread.post({ markdown: `Agent completed. [View full output](${THREAD_VIEWER_URL}/${encodeURIComponent(threadKey)})` });
        }
        return;
      }
      throw streamErr;
    }

    const finalText = (tracker.resultText || tracker.lastAssistantText).trim();
    const durationS = (Date.now() - startTime) / 1000;
    log.info("execute_complete", {
      thread_key: threadKey,
      harness,
      duration_s: Math.round(durationS * 10) / 10,
      result_length: finalText.length,
    });

    if (finalText && !isLowValue(finalText)) {
      try {
        const editParts = [formatFinal(finalText, harness, tracker, startTime)];
        if (THREAD_VIEWER_URL) {
          editParts.push(`\n\n[Thread Viewer](${THREAD_VIEWER_URL}/${encodeURIComponent(threadKey)})`);
        }
        await sentMessage!.edit({ markdown: editParts.join("") });
      } catch {
        // best-effort — streamed message already has the final text
      }
    }

    if (finalText) {
      try {
        const slack = bot.getAdapter("slack") as SlackAdapter;
        const { channel, threadTs } = splitThreadKey(rawThreadId);
        await slack.setAssistantTitle(channel, threadTs, finalText.slice(0, 60));
      } catch {
        // best-effort — only works in assistant threads (DMs)
      }
    }
  } catch (error) {
    log.error("execute_error", { thread_key: threadKey, error: error instanceof Error ? error.message : String(error) });
    await thread.post(async function* () {
      yield { type: "task_update" as const, id: "init", title: "Failed", status: "error" as const };
      yield { type: "markdown_text" as const, text: formatErrorForSlack(error, "Agent request failed") };
    }());
  }
}

// ── Bot setup ───────────────────────────────────────────────────────────────

export function getSlackBootstrapState(): { ready: boolean; missingEnvKeys: string[] } {
  const required = ["SLACK_BOT_TOKEN", "SLACK_SIGNING_SECRET"] as const;
  const missingEnvKeys = required.filter((k) => !process.env[k]?.trim());
  return { ready: missingEnvKeys.length === 0, missingEnvKeys: [...missingEnvKeys] };
}

function messageIdentifier(message: { ts?: string; userId?: string; text?: string; threadId?: string }): string {
  const ts = String(message.ts || "").trim();
  if (ts) return ts;
  return crypto.createHash("sha1").update(`${message.threadId || ""}:${message.userId || ""}:${message.text || ""}`).digest("hex");
}

function createBot() {
  const hasSlackCreds = Boolean(process.env.SLACK_BOT_TOKEN && process.env.SLACK_SIGNING_SECRET);

  const bot = new Chat({
    userName: SLACK_BOT_USERNAME,
    adapters: hasSlackCreds ? { slack: createSlackAdapter() } : {},
    state: createPostgresState({ client: getPool() }),
    onLockConflict: "force",
  } as ConstructorParameters<typeof Chat>[0]);

  // ── Mentions ────────────────────────────────────────────────────────────

  bot.onNewMention(async (thread, message) => {
    if (message.author.isMe || message.author.isBot) return;
    await thread.subscribe();

    let attachments = message.attachments ? [...message.attachments] : [];
    const mentionTs = (message as { ts?: string }).ts || "";

    if (attachments.length === 0 && mentionTs) {
      try {
        const slack = bot.getAdapter("slack") as SlackAdapter;
        const refetched = await slack.fetchMessage(thread.id, mentionTs);
        if (refetched?.attachments?.length) {
          attachments = [...refetched.attachments];
          log.info("mention_files_refetched", { thread: thread.id, count: attachments.length });
        }
      } catch (err) {
        log.warn("mention_files_refetch_failed", {
          thread: thread.id,
          error: err instanceof Error ? err.message : String(err),
        });
      }
    }

    await handleMessage(bot, thread, message.text, true, attachments, message.author.userId, mentionTs);
  });

  // ── Subscribed messages ─────────────────────────────────────────────────

  bot.onSubscribedMessage(async (thread, message) => {
    if (message.author.isMe || message.author.isBot) return;

    if (message.isMention) {
      const subTs = (message as { ts?: string }).ts || "";
      await handleMessage(bot, thread, message.text, false, message.attachments || [], message.author.userId, subTs);
      return;
    }

    const text = (message.text || "").trim();
    const threadKey = normalizeThreadKey(thread.id);
    const rawAttachments = message.attachments || [];
    const files = rawAttachments
      .filter((a) => !!a.url && !!a.name)
      .map((a) => ({ url: a.url!, name: a.name!, mimeType: a.mimeType }));
    if (!text && files.length === 0) return;

    const mid = messageIdentifier({
      ts: (message as { ts?: string }).ts || (message as { id?: string }).id,
      userId: message.author.userId,
      text,
      threadId: thread.id,
    });
    const slackTs = (message as { ts?: string }).ts || "";

    try {
      await postContextMessage(threadKey, text || "Shared attachment in thread.", {
        source: "slack_subscribed_message",
        userId: message.author.userId,
        messageId: mid,
        slackTs,
        attachments: files.length > 0 ? files : undefined,
      });
    } catch (error) {
      log.warn("thread_context_post_failed", {
        thread: threadKey,
        error: error instanceof Error ? error.message : String(error),
      });
    }
  });

  // ── Orphan recovery ─────────────────────────────────────────────────────

  async function checkOrphanedCompletions() {
    if (!hasSlackCreds) return;
    try {
      const res = await apiFetch(`${API_URL}/agent/orphaned?max_age_s=300`, {
        timeoutMs: 10_000,
        maxAttempts: 1,
      });
      if (!res.ok) return;
      const orphans = (await res.json()) as Array<{ thread_key: string; text: string; updated_at: string | null }>;
      if (orphans.length === 0) return;
      log.info("orphan_check_found", { count: orphans.length });

      const slack = bot.getAdapter("slack") as SlackAdapter;

      for (const orphan of orphans) {
        if (!orphan.text) continue;
        let channel: string, threadTs: string;
        try {
          ({ channel, threadTs } = splitThreadKey(orphan.thread_key));
        } catch {
          continue;
        }
        if (!/^[CDG]/.test(channel)) continue;

        try {
          const claimRes = await apiFetch(`${API_URL}/agent/claim-delivery`, {
            method: "POST",
            body: JSON.stringify({ thread_key: orphan.thread_key }),
            maxAttempts: 1,
          });
          if (!claimRes.ok) continue;
          const { claimed } = (await claimRes.json()) as { claimed: boolean };
          if (!claimed) continue;
        } catch {
          continue;
        }

        try {
          await slack.postMessage(`slack:${channel}:${threadTs}`, orphan.text);
          log.info("orphan_delivered", { thread_key: orphan.thread_key });
          await apiFetch(`${API_URL}/agent/mark-delivered`, {
            method: "POST",
            body: JSON.stringify({ thread_key: orphan.thread_key }),
          });
        } catch (err) {
          log.warn("orphan_delivery_failed", {
            thread_key: orphan.thread_key,
            error: err instanceof Error ? err.message : String(err),
          });
        }
      }
    } catch (err) {
      log.warn("orphan_check_failed", { error: err instanceof Error ? err.message : String(err) });
    }
  }

  setTimeout(checkOrphanedCompletions, 10_000);
  setInterval(checkOrphanedCompletions, 60_000);

  return bot;
}

let _bot: ReturnType<typeof createBot> | null = null;
export function getBot() {
  if (!_bot) _bot = createBot();
  return _bot;
}
