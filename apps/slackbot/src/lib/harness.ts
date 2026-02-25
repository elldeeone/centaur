/**
 * Harness execution — spawn agent CLI processes and stream output.
 *
 * Supports amp (--stream-json) and codex (--json) output formats.
 * Returns an AsyncIterable<string> that Chat SDK can post directly.
 */

import { spawn, type ChildProcess } from "child_process";

export type Harness = "amp" | "claude-code" | "codex";

/** Parse "harness=amp" or "harness=codex" from message text. */
export function extractHarness(text: string): { harness: Harness; cleanedText: string } {
  const match = text.match(/\bharness\s*=\s*(amp|claude-code|codex)\b/i);
  if (match) {
    const harness = match[1].toLowerCase() as Harness;
    const cleanedText = (text.slice(0, match.index) + text.slice(match.index! + match[0].length)).trim();
    return { harness, cleanedText };
  }
  return { harness: "amp", cleanedText: text };
}

function buildCommand(
  harness: Harness,
  message: string,
  threadId: string | null
): { cmd: string; args: string[] } {
  switch (harness) {
    case "amp":
      return {
        cmd: "amp",
        args: [
          "--no-ide",
          "--no-notifications",
          "--dangerously-allow-all",
          "--stream-json",
          ...(threadId ? ["threads", "continue", threadId] : []),
          "-x",
          message,
        ],
      };

    case "claude-code":
      return {
        cmd: "claude",
        args: [
          "--dangerously-skip-permissions",
          "--output-format", "stream-json",
          ...(threadId ? ["--continue", threadId] : []),
          "-p",
          message,
        ],
      };

    case "codex":
      return {
        cmd: "codex",
        args: [
          "exec",
          "--json",
          ...(threadId ? ["resume", threadId] : []),
          message,
        ],
      };
  }
}

interface AmpEvent {
  type: string;
  subtype?: string;
  session_id?: string;
  message?: {
    content?: Array<{ type: string; text?: string; name?: string }>;
  };
  result?: string;
  error?: string;
}

/** Translate codex JSON events to amp format. */
function translateCodexEvent(raw: Record<string, unknown>): AmpEvent | null {
  const type = raw.type as string;

  if (type === "thread.started") {
    return { type: "system", subtype: "init", session_id: raw.thread_id as string };
  }
  if (type === "item.completed") {
    const item = raw.item as Record<string, unknown> | undefined;
    if (item?.type === "agent_message" && item.text) {
      return { type: "assistant", message: { content: [{ type: "text", text: item.text as string }] } };
    }
  }
  if (type === "turn.completed") {
    const items = raw.items as Array<Record<string, unknown>> | undefined;
    const last = items?.filter((i) => i.type === "agent_message").pop();
    return { type: "result", result: (last?.text as string) || "" };
  }
  if (type === "error") {
    return { type: "error", error: raw.message as string };
  }
  return null;
}

export interface ExecResult {
  textStream: AsyncIterable<string>;
  /** Resolves to the agent thread ID once captured from the stream. */
  threadIdPromise: Promise<string | null>;
}

/**
 * Execute a message via a harness CLI and return a streamable result.
 *
 * The returned textStream is an AsyncIterable<string> that Chat SDK
 * can pass directly to thread.post() for native Slack streaming.
 */
export function executeMessage(
  harness: Harness,
  message: string,
  threadId: string | null
): ExecResult {
  const { cmd, args } = buildCommand(harness, message, threadId);

  let resolveThreadId: (id: string | null) => void;
  const threadIdPromise = new Promise<string | null>((resolve) => {
    resolveThreadId = resolve;
  });

  let threadIdResolved = false;

  const proc = spawn(cmd, args, {
    env: { ...process.env, FORCE_COLOR: "0" },
    stdio: ["ignore", "pipe", "pipe"],
  });

  async function* stream(): AsyncIterable<string> {
    let buf = "";
    let lastYieldedResult = false;

    try {
      for await (const chunk of proc.stdout! as AsyncIterable<Buffer>) {
        buf += chunk.toString("utf-8");

        while (buf.includes("\n")) {
          const idx = buf.indexOf("\n");
          const line = buf.slice(0, idx).trim();
          buf = buf.slice(idx + 1);

          if (!line) continue;

          let event: AmpEvent;
          try {
            const parsed = JSON.parse(line);
            if (harness === "codex") {
              const translated = translateCodexEvent(parsed);
              if (!translated) continue;
              event = translated;
            } else {
              event = parsed as AmpEvent;
            }
          } catch {
            continue; // non-JSON output
          }

          // Capture thread ID from init event
          if (event.type === "system" && event.subtype === "init" && event.session_id) {
            if (!threadIdResolved) {
              resolveThreadId(event.session_id);
              threadIdResolved = true;
            }
          }

          // Yield the final result text
          if (event.type === "result" && event.result) {
            yield event.result;
            lastYieldedResult = true;
          }

          // Yield assistant text (intermediate messages)
          if (event.type === "assistant" && event.message?.content) {
            for (const part of event.message.content) {
              if (part.type === "text" && part.text) {
                yield part.text;
              }
            }
          }

          // Yield errors
          if (event.type === "error" && event.error) {
            yield `❌ ${event.error}`;
          }
        }
      }
    } finally {
      if (!threadIdResolved) {
        resolveThreadId(null);
      }
    }

    // Process remaining buffer
    if (buf.trim()) {
      try {
        const event = JSON.parse(buf.trim()) as AmpEvent;
        if (event.type === "result" && event.result && !lastYieldedResult) {
          yield event.result;
        }
      } catch {
        // ignore
      }
    }
  }

  return { textStream: stream(), threadIdPromise };
}

/**
 * Kill a running harness process (for interrupts).
 */
export function killProcess(proc: ChildProcess): void {
  try {
    proc.kill("SIGINT");
  } catch {
    // already dead
  }
}
