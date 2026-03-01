import type { UIMessage } from "ai";
import type { LucideIcon } from "lucide-react";
import { categorizeToolCall, summarizeGroup, type Step, type ToolCall } from "@/lib/describe";

function asString(value: unknown): string {
  return typeof value === "string" ? value : "";
}

function asRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" ? (value as Record<string, unknown>) : {};
}

function outputToText(output: unknown): string | undefined {
  if (output === undefined || output === null) return undefined;
  if (typeof output === "string") return output;
  try {
    return JSON.stringify(output, null, 2);
  } catch {
    return String(output);
  }
}

function toolNameFromPart(part: Record<string, unknown>): string | null {
  if (typeof part.toolName === "string" && part.toolName) return part.toolName;
  const type = asString(part.type);
  if (type.startsWith("tool-")) return type.slice("tool-".length);
  return null;
}

export function stepsFromUiMessages(messages: UIMessage[]): Step[] {
  const steps: Step[] = [];
  const byId = new Map<string, number>();
  let pendingGroup: { id: string; category: string; icon: LucideIcon; calls: ToolCall[] } | null =
    null;

  const pushStep = (step: Step) => {
    const existingIndex = byId.get(step.id);
    if (existingIndex !== undefined) {
      steps[existingIndex] = step;
      return;
    }
    byId.set(step.id, steps.length);
    steps.push(step);
  };

  const flushGroup = () => {
    if (!pendingGroup || pendingGroup.calls.length === 0) return;
    pushStep({
      id: pendingGroup.id,
      type: "tool-group",
      icon: pendingGroup.icon,
      category: pendingGroup.category,
      summary: summarizeGroup(pendingGroup.category, pendingGroup.calls),
      calls: pendingGroup.calls,
    });
    pendingGroup = null;
  };

  for (const message of messages) {
    if (message.role !== "assistant") continue;
    for (const [partIndex, rawPart] of (message.parts ?? []).entries()) {
      const part = rawPart as Record<string, unknown>;
      const partType = asString(part.type);

      if (partType === "text") {
        const text = asString(part.text).trim();
        if (!text) continue;
        flushGroup();
        pushStep({
          id: `result-${message.id}-${partIndex}`,
          type: "result",
          text,
          streaming: asString(part.state) === "streaming",
        });
        continue;
      }

      if (partType === "reasoning") {
        const text = asString(part.text).trim();
        if (!text) continue;
        flushGroup();
        pushStep({
          id: `thinking-${message.id}-${partIndex}`,
          type: "thinking",
          text,
        });
        continue;
      }

      if (partType === "data-file-changes") {
        flushGroup();
        const data = asRecord(part.data);
        const changesRaw = Array.isArray(data.changes) ? data.changes : [];
        const changes = changesRaw
          .map((item) => asRecord(item))
          .map((item) => ({
            path: asString(item.path),
            kind: (asString(item.kind) as "add" | "delete" | "update") || "update",
          }))
          .filter((item) => item.path);
        if (changes.length > 0) {
          pushStep({
            id: `file-changes-${message.id}-${partIndex}`,
            type: "file-changes",
            changes,
          });
        }
        continue;
      }

      if (partType === "data-phase-progress") {
        const data = asRecord(part.data);
        const phase = asString(data.phase);
        if (!phase) continue;
        flushGroup();
        pushStep({
          id: `phase-${message.id}-${partIndex}-${phase}`,
          type: "phase",
          phase,
        });
        continue;
      }

      if (partType === "data-shell-command") {
        flushGroup();
        const data = asRecord(part.data);
        pushStep({
          id: `terminal-${message.id}-${partIndex}`,
          type: "terminal",
          description: "Ran shell command",
          command: asString(data.command),
          output: outputToText(data.output),
          exitCode: typeof data.exitCode === "number" ? data.exitCode : undefined,
        });
        continue;
      }

      if (partType === "data-user-message") {
        flushGroup();
        const data = asRecord(part.data);
        const text = asString(data.text).trim();
        if (!text) continue;
        const messageId = asString(data.id) || `user-${message.id}-${partIndex}`;
        pushStep({
          id: `user-message-${messageId}`,
          type: "user-message",
          text,
          source: asString(data.source) || undefined,
          userId: asString(data.user_id) || undefined,
          createdAt: asString(data.created_at) || undefined,
        });
        continue;
      }

      if (partType === "data-context-message") {
        flushGroup();
        const data = asRecord(part.data);
        const text = asString(data.text).trim();
        if (!text) continue;
        const contextItemId = asString(data.id) || `context-${message.id}-${partIndex}`;
        const turnId = Number(data.turn_id ?? 0);
        const groupId = turnId > 0 ? `context-group-${turnId}` : "context-group";
        const existingIndex = byId.get(groupId);
        const existing =
          existingIndex !== undefined
            ? (steps[existingIndex] as Extract<Step, { type: "context-group" }>)
            : null;
        const items = existing ? [...existing.items] : [];
        const itemIndex = items.findIndex((item) => item.id === contextItemId);
        const item = {
          id: contextItemId,
          text,
          source: asString(data.source) || undefined,
          userId: asString(data.user_id) || undefined,
          createdAt: asString(data.created_at) || undefined,
        };
        if (itemIndex >= 0) {
          items[itemIndex] = item;
        } else {
          items.push(item);
        }
        pushStep({
          id: groupId,
          type: "context-group",
          items: items.slice(-50),
        });
        continue;
      }

      if (partType === "dynamic-tool" || partType.startsWith("tool-")) {
        const toolName = toolNameFromPart(part);
        if (!toolName) continue;
        const toolInput = asRecord(part.input);
        const toolCallId = asString(part.toolCallId) || `${message.id}-${toolName}-${partIndex}`;
        const outputText = outputToText(part.output);
        const errorText = asString(part.errorText);
        const partState = asString(part.state);
        const call: ToolCall = {
          id: toolCallId,
          name: toolName,
          input: toolInput,
          output: outputText ?? (errorText || undefined),
          state:
            partState === "output-error"
              ? "error"
              : partState === "output-available"
                ? "done"
                : "loading",
        };

        if (toolName === "str_replace") {
          flushGroup();
          const path = asString(toolInput.path);
          const ext = path.split(".").pop()?.toLowerCase();
          pushStep({
            id: `diff-${toolCallId}`,
            type: "diff",
            file: path,
            lang: ext || "txt",
            oldStr: asString(toolInput.old ?? toolInput.old_str),
            newStr: asString(toolInput.new ?? toolInput.new_str),
            result: call.output,
          });
          continue;
        }

        if (toolName === "shell" || toolName === "bash") {
          flushGroup();
          pushStep({
            id: `terminal-${toolCallId}`,
            type: "terminal",
            description: "Ran shell command",
            command: asString(toolInput.command),
            output: call.output,
          });
          continue;
        }

        const { icon, category } = categorizeToolCall(toolName);
        if (pendingGroup && pendingGroup.category === category) {
          pendingGroup.calls.push(call);
        } else {
          flushGroup();
          pendingGroup = {
            id: `tool-group-${category}-${message.id}-${partIndex}`,
            category,
            icon,
            calls: [call],
          };
        }
      }
    }
  }

  flushGroup();
  const deduped: Step[] = [];
  for (const step of steps) {
    if (step.type === "result" && deduped.length > 0) {
      const previous = deduped[deduped.length - 1];
      const isStreamingReplay =
        previous.type === "result" &&
        previous.text === step.text &&
        (Boolean(previous.streaming) || Boolean(step.streaming));
      if (isStreamingReplay) {
        // Preserve completion when replayed duplicate arrives after a streaming fragment.
        if (previous.type === "result" && previous.streaming && !step.streaming) {
          previous.streaming = false;
        }
        continue;
      }
    }
    deduped.push(step);
  }
  return deduped;
}
