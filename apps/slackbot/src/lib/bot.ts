import crypto from "node:crypto";
import { Chat, parseMarkdown, type Root } from "chat";
import { createSlackAdapter } from "@chat-adapter/slack";
import { createRedisState } from "@chat-adapter/state-redis";
import { createMemoryState } from "@chat-adapter/state-memory";
import {
  execute,
  extractRunOptions,
  interrupt,
  normalizeThreadKey,
  postThreadContextMessage,
  replyEngineerFlow,
  spawn,
  startEngineerFlow,
  watchProgress,
  type AgentMode,
  type BudgetMode,
  type FileAttachment,
  type Harness,
} from "./harness";
import { ApiError } from "./api-client";
import { truncateSlackText } from "./slack-text";

function formatErrorForSlack(error: unknown, context: string): string {
  if (error instanceof ApiError) {
    if (error.retryable && error.status === null) {
      return `${context}: API is unreachable (retried ${RETRY_DEFAULTS_MAX} times). The service may be restarting — try again in ~30s.`;
    }
    if (error.status && error.status >= 500) {
      return `${context}: API returned ${error.status}. The service may be overloaded — try again shortly.`;
    }
    return `${context}: ${error.message}`;
  }
  if (error instanceof Error) {
    return `${context}: ${error.message}`;
  }
  return `${context}: unknown error`;
}

const RETRY_DEFAULTS_MAX = 4;

const THREAD_VIEWER_URL = process.env.THREAD_VIEWER_URL || "https://svc-ai.paradigm.xyz";
const MAX_TRACKED_THREAD_MODES = 500;
const SLACK_BOT_USERNAME = process.env.SLACK_BOT_USERNAME || "paradigm-ai";

type MarkdownNode = Root | Root["children"][number];
type ThreadModeConfig = {
  mode: AgentMode;
  modelPreference: string | null;
  budgetMode: BudgetMode | null;
};

const HARNESSES: readonly Harness[] = ["amp", "claude-code", "codex", "pi-mono"] as const;

function isHarness(value: string | null | undefined): value is Harness {
  return HARNESSES.includes((value ?? "") as Harness);
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function messageIdentifier(message: {
  ts?: string;
  userId?: string;
  text?: string;
  threadId?: string;
}): string {
  const ts = String(message.ts || "").trim();
  if (ts) return ts;
  const raw = `${message.threadId || ""}:${message.userId || ""}:${message.text || ""}`;
  return crypto.createHash("sha1").update(raw).digest("hex");
}

function isBusyRunError(message: string): boolean {
  const normalized = message.toLowerCase();
  return normalized.includes("already in progress") || normalized.includes("run is already in progress");
}

/**
 * Convert Slack-style links to markdown before AST parsing.
 * LLMs sometimes output `<url|text>` or `&lt;url|text&gt;` despite being
 * told to use markdown — this catches those and converts them so
 * the markdown parser produces proper link nodes.
 */
function preprocessSlackLinks(text: string): string {
  let result = text;
  // HTML-encoded Slack links: &lt;https://...|text&gt; → [text](url)
  result = result.replace(/&lt;(https?:\/\/[^|&]+)\|([^&]+)&gt;/g, "[$2]($1)");
  // Raw Slack links: <https://...|text> → [text](url)
  // Only matches URLs (not Slack mentions like <@U...> or <#C...>)
  result = result.replace(/<(https?:\/\/[^|>]+)\|([^>]+)>/g, "[$2]($1)");
  return result;
}

function renderSlackMessage(markdown: string) {
  const ast = parseMarkdown(preprocessSlackLinks(markdown));
  const escapeLiteralTildes = (
    node: MarkdownNode,
    inDelete = false
  ): void => {
    const insideDelete = inDelete || node.type === "delete";

    if (node.type === "text" && !insideDelete) {
      // Slack treats paired single tildes as strikethrough; escape literal tildes.
      node.value = node.value.replace(/~/g, "\\~");
    }

    if ("children" in node && Array.isArray(node.children)) {
      for (const child of node.children as Root["children"]) {
        escapeLiteralTildes(child, insideDelete);
      }
    }
  };

  escapeLiteralTildes(ast);

  return { ast };
}

function toSlackMessage(markdown: string) {
  return renderSlackMessage(truncateSlackText(markdown));
}

function createBot() {
  const hasSlackCreds =
    process.env.SLACK_BOT_TOKEN && process.env.SLACK_SIGNING_SECRET;

  const bot = new Chat({
    userName: SLACK_BOT_USERNAME,
    adapters: hasSlackCreds ? { slack: createSlackAdapter() } : {},
    state: process.env.REDIS_URL ? createRedisState() : createMemoryState(),
  });
  const threadModes = new Map<string, ThreadModeConfig>();

  function setThreadMode(threadKey: string, config: ThreadModeConfig): void {
    if (threadModes.has(threadKey)) {
      threadModes.delete(threadKey);
    }
    if (!threadModes.has(threadKey) && threadModes.size >= MAX_TRACKED_THREAD_MODES) {
      const oldestKey = threadModes.keys().next().value as string | undefined;
      if (oldestKey) threadModes.delete(oldestKey);
    }
    threadModes.set(threadKey, config);
  }

  function buildSessionContext(threadId: string): string {
    const now = new Date().toISOString().replace("T", " ").slice(0, 19);
    return [
      "# Session Context",
      "",
      `- **Date/Time**: ${now} UTC`,
      `- **Thread ID**: ${threadId}`,
      `- **Platform**: Slack`,
      "",
      "## Slack Formatting Rules",
      "",
      "- Use standard markdown links `[Display Text](URL)` for all hyperlinks — they are auto-converted to Slack format",
      "- Do NOT use Slack-native `<URL|text>` link syntax — it breaks the rendering pipeline",
      "- Preserve Slack user mentions (`<@UXXXXXXX>`) exactly as-is — only use these for actual Slack users",
      "- For Twitter/X handles, always link to the profile: `[@handle](https://x.com/handle)` — bare @handle gets auto-converted to a broken Slack mention",
      "- Slack enforces a 4,000 character limit per message — split long responses across multiple messages or summarize",
      "- Use Slack Block Kit formatting for tables, not markdown or ASCII",
      "- After completing a long task, tag the requester with `@username`",
      "",
      "---",
      "",
    ].join("\n");
  }

  async function handleMessage(
    thread: Parameters<Parameters<typeof bot.onNewMention>[0]>[0],
    messageText: string,
    isFirstMessage: boolean,
    attachments?: Array<{ url?: string; name?: string }>,
    userId?: string,
  ) {
    const parsed = extractRunOptions(messageText);
    const requestId = crypto.randomUUID().slice(0, 8);
    const rawThreadKey = thread.id;
    const threadKey = normalizeThreadKey(rawThreadKey);
    const previous = threadModes.get(threadKey);
    const files: FileAttachment[] = (attachments || [])
      .filter((a): a is { url: string; name: string } => !!a.url && !!a.name)
      .map((a) => ({ url: a.url, name: a.name }));

    const ampEngRequested =
      parsed.mode === "eng" &&
      (parsed.modelPreference === "amp" || parsed.harness === "amp");
    const requestedMode: AgentMode = ampEngRequested ? "default" : parsed.mode;
    const mode: AgentMode = isFirstMessage
      ? requestedMode
      : (previous?.mode ?? requestedMode);

    if (
      !isFirstMessage &&
      previous &&
      parsed.modeExplicit &&
      requestedMode !== previous.mode
    ) {
      await thread.post(
        toSlackMessage(
          "This thread is already running in a different mode. Start a new thread to switch modes."
        )
      );
      return;
    }

    if (ampEngRequested && isFirstMessage) {
      await thread.post(
        toSlackMessage(
          "Routing `--eng --amp` through standard `--amp` mode for reliability."
        )
      );
    }

    if (!parsed.cleanedText) {
      await thread.post(
        toSlackMessage(
          "Please provide a prompt after flags. Example: `--eng --claude implement retry logic` (after mentioning the bot)."
        )
      );
      return;
    }

    // Recovery path: after bot restarts we may lose in-memory mode state,
    // so probe the API for an active engineer session before default routing.
    if (
      !isFirstMessage &&
      !previous &&
      !parsed.modeExplicit &&
      !parsed.harnessExplicit &&
      !parsed.budgetExplicit
    ) {
      try {
        const reply = await replyEngineerFlow(threadKey, parsed.cleanedText);
        if (reply.status === "accepted") {
          setThreadMode(threadKey, {
            mode: "eng",
            modelPreference: null,
            budgetMode: null,
          });
          return;
        }
      } catch (error) {
        console.warn("engineer_recovery_probe_failed", {
          thread: threadKey,
          error: error instanceof Error ? error.message : String(error),
        });
        await thread.post(
          toSlackMessage(
            "Couldn't verify the existing engineer session right now. Please retry in this thread."
          )
        );
        return;
      }
    }

    if (mode === "eng") {
      const modelPreference =
        parsed.modelPreference ?? parsed.harness ?? previous?.modelPreference ?? null;
      const budgetMode = parsed.budgetMode ?? previous?.budgetMode ?? null;

      try {
        if (isFirstMessage) {
          await thread.startTyping("Starting engineer flow...");
          const result = await startEngineerFlow(
            threadKey,
            parsed.cleanedText,
            modelPreference,
            budgetMode,
            files.length > 0 ? files : undefined
          );
          const viewerUrl = `${THREAD_VIEWER_URL}/threads/${encodeURIComponent(normalizeThreadKey(threadKey))}`;
          const preferenceLine = modelPreference
            ? `\nModel preference: \`${modelPreference}\``
            : "";
          const modeLine = budgetMode ? `\nMode: \`${budgetMode}\`` : "";
          const statusLine = (() => {
            if (result.status === "already_running") {
              setThreadMode(threadKey, { mode: "eng", modelPreference, budgetMode });
              return "Engineer flow is already running for this thread.";
            }
            if (result.status === "rejected") {
              return (
                result.error ??
                "Engineer flow could not start because another harness session is active in this thread."
              );
            }
            setThreadMode(threadKey, { mode: "eng", modelPreference, budgetMode });
            return "Engineer flow started.";
          })();
          await thread.post(
            toSlackMessage(
              `[🔗 Thread Viewer](${viewerUrl})\n\n${statusLine}${preferenceLine}${modeLine}`
            )
          );
          return;
        }

        const reply = await replyEngineerFlow(
          threadKey,
          parsed.cleanedText,
          files.length > 0 ? files : undefined
        );
        if (reply.status === "no_active_session") {
          threadModes.delete(threadKey);
          await thread.post(
            toSlackMessage(
              "No active engineer session for this thread. Start a new run with `--eng`."
            )
          );
        } else if (reply.status === "not_waiting_for_reply") {
          await thread.post(
            toSlackMessage("Engineer is not currently waiting for a reply.")
          );
        } else if (reply.status === "accepted") {
          setThreadMode(threadKey, { mode: "eng", modelPreference, budgetMode });
        }
        return;
      } catch (error) {
        await thread.post(
          toSlackMessage(formatErrorForSlack(error, "Engineer flow request failed"))
        );
        return;
      }
    }

    const previousHarness =
      previous?.mode === "default" && isHarness(previous.modelPreference)
        ? previous.modelPreference
        : null;
    const harness = parsed.harness ?? previousHarness ?? "amp";
    setThreadMode(threadKey, { mode: "default", modelPreference: harness, budgetMode: null });
    try {
      const instruction = parsed.cleanedText;
      if (!isFirstMessage) {
        try {
          await interrupt(threadKey, requestId);
        } catch (error) {
          console.warn("agent_interrupt_failed", {
            thread: threadKey,
            error: error instanceof Error ? error.message : String(error),
          });
        }
      }

      await thread.startTyping("Spawning agent...");
      await spawn(threadKey, harness, undefined, requestId);

      await thread.startTyping("Running...");
      const message = isFirstMessage
        ? buildSessionContext(threadKey) + instruction
        : instruction;

      const stopProgress = watchProgress(threadKey, (status) => {
        thread.startTyping(status).catch(() => {});
      });

      let result = "";
      try {
        const maxAttempts = 6;
        for (let attempt = 1; attempt <= maxAttempts; attempt += 1) {
          try {
            result = await execute(
              threadKey,
              message,
              harness,
              requestId,
              files.length > 0 ? files : undefined,
              userId,
              "slack",
            );
            break;
          } catch (error) {
            const detail = error instanceof Error ? error.message : String(error);
            const shouldRetry = isBusyRunError(detail) && attempt < maxAttempts;
            if (!shouldRetry) {
              throw error;
            }
            await sleep(Math.min(500 * Math.pow(2, attempt - 1), 5000));
          }
        }
      } finally {
        stopProgress();
      }
      let finalMessage = result;
      if (isFirstMessage) {
        const viewerUrl = `${THREAD_VIEWER_URL}/threads/${encodeURIComponent(normalizeThreadKey(threadKey))}`;
        finalMessage = `[🔗 Thread Viewer](${viewerUrl})\n\n` + finalMessage;
      }
      if (finalMessage.trim()) {
        await thread.post(toSlackMessage(finalMessage));
      }
    } catch (error) {
      await thread.post(
        toSlackMessage(formatErrorForSlack(error, "Agent request failed"))
      );
    }
  }

  bot.onNewMention(async (thread, message) => {
    if (message.author.isMe) return;
    if (message.author.isBot) return;
    await thread.subscribe();
    const attachments = message.attachments?.map((a) => ({ url: a.url, name: a.name }));
    await handleMessage(thread, message.text, true, attachments, message.author.userId);
  });

  bot.onSubscribedMessage(async (thread, message) => {
    if (message.author.isMe) return;
    if (message.author.isBot) return;
    const attachments = message.attachments?.map((a) => ({ url: a.url, name: a.name }));
    if (!message.isMention) {
      const text = (message.text || "").trim();
      const threadKey = normalizeThreadKey(thread.id);
      const knownMode = threadModes.get(threadKey)?.mode;
      const files: FileAttachment[] = (attachments || [])
        .filter((a): a is { url: string; name: string } => !!a.url && !!a.name)
        .map((a) => ({ url: a.url, name: a.name }));
      if (!text && files.length === 0) return;
      const messageId = messageIdentifier({
        ts: (message as { ts?: string }).ts || (message as { id?: string }).id,
        userId: message.author.userId,
        text,
        threadId: thread.id,
      });

      try {
        const reply = await replyEngineerFlow(
          threadKey,
          text,
          files.length > 0 ? files : undefined,
          {
            source: "slack_subscribed_message",
            userId: message.author.userId,
            messageId,
          },
        );
        if (reply.status === "accepted") return;
        if (reply.status === "not_waiting_for_reply") {
          if (knownMode === "eng") {
            await thread.post(
              toSlackMessage("Engineer is not currently waiting for a reply.")
            );
          }
          return;
        }
        if (reply.status === "no_active_session" && knownMode === "eng") {
          threadModes.delete(threadKey);
          await thread.post(
            toSlackMessage("No active engineer session for this thread. Start a new run with `--eng`.")
          );
        }
      } catch (error) {
        console.warn("engineer_plain_reply_failed", {
          thread: threadKey,
          error: error instanceof Error ? error.message : String(error),
        });
        if (knownMode === "eng") {
          await thread.post(
            toSlackMessage("Could not deliver your reply to engineer right now. Please retry.")
          );
        }
      }

      const contextText = text || "Shared attachment in thread.";
      try {
        await postThreadContextMessage(threadKey, contextText, {
          source: "slack_subscribed_message",
          userId: message.author.userId,
          messageId,
          attachments: files.length > 0 ? files : undefined,
        });
      } catch (error) {
        console.warn("thread_context_post_failed", {
          thread: threadKey,
          error: error instanceof Error ? error.message : String(error),
        });
      }
      return;
    }
    await handleMessage(thread, message.text, false, attachments, message.author.userId);
  });

  return bot;
}

let _bot: ReturnType<typeof createBot> | null = null;
export function getBot() {
  if (!_bot) _bot = createBot();
  return _bot;
}
