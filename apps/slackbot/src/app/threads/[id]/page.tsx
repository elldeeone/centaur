/** @jsxImportSource react */
"use client";

import { useEffect, useState, useCallback } from "react";
import { useParams } from "next/navigation";
import Link from "next/link";

const BASE = process.env.NEXT_PUBLIC_BASE_PATH ?? "";

type ThreadEvent = {
  type: string;
  subtype?: string;
  session_id?: string;
  thread_id?: string;
  message?: {
    role?: string;
    content?: ContentBlock[];
  };
  content?: ContentBlock[];
  result?: string;
  error?: string;
  text?: string;
  item?: { type?: string; text?: string };
  items?: { type?: string; text?: string }[];
  [key: string]: unknown;
};

type ContentBlock = {
  type: string;
  text?: string;
  name?: string;
  id?: string;
  input?: Record<string, unknown>;
  tool_use_id?: string;
  content?: string | ContentBlock[];
};

type Turn = {
  turn_id: number;
  user_message: string;
  events: ThreadEvent[];
  result: string;
  started_at: number;
  finished_at: number | null;
  exit_code: number | null;
  timed_out: boolean;
  duration_s: number;
};

type ThreadDetail = {
  slack_thread_key: string;
  container_id: string;
  harness: string;
  agent_thread_id: string | null;
  state: string;
  created_at: number;
  last_activity: number;
  turns: Turn[];
  error?: string;
};

const HARNESS_COLORS: Record<string, { bg: string; fg: string }> = {
  amp: { bg: "rgba(0, 217, 255, 0.12)", fg: "#00d9ff" },
  "claude-code": { bg: "rgba(192, 132, 252, 0.12)", fg: "#c084fc" },
  codex: { bg: "rgba(52, 211, 153, 0.12)", fg: "#34d399" },
};

const MONO = '"JetBrains Mono", "SF Mono", "Fira Code", monospace';
const SANS = 'Inter, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif';

function timeAgo(ts: number): string {
  const diff = Date.now() / 1000 - ts;
  if (diff < 60) return `${Math.floor(diff)}s ago`;
  if (diff < 3600) return `${Math.floor(diff / 60)}m ago`;
  if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`;
  return `${Math.floor(diff / 86400)}d ago`;
}

function formatTime(ts: number): string {
  return new Date(ts * 1000).toLocaleTimeString([], {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}

function formatDuration(s: number): string {
  if (s < 60) return `${s.toFixed(1)}s`;
  const m = Math.floor(s / 60);
  const rem = Math.round(s % 60);
  return `${m}m ${rem}s`;
}

/** Render a tool_result content block */
function ToolResultView({ block }: { block: ContentBlock }) {
  const [expanded, setExpanded] = useState(false);
  const content =
    typeof block.content === "string"
      ? block.content
      : JSON.stringify(block.content, null, 2);
  const truncated = content.length > 500;
  const preview = truncated ? content.slice(0, 500) + "…" : content;

  return (
    <div style={s.toolResult}>
      <button
        onClick={() => setExpanded(!expanded)}
        className="tool-header"
        style={s.toolResultHeader}
      >
        <span style={s.chevron}>{expanded ? "▾" : "▸"}</span>
        <span style={s.toolResultLabel}>
          result → {block.tool_use_id?.slice(0, 12)}
        </span>
        <span style={s.toolResultSize}>
          {content.length.toLocaleString()} chars
        </span>
      </button>
      {expanded && <pre style={s.toolResultContent}>{content}</pre>}
      {!expanded && truncated && (
        <pre style={s.toolResultPreview}>{preview}</pre>
      )}
    </div>
  );
}

/** Render a single content block from an assistant message */
function ContentBlockView({ block }: { block: ContentBlock }) {
  const [expanded, setExpanded] = useState(false);

  if (block.type === "text" && block.text) {
    return <div style={s.textBlock}>{block.text}</div>;
  }

  if (block.type === "tool_use") {
    const inputStr = block.input
      ? JSON.stringify(block.input, null, 2)
      : "{}";
    return (
      <div style={s.toolCall}>
        <button
          onClick={() => setExpanded(!expanded)}
          className="tool-header"
          style={s.toolCallHeader}
        >
          <span style={s.chevron}>{expanded ? "▾" : "▸"}</span>
          <span style={s.toolCallName}>{block.name}</span>
          <span style={s.toolCallId}>{block.id?.slice(0, 12)}</span>
        </button>
        {expanded && <pre style={s.toolCallInput}>{inputStr}</pre>}
      </div>
    );
  }

  if (block.type === "tool_result") {
    return <ToolResultView block={block} />;
  }

  return null;
}

/** Render a single event from the stream */
function EventView({ event }: { event: ThreadEvent }) {
  if (event.type === "system" && event.subtype === "init") {
    return (
      <div style={s.systemEvent}>
        Session initialized: {event.session_id}
      </div>
    );
  }

  if (event.type === "assistant" && event.message?.content) {
    return (
      <div style={s.assistantEvent}>
        {event.message.content.map((block, i) => (
          <ContentBlockView key={i} block={block} />
        ))}
      </div>
    );
  }

  if (event.type === "tool" && event.content) {
    return (
      <div style={s.toolEvent}>
        {event.content.map((block, i) => (
          <ContentBlockView key={i} block={block} />
        ))}
      </div>
    );
  }

  if (event.type === "result" && event.result) {
    return null;
  }

  if (event.type === "error") {
    return (
      <div style={s.errorEvent}>
        ❌ {event.error || (typeof event.message === "string" ? event.message : "Unknown error")}
      </div>
    );
  }

  if (event.type === "thread.started") {
    return (
      <div style={s.systemEvent}>
        Codex thread started: {event.thread_id}
      </div>
    );
  }

  if (event.type === "item.completed" && event.item) {
    return (
      <div style={s.assistantEvent}>
        <div style={s.textBlock}>{event.item.text}</div>
      </div>
    );
  }

  if (event.type === "raw" && event.text) {
    return <div style={s.rawEvent}>{event.text}</div>;
  }

  return null;
}

/** A single turn in the conversation */
function TurnView({ turn, harness }: { turn: Turn; harness: string }) {
  const [collapsed, setCollapsed] = useState(false);

  return (
    <div style={s.turn}>
      <div style={s.turnHeader}>
        <button
          onClick={() => setCollapsed(!collapsed)}
          style={s.turnToggle}
        >
          {collapsed ? "▸" : "▾"}
        </button>
        <span style={s.turnNumber}>Turn {turn.turn_id}</span>
        <div style={s.turnMeta}>
          {turn.timed_out && <span style={s.turnBadgeError}>TIMEOUT</span>}
          {turn.exit_code !== null && turn.exit_code !== 0 && (
            <span style={s.turnBadgeError}>exit {turn.exit_code}</span>
          )}
          <span style={s.turnDuration}>{formatDuration(turn.duration_s)}</span>
          <span style={s.turnTime}>{formatTime(turn.started_at)}</span>
        </div>
      </div>

      {/* User message */}
      <div style={s.userMessage}>
        <div style={s.userLabel}>User</div>
        <div style={s.userText}>{turn.user_message}</div>
      </div>

      {/* Events */}
      {!collapsed && (
        <div style={s.events}>
          {turn.events.map((event, i) => (
            <EventView key={i} event={event} />
          ))}
        </div>
      )}

      {/* Result */}
      {turn.result && (
        <div style={s.turnResult}>
          <div style={s.resultLabel}>Result</div>
          <div style={s.resultText}>{turn.result}</div>
        </div>
      )}
    </div>
  );
}

export default function ThreadDetailPage() {
  const params = useParams();
  const threadKey = decodeURIComponent(params.id as string);
  const [thread, setThread] = useState<ThreadDetail | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const fetchThread = useCallback(async () => {
    try {
      const res = await fetch(
        `${BASE}/api/threads/detail?key=${encodeURIComponent(threadKey)}`
      );
      if (!res.ok) {
        setError(`Thread not found: ${threadKey}`);
        return;
      }
      const data = await res.json();
      if (data.error) {
        setError(data.error);
        return;
      }
      setThread(data);
      setError(null);
    } catch {
      setError("Failed to fetch thread");
    } finally {
      setLoading(false);
    }
  }, [threadKey]);

  useEffect(() => {
    fetchThread();
    const interval = setInterval(fetchThread, 3000);
    return () => clearInterval(interval);
  }, [fetchThread]);

  if (loading) {
    return (
      <main style={s.main}>
        <p style={s.loadingText}>Loading…</p>
      </main>
    );
  }

  if (error || !thread) {
    return (
      <main style={s.main}>
        <Link href="/threads" className="back-link" style={s.backLink}>
          ← Threads
        </Link>
        <p style={s.errorText}>{error || "Thread not found"}</p>
      </main>
    );
  }

  const hc = HARNESS_COLORS[thread.harness] || { bg: "#27272a", fg: "#a1a1aa" };

  return (
    <main style={s.main}>
      <Link href="/threads" className="back-link" style={s.backLink}>
        ← Threads
      </Link>

      {/* Thread header */}
      <div style={s.threadHeader}>
        <div style={s.threadHeaderTop}>
          <span style={{ ...s.harnessBadge, backgroundColor: hc.bg, color: hc.fg }}>
            {thread.harness}
          </span>
          <span
            className={thread.state === "working" ? "state-dot-working" : ""}
            style={{
              ...s.stateDot,
              backgroundColor:
                thread.state === "working"
                  ? "#f59e0b"
                  : thread.state === "running"
                    ? "#22c55e"
                    : "#52525b",
            }}
          />
          <span style={s.threadState}>{thread.state}</span>
          <button onClick={fetchThread} className="refresh-btn" style={s.refreshBtn}>
            ↻
          </button>
        </div>
        <div style={s.threadMeta}>
          <div style={s.metaRow}>
            <span style={s.metaLabel}>Thread</span>
            <span style={s.metaValue}>{thread.slack_thread_key}</span>
          </div>
          {thread.agent_thread_id && (
            <div style={s.metaRow}>
              <span style={s.metaLabel}>Agent ID</span>
              <span style={s.metaValue}>{thread.agent_thread_id}</span>
            </div>
          )}
          <div style={s.metaRow}>
            <span style={s.metaLabel}>Container</span>
            <span style={s.metaValue}>{thread.container_id}</span>
          </div>
          <div style={s.metaRow}>
            <span style={s.metaLabel}>Last active</span>
            <span style={s.metaValue}>{timeAgo(thread.last_activity)}</span>
          </div>
        </div>
      </div>

      {/* Turns */}
      {thread.turns.length === 0 ? (
        <div style={s.emptyTurns}>
          Container spawned, no messages executed yet
        </div>
      ) : (
        <div style={s.turnList}>
          {thread.turns.map((turn) => (
            <TurnView
              key={turn.turn_id}
              turn={turn}
              harness={thread.harness}
            />
          ))}
        </div>
      )}
    </main>
  );
}

const s: Record<string, React.CSSProperties> = {
  main: {
    minHeight: "100vh",
    backgroundColor: "#09090b",
    color: "#e4e4e7",
    fontFamily: SANS,
    padding: "2rem",
    maxWidth: "960px",
    margin: "0 auto",
  },
  loadingText: {
    color: "#52525b",
    textAlign: "center",
    padding: "4rem 0",
    fontSize: "0.875rem",
  },
  errorText: {
    color: "#ef4444",
    textAlign: "center",
    padding: "4rem 0",
    fontSize: "0.875rem",
  },
  backLink: {
    color: "#52525b",
    textDecoration: "none",
    fontSize: "0.8125rem",
    fontWeight: 500,
    display: "inline-block",
    marginBottom: "1.5rem",
    transition: "color 0.15s",
  },

  // Thread header
  threadHeader: {
    backgroundColor: "#111113",
    border: "1px solid #1c1c1e",
    borderRadius: "10px",
    padding: "1.25rem",
    marginBottom: "1.5rem",
  },
  threadHeaderTop: {
    display: "flex",
    alignItems: "center",
    gap: "0.625rem",
    marginBottom: "1rem",
  },
  harnessBadge: {
    fontSize: "0.6875rem",
    fontWeight: 600,
    textTransform: "uppercase" as const,
    letterSpacing: "0.04em",
    padding: "3px 10px",
    borderRadius: "5px",
  },
  stateDot: {
    width: "7px",
    height: "7px",
    borderRadius: "50%",
  },
  threadState: {
    fontSize: "0.8125rem",
    color: "#71717a",
    fontWeight: 500,
  },
  refreshBtn: {
    marginLeft: "auto",
    background: "none",
    border: "1px solid #27272a",
    borderRadius: "6px",
    color: "#71717a",
    padding: "4px 12px",
    cursor: "pointer",
    fontSize: "1rem",
    fontFamily: "inherit",
    transition: "all 0.15s",
  },
  threadMeta: {
    display: "grid",
    gridTemplateColumns: "1fr 1fr",
    gap: "0.5rem 1.5rem",
  },
  metaRow: {
    display: "flex",
    flexDirection: "column",
    gap: "0.125rem",
  },
  metaLabel: {
    fontSize: "0.6875rem",
    color: "#3f3f46",
    fontWeight: 600,
    textTransform: "uppercase" as const,
    letterSpacing: "0.04em",
  },
  metaValue: {
    color: "#a1a1aa",
    fontFamily: MONO,
    fontSize: "0.75rem",
    overflow: "hidden",
    textOverflow: "ellipsis",
    whiteSpace: "nowrap",
  },
  emptyTurns: {
    color: "#3f3f46",
    textAlign: "center",
    padding: "3rem 0",
    fontSize: "0.875rem",
    fontWeight: 500,
  },

  // Turns
  turnList: {
    display: "flex",
    flexDirection: "column",
    gap: "0.75rem",
  },
  turn: {
    backgroundColor: "#111113",
    border: "1px solid #1c1c1e",
    borderRadius: "10px",
    overflow: "hidden",
  },
  turnHeader: {
    display: "flex",
    alignItems: "center",
    gap: "0.625rem",
    padding: "0.75rem 1rem",
    borderBottom: "1px solid #18181b",
    backgroundColor: "#0c0c0e",
  },
  turnToggle: {
    background: "none",
    border: "none",
    color: "#3f3f46",
    cursor: "pointer",
    fontSize: "0.75rem",
    padding: 0,
    fontFamily: "inherit",
  },
  turnNumber: {
    fontWeight: 600,
    fontSize: "0.8125rem",
    color: "#a1a1aa",
    letterSpacing: "-0.01em",
  },
  turnMeta: {
    display: "flex",
    alignItems: "center",
    gap: "0.625rem",
    marginLeft: "auto",
  },
  turnTime: {
    fontSize: "0.75rem",
    color: "#3f3f46",
    fontFamily: MONO,
  },
  turnDuration: {
    fontSize: "0.75rem",
    color: "#52525b",
    fontFamily: MONO,
  },
  turnBadgeError: {
    fontSize: "0.625rem",
    fontWeight: 700,
    color: "#fca5a5",
    backgroundColor: "rgba(239, 68, 68, 0.1)",
    padding: "2px 8px",
    borderRadius: "4px",
    textTransform: "uppercase" as const,
    letterSpacing: "0.04em",
  },

  // User message
  userMessage: {
    padding: "0.875rem 1rem",
    borderBottom: "1px solid #18181b",
  },
  userLabel: {
    fontSize: "0.625rem",
    fontWeight: 700,
    textTransform: "uppercase" as const,
    letterSpacing: "0.05em",
    color: "#3b82f6",
    marginBottom: "0.375rem",
  },
  userText: {
    fontSize: "0.875rem",
    lineHeight: 1.6,
    color: "#d4d4d8",
    whiteSpace: "pre-wrap" as const,
  },

  // Events
  events: {
    padding: "0.625rem 1rem",
  },
  toolEvent: {
    padding: "0.125rem 0",
  },

  // System event
  systemEvent: {
    fontSize: "0.75rem",
    color: "#3f3f46",
    fontStyle: "italic" as const,
    padding: "0.25rem 0",
  },

  // Assistant event
  assistantEvent: {
    padding: "0.25rem 0",
  },
  textBlock: {
    fontSize: "0.875rem",
    lineHeight: 1.65,
    color: "#d4d4d8",
    whiteSpace: "pre-wrap" as const,
    padding: "0.25rem 0",
  },

  // Shared chevron
  chevron: {
    fontSize: "0.6875rem",
    color: "#3f3f46",
    width: "12px",
    display: "inline-block",
    flexShrink: 0,
  },

  // Tool call
  toolCall: {
    margin: "0.25rem 0",
    border: "1px solid #1c1c1e",
    borderRadius: "8px",
    overflow: "hidden",
  },
  toolCallHeader: {
    display: "flex",
    alignItems: "center",
    gap: "0.5rem",
    width: "100%",
    background: "#0f0f11",
    border: "none",
    color: "#a1a1aa",
    padding: "0.5rem 0.75rem",
    cursor: "pointer",
    fontSize: "0.8125rem",
    textAlign: "left" as const,
    fontFamily: "inherit",
    transition: "background-color 0.1s",
  },
  toolCallName: {
    fontWeight: 600,
    color: "#f59e0b",
    fontFamily: MONO,
    fontSize: "0.8125rem",
  },
  toolCallId: {
    marginLeft: "auto",
    fontSize: "0.6875rem",
    color: "#27272a",
    fontFamily: MONO,
  },
  toolCallInput: {
    margin: 0,
    padding: "0.75rem",
    backgroundColor: "#0a0a0c",
    fontSize: "0.75rem",
    fontFamily: MONO,
    color: "#52525b",
    overflow: "auto",
    maxHeight: "300px",
    borderTop: "1px solid #1c1c1e",
    whiteSpace: "pre-wrap" as const,
    wordBreak: "break-all" as const,
  },

  // Tool result
  toolResult: {
    margin: "0.25rem 0",
    border: "1px solid #1c1c1e",
    borderRadius: "8px",
    overflow: "hidden",
  },
  toolResultHeader: {
    display: "flex",
    alignItems: "center",
    gap: "0.5rem",
    width: "100%",
    background: "#0d0d10",
    border: "none",
    color: "#52525b",
    padding: "0.4rem 0.75rem",
    cursor: "pointer",
    fontSize: "0.75rem",
    textAlign: "left" as const,
    fontFamily: "inherit",
    transition: "background-color 0.1s",
  },
  toolResultLabel: {
    fontFamily: MONO,
    fontSize: "0.6875rem",
  },
  toolResultSize: {
    marginLeft: "auto",
    fontSize: "0.625rem",
    color: "#27272a",
    fontFamily: MONO,
  },
  toolResultContent: {
    margin: 0,
    padding: "0.75rem",
    backgroundColor: "#0a0a0c",
    fontSize: "0.6875rem",
    fontFamily: MONO,
    color: "#52525b",
    overflow: "auto",
    maxHeight: "400px",
    borderTop: "1px solid #1c1c1e",
    whiteSpace: "pre-wrap" as const,
    wordBreak: "break-all" as const,
  },
  toolResultPreview: {
    margin: 0,
    padding: "0.5rem 0.75rem",
    backgroundColor: "#0a0a0c",
    fontSize: "0.6875rem",
    fontFamily: MONO,
    color: "#27272a",
    overflow: "hidden",
    maxHeight: "60px",
    borderTop: "1px solid #1c1c1e",
    whiteSpace: "pre-wrap" as const,
    wordBreak: "break-all" as const,
  },

  // Error event
  errorEvent: {
    padding: "0.5rem 0.75rem",
    backgroundColor: "rgba(239, 68, 68, 0.08)",
    border: "1px solid rgba(239, 68, 68, 0.15)",
    borderRadius: "8px",
    fontSize: "0.8125rem",
    color: "#fca5a5",
    margin: "0.25rem 0",
  },

  // Raw event
  rawEvent: {
    fontSize: "0.75rem",
    color: "#27272a",
    fontFamily: MONO,
    padding: "0.125rem 0",
  },

  // Turn result
  turnResult: {
    padding: "0.875rem 1rem",
    borderTop: "1px solid #18181b",
    backgroundColor: "rgba(34, 197, 94, 0.04)",
  },
  resultLabel: {
    fontSize: "0.625rem",
    fontWeight: 700,
    textTransform: "uppercase" as const,
    letterSpacing: "0.05em",
    color: "#22c55e",
    marginBottom: "0.375rem",
  },
  resultText: {
    fontSize: "0.875rem",
    lineHeight: 1.65,
    color: "#bbf7d0",
    whiteSpace: "pre-wrap" as const,
  },
};
