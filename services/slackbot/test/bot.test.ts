import { beforeEach, describe, it, expect, vi } from "vitest";
import fs from "node:fs";
import path from "node:path";

import { ProgressTracker } from "../src/lib/bot/progress-tracker";
import {
  SlackBot,
  splitSlackMessage,
  parsePromptSelectorFlag,
  setPersonaRegistryForTest,
  resetPersonaRegistryForTest,
  extractFlagSelector,
  bareFlagPrompt,
  rewriteSlackFileLinks,
} from "../src/lib/bot/bot";
import { normalizeHarnessEvent, type CanonicalEvent } from "@centaur/harness-events";
import { CentaurClient } from "@centaur/api-client";
import { log } from "../src/lib/logger";
import { SlackApiCallError } from "../src/lib/slack/errors";

// ─────────────────────────────────────────────────────────────────────────────
// Helpers
// ─────────────────────────────────────────────────────────────────────────────

beforeEach(() => {
  setPersonaRegistryForTest({
    eng: {},
    invest: {},
  });
});

function finalMessage(t: ProgressTracker): string {
  return (t.resultText || t.lastAssistantText).trim();
}

// Mirror of the content block resolution logic in bot.ts — kept in sync.
type ContentBlock =
  | { type: "text"; text: string }
  | { type: "image"; source: { type: "base64"; media_type: string; data: string } }
  | { type: "document"; source: { type: "base64"; media_type: string; data: string } };

async function resolveAttachmentBlocks(
  attachments: Array<{ url?: string; name?: string; mimeType?: string; fetchData?: () => Promise<Buffer> }>,
): Promise<ContentBlock[]> {
  const blocks: ContentBlock[] = [];
  for (const att of attachments) {
    if (!att.fetchData || !att.mimeType) continue;
    try {
      const data = await att.fetchData();
      const b64 = data.toString("base64");
      if (att.mimeType.startsWith("image/")) {
        blocks.push({
          type: "image",
          source: { type: "base64", media_type: att.mimeType, data: b64 },
        });
      } else {
        blocks.push({
          type: "document",
          source: { type: "base64", media_type: att.mimeType, data: b64 },
        });
      }
    } catch {
      // skip failed fetches
    }
  }
  return blocks;
}

function parseSSEFile(filePath: string): Record<string, unknown>[] {
  const raw = fs.readFileSync(filePath, "utf-8");
  const events: Record<string, unknown>[] = [];
  for (const line of raw.split("\n")) {
    const trimmed = line.trim();
    if (!trimmed.startsWith("data: ")) continue;
    const payload = trimmed.slice(6);
    try {
      events.push(JSON.parse(payload));
    } catch {
      // skip
    }
  }
  return events;
}

function replayFixture(name: string) {
  const filePath = path.join(
    __dirname,
    "../src/lib/bot/fixtures",
    `${name}.sse`,
  );
  const rawEvents = parseSSEFile(filePath);
  const tracker = new ProgressTracker();
  const allCanonical: CanonicalEvent[] = [];
  const allChunks: unknown[] = [];
  let turnDoneResult = "";

  for (const raw of rawEvents) {
    if (raw.type === "turn.done") {
      turnDoneResult = typeof raw.result === "string" ? raw.result : "";
    }
    const canonical = normalizeHarnessEvent("amp", raw);
    for (const ce of canonical) {
      allCanonical.push(ce);
      allChunks.push(...tracker.update(ce));
    }
  }

  return { tracker, allCanonical, allChunks, rawEvents, turnDoneResult };
}

// ═══════════════════════════════════════════════════════════════════════════════
// 1. Attachment annotations
// ═══════════════════════════════════════════════════════════════════════════════

describe("attachment content blocks", () => {
  it("image mimeType → image content block", async () => {
    const blocks = await resolveAttachmentBlocks([
      {
        name: "screenshot.png",
        mimeType: "image/png",
        fetchData: async () => Buffer.from("fake-png-data"),
      },
    ]);
    expect(blocks).toHaveLength(1);
    expect(blocks[0].type).toBe("image");
    expect((blocks[0] as any).source.media_type).toBe("image/png");
    expect((blocks[0] as any).source.data).toBe(Buffer.from("fake-png-data").toString("base64"));
  });

  it("non-image mimeType → document content block", async () => {
    const blocks = await resolveAttachmentBlocks([
      {
        name: "report.pdf",
        mimeType: "application/pdf",
        fetchData: async () => Buffer.from("fake-pdf-data"),
      },
    ]);
    expect(blocks).toHaveLength(1);
    expect(blocks[0].type).toBe("document");
    expect((blocks[0] as any).source.media_type).toBe("application/pdf");
  });

  it("skips attachments without fetchData", async () => {
    const blocks = await resolveAttachmentBlocks([
      { url: "https://files.slack.com/a", name: "data.csv", mimeType: "text/csv" },
    ]);
    expect(blocks).toHaveLength(0);
  });

  it("skips attachments without mimeType", async () => {
    const blocks = await resolveAttachmentBlocks([
      { name: "mystery", fetchData: async () => Buffer.from("data") },
    ]);
    expect(blocks).toHaveLength(0);
  });

  it("mixed image and document attachments", async () => {
    const blocks = await resolveAttachmentBlocks([
      { name: "photo.jpg", mimeType: "image/jpeg", fetchData: async () => Buffer.from("jpg") },
      { name: "doc.xlsx", mimeType: "application/vnd.ms-excel", fetchData: async () => Buffer.from("xlsx") },
      { name: "chart.gif", mimeType: "image/gif", fetchData: async () => Buffer.from("gif") },
    ]);
    expect(blocks).toHaveLength(3);
    expect(blocks[0].type).toBe("image");
    expect(blocks[1].type).toBe("document");
    expect(blocks[2].type).toBe("image");
  });

  it("returns empty array for empty input", async () => {
    const blocks = await resolveAttachmentBlocks([]);
    expect(blocks).toHaveLength(0);
  });

  it("skips attachments where fetchData throws", async () => {
    const blocks = await resolveAttachmentBlocks([
      {
        name: "broken.pdf",
        mimeType: "application/pdf",
        fetchData: async () => { throw new Error("network error"); },
      },
      {
        name: "good.png",
        mimeType: "image/png",
        fetchData: async () => Buffer.from("png-data"),
      },
    ]);
    expect(blocks).toHaveLength(1);
    expect(blocks[0].type).toBe("image");
  });

  it("image/svg+xml counts as image", async () => {
    const blocks = await resolveAttachmentBlocks([
      { name: "logo.svg", mimeType: "image/svg+xml", fetchData: async () => Buffer.from("svg") },
    ]);
    expect(blocks).toHaveLength(1);
    expect(blocks[0].type).toBe("image");
  });

  it("video and audio are documents, not images", async () => {
    const blocks = await resolveAttachmentBlocks([
      { name: "clip.mp4", mimeType: "video/mp4", fetchData: async () => Buffer.from("mp4") },
      { name: "voice.ogg", mimeType: "audio/ogg", fetchData: async () => Buffer.from("ogg") },
    ]);
    expect(blocks).toHaveLength(2);
    expect(blocks[0].type).toBe("document");
    expect(blocks[1].type).toBe("document");
  });

  it("content blocks include correct base64 source structure", async () => {
    const blocks = await resolveAttachmentBlocks([
      {
        name: "test.pdf",
        mimeType: "application/pdf",
        fetchData: async () => Buffer.from("hello world"),
      },
    ]);
    expect(blocks).toHaveLength(1);
    const block = blocks[0] as { type: "document"; source: { type: string; media_type: string; data: string } };
    expect(block.source.type).toBe("base64");
    expect(block.source.media_type).toBe("application/pdf");
    expect(block.source.data).toBe(Buffer.from("hello world").toString("base64"));
  });
});

// ═══════════════════════════════════════════════════════════════════════════════
// 2. ProgressTracker
// ═══════════════════════════════════════════════════════════════════════════════

describe("ProgressTracker", () => {
  it("captures text-only assistant message as finalMessage", () => {
    const t = new ProgressTracker();
    [...t.update({
      type: "assistant",
      message: { content: [{ type: "text", text: "Here is your answer." }] },
    })];
    expect(finalMessage(t)).toBe("Here is your answer.");
  });

  it("last text event wins", () => {
    const t = new ProgressTracker();
    [...t.update({ type: "assistant", message: { content: [{ type: "text", text: "First." }] } })];
    [...t.update({ type: "assistant", message: { content: [{ type: "text", text: "Second." }] } })];
    expect(finalMessage(t)).toBe("Second.");
  });

  it("clears preamble when tool_use starts (separate events)", () => {
    const t = new ProgressTracker();
    [...t.update({ type: "assistant", message: { content: [{ type: "text", text: "Let me look..." }] } })];
    [...t.update({
      type: "assistant",
      message: { content: [{ type: "tool_use", id: "t1", name: "Read", input: { path: "/x" } }] },
    })];
    expect(finalMessage(t)).toBe("");
  });

  it("clears preamble when tool_use starts (same event)", () => {
    const t = new ProgressTracker();
    [...t.update({
      type: "assistant",
      message: {
        content: [
          { type: "text", text: "Let me search..." },
          { type: "tool_use", id: "t1", name: "finder", input: { query: "auth" } },
        ],
      },
    })];
    expect(finalMessage(t)).toBe("");
  });

  it("captures final text after tool completes", () => {
    const t = new ProgressTracker();
    [...t.update({
      type: "assistant",
      message: { content: [{ type: "tool_use", id: "t1", name: "Read", input: { path: "/x" } }] },
    })];
    [...t.update({ type: "tool", content: [{ tool_use_id: "t1", content: "data", is_error: false }] })];
    [...t.update({
      type: "assistant",
      message: { content: [{ type: "text", text: "Done fixing the bug." }] },
    })];
    expect(finalMessage(t)).toBe("Done fixing the bug.");
  });

  it("result event takes priority over lastAssistantText", () => {
    const t = new ProgressTracker();
    [...t.update({ type: "assistant", message: { content: [{ type: "text", text: "Intermediate." }] } })];
    [...t.update({ type: "result", text: "Final from turn.done" })];
    expect(finalMessage(t)).toBe("Final from turn.done");
  });

  it("stream death after tool_use → empty finalMessage", () => {
    const t = new ProgressTracker();
    [...t.update({ type: "assistant", message: { content: [{ type: "text", text: "Let me check..." }] } })];
    [...t.update({
      type: "assistant",
      message: { content: [{ type: "tool_use", id: "t1", name: "Read", input: {} }] },
    })];
    expect(finalMessage(t)).toBe("");
  });

  it("error event produces markdown_text chunk", () => {
    const t = new ProgressTracker();
    const chunks = [...t.update({ type: "error", error: "OOM killed" })];
    expect(chunks.some((c) => c.type === "markdown_text" && "text" in c && (c as any).text.includes("OOM"))).toBe(true);
  });

  it("reasoning event does not affect lastAssistantText", () => {
    const t = new ProgressTracker();
    [...t.update({ type: "reasoning", text: "Thinking hard..." })];
    expect(finalMessage(t)).toBe("");
  });

  it("each tool gets its own unique task ID (no sliding window)", () => {
    const t = new ProgressTracker();
    const starts: unknown[] = [];
    for (let i = 0; i < 5; i++) {
      const chunks = [...t.update({
        type: "assistant",
        message: { content: [{ type: "tool_use", id: `t${i}`, name: "Bash", input: { cmd: `echo ${i}` } }] },
      })];
      starts.push(...chunks.filter((c) => c.type === "task_update"));
      [...t.update({ type: "tool", content: [{ tool_use_id: `t${i}`, content: "ok", is_error: false }] })];
    }
    const ids = (starts as any[]).map((c) => c.id);
    expect(ids).toEqual(["t0", "t1", "t2", "t3", "t4"]);
  });

  it("10 tools produce 10 unique task IDs — Slack plan block handles display", () => {
    const t = new ProgressTracker();
    const allChunks: unknown[] = [];
    for (let i = 0; i < 10; i++) {
      allChunks.push(...t.update({
        type: "assistant",
        message: { content: [{ type: "tool_use", id: `t${i}`, name: "Read", input: { path: `/f${i}` } }] },
      }));
      allChunks.push(...t.update({ type: "tool", content: [{ tool_use_id: `t${i}`, content: "ok", is_error: false }] }));
    }
    const ids = new Set(
      (allChunks as any[])
        .filter((c) => c.type === "task_update")
        .map((c) => c.id),
    );
    expect(ids).toEqual(new Set(["t0", "t1", "t2", "t3", "t4", "t5", "t6", "t7", "t8", "t9"]));
  });

  it("subagent events produce task_update chunks", () => {
    const t = new ProgressTracker();
    const chunks = [...t.update({ type: "subagent", status: "started", subagent_id: "sa-1", name: "Research" })];
    expect(chunks.some((c) => c.type === "task_update" && (c as any).status === "in_progress")).toBe(true);
    expect(t.lastAssistantText).toBe("");
  });

  it("addHandoff clears state and produces task_update", () => {
    const t = new ProgressTracker();
    [...t.update({ type: "assistant", message: { content: [{ type: "text", text: "intermediate" }] } })];
    const chunks = [...t.addHandoff("Continue research")];
    expect(t.lastAssistantText).toBe("");
    expect(t.resultText).toBe("");
    expect(chunks.some((c) => c.type === "task_update" && (c as any).title.includes("Continue research"))).toBe(true);
  });
});



// ═══════════════════════════════════════════════════════════════════════════════
// 3. SSE fixture replay
// ═══════════════════════════════════════════════════════════════════════════════

const fixtureDir = path.join(__dirname, "../src/lib/bot/fixtures");
const fixtureFiles = fs.readdirSync(fixtureDir).filter((f) => f.endsWith(".sse"));

describe("SSE fixture replay", () => {
  for (const file of fixtureFiles) {
    const name = file.replace(".sse", "");

    describe(name, () => {
      const { tracker, allCanonical, allChunks, rawEvents, turnDoneResult } =
        replayFixture(name);
      const fm = finalMessage(tracker);

      it("parses raw SSE events", () => {
        expect(rawEvents.length).toBeGreaterThan(0);
      });

      it("produces canonical events", () => {
        expect(allCanonical.length).toBeGreaterThan(0);
      });

      const toolUseEvents = allCanonical.filter(
        (e) =>
          e.type === "assistant" &&
          e.message?.content.some((b: { type: string }) => b.type === "tool_use"),
      );

      if (toolUseEvents.length > 0) {
        it("tool_use events produce task_update chunks", () => {
          const taskUpdates = (allChunks as any[]).filter(
            (c) => c.type === "task_update" && c.id !== "init",
          );
          expect(taskUpdates.length).toBeGreaterThan(0);
        });
      }

      if (turnDoneResult) {
        it("finalMessage matches turn.done result", () => {
          expect(fm).toBe(turnDoneResult);
        });

        it("no dangling active tools at end", () => {
          const activeTools = (tracker as any).activeTools as Map<string, unknown>;
          expect(activeTools.size).toBe(0);
        });
      }

      if (name !== "handoff" && turnDoneResult) {
        it("finalMessage is non-empty", () => {
          expect(fm.length).toBeGreaterThan(0);
        });
      }

      if (toolUseEvents.length > 0 && fm) {
        it("finalMessage is NOT preamble text", () => {
          let firstTextBeforeTool = "";
          let seenToolUse = false;
          for (const ce of allCanonical) {
            if (ce.type === "assistant" && ce.message?.content) {
              for (const block of ce.message.content) {
                if (block.type === "text" && block.text && !seenToolUse) {
                  firstTextBeforeTool = block.text;
                }
                if (block.type === "tool_use") {
                  seenToolUse = true;
                }
              }
            }
          }
          if (firstTextBeforeTool && firstTextBeforeTool !== turnDoneResult) {
            expect(fm).not.toBe(firstTextBeforeTool);
          }
        });
      }
    });
  }
});

// ═══════════════════════════════════════════════════════════════════════════════
// 4. Simulated stream deaths
// ═══════════════════════════════════════════════════════════════════════════════

describe("simulated stream deaths", () => {
  for (const file of fixtureFiles) {
    const name = file.replace(".sse", "");
    const filePath = path.join(fixtureDir, file);
    const rawEvents = parseSSEFile(filePath);

    // Find first event index that contains tool_use
    let firstToolUseIdx = -1;
    for (let i = 0; i < rawEvents.length; i++) {
      const canonical = normalizeHarnessEvent("amp", rawEvents[i]);
      for (const ce of canonical) {
        if (
          ce.type === "assistant" &&
          ce.message?.content.some((b: { type: string }) => b.type === "tool_use")
        ) {
          firstToolUseIdx = i;
          break;
        }
      }
      if (firstToolUseIdx >= 0) break;
    }

    if (firstToolUseIdx < 0) continue;

    it(`${name}: EOF after first tool_use → empty finalMessage`, () => {
      const tracker = new ProgressTracker();
      for (let i = 0; i <= firstToolUseIdx; i++) {
        const canonical = normalizeHarnessEvent("amp", rawEvents[i]);
        for (const ce of canonical) {
          [...tracker.update(ce)];
        }
      }
      expect(finalMessage(tracker)).toBe("");
    });
  }
});

// ═══════════════════════════════════════════════════════════════════════════════
// 5. execute streaming bootstrap
// ═══════════════════════════════════════════════════════════════════════════════

describe("execute streams structured progress immediately", () => {
  it("starts the Slack stream on the first task chunk, not the first text chunk", async () => {
    const mockClient = {
      execute: vi.fn().mockResolvedValue({ execution_id: "exe-structured-first" }),
    };

    const { SlackBot } = await import("../src/lib/bot/bot");
    const bot = new SlackBot(mockClient as any);

    const streamedChunks = [
      { type: "task_update", id: "read-1", title: "Read - /tmp/input.txt", status: "in_progress" },
      { type: "plan_update", title: "Read - /tmp/input.txt" },
      { type: "markdown_text", text: "Final answer." },
    ];

    vi.spyOn(bot as any, "streamExecution").mockImplementation(async function* (
      _threadKey: string,
      _executionId: string,
      tracker: { resultText: string },
    ) {
      tracker.resultText = "Final answer.";
      for (const chunk of streamedChunks) {
        yield chunk;
      }
    });
    vi.spyOn(bot as any, "ackFinalDelivery").mockResolvedValue(undefined);
    vi.spyOn(bot as any, "setAssistantTitle").mockResolvedValue(undefined);

    const postedChunks: unknown[] = [];
    const thread = {
      id: "C123456:1770000000.000100",
      post: vi.fn(async (content: AsyncIterable<unknown>) => {
        for await (const chunk of content) {
          postedChunks.push(chunk);
        }
        return { id: "m-1", edit: async () => {} };
      }),
    };

    await (bot as any).execute(thread, thread.id, {
      assignmentGeneration: 7,
      userId: "U123456",
      teamId: "T123456",
    });

    expect(mockClient.execute).toHaveBeenCalledOnce();
    expect(thread.post).toHaveBeenCalledOnce();
    expect(thread.post).toHaveBeenCalledWith(expect.anything(), expect.objectContaining({
      taskDisplayMode: "plan",
      threadKey: thread.id,
      executionId: "exe-structured-first",
    }));
    expect(postedChunks).toEqual(streamedChunks);
  });

  it("upgrades streamed table replies through edit() after the stream completes", async () => {
    const mockClient = {
      execute: vi.fn().mockResolvedValue({ execution_id: "exe-stream-table-upgrade" }),
    };

    const { SlackBot } = await import("../src/lib/bot/bot");
    const bot = new SlackBot(mockClient as any);

    vi.spyOn(bot as any, "streamExecution").mockImplementation((async function* (
      _threadKey: string,
      _executionId: string,
      tracker: { lastAssistantText: string },
    ) {
      tracker.lastAssistantText = [
        "Summary",
        "",
        "| Time | Block |",
        "| --- | --- |",
        "| 8:00 | Breakfast |",
      ].join("\n");
      yield { type: "markdown_text", text: "\u200b" };
      yield {
        type: "blocks",
        blocks: [
          { type: "markdown", text: "Summary" },
          {
            type: "table",
            rows: [
              [
                { type: "raw_text", text: "Time" },
                { type: "raw_text", text: "Block" },
              ],
              [
                { type: "raw_text", text: "8:00" },
                { type: "raw_text", text: "Breakfast" },
              ],
            ],
          },
        ],
      };
    }) as any);
    vi.spyOn(bot as any, "ackFinalDelivery").mockResolvedValue(undefined);
    vi.spyOn(bot as any, "setAssistantTitle").mockResolvedValue(undefined);

    const edit = vi.fn(async () => {});
    const thread = {
      id: "C123456:1770000000.000200",
      post: vi.fn(async () => ({ id: "m-2", edit })),
    };

    await (bot as any).execute(thread, thread.id, {
      assignmentGeneration: 8,
      userId: "U123456",
      teamId: "T123456",
    });

    expect(thread.post).toHaveBeenCalledOnce();
    expect(edit).toHaveBeenCalledOnce();
    expect(edit).toHaveBeenCalledWith({
      markdown: [
        "Summary",
        "",
        "| Time | Block |",
        "| --- | --- |",
        "| 8:00 | Breakfast |",
      ].join("\n"),
    });
  });

  it("skips final stream edit and alert when overflow follow-ups were posted", async () => {
    const mockClient = {
      execute: vi.fn().mockResolvedValue({ execution_id: "exe-overflow-duplicate" }),
    };

    const { SlackBot } = await import("../src/lib/bot/bot");
    const alertPost = vi.fn(async (_threadId: string, _message: { markdown: string }) => ({ id: "alert-1" }));
    const bot = new SlackBot(
      mockClient as any,
      "",
      { postMessage: alertPost } as any,
      "C_ENG_CENTAUR_ALERTS",
    );

    vi.spyOn(bot as any, "streamExecution").mockImplementation((async function* (
      _threadKey: string,
      _executionId: string,
      tracker: { resultText: string; agentThreadId: string },
    ) {
      tracker.resultText = "Final answer from overflow path";
      tracker.agentThreadId = "T-amp-thread";
      yield { type: "markdown_text", text: "Working..." };
    }) as any);
    const ackSpy = vi.spyOn(bot as any, "ackFinalDelivery").mockResolvedValue(undefined);
    vi.spyOn(bot as any, "setAssistantTitle").mockResolvedValue(undefined);
    const infoSpy = vi.spyOn(log, "info").mockImplementation(() => {});
    const warnSpy = vi.spyOn(log, "warn").mockImplementation(() => {});

    const edit = vi.fn(async () => {});
    const thread = {
      id: "C123456:1770000000.000800",
      post: vi.fn(async (content: AsyncIterable<unknown>) => {
        for await (const _chunk of content) { /* drain */ }
        return {
          id: "1770000000.000801",
          streamMessageTs: "1770000000.000801",
          overflowFollowupsPosted: true,
          overflowReason: "slack_rejected",
          overflowFollowupCount: 1,
          overflowChars: 42,
          edit,
        };
      }),
    };

    await (bot as any).execute(thread, thread.id, {
      assignmentGeneration: 7,
      userId: "U123456",
      teamId: "T123456",
    });

    expect(edit).not.toHaveBeenCalled();
    expect(ackSpy).toHaveBeenCalledWith("exe-overflow-duplicate", thread.id, { requireLease: false });
    expect(alertPost).not.toHaveBeenCalled();
    expect(infoSpy).toHaveBeenCalledWith("streamed_reply_block_upgrade_skipped", expect.objectContaining({
      thread_key: thread.id,
      execution_id: "exe-overflow-duplicate",
      agent_thread_id: "T-amp-thread",
      stream_message_ts: "1770000000.000801",
      reason: "overflow_followups_posted",
      overflow_reason: "slack_rejected",
      overflow_followup_count: 1,
      overflow_chars: 42,
      result_length: "Final answer from overflow path".length,
    }));
    expect(warnSpy).not.toHaveBeenCalledWith("slack_stream_overflow_duplicate_rendered", expect.anything());

    infoSpy.mockRestore();
    warnSpy.mockRestore();
  });

  it("does not attempt a failed final stream edit after overflow follow-ups were posted", async () => {
    const mockClient = {
      execute: vi.fn().mockResolvedValue({ execution_id: "exe-overflow-upgrade-failed" }),
    };

    const { SlackBot } = await import("../src/lib/bot/bot");
    const alertPost = vi.fn(async (_threadId: string, _message: { markdown: string }) => ({ id: "alert-1" }));
    const bot = new SlackBot(
      mockClient as any,
      "",
      { postMessage: alertPost } as any,
      "C_ENG_CENTAUR_ALERTS",
    );

    vi.spyOn(bot as any, "streamExecution").mockImplementation((async function* (
      _threadKey: string,
      _executionId: string,
      tracker: { resultText: string; agentThreadId: string },
    ) {
      tracker.resultText = "Final answer that cannot be upgraded";
      tracker.agentThreadId = "T-amp-thread";
      yield { type: "markdown_text", text: "Working..." };
    }) as any);
    vi.spyOn(bot as any, "ackFinalDelivery").mockResolvedValue(undefined);
    vi.spyOn(bot as any, "setAssistantTitle").mockResolvedValue(undefined);
    const infoSpy = vi.spyOn(log, "info").mockImplementation(() => {});
    const warnSpy = vi.spyOn(log, "warn").mockImplementation(() => {});

    const edit = vi.fn(async () => {});
    const thread = {
      id: "C123456:1770000000.000900",
      post: vi.fn(async (content: AsyncIterable<unknown>) => {
        for await (const _chunk of content) { /* drain */ }
        return {
          id: "1770000000.000901",
          streamMessageTs: "1770000000.000901",
          overflowFollowupsPosted: true,
          overflowReason: "slack_rejected",
          overflowFollowupCount: 2,
          overflowChars: 84,
          edit,
        };
      }),
    };

    await (bot as any).execute(thread, thread.id, {
      assignmentGeneration: 7,
      userId: "U123456",
      teamId: "T123456",
    });

    expect(edit).not.toHaveBeenCalled();
    expect(alertPost).not.toHaveBeenCalled();
    expect(infoSpy).toHaveBeenCalledWith("streamed_reply_block_upgrade_skipped", expect.objectContaining({
      execution_id: "exe-overflow-upgrade-failed",
      stream_message_ts: "1770000000.000901",
      reason: "overflow_followups_posted",
      overflow_reason: "slack_rejected",
      overflow_followup_count: 2,
      overflow_chars: 84,
    }));
    expect(infoSpy).not.toHaveBeenCalledWith("streamed_reply_block_upgrade_failed", expect.anything());
    expect(warnSpy).not.toHaveBeenCalledWith("slack_stream_overflow_duplicate_rendered", expect.anything());

    infoSpy.mockRestore();
    warnSpy.mockRestore();
  });

  it("does not alert when a live stream ends before a completed terminal result", async () => {
    const mockClient = {
      execute: vi.fn().mockResolvedValue({ execution_id: "exe-nonterminal-empty-live" }),
    };

    const { SlackBot } = await import("../src/lib/bot/bot");
    const alertPost = vi.fn(async (_threadId: string, _message: { markdown: string }) => ({ id: "alert-1" }));
    const bot = new SlackBot(
      mockClient as any,
      "",
      { postMessage: alertPost } as any,
      "C_ENG_CENTAUR_ALERTS",
    );

    vi.spyOn(bot as any, "streamExecution").mockImplementation((async function* (
      _threadKey: string,
      _executionId: string,
      tracker: { agentThreadId: string },
    ) {
      tracker.agentThreadId = "T-nonterminal-empty-live";
      yield { type: "plan_update", title: "Completed" };
    }) as any);
    vi.spyOn(bot as any, "ackFinalDelivery").mockResolvedValue(undefined);
    vi.spyOn(bot as any, "setAssistantTitle").mockResolvedValue(undefined);
    const errorSpy = vi.spyOn(log, "error").mockImplementation(() => {});

    const thread = {
      id: "C123456:1770000000.001000",
      post: vi.fn(async (content: AsyncIterable<unknown>) => {
        for await (const _chunk of content) { /* drain */ }
        return {
          id: "1770000000.001001",
          streamMessageTs: "1770000000.001001",
          edit: async () => {},
        };
      }),
    };

    try {
      await (bot as any).execute(thread, thread.id, {
        assignmentGeneration: 7,
        userId: "U123456",
        teamId: "T123456",
      });

      expect(errorSpy).not.toHaveBeenCalledWith(
        "slack_empty_bot_message_detected",
        expect.anything(),
      );
      expect(alertPost).not.toHaveBeenCalled();
    } finally {
      errorSpy.mockRestore();
    }
  });

  it("does not alert when a live stream ends with a cancelled terminal result", async () => {
    const mockClient = {
      execute: vi.fn().mockResolvedValue({ execution_id: "exe-cancelled-live" }),
    };

    const { SlackBot } = await import("../src/lib/bot/bot");
    const alertPost = vi.fn(async (_threadId: string, _message: { markdown: string }) => ({ id: "alert-1" }));
    const bot = new SlackBot(
      mockClient as any,
      "",
      { postMessage: alertPost } as any,
      "C_ENG_CENTAUR_ALERTS",
    );

    vi.spyOn(bot as any, "streamExecution").mockImplementation((async function* (
      _threadKey: string,
      _executionId: string,
      tracker: { agentThreadId: string; observeTerminal: (source: unknown) => void },
    ) {
      tracker.agentThreadId = "T-cancelled-live";
      tracker.observeTerminal({
        status: "cancelled",
        terminalReason: "cancel_requested",
        resultText: "",
        errorText: "cancel_requested",
      });
      yield { type: "plan_update", title: "Cancelled" };
    }) as any);
    vi.spyOn(bot as any, "ackFinalDelivery").mockResolvedValue(undefined);
    vi.spyOn(bot as any, "setAssistantTitle").mockResolvedValue(undefined);
    const errorSpy = vi.spyOn(log, "error").mockImplementation(() => {});

    const thread = {
      id: "C123456:1770000000.001010",
      post: vi.fn(async (content: AsyncIterable<unknown>) => {
        for await (const _chunk of content) { /* drain */ }
        return {
          id: "1770000000.001011",
          streamMessageTs: "1770000000.001011",
          edit: async () => {},
        };
      }),
    };

    try {
      await (bot as any).execute(thread, thread.id, {
        assignmentGeneration: 7,
        userId: "U123456",
        teamId: "T123456",
      });

      expect(errorSpy).not.toHaveBeenCalledWith(
        "slack_empty_bot_message_detected",
        expect.anything(),
      );
      expect(alertPost).not.toHaveBeenCalled();
    } finally {
      errorSpy.mockRestore();
    }
  });

  it("logs and alerts when a live stream reaches a completed terminal result with empty output", async () => {
    const mockClient = {
      execute: vi.fn().mockResolvedValue({ execution_id: "exe-empty-live" }),
    };

    const { SlackBot } = await import("../src/lib/bot/bot");
    const alertPost = vi.fn(async (_threadId: string, _message: { markdown: string }) => ({ id: "alert-1" }));
    const bot = new SlackBot(
      mockClient as any,
      "",
      { postMessage: alertPost } as any,
      "C_ENG_CENTAUR_ALERTS",
    );

    vi.spyOn(bot as any, "streamExecution").mockImplementation((async function* (
      _threadKey: string,
      _executionId: string,
      tracker: { agentThreadId: string; observeTerminal: (source: unknown) => void },
    ) {
      tracker.agentThreadId = "T-empty-live";
      tracker.observeTerminal({
        status: "completed",
        resultText: "",
        errorText: "",
      });
      yield { type: "plan_update", title: "Completed" };
    }) as any);
    vi.spyOn(bot as any, "ackFinalDelivery").mockResolvedValue(undefined);
    vi.spyOn(bot as any, "setAssistantTitle").mockResolvedValue(undefined);
    const errorSpy = vi.spyOn(log, "error").mockImplementation(() => {});

    const thread = {
      id: "C123456:1770000000.001000",
      post: vi.fn(async (content: AsyncIterable<unknown>) => {
        for await (const _chunk of content) { /* drain */ }
        return {
          id: "1770000000.001001",
          streamMessageTs: "1770000000.001001",
          edit: async () => {},
        };
      }),
    };

    try {
      await (bot as any).execute(thread, thread.id, {
        assignmentGeneration: 7,
        userId: "U123456",
        teamId: "T123456",
      });

      expect(errorSpy).toHaveBeenCalledWith("slack_empty_bot_message_detected", expect.objectContaining({
        thread_key: thread.id,
        execution_id: "exe-empty-live",
        agent_thread_id: "T-empty-live",
        delivery_path: "live_stream",
        stream_message_ts: "1770000000.001001",
        status: "completed",
        result_length: 0,
        error_text_length: 0,
        rendered_markdown_length: 0,
        delivered_to_slack: true,
      }));
      expect(alertPost).toHaveBeenCalledWith(
        "slack:C_ENG_CENTAUR_ALERTS",
        { markdown: expect.stringContaining("**Slack empty message detected**") },
      );
    } finally {
      errorSpy.mockRestore();
    }
  });

  it("posts a fallback and alerts when final delivery has an empty completed result", async () => {
    const mockClient = {
      markFinalDelivered: vi.fn(async () => ({ ok: true })),
    };

    const { SlackBot } = await import("../src/lib/bot/bot");
    const postMessage = vi.fn(async (_threadId: string, _message: { markdown: string }) => ({ id: "msg-1" }));
    const bot = new SlackBot(
      mockClient as any,
      "",
      { postMessage } as any,
      "C_ENG_CENTAUR_ALERTS",
    );
    vi.spyOn(bot as any, "setAssistantTitle").mockResolvedValue(undefined);
    const errorSpy = vi.spyOn(log, "error").mockImplementation(() => {});

    try {
      await (bot as any).processFinalDelivery({
        execution_id: "exe-empty-final",
        thread_key: "slack:C123456:1770000000.001100",
        delivery: {
          platform: "slack",
          channel: "C123456",
          thread_ts: "1770000000.001100",
        },
        final_payload: {
          status: "completed",
          result_text: "",
          error_text: "",
          agent_thread_id: "T-empty-final",
        },
      });

      expect(errorSpy).toHaveBeenCalledWith("slack_empty_bot_message_detected", expect.objectContaining({
        thread_key: "slack:C123456:1770000000.001100",
        execution_id: "exe-empty-final",
        agent_thread_id: "T-empty-final",
        delivery_path: "final_delivery",
        status: "completed",
        result_length: 0,
        error_text_length: 0,
      }));
      expect(postMessage).toHaveBeenCalledWith(
        "slack:C_ENG_CENTAUR_ALERTS",
        { markdown: expect.stringContaining("**Slack empty message detected**") },
      );
      expect(postMessage).toHaveBeenCalledWith(
        "slack:C123456:1770000000.001100",
        { markdown: "Agent completed with no output." },
      );
      expect(mockClient.markFinalDelivered).toHaveBeenCalledWith("exe-empty-final", expect.any(String));
    } finally {
      errorSpy.mockRestore();
    }
  });

  it.each([
    "user_not_found",
    "restricted_action_thread_locked",
    "no_permission",
  ])("falls back to plain Slack posts when streaming rejects %s", async (errorCode) => {
    const mockClient = {
      execute: vi.fn().mockResolvedValue({ execution_id: `exe-fallback-${errorCode}` }),
    };

    const { SlackBot } = await import("../src/lib/bot/bot");
    const bot = new SlackBot(mockClient as any);

    vi.spyOn(bot as any, "streamExecution").mockImplementation((async function* (
      _threadKey: string,
      _executionId: string,
      tracker: { resultText: string },
    ) {
      tracker.resultText = "Final answer from fallback";
      yield { type: "markdown_text", text: "Working..." };
    }) as any);
    const ackSpy = vi.spyOn(bot as any, "ackFinalDelivery").mockResolvedValue(undefined);

    const plainPosts: Array<{ markdown: string }> = [];
    const thread = {
      id: "C123456:1770000000.000300",
      post: vi.fn(async (content: AsyncIterable<unknown> | { markdown: string }) => {
        if (typeof (content as AsyncIterable<unknown>)[Symbol.asyncIterator] === "function") {
          throw new SlackApiCallError("chat.startStream", errorCode, { ok: false, error: errorCode });
        }
        plainPosts.push(content as { markdown: string });
        return { id: "m-fallback", edit: async () => {} };
      }),
    };

    await (bot as any).execute(thread, thread.id, {
      assignmentGeneration: 9,
      userId: "U123456",
      teamId: "T123456",
    });

    expect(thread.post).toHaveBeenCalledTimes(2);
    expect(plainPosts).toEqual([{ markdown: "Final answer from fallback" }]);
    expect(ackSpy).toHaveBeenCalledWith(`exe-fallback-${errorCode}`, thread.id, { requireLease: false });
  });

  it.each([
    "user_not_found",
    "restricted_action_thread_locked",
  ])("does not ack final delivery when fallback post fails after %s stream rejection", async (errorCode) => {
    const mockClient = {
      execute: vi.fn().mockResolvedValue({ execution_id: `exe-fallback-post-fails-${errorCode}` }),
    };

    const { SlackBot } = await import("../src/lib/bot/bot");
    const bot = new SlackBot(mockClient as any);

    vi.spyOn(bot as any, "streamExecution").mockImplementation((async function* (
      _threadKey: string,
      _executionId: string,
      tracker: { resultText: string },
    ) {
      tracker.resultText = "Final answer that never reaches Slack";
      yield { type: "markdown_text", text: "Working..." };
    }) as any);
    const ackSpy = vi.spyOn(bot as any, "ackFinalDelivery").mockResolvedValue(undefined);
    const warnSpy = vi.spyOn(log, "warn").mockImplementation(() => {});

    const plainPosts: Array<{ markdown: string }> = [];
    const thread = {
      id: "C123456:1770000000.000500",
      post: vi.fn(async (content: AsyncIterable<unknown> | { markdown: string }) => {
        if (typeof (content as AsyncIterable<unknown>)[Symbol.asyncIterator] === "function") {
          throw new SlackApiCallError("chat.startStream", errorCode, { ok: false, error: errorCode });
        }
        plainPosts.push(content as { markdown: string });
        throw new SlackApiCallError("chat.postMessage", "channel_not_found", { ok: false, error: "channel_not_found" });
      }),
    };

    await (bot as any).execute(thread, thread.id, {
      assignmentGeneration: 10,
      userId: "U123456",
      teamId: "T123456",
    });

    expect(thread.post).toHaveBeenCalledTimes(2);
    expect(plainPosts).toEqual([{ markdown: "Final answer that never reaches Slack" }]);
    expect(ackSpy).not.toHaveBeenCalled();
    expect(warnSpy).toHaveBeenCalledWith("slack_stream_fallback_post_failed", expect.objectContaining({
      error_class: "invalid_destination",
      error_code: "channel_not_found",
      execution_id: `exe-fallback-post-fails-${errorCode}`,
    }));

    warnSpy.mockRestore();
  });

  it("leaves multi-chunk fallback unacked when a later fallback post fails", async () => {
    const mockClient = {
      execute: vi.fn().mockResolvedValue({ execution_id: "exe-fallback-later-chunk-fails" }),
    };

    const { SlackBot } = await import("../src/lib/bot/bot");
    const bot = new SlackBot(mockClient as any);

    const longFallback = Array.from({ length: 55 }, (_unused, index) => `Paragraph ${index}`).join("\n\n");
    vi.spyOn(bot as any, "streamExecution").mockImplementation((async function* (
      _threadKey: string,
      _executionId: string,
      tracker: { resultText: string },
    ) {
      tracker.resultText = longFallback;
      yield { type: "markdown_text", text: "Working..." };
    }) as any);
    const ackSpy = vi.spyOn(bot as any, "ackFinalDelivery").mockResolvedValue(undefined);
    const warnSpy = vi.spyOn(log, "warn").mockImplementation(() => {});

    const deliveredPlainPosts: Array<{ markdown: string }> = [];
    const attemptedPlainPosts: Array<{ markdown: string }> = [];
    const thread = {
      id: "C123456:1770000000.000600",
      post: vi.fn(async (content: AsyncIterable<unknown> | { markdown: string }) => {
        if (typeof (content as AsyncIterable<unknown>)[Symbol.asyncIterator] === "function") {
          throw new SlackApiCallError("chat.startStream", "restricted_action_thread_locked", {
            ok: false,
            error: "restricted_action_thread_locked",
          });
        }
        const payload = content as { markdown: string };
        attemptedPlainPosts.push(payload);
        if (attemptedPlainPosts.length === 2) {
          throw new SlackApiCallError("chat.postMessage", "channel_not_found", { ok: false, error: "channel_not_found" });
        }
        deliveredPlainPosts.push(payload);
        return { id: `m-fallback-${attemptedPlainPosts.length}`, edit: async () => {} };
      }),
    };

    await (bot as any).execute(thread, thread.id, {
      assignmentGeneration: 11,
      userId: "U123456",
      teamId: "T123456",
    });

    expect(thread.post).toHaveBeenCalledTimes(3);
    expect(attemptedPlainPosts).toHaveLength(2);
    expect(deliveredPlainPosts).toHaveLength(1);
    expect(attemptedPlainPosts[0].markdown).toContain("Paragraph 0");
    expect(attemptedPlainPosts[1].markdown).toContain("Paragraph 50");
    expect(ackSpy).not.toHaveBeenCalled();
    expect(warnSpy).toHaveBeenCalledWith("slack_stream_fallback_post_failed", expect.objectContaining({
      error_class: "invalid_destination",
      error_code: "channel_not_found",
      execution_id: "exe-fallback-later-chunk-fails",
    }));

    warnSpy.mockRestore();
  });

  it("skips assistant title warnings for restricted destinations", async () => {
    const { SlackBot } = await import("../src/lib/bot/bot");
    const slack = {
      setAssistantTitle: vi.fn().mockRejectedValue(new SlackApiCallError(
        "assistant.threads.setTitle",
        "restricted_action_thread_locked",
        { ok: false, error: "restricted_action_thread_locked" },
      )),
    };
    const bot = new SlackBot({} as any, "", slack as any);
    const infoSpy = vi.spyOn(log, "info").mockImplementation(() => {});
    const warnSpy = vi.spyOn(log, "warn").mockImplementation(() => {});

    await (bot as any).setAssistantTitle("C123456:1770000000.000400", {}, "Final answer from fallback");
    await (bot as any).setAssistantTitle("C123456:1770000000.000400", {}, "Final answer from fallback again");

    expect(slack.setAssistantTitle).toHaveBeenCalledOnce();
    expect(infoSpy).toHaveBeenCalledWith("set_title_skipped", expect.objectContaining({
      error_class: "restricted_destination",
      error_code: "restricted_action_thread_locked",
      retryable: false,
    }));
    expect(warnSpy).not.toHaveBeenCalled();

    infoSpy.mockRestore();
    warnSpy.mockRestore();
  });
});

// ═══════════════════════════════════════════════════════════════════════════════
// 5b. ackFinalDelivery retries on transient failures
// ═══════════════════════════════════════════════════════════════════════════════

describe("ackFinalDelivery retries on transient 500 errors", () => {
  it("retries and succeeds on second attempt after a 500", async () => {
    let attempts = 0;
    const mockClient = {
      markFinalDelivered: vi.fn(async () => {
        attempts++;
        if (attempts === 1) {
          const err = new Error("Request failed with status code 500") as Error & { response?: { status: number } };
          err.response = { status: 500 };
          throw err;
        }
      }),
    };

    const { SlackBot } = await import("../src/lib/bot/bot");
    const bot = new SlackBot(mockClient as any);
    const infoSpy = vi.spyOn(log, "info").mockImplementation(() => {});

    await (bot as any).ackFinalDelivery("exe-retry-test", "C123:1700000000.000100", { requireLease: false });

    expect(mockClient.markFinalDelivered).toHaveBeenCalledTimes(2);
    expect(infoSpy).toHaveBeenCalledWith("final_delivery_ack_retrying", expect.objectContaining({
      execution_id: "exe-retry-test",
      attempt: 1,
      status: 500,
    }));

    infoSpy.mockRestore();
  });

  it("does not retry on 409 (non-retryable)", async () => {
    const mockClient = {
      markFinalDelivered: vi.fn(async () => {
        const err = new Error("delivery not claimable") as Error & { response?: { status: number } };
        err.response = { status: 409 };
        throw err;
      }),
    };

    const { SlackBot } = await import("../src/lib/bot/bot");
    const bot = new SlackBot(mockClient as any);
    const warnSpy = vi.spyOn(log, "warn").mockImplementation(() => {});

    await (bot as any).ackFinalDelivery("exe-no-retry", "C123:1700000000.000100", { requireLease: false });

    expect(mockClient.markFinalDelivered).toHaveBeenCalledTimes(1);
    expect(warnSpy).toHaveBeenCalledWith("final_delivery_ack_failed", expect.objectContaining({
      execution_id: "exe-no-retry",
      status: 409,
      attempts: 1,
    }));

    warnSpy.mockRestore();
  });

  it("acks before the streamed reply edit to prevent final-delivery race", async () => {
    const callOrder: string[] = [];
    const mockClient = {
      execute: vi.fn().mockResolvedValue({ execution_id: "exe-ack-order" }),
      markFinalDelivered: vi.fn(async () => { callOrder.push("ack"); }),
    };

    const { SlackBot } = await import("../src/lib/bot/bot");
    const bot = new SlackBot(mockClient as any);

    vi.spyOn(bot as any, "streamExecution").mockImplementation((async function* (
      _threadKey: string,
      _executionId: string,
      tracker: { resultText: string },
    ) {
      tracker.resultText = "Final answer";
      yield { type: "markdown_text", text: "Final answer" };
    }) as any);
    vi.spyOn(bot as any, "setAssistantTitle").mockImplementation(async () => { callOrder.push("title"); });

    const thread = {
      id: "C123456:1770000000.000700",
      post: vi.fn(async (content: AsyncIterable<unknown> | { markdown: string }) => {
        if (typeof (content as AsyncIterable<unknown>)[Symbol.asyncIterator] === "function") {
          for await (const _chunk of content as AsyncIterable<unknown>) { /* drain */ }
        }
        return {
          id: "m-1",
          edit: vi.fn(async () => { callOrder.push("edit"); }),
        };
      }),
    };

    await (bot as any).execute(thread, thread.id, {
      assignmentGeneration: 7,
      userId: "U123456",
      teamId: "T123456",
    });

    expect(callOrder.indexOf("ack")).toBeLessThan(callOrder.indexOf("edit"));
    expect(callOrder.indexOf("ack")).toBeLessThan(callOrder.indexOf("title"));
  });
});

// ═══════════════════════════════════════════════════════════════════════════════
// 6. consumeWire reconnect on graceful EOF (API restart mid-turn)
// ═══════════════════════════════════════════════════════════════════════════════

describe("consumeWire reconnects on graceful EOF without turn.done", () => {
  /**
   * Simulates the exact bug: API restarts mid-turn → SSE iterator returns
   * { done: true } without emitting turn.done → slackbot should reconnect
   * and eventually get the result, NOT show "Agent completed with no output."
   */
  it("treats iterator done (no turn.done) as wire break and reconnects", async () => {
    let streamCalls = 0;

    const mockClient = {
      streamEvents: () => {
        streamCalls++;
        if (streamCalls === 1) {
          return (async function* () {
            yield {
              eventId: 1,
              eventKind: "amp_raw_event",
              data: {
                type: "assistant",
                message: { content: [{ type: "text", text: "Working on it..." }] },
              },
            };
            // EOF without terminal event: should trigger reconnect.
          })();
        }

        return (async function* () {
          yield {
            eventId: 2,
            eventKind: "amp_raw_event",
            data: {
              type: "assistant",
              message: { content: [{ type: "text", text: "Here is the answer." }] },
            },
          };
          yield {
            eventId: 3,
            eventKind: "amp_raw_event",
            data: {
              type: "turn.done",
              turn_id: 1,
              result: "Here is the answer.",
              agent_thread_id: "",
            },
          };
        })();
      },
      markFinalDelivered: async () => ({ ok: true }),
    };

    const { SlackBot } = await import("../src/lib/bot/bot");
    const bot = new SlackBot(mockClient as any);

    const chunks: any[] = [];
    const streamGen = (bot as any).streamExecution(
      "test:reconnect-1",
      "exe-reconnect-1",
      new ProgressTracker(),
      Date.now(),
      new AbortController().signal,
    );

    for await (const chunk of streamGen) {
      chunks.push(chunk);
    }

    // Verify reconnect happened via a second streamEvents call.
    expect(streamCalls).toBeGreaterThanOrEqual(2);

    // Verify the final output contains the answer (not "Agent completed with no output")
    const finalOutput = chunks
      .flatMap((chunk) => chunk.type === "blocks" ? chunk.blocks : [])
      .filter((block) => block.type === "markdown")
      .map((block) => block.text)
      .join("");
    expect(finalOutput).toContain("Here is the answer.");
    expect(finalOutput).not.toContain("no output");
  });

  it("gives up after max retries and shows no output", { timeout: 30_000 }, async () => {
    const mockClient = {
      streamEvents: () => (async function* () {
        // EOF immediately on every attempt.
      })(),
      markFinalDelivered: async () => ({ ok: true }),
    };

    const { SlackBot } = await import("../src/lib/bot/bot");
    const bot = new SlackBot(mockClient as any);

    const chunks: any[] = [];
    const streamGen = (bot as any).streamExecution(
      "test:reconnect-exhaust",
      "exe-reconnect-exhaust",
      new ProgressTracker(),
      Date.now(),
      new AbortController().signal,
    );

    for await (const chunk of streamGen) {
      chunks.push(chunk);
    }

    // Should eventually give up and show "no output"
    const markdownChunks = chunks.filter((c) => c.type === "markdown_text");
    expect(markdownChunks.length).toBeGreaterThan(0);
    expect(markdownChunks[markdownChunks.length - 1].text).toContain("no output");
  });

  it("emits structured keepalives without blank markdown", async () => {
    vi.useFakeTimers();
    try {
      const mockClient = {
        streamEvents: () => (async function* () {
          await new Promise(() => {});
        })(),
        markFinalDelivered: async () => ({ ok: true }),
      };

      const { SlackBot } = await import("../src/lib/bot/bot");
      const bot = new SlackBot(mockClient as any);
      const abortController = new AbortController();
      const streamGen = (bot as any).consumeExecutionEvents(
        "test:keepalive",
        "exe-keepalive",
        new ProgressTracker(),
        abortController.signal,
      );

      const firstChunkPromise = streamGen.next();
      await vi.advanceTimersByTimeAsync(120_000);
      expect(await firstChunkPromise).toEqual({
        done: false,
        value: { type: "plan_update", title: "Still working…" },
      });

      const secondChunkPromise = streamGen.next();
      await vi.advanceTimersByTimeAsync(30_000);
      expect(await secondChunkPromise).toEqual({
        done: false,
        value: { type: "plan_update", title: "Still working…" },
      });

      abortController.abort();
      expect(await streamGen.next()).toEqual({ done: true, value: undefined });
    } finally {
      vi.useRealTimers();
    }
  });

  it("emits run metadata as a context block before heading-based answer sections", async () => {
    const mockClient = {
      streamEvents: () => (async function* () {
        yield {
          eventId: 1,
          eventKind: "amp_raw_event",
          data: { type: "system", subtype: "init", session_id: "T-test-thread" },
        };
        yield {
          eventId: 2,
          eventKind: "amp_raw_event",
          data: {
            type: "turn.done",
            turn_id: 1,
            result: "# Summary\n\nHello.",
          },
        };
      })(),
      markFinalDelivered: async () => ({ ok: true }),
    };
    const { SlackBot } = await import("../src/lib/bot/bot");
    const bot = new SlackBot(mockClient as any);

    const chunks: any[] = [];
    for await (const chunk of (bot as any).streamExecution(
      "test:context-block",
      "exe-context-block",
      new ProgressTracker(),
      Date.now() - 1200,
      new AbortController().signal,
    )) {
      chunks.push(chunk);
    }

    const contextChunk = chunks.find(
      (chunk) => chunk.type === "blocks" && chunk.blocks[0]?.type === "rich_text",
    );
    const appName = process.env.APP_NAME || "Centaur";
    expect(contextChunk?.blocks[0].elements[0].elements).toEqual([
      { type: "text", text: `${appName} · ` },
      {
        type: "link",
        url: "https://ampcode.com/threads/T-test-thread",
        text: "agent",
      },
      expect.objectContaining({ type: "text", text: expect.stringContaining(" · ") }),
    ]);
    expect(chunks.some((chunk) => chunk.type === "markdown_text" && chunk.text.includes("Centaur"))).toBe(false);
    expect(chunks).toContainEqual({
      type: "blocks",
      blocks: [
        { type: "markdown", text: "# Summary" },
        { type: "markdown", text: "Hello." },
        {
          type: "rich_text",
          elements: [{
            type: "rich_text_section",
            elements: [
              {
                type: "link",
                url: "https://ampcode.com/threads/T-test-thread",
                text: "View in Amp",
              },
              { type: "text", text: " · " },
              {
                type: "text",
                text: "amp threads continue T-test-thread",
                style: { code: true },
              },
            ],
          }],
        },
      ],
    });
  });

  it("uses turn.done agent_thread_id when system init was missed", async () => {
    const mockClient = {
      streamEvents: () => (async function* () {
        yield {
          eventId: 1,
          eventKind: "amp_raw_event",
          data: {
            type: "turn.done",
            turn_id: 1,
            result: "Hello.",
            agent_thread_id: "T-turn-done-thread",
          },
        };
      })(),
      markFinalDelivered: async () => ({ ok: true }),
    } as unknown as CentaurClient
    const { SlackBot } = await import("../src/lib/bot/bot");
    const bot = new SlackBot(mockClient);

    const chunks: any[] = [];
    for await (const chunk of (bot as any).streamExecution(
      "test:turn-done-thread",
      "exe-turn-done-thread",
      new ProgressTracker(),
      Date.now() - 1200,
      new AbortController().signal,
    )) {
      chunks.push(chunk);
    }

    expect(JSON.stringify(chunks)).toContain("https://ampcode.com/threads/T-turn-done-thread");
  });

  it("splits long plain text at Slack's markdown block payload budget", async () => {
    const result = "a".repeat(13_000);
    const mockClient = {
      streamEvents: () => (async function* () {
        yield {
          eventId: 1,
          eventKind: "amp_raw_event",
          data: { type: "turn.done", turn_id: 1, result },
        };
      })(),
      markFinalDelivered: async () => ({ ok: true }),
    };
    const { SlackBot } = await import("../src/lib/bot/bot");
    const bot = new SlackBot(mockClient as any);
    const tracker = new ProgressTracker();

    const chunks: any[] = [];
    for await (const chunk of (bot as any).streamExecution(
      "test:long-plain-text",
      "exe-long-plain-text",
      tracker,
      Date.now() - 1200,
      new AbortController().signal,
    )) {
      chunks.push(chunk);
    }

    const markdownBlocks = chunks
      .flatMap((chunk) => chunk.type === "blocks" ? chunk.blocks : [])
      .filter((block) => block.type === "markdown");
    expect(markdownBlocks).toHaveLength(1);
    expect(markdownBlocks[0].text + tracker.overflowChunks.join("")).toBe(result);
    expect(markdownBlocks[0].text.length).toBeLessThanOrEqual(12_000);
    expect(tracker.overflowChunks).toHaveLength(1);
  });
});

// ═══════════════════════════════════════════════════════════════════════════════
// 6b. Prompt selector parsing (flag-only; no auto-routing)
// ═══════════════════════════════════════════════════════════════════════════════

describe("parsePromptSelectorFlag", () => {
  it("extracts --invest", () => {
    expect(parsePromptSelectorFlag("--invest hyperliquid miqs")).toBe("invest");
  });

  it("extracts personas loaded from the registry", () => {
    setPersonaRegistryForTest({ eng: {}, legal: {} });
    expect(parsePromptSelectorFlag("--legal review this nda")).toBe("legal");
  });

  it("handles --INVEST (case insensitive)", () => {
    expect(parsePromptSelectorFlag("--INVEST")).toBe("invest");
  });

  it("resolves aliases (claude → claude-code, pi → pi-mono)", () => {
    expect(parsePromptSelectorFlag("--claude do a thing")).toBe("claude-code");
    expect(parsePromptSelectorFlag("--pi analyse")).toBe("pi-mono");
  });

  it("skips reserved non-harness flags", () => {
    expect(parsePromptSelectorFlag("--engine=opus --opus --sonnet --haiku --model")).toBeUndefined();
  });

  it("returns undefined when no flag is present", () => {
    expect(parsePromptSelectorFlag("thoughts on hyperliquid?")).toBeUndefined();
  });

  it("last flag wins (unchanged from legacy single-flag behavior)", () => {
    expect(parsePromptSelectorFlag("--invest --codex")).toBe("codex");
  });

  it("does NOT infer persona from DocSend/Drive URLs or attachments", () => {
    expect(parsePromptSelectorFlag("look at this co https://docsend.com/view/s/abc")).toBeUndefined();
    expect(parsePromptSelectorFlag("https://drive.google.com/file/d/abc")).toBeUndefined();
  });
});

describe("persona registry refresh", () => {
  it("loads prompt selectors from the API", async () => {
    resetPersonaRegistryForTest();
    const client = {
      http: {
        get: vi.fn().mockResolvedValue({
          data: {
            eng: {},
            legal: {},
            invest: {},
          },
        }),
      },
    };
    const bot = new SlackBot(client as unknown as CentaurClient);

    await (bot as unknown as { refreshPersonaRegistry(): Promise<void> }).refreshPersonaRegistry();

    expect(parsePromptSelectorFlag("--legal review this")).toBe("legal");
  });

  it("falls back to the floor persona when registry fetch fails", async () => {
    resetPersonaRegistryForTest();
    const client = {
      http: {
        get: vi.fn().mockRejectedValue(new Error("registry down")),
      },
    };
    const bot = new SlackBot(client as unknown as CentaurClient);

    await (bot as unknown as { refreshPersonaRegistry(): Promise<void> }).refreshPersonaRegistry();

    expect(parsePromptSelectorFlag("--eng review")).toBe("eng");
    expect(parsePromptSelectorFlag("--legal review")).toBeUndefined();
  });

  it("keeps existing personas when the API returns an empty payload", async () => {
    setPersonaRegistryForTest({ eng: {}, legal: {} });
    const client = {
      http: {
        get: vi.fn().mockResolvedValue({ data: {} }),
      },
    };
    const bot = new SlackBot(client as unknown as CentaurClient);

    await (bot as unknown as { refreshPersonaRegistry(): Promise<void> }).refreshPersonaRegistry();

    // An empty `{}` is not enough evidence to wipe out overlay personas the
    // slackbot was already routing — `--legal` must still resolve.
    expect(parsePromptSelectorFlag("--legal review")).toBe("legal");
  });

  it("retries quickly after a fetch failure (does not lock out for the full TTL)", async () => {
    resetPersonaRegistryForTest();
    const get = vi
      .fn()
      .mockRejectedValueOnce(new Error("transient"))
      .mockResolvedValueOnce({ data: { eng: {}, legal: {} } });
    const bot = new SlackBot({ http: { get } } as unknown as CentaurClient);

    await (bot as unknown as { refreshPersonaRegistry(): Promise<void> }).refreshPersonaRegistry();
    expect(parsePromptSelectorFlag("--legal review")).toBeUndefined();

    // Failure backoff defaults to 30s; the retry must happen well before the
    // 5-minute success TTL would have elapsed. Bumping the clock past the
    // backoff window simulates the next user message landing.
    const realNow = Date.now;
    try {
      const advancedBy = 35_000;
      Date.now = () => realNow() + advancedBy;
      await (bot as unknown as { refreshPersonaRegistry(): Promise<void> }).refreshPersonaRegistry();
    } finally {
      Date.now = realNow;
    }

    expect(get).toHaveBeenCalledTimes(2);
    expect(parsePromptSelectorFlag("--legal review")).toBe("legal");
  });
});

describe("extractFlagSelector — returns stripped text for the agent", () => {
  it("strips the flag token at start", () => {
    const { selector, cleaned } = extractFlagSelector("--invest hyperliquid miqs");
    expect(selector).toBe("invest");
    expect(cleaned).toBe("hyperliquid miqs");
  });

  it("strips the flag at end of string", () => {
    const { selector, cleaned } = extractFlagSelector("hyperliquid miqs --invest");
    expect(selector).toBe("invest");
    expect(cleaned).toBe("hyperliquid miqs");
  });

  it("bare --invest strips to empty text", () => {
    const { selector, cleaned } = extractFlagSelector("--invest");
    expect(selector).toBe("invest");
    expect(cleaned).toBe("");
  });

  it("no flag leaves text untouched", () => {
    const { selector, cleaned } = extractFlagSelector("hyperliquid miqs");
    expect(selector).toBeUndefined();
    expect(cleaned).toBe("hyperliquid miqs");
  });

  it("collapses whitespace across multiple flags", () => {
    const { selector, cleaned } = extractFlagSelector("--invest --codex hyperliquid");
    expect(selector).toBe("codex");
    expect(cleaned).toBe("hyperliquid");
  });

  it("preserves DocSend/URL content in cleaned text when no flag present", () => {
    const { selector, cleaned } = extractFlagSelector("looking at this co https://docsend.com/view/s/abc");
    expect(selector).toBeUndefined();
    expect(cleaned).toBe("looking at this co https://docsend.com/view/s/abc");
  });

  it("ignores unknown --flags and leaves them in text (prevents unknown persona_id errors)", () => {
    const { selector, cleaned } = extractFlagSelector("set --rpc-url https://mainnet.infura.io");
    expect(selector).toBeUndefined();
    expect(cleaned).toBe("set --rpc-url https://mainnet.infura.io");
  });

  it("ignores --installed embedded in technical text", () => {
    const { selector, cleaned } = extractFlagSelector("is foundry --installed on this machine?");
    expect(selector).toBeUndefined();
    expect(cleaned).toBe("is foundry --installed on this machine?");
  });

  it("strips known flags but preserves unknown flags in the same message", () => {
    const { selector, cleaned } = extractFlagSelector("--invest check --rpc-url endpoint");
    expect(selector).toBe("invest");
    expect(cleaned).toBe("check --rpc-url endpoint");
  });
});

describe("bareFlagPrompt (dynamic persona invocation for bare flags)", () => {
  it("returns a generic persona prompt for bare --invest with no other content", () => {
    const { selector, cleaned } = extractFlagSelector("--invest");
    expect(bareFlagPrompt(selector, cleaned, 0)).toBe(
      "Introduce yourself briefly according to your active persona and ask what we should work on.",
    );
  });

  it("returns the generic prompt when only the bot mention remains after flag strip", () => {
    // After flag strip, this leaves "<@U0AH5TRP0H0>" — should still trigger the persona.
    const { selector, cleaned } = extractFlagSelector("<@U0AH5TRP0H0> --invest");
    expect(bareFlagPrompt(selector, cleaned, 0)).toBe(
      "Introduce yourself briefly according to your active persona and ask what we should work on.",
    );
  });

  it("returns undefined when --invest has a payload", () => {
    const { selector, cleaned } = extractFlagSelector("--invest hyperliquid miqs");
    expect(bareFlagPrompt(selector, cleaned, 0)).toBeUndefined();
  });

  it("returns undefined when there are attachments (user dropped a file)", () => {
    const { selector, cleaned } = extractFlagSelector("--invest");
    expect(bareFlagPrompt(selector, cleaned, 1)).toBeUndefined();
  });

  it("returns undefined for unknown flags (not in the live persona registry)", () => {
    resetPersonaRegistryForTest();
    const { selector, cleaned } = extractFlagSelector("--legal");
    // --legal isn't in the floor registry, so the selector is undefined
    expect(selector).toBeUndefined();
    expect(bareFlagPrompt(selector, cleaned, 0)).toBeUndefined();
  });

  it("returns undefined for harness flags (engine switch, not a persona to introduce)", () => {
    const { selector, cleaned } = extractFlagSelector("--codex");
    expect(selector).toBe("codex");
    expect(bareFlagPrompt(selector, cleaned, 0)).toBeUndefined();
  });

  it("returns undefined when no flag is present", () => {
    const { selector, cleaned } = extractFlagSelector("hey");
    expect(bareFlagPrompt(selector, cleaned, 0)).toBeUndefined();
  });
});

describe("prompt switch release", () => {
  it("retries once before succeeding", async () => {
    const releaseThread = vi
      .fn()
      .mockRejectedValueOnce(new Error("temporary"))
      .mockResolvedValueOnce({ ok: true });
    const bot = new SlackBot({ releaseThread, http: { get: vi.fn() } } as unknown as CentaurClient);

    await expect(
      (bot as unknown as { releaseForPromptSwitch(threadKey: string): Promise<boolean> })
        .releaseForPromptSwitch("slack:C:T"),
    ).resolves.toBe(true);

    expect(releaseThread).toHaveBeenCalledTimes(2);
  });

  it("posts a persona switch failure, stops typing, and aborts the workflow", async () => {
    const startWorkflowRun = vi.fn();
    const releaseThread = vi.fn().mockRejectedValue(new Error("still busy"));
    const client = { releaseThread, startWorkflowRun, http: { get: vi.fn() } };
    const bot = new SlackBot(client as unknown as CentaurClient);
    const post = vi.fn().mockResolvedValue({ id: "reply", edit: vi.fn() });
    const stopTyping = vi.fn().mockResolvedValue(undefined);
    const thread = {
      id: "slack:C:T",
      post,
      subscribe: vi.fn(),
      startTyping: vi.fn(),
      stopTyping,
    };

    await (bot as unknown as {
      bufferAndExecute(
        thread: unknown,
        text: string,
        parts: unknown[],
        delivery: Record<string, unknown>,
        promptSelectorOverride?: string,
      ): Promise<void>;
    }).bufferAndExecute(
      thread,
      "review this",
      [{ type: "text", text: "review this" }],
      {},
      "invest",
    );

    expect(releaseThread).toHaveBeenCalledTimes(2);
    expect(startWorkflowRun).not.toHaveBeenCalled();
    // Without stopTyping the indicator stays on after we abort the spawn,
    // confusing the user about whether the bot is still working.
    expect(stopTyping).toHaveBeenCalledTimes(1);
    expect(post).toHaveBeenCalledWith({
      markdown: "Could not switch persona - there's an active task that wouldn't release. Try again, or wait for the current task to finish.",
    });
  });
});

// ═══════════════════════════════════════════════════════════════════════════════
// 7. splitSlackMessage
// ═══════════════════════════════════════════════════════════════════════════════

describe("splitSlackMessage", () => {
  it("returns single chunk for short text", () => {
    const chunks = splitSlackMessage("Hello world", 100);
    expect(chunks).toEqual(["Hello world"]);
  });

  it("returns single chunk for text exactly at limit", () => {
    const text = "a".repeat(100);
    expect(splitSlackMessage(text, 100)).toEqual([text]);
  });

  it("splits on paragraph boundary", () => {
    const para1 = "a".repeat(60);
    const para2 = "b".repeat(60);
    const text = `${para1}\n\n${para2}`;
    const chunks = splitSlackMessage(text, 80);
    expect(chunks).toHaveLength(2);
    expect(chunks[0]).toBe(para1);
    expect(chunks[1]).toBe(para2);
  });

  it("splits on newline when no paragraph break available", () => {
    const line1 = "a".repeat(60);
    const line2 = "b".repeat(60);
    const text = `${line1}\n${line2}`;
    const chunks = splitSlackMessage(text, 80);
    expect(chunks).toHaveLength(2);
    expect(chunks[0]).toBe(line1);
    expect(chunks[1]).toBe(line2);
  });

  it("splits on space when no newline available", () => {
    const text = "word ".repeat(20).trim(); // 99 chars
    const chunks = splitSlackMessage(text, 50);
    expect(chunks.length).toBeGreaterThanOrEqual(2);
    for (const chunk of chunks) {
      expect(chunk.length).toBeLessThanOrEqual(50);
    }
  });

  it("hard cuts when no natural break point", () => {
    const text = "a".repeat(200);
    const chunks = splitSlackMessage(text, 100);
    expect(chunks).toHaveLength(2);
    expect(chunks[0]).toBe("a".repeat(100));
    expect(chunks[1]).toBe("a".repeat(100));
  });

  it("handles real-world long response with multiple paragraphs", () => {
    const paragraphs = Array.from({ length: 10 }, (_, i) => `Paragraph ${i}: ${"x".repeat(500)}`);
    const text = paragraphs.join("\n\n");
    const chunks = splitSlackMessage(text, 3900);
    for (const chunk of chunks) {
      expect(chunk.length).toBeLessThanOrEqual(3900);
    }
    // Reassembling should recover all content
    const reassembled = chunks.join("\n\n");
    for (const para of paragraphs) {
      expect(reassembled).toContain(para);
    }
  });

  it("uses Slack's 40k plain text message limit by default", () => {
    const text = "a".repeat(40_000);
    expect(splitSlackMessage(text)).toEqual([text]);
    const longText = "a".repeat(40_001);
    expect(splitSlackMessage(longText).length).toBeGreaterThanOrEqual(2);
  });
});

describe("rewriteSlackFileLinks", () => {
  it("rewrites workspace file links to the checked-out commit permalink", () => {
    const markdown = "See [bot.ts](file:///home/agent/workspace/services/slackbot/src/lib/bot/bot.ts#L1-L5).";

    expect(rewriteSlackFileLinks(markdown)).toBe(
      markdown,
    );

    expect(rewriteSlackFileLinks(markdown, {
      repoOwner: "paradigmxyz",
      repoName: "centaur",
      gitCommit: "490cd7aed56fb93efd52e4fa3dd06874d762d88a",
    })).toBe(
      "See [bot.ts](https://github.com/paradigmxyz/centaur/blob/490cd7aed56fb93efd52e4fa3dd06874d762d88a/services/slackbot/src/lib/bot/bot.ts#L1-L5).",
    );
  });

  it("rewrites mounted repo links to the checked-out commit when the repo matches", () => {
    const markdown = "See [bot.ts](file:///home/agent/github/paradigmxyz/centaur/services/slackbot/src/lib/bot/bot.ts#L1-L5).";

    expect(rewriteSlackFileLinks(markdown, {
      repoOwner: "paradigmxyz",
      repoName: "centaur",
      gitCommit: "490cd7aed56fb93efd52e4fa3dd06874d762d88a",
    })).toBe(
      "See [bot.ts](https://github.com/paradigmxyz/centaur/blob/490cd7aed56fb93efd52e4fa3dd06874d762d88a/services/slackbot/src/lib/bot/bot.ts#L1-L5).",
    );
  });

  it("falls back to main for other mounted repos when no exact ref is available", () => {
    const markdown = "See [bot.ts](file:///home/agent/github/paradigmxyz/centaur/services/slackbot/src/lib/bot/bot.ts#L1-L5).";

    expect(rewriteSlackFileLinks(markdown)).toBe(
      "See [bot.ts](https://github.com/paradigmxyz/centaur/blob/main/services/slackbot/src/lib/bot/bot.ts#L1-L5).",
    );
  });

  it("leaves non-file links unchanged", () => {
    const markdown = "Open [Centaur](https://github.com/paradigmxyz/centaur).";
    expect(rewriteSlackFileLinks(markdown)).toBe(markdown);
  });
});
