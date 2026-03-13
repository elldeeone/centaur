/**
 * Integration tests for SlackBot against a running Centaur API.
 *
 * Prerequisites:
 *   docker compose up -d postgres api
 *   docker compose build sandbox
 *   source .env
 *
 * Run:
 *   CENTAUR_API_URL=http://localhost:8000 API_SECRET_KEY=<key> pnpm vitest run test/integration.test.ts
 */
import { describe, it, expect, beforeAll } from "vitest";
import { SlackBot, type BotThread, type BotMessage } from "@/lib/bot/bot";
import { CentaurClient } from "@centaur/api-client";
import type { StreamChunk } from "chat";

// ── Config ────────────────────────────────────────────────────────────────

const API_URL = process.env.CENTAUR_API_URL || "http://localhost:8001";
const API_KEY = process.env.SLACKBOT_API_KEY || process.env.API_SECRET_KEY || "";
const RUN_INTEGRATION = Boolean(API_KEY);

// ── Mock BotThread ────────────────────────────────────────────────────────

function createMockThread(id: string) {
  const chunks: StreamChunk[] = [];
  let editMarkdown = "";
  let subscribed = false;
  let postCount = 0;

  const thread: BotThread = {
    id,
    async subscribe() {
      subscribed = true;
    },
    async post(content) {
      postCount++;
      if ("markdown" in content) {
        editMarkdown = content.markdown;
      } else {
        // Consume async generator
        for await (const chunk of content) {
          chunks.push(chunk);
        }
      }
      return {
        async edit(c: { markdown: string }) {
          editMarkdown = c.markdown;
        },
      };
    },
  };

  return {
    thread,
    get chunks() { return chunks; },
    get editMarkdown() { return editMarkdown; },
    get subscribed() { return subscribed; },
    get postCount() { return postCount; },
  };
}

// ── Mock BotMessage ───────────────────────────────────────────────────────

function userMessage(text: string, opts?: { isMention?: boolean }): BotMessage {
  return {
    text,
    isMention: opts?.isMention,
    author: { isMe: false, isBot: false, userId: "U-test" },
  };
}

function botMessage(text: string): BotMessage {
  return {
    text,
    author: { isMe: true, isBot: true },
  };
}

// ── Tests ─────────────────────────────────────────────────────────────────

describe.skipIf(!RUN_INTEGRATION)("SlackBot integration", () => {
  let bot: SlackBot;

  beforeAll(() => {
    const client = new CentaurClient({ apiUrl: API_URL, apiKey: API_KEY });
    bot = new SlackBot(client);
  });

  it("onNewMention: executes a turn and streams chunks", async () => {
    const threadKey = `slack:C-test:${Date.now()}.001`;
    const mock = createMockThread(threadKey);

    await bot.onNewMention(mock.thread, userMessage("What is 2 + 2?"));

    // Thread should have been subscribed
    expect(mock.subscribed).toBe(true);

    // Should have streamed chunks
    expect(mock.chunks.length).toBeGreaterThan(0);

    // Should start with the init task_update
    const initChunks = mock.chunks.filter(
      (c) => c.type === "task_update" && c.id === "init",
    );
    expect(initChunks.length).toBeGreaterThan(0);

    // Final edit should contain metadata + answer text
    expect(mock.editMarkdown).toBeTruthy();
    expect(mock.editMarkdown.length).toBeGreaterThan(10);

    // Should have been posted exactly once (the streaming post)
    expect(mock.postCount).toBe(1);
  }, 120_000);

  it("onNewMention: ignores bot's own messages", async () => {
    const mock = createMockThread("slack:C-test:ignore-self");

    await bot.onNewMention(mock.thread, botMessage("I said something"));

    expect(mock.subscribed).toBe(false);
    expect(mock.chunks).toHaveLength(0);
    expect(mock.postCount).toBe(0);
  });

  it("onSubscribedMessage: non-mention posts context without executing", async () => {
    const threadKey = `slack:C-test:${Date.now()}.002`;
    const mock = createMockThread(threadKey);

    // First create a session via a mention so the thread exists
    await bot.onNewMention(mock.thread, userMessage("Hello"));

    // Now send a non-mention follow-up — should post context, not execute
    const beforePostCount = mock.postCount;
    await bot.onSubscribedMessage(mock.thread, userMessage("some context info"));

    // postContext doesn't call thread.post, so postCount shouldn't change
    expect(mock.postCount).toBe(beforePostCount);
  }, 120_000);

  it("onSubscribedMessage: mention triggers a full turn", async () => {
    const threadKey = `slack:C-test:${Date.now()}.003`;
    const mock = createMockThread(threadKey);

    // Initial mention to create session
    await bot.onNewMention(mock.thread, userMessage("Hello"));
    const chunksAfterFirst = mock.chunks.length;

    // Follow-up mention in subscribed thread
    await bot.onSubscribedMessage(mock.thread, userMessage("Now what is 3 + 3?", { isMention: true }));

    // Should have streamed more chunks
    expect(mock.chunks.length).toBeGreaterThan(chunksAfterFirst);
  }, 120_000);

  it("onSubscribedMessage: ignores bot messages", async () => {
    const mock = createMockThread("slack:C-test:ignore-bot");

    await bot.onSubscribedMessage(mock.thread, botMessage("bot talking to itself"));

    expect(mock.chunks).toHaveLength(0);
    expect(mock.postCount).toBe(0);
  });

  it("onSubscribedMessage: ignores empty non-mention messages", async () => {
    const mock = createMockThread("slack:C-test:ignore-empty");

    await bot.onSubscribedMessage(mock.thread, userMessage(""));

    expect(mock.postCount).toBe(0);
  });
});
