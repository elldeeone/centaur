import { describe, it, expect, vi } from "vitest";
import fs from "node:fs";
import path from "node:path";

import { ProgressTracker } from "../src/lib/bot/progress-tracker";
import { normalizeHarnessEvent, type CanonicalEvent } from "@centaur/harness-events";

// ─────────────────────────────────────────────────────────────────────────────
// Helpers
// ─────────────────────────────────────────────────────────────────────────────

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


