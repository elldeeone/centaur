/**
 * Chat SDK bot — Slack adapter with Redis state.
 *
 * On @mention, spawns a local agent harness CLI (amp/codex/claude-code),
 * streams JSON events from stdout, and posts text back to Slack via
 * Chat SDK's native streaming.
 *
 * Thread continuity: stores amp thread IDs in Redis (via Chat SDK state)
 * so follow-up messages resume the same agent thread.
 */

import { Chat } from "chat";
import { createSlackAdapter } from "@chat-adapter/slack";
import { createRedisState } from "@chat-adapter/state-redis";
import { createMemoryState } from "@chat-adapter/state-memory";
import { extractHarness, executeMessage, type Harness } from "./harness";

// Thread ID storage — maps "channel:thread_ts" → { agentThreadId, harness }
const threadStore = new Map<string, { agentThreadId: string; harness: Harness }>();

function createBot() {
  const hasSlackCreds = process.env.SLACK_BOT_TOKEN && process.env.SLACK_SIGNING_SECRET;

  const bot = new Chat({
    userName: "tempo-ai",
    adapters: hasSlackCreds ? { slack: createSlackAdapter() } : {},
    state: process.env.REDIS_URL ? createRedisState() : createMemoryState(),
  });

  async function handleMessage(
    thread: Parameters<Parameters<typeof bot.onNewMention>[0]>[0],
    messageText: string,
    isNewThread: boolean
  ) {
    // Extract harness directive (e.g. "harness=codex fix the bug")
    const { harness, cleanedText } = extractHarness(messageText);

    // Look up existing agent thread ID for continuity
    const threadId = thread.id;
    const existing = threadStore.get(threadId);
    const agentThreadId = existing?.agentThreadId ?? null;
    const activeHarness = existing?.harness ?? harness;

    await thread.startTyping("Running...");

    const { textStream, threadIdPromise } = executeMessage(
      activeHarness,
      cleanedText,
      agentThreadId
    );

    // Post the stream — Chat SDK handles native Slack streaming
    await thread.post(textStream);

    // Store the agent thread ID for follow-ups
    const newThreadId = await threadIdPromise;
    if (newThreadId) {
      threadStore.set(threadId, { agentThreadId: newThreadId, harness: activeHarness });
    }
  }

  // First @mention — subscribe and run
  bot.onNewMention(async (thread, message) => {
    await thread.subscribe();
    await handleMessage(thread, message.text, true);
  });

  // Follow-up messages in subscribed threads
  bot.onSubscribedMessage(async (thread, message) => {
    if (!message.isMention) return;
    await handleMessage(thread, message.text, false);
  });

  return bot;
}

let _bot: ReturnType<typeof createBot> | null = null;
export function getBot() {
  if (!_bot) _bot = createBot();
  return _bot;
}
