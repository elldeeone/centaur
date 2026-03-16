import type { CanonicalEvent } from "@centaur/harness-events";
import type { StreamChunk } from "chat";

/**
 * ProgressTracker — converts CanonicalEvents into Slack streaming chunks.
 *
 * Uses Slack's native AI streaming primitives:
 *   - `task_update`  → task_card blocks (individual agent steps)
 *   - `plan_update`  → plan block title (groups tasks under a heading)
 *   - `markdown_text` → streamed text content
 *
 * Task cards support `details` (markdown shown under the title while in progress)
 * and `output` (shown when complete). We use these to give visibility into what
 * tools, subagents, and commands are actually doing.
 */

type TaskStatus = "pending" | "in_progress" | "complete" | "error";
type ActiveTool = { name: string; input: Record<string, unknown> };

// The chat library's TaskUpdateChunk only has `output?: string`.
// Slack's API also supports `details` (markdown). The adapter passes
// chunks through as-is, so extra fields work at runtime.
type RichTaskChunk = StreamChunk & {
  details?: string;
  output?: string;
};

function taskChunk(
  id: string, title: string, status: TaskStatus,
  opts?: { details?: string; output?: string },
): StreamChunk {
  const chunk: StreamChunk = { type: "task_update", id, title, status };
  if (opts?.details) (chunk as any).details = opts.details;
  if (opts?.output) chunk.output = opts.output;
  return chunk;
}

export class ProgressTracker {
  /** Last assistant text block (used as fallback final answer). */
  lastAssistantText = "";
  /** Explicit result from turn.done. Takes priority over lastAssistantText. */
  resultText = "";
  /** Agent thread ID (Amp session ID from system.init). */
  agentThreadId = "";
  /** Overflow chunks when the final text exceeds Slack's message limit. */
  overflowChunks: string[] = [];
  /** Full markdown of the streamed message (used for post-stream table reformatting). */
  streamedMarkdown = "";

  private activeTools = new Map<string, ActiveTool>();
  private tasks = new Map<string, { title: string; status: TaskStatus }>();

  // ── Public API ───────────────────────────────────────────────────────────

  /** Process a canonical event and yield streaming chunks for Slack. */
  *update(event: CanonicalEvent): Generator<StreamChunk> {
    switch (event.type) {
      case "assistant":
        yield* this.onAssistant(event);
        break;
      case "tool":
        yield* this.onToolResult(event);
        break;
      case "subagent":
        yield* this.onSubagent(event);
        break;
      case "command_execution":
        yield* this.onCommand(event);
        break;
      case "result":
        this.resultText = event.text;
        break;
      case "error":
        yield { type: "markdown_text", text: `Error: ${event.error || "Unknown error"}` };
        break;
      case "system":
        if (event.subtype === "init" && event.session_id) {
          this.agentThreadId = event.session_id;
        }
        break;
      // reasoning, file_change, usage — no Slack output
    }
  }

  /** Finalize all in-progress tasks and set the plan title to "Completed". */
  *finalize(): Generator<StreamChunk> {
    for (const [id, task] of this.tasks) {
      if (task.status === "in_progress" || task.status === "pending") {
        task.status = "complete";
        yield taskChunk(id, task.title, "complete");
      }
    }
    yield { type: "plan_update", title: "Completed" };
  }

  /** Record a handoff as a completed task. */
  *addHandoff(goal: string): Generator<StreamChunk> {
    this.activeTools.clear();
    this.lastAssistantText = "";
    this.resultText = "";
    const id = `handoff-${Date.now()}`;
    const title = `Handed off → ${goal}`;
    this.tasks.set(id, { title, status: "complete" });
    yield taskChunk(id, title, "complete");
  }

  // ── Event handlers ─────────────────────────────────────────────────────

  private *onAssistant(event: Extract<CanonicalEvent, { type: "assistant" }>): Generator<StreamChunk> {
    if (!event.message?.content) return;
    let textInThisEvent = "";
    for (const block of event.message.content) {
      if (block.type === "tool_use") {
        this.lastAssistantText = "";
        this.activeTools.set(block.id, { name: block.name, input: block.input });
        const title = friendlyToolLabel(block.name, block.input);
        const details = toolDetails(block.name, block.input);
        this.tasks.set(block.id, { title, status: "in_progress" });
        yield taskChunk(block.id, title, "in_progress", { details });
        yield* this.emitPlanTitle(title);
      } else if (block.type === "text" && block.text) {
        textInThisEvent = block.text;
      }
    }
    if (textInThisEvent && this.activeTools.size === 0) {
      this.lastAssistantText = textInThisEvent;
    }
  }

  private *onToolResult(event: Extract<CanonicalEvent, { type: "tool" }>): Generator<StreamChunk> {
    if (!event.content) return;
    for (const block of event.content) {
      const active = this.activeTools.get(block.tool_use_id);
      if (!active) continue;
      this.activeTools.delete(block.tool_use_id);
      const status: TaskStatus = block.is_error ? "error" : "complete";
      const title = friendlyToolLabel(active.name, active.input, !block.is_error);
      const output = toolResultSummary(active.name, block.content, block.is_error);
      const task = this.tasks.get(block.tool_use_id);
      if (task) { task.title = title; task.status = status; }
      yield taskChunk(block.tool_use_id, title, status, { output });
    }
  }

  private *onSubagent(event: Extract<CanonicalEvent, { type: "subagent" }>): Generator<StreamChunk> {
    const label = event.name || "Subagent";
    const id = event.subagent_id;
    if (event.status === "started") {
      const title = `Subagent: ${label}`;
      this.tasks.set(id, { title, status: "in_progress" });
      yield taskChunk(id, title, "in_progress");
      yield* this.emitPlanTitle(title);
    } else if (event.status === "working") {
      const activities = (event.activities || []).map((a) => `- ${a.description}`).join("\n");
      const activity = event.activity || event.activities?.[0]?.description || "";
      const title = activity ? `Subagent: ${label} — ${truncate(activity, 60)}` : `Subagent: ${label}`;
      const task = this.tasks.get(id);
      if (task) { task.title = title; }
      yield taskChunk(id, title, "in_progress", { details: activities || undefined });
      yield* this.emitPlanTitle(title);
    } else if (event.status === "completed" || event.status === "failed") {
      const status: TaskStatus = event.status === "completed" ? "complete" : "error";
      const title = `Subagent: ${label}`;
      const output = event.summary || (event.error ? `Error: ${event.error}` : undefined);
      const task = this.tasks.get(id);
      if (task) { task.title = title; task.status = status; }
      yield taskChunk(id, title, status, { output });
    }
  }

  private *onCommand(event: Extract<CanonicalEvent, { type: "command_execution" }>): Generator<StreamChunk> {
    const id = `cmd-${simpleHash(event.command)}`;
    const isError = event.exit_code !== undefined && event.exit_code !== 0;
    const status: TaskStatus = isError ? "error" : "complete";
    const friendly = friendlyCommand(event.command, isError);
    const output = event.aggregated_output ? truncate(event.aggregated_output, 200) : undefined;
    this.tasks.set(id, { title: friendly.title, status });
    yield taskChunk(id, friendly.title, status, { details: friendly.details, output });
  }

  // ── Plan title ─────────────────────────────────────────────────────────

  private planTitle = "";

  private *emitPlanTitle(activityTitle: string): Generator<StreamChunk> {
    const newTitle = truncate(activityTitle, 80);
    if (newTitle !== this.planTitle) {
      this.planTitle = newTitle;
      yield { type: "plan_update", title: newTitle };
    }
  }
}

// ── Tool details (shown as `details` on in_progress task cards) ───────────

function toolDetails(name: string, input: Record<string, unknown>): string | undefined {
  const str = (key: string) => (typeof input[key] === "string" ? (input[key] as string) : "");
  switch (name) {
    case "Read":
      return str("path") ? `Reading \`${shortPath(str("path"))}\`` : undefined;
    case "edit_file":
      return str("path") ? `Editing \`${shortPath(str("path"))}\`` : undefined;
    case "create_file":
      return str("path") ? `Creating \`${shortPath(str("path"))}\`` : undefined;
    case "Bash":
      return str("cmd") ? `\`${truncate(str("cmd"), 120)}\`` : undefined;
    case "Grep":
      return str("pattern") ? `Pattern: \`${truncate(str("pattern"), 80)}\`` : undefined;
    case "finder":
      return str("query") ? truncate(str("query"), 150) : undefined;
    case "librarian":
      return str("query") ? truncate(str("query"), 200) : undefined;
    case "oracle":
      return str("task") ? truncate(str("task"), 200) : undefined;
    case "web_search":
      return str("objective") ? truncate(str("objective"), 150) : undefined;
    case "read_web_page":
      return str("url") || undefined;
    case "Task":
      return str("description") || str("prompt") ? truncate(str("description") || str("prompt"), 200) : undefined;
    case "look_at":
      return [str("path") && `File: \`${shortPath(str("path"))}\``, str("objective")].filter(Boolean).join("\n") || undefined;
    case "skill":
      return str("name") ? `Loading skill: ${str("name")}` : undefined;
    default:
      return undefined;
  }
}

// ── Tool result summary (shown as `output` on completed task cards) ──────

function toolResultSummary(name: string, content: unknown, isError: boolean): string | undefined {
  if (isError) {
    const text = extractText(content);
    return text ? `Error: ${truncate(text, 150)}` : "Error";
  }
  // For most tools, the result is too large / not useful to show in the card.
  // Only show output for tools where a brief summary is meaningful.
  switch (name) {
    case "finder":
    case "librarian":
    case "oracle":
    case "web_search": {
      const text = extractText(content);
      return text ? truncate(text, 200) : undefined;
    }
    default:
      return undefined;
  }
}

function extractText(content: unknown): string {
  if (typeof content === "string") return content;
  if (Array.isArray(content)) {
    return content
      .map((c) => (typeof c === "string" ? c : typeof c === "object" && c && "text" in c ? (c as { text: string }).text : ""))
      .join(" ")
      .trim();
  }
  if (typeof content === "object" && content && "text" in content) {
    return (content as { text: string }).text;
  }
  return "";
}

// ── Tool labels ───────────────────────────────────────────────────────────

const TOOL_VERBS: Record<string, [active: string, done: string]> = {
  Read: ["Reading", "Read"],
  Bash: ["Running", "Ran"],
  Grep: ["Searching", "Searched"],
  glob: ["Finding files", "Found files"],
  finder: ["Searching codebase", "Searched codebase"],
  edit_file: ["Editing", "Edited"],
  create_file: ["Creating file", "Created file"],
  Task: ["Running subtask", "Ran subtask"],
  web_search: ["Searching the web", "Searched the web"],
  read_web_page: ["Reading webpage", "Read webpage"],
  librarian: ["Researching codebase", "Researched codebase"],
  oracle: ["Consulting oracle", "Consulted oracle"],
  mermaid: ["Drawing diagram", "Drew diagram"],
  look_at: ["Analyzing file", "Analyzed file"],
  skill: ["Loading skill", "Loaded skill"],
};

function friendlyToolLabel(name: string, input: Record<string, unknown>, done?: boolean): string {
  const pair = TOOL_VERBS[name];
  const verb = pair ? pair[done ? 1 : 0] : name;
  const ctx = friendlyToolContext(name, input);
  return ctx ? `${verb} — ${ctx}` : verb;
}

function friendlyToolContext(name: string, input: Record<string, unknown>): string {
  const str = (key: string) => (typeof input[key] === "string" ? (input[key] as string) : "");
  switch (name) {
    case "Read": case "edit_file": case "create_file": case "look_at":
      return shortPath(str("path"));
    case "Bash":
      return friendlyBashContext(str("cmd"));
    case "Grep":
      return truncate(str("pattern"), 50);
    case "glob":
      return truncate(str("filePattern"), 50);
    case "finder":
      return truncate(str("query"), 60);
    case "web_search":
      return truncate(str("objective"), 60);
    case "read_web_page":
      return truncate(str("url"), 60);
    case "Task":
      return truncate(str("description"), 60);
    case "skill":
      return str("name");
    default:
      return summarizeInput(input);
  }
}

function friendlyBashContext(cmd: string): string {
  if (!cmd) return "";
  const trimmed = cmd.trim();
  const parsed = parseCallCommand(trimmed);
  if (parsed) return `${parsed.tool}.${parsed.method}`;
  return truncate(trimmed, 60);
}

/** Parse a `call <tool> <method> '<json>'` command into structured parts. */
function parseCallCommand(cmd: string): { tool: string; method: string; args?: Record<string, unknown> } | null {
  const match = cmd.match(/^call\s+(\S+)\s+(\S+)(?:\s+'(.+)')?$/s);
  if (!match) return null;
  const [, tool, method, jsonStr] = match;
  if (!jsonStr) return { tool, method };
  try {
    const args = JSON.parse(jsonStr);
    return { tool, method, args };
  } catch {
    return { tool, method };
  }
}

/** Human-readable title + details for a command_execution event. */
function friendlyCommand(cmd: string, isError: boolean): { title: string; details?: string } {
  const verb = isError ? "Failed" : "Ran";
  const trimmed = cmd.trim();
  const parsed = parseCallCommand(trimmed);

  if (parsed) {
    const label = `${parsed.tool}.${parsed.method}`;
    const title = `${verb} — ${label}`;

    // Extract a human-readable summary from the JSON args
    if (parsed.args && typeof parsed.args === "object") {
      const summary = humanizeToolArgs(parsed.tool, parsed.method, parsed.args);
      if (summary) return { title, details: summary };
    }
    return { title };
  }

  return { title: `${verb} — ${truncate(trimmed, 60)}` };
}

const TOOL_FRIENDLY_NAMES: Record<string, string> = {
  slack: "Slack",
  notion: "Notion",
  websearch: "Web",
  linear: "Linear",
  figma: "Figma",
  google_news: "Google News",
  telegram: "Telegram",
  twitter: "Twitter",
  alchemy: "Alchemy",
  allium: "Allium",
  dune: "Dune",
  etherscan: "Etherscan",
  nansen: "Nansen",
  posthog: "PostHog",
  crunchbase: "Crunchbase",
};

/** Turn tool args into a readable one-liner: "paradigm office email SF NYC" */
function humanizeToolArgs(tool: string, _method: string, args: Record<string, unknown>): string | undefined {
  // Pull out the most meaningful string value from the args
  const meaningful = args.query ?? args.search ?? args.prompt ?? args.message ?? args.q;
  if (typeof meaningful === "string" && meaningful.length > 0) {
    const friendly = TOOL_FRIENDLY_NAMES[tool] || tool;
    return `${friendly}: ${truncate(meaningful, 120)}`;
  }
  // Fallback: show all string values
  const parts: string[] = [];
  for (const [key, val] of Object.entries(args)) {
    if (typeof val === "string" && val.length > 0) {
      parts.push(`${key}: ${truncate(val, 60)}`);
    }
  }
  return parts.length > 0 ? parts.join(", ") : undefined;
}

function shortPath(p: string): string {
  if (!p) return "";
  const parts = p.split("/");
  return parts.length <= 3 ? p : `…/${parts.slice(-2).join("/")}`;
}

function truncate(s: string, max: number): string {
  if (!s) return "";
  const line = s.replace(/\n/g, " ").trim();
  return line.length > max ? `${line.slice(0, max)}…` : line;
}

function summarizeInput(input: Record<string, unknown>): string {
  for (const key of ["query", "pattern", "command", "cmd", "prompt", "path", "url", "message"]) {
    if (typeof input[key] === "string") return `${key}: "${input[key]}"`;
  }
  for (const [key, val] of Object.entries(input)) {
    if (typeof val === "string" && val.length > 0) return `${key}: "${val}"`;
  }
  return "";
}

function simpleHash(s: string): string {
  let h = 0;
  for (let i = 0; i < s.length; i++) {
    h = ((h << 5) - h + s.charCodeAt(i)) | 0;
  }
  return (h >>> 0).toString(36);
}
