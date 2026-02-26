/** @jsxImportSource react */
"use client";

import { useEffect, useState, useCallback } from "react";
import Link from "next/link";

const BASE = process.env.NEXT_PUBLIC_BASE_PATH ?? "";

type Thread = {
  slack_thread_key: string;
  container_id: string;
  harness: string;
  agent_thread_id: string | null;
  state: string;
  created_at: number;
  last_activity: number;
  turn_count: number;
  last_result: string;
};

const HARNESS_COLORS: Record<string, { bg: string; fg: string }> = {
  amp: { bg: "rgba(0, 217, 255, 0.12)", fg: "#00d9ff" },
  "claude-code": { bg: "rgba(192, 132, 252, 0.12)", fg: "#c084fc" },
  codex: { bg: "rgba(52, 211, 153, 0.12)", fg: "#34d399" },
};

const STATE_COLORS: Record<string, string> = {
  running: "#22c55e",
  idle: "#52525b",
  working: "#f59e0b",
};

function timeAgo(ts: number): string {
  const diff = Date.now() / 1000 - ts;
  if (diff < 60) return `${Math.floor(diff)}s ago`;
  if (diff < 3600) return `${Math.floor(diff / 60)}m ago`;
  if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`;
  return `${Math.floor(diff / 86400)}d ago`;
}

export default function ThreadsPage() {
  const [threads, setThreads] = useState<Thread[]>([]);
  const [loading, setLoading] = useState(true);

  const fetchThreads = useCallback(async () => {
    try {
      const res = await fetch(`${BASE}/api/threads`);
      const data = await res.json();
      setThreads(data.threads || []);
    } catch {
      /* ignore */
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    fetchThreads();
    const interval = setInterval(fetchThreads, 5000);
    return () => clearInterval(interval);
  }, [fetchThreads]);

  return (
    <main style={styles.main}>
      <div style={styles.header}>
        <div>
          <h1 style={styles.title}>Threads</h1>
          <p style={styles.subtitle}>
            {threads.length} active agent{threads.length !== 1 ? "s" : ""}
          </p>
        </div>
        <button onClick={fetchThreads} className="refresh-btn" style={styles.refreshBtn}>
          ↻ Refresh
        </button>
      </div>

      {loading ? (
        <p style={styles.loading}>Loading…</p>
      ) : threads.length === 0 ? (
        <div style={styles.empty}>
          <div style={styles.emptyIcon}>⊘</div>
          <p style={styles.emptyText}>No active agent threads</p>
          <p style={styles.emptyHint}>
            Mention @tempo-ai in a Slack thread to start one
          </p>
        </div>
      ) : (
        <div style={styles.grid}>
          {threads.map((t) => {
            const hc = HARNESS_COLORS[t.harness] || { bg: "#27272a", fg: "#a1a1aa" };
            return (
              <Link
                key={t.slack_thread_key}
                href={`/threads/${encodeURIComponent(t.slack_thread_key)}`}
                className="thread-card"
                style={styles.card}
              >
                <div style={styles.cardHeader}>
                  <span
                    style={{
                      ...styles.harnessBadge,
                      backgroundColor: hc.bg,
                      color: hc.fg,
                    }}
                  >
                    {t.harness}
                  </span>
                  <div style={styles.stateGroup}>
                    <span
                      className={t.state === "working" ? "state-dot-working" : ""}
                      style={{
                        ...styles.stateDot,
                        backgroundColor: STATE_COLORS[t.state] || "#52525b",
                      }}
                    />
                    <span style={styles.stateLabel}>{t.state}</span>
                  </div>
                </div>

                <div style={styles.threadKey}>{t.slack_thread_key}</div>

                {t.agent_thread_id && (
                  <div style={styles.agentId}>
                    {t.agent_thread_id.slice(0, 24)}…
                  </div>
                )}

                <div style={styles.cardMeta}>
                  <span>
                    {t.turn_count} turn{t.turn_count !== 1 ? "s" : ""}
                  </span>
                  <span style={styles.metaSep}>·</span>
                  <span>{timeAgo(t.last_activity)}</span>
                </div>

                {t.last_result && (
                  <div style={styles.lastResult}>
                    {t.last_result.slice(0, 140)}
                    {t.last_result.length > 140 ? "…" : ""}
                  </div>
                )}
              </Link>
            );
          })}
        </div>
      )}
    </main>
  );
}

const styles: Record<string, React.CSSProperties> = {
  main: {
    minHeight: "100vh",
    backgroundColor: "#09090b",
    color: "#e4e4e7",
    fontFamily: 'Inter, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif',
    padding: "2rem",
    maxWidth: "1200px",
    margin: "0 auto",
  },
  header: {
    display: "flex",
    justifyContent: "space-between",
    alignItems: "flex-start",
    marginBottom: "2rem",
    paddingBottom: "1.25rem",
    borderBottom: "1px solid #1c1c1e",
  },
  title: {
    fontSize: "1.375rem",
    fontWeight: 600,
    color: "#fafafa",
    margin: 0,
    letterSpacing: "-0.02em",
  },
  subtitle: {
    fontSize: "0.8125rem",
    color: "#52525b",
    margin: "0.25rem 0 0",
  },
  refreshBtn: {
    background: "none",
    border: "1px solid #27272a",
    borderRadius: "6px",
    color: "#71717a",
    padding: "6px 14px",
    cursor: "pointer",
    fontSize: "0.8125rem",
    fontFamily: "inherit",
    fontWeight: 500,
    transition: "all 0.15s",
  },
  loading: {
    color: "#52525b",
    textAlign: "center",
    padding: "4rem 0",
    fontSize: "0.875rem",
  },
  empty: {
    textAlign: "center",
    padding: "5rem 0",
  },
  emptyIcon: {
    fontSize: "2rem",
    color: "#27272a",
    marginBottom: "1rem",
  },
  emptyText: {
    color: "#52525b",
    fontSize: "1rem",
    marginBottom: "0.375rem",
    fontWeight: 500,
  },
  emptyHint: {
    color: "#3f3f46",
    fontSize: "0.8125rem",
  },
  grid: {
    display: "grid",
    gridTemplateColumns: "repeat(auto-fill, minmax(380px, 1fr))",
    gap: "0.75rem",
  },
  card: {
    display: "block",
    backgroundColor: "#111113",
    border: "1px solid #1c1c1e",
    borderRadius: "10px",
    padding: "1.25rem",
    textDecoration: "none",
    color: "inherit",
    transition: "all 0.15s ease",
  },
  cardHeader: {
    display: "flex",
    alignItems: "center",
    justifyContent: "space-between",
    marginBottom: "0.75rem",
  },
  harnessBadge: {
    fontSize: "0.6875rem",
    fontWeight: 600,
    textTransform: "uppercase" as const,
    letterSpacing: "0.04em",
    padding: "3px 10px",
    borderRadius: "5px",
  },
  stateGroup: {
    display: "flex",
    alignItems: "center",
    gap: "0.375rem",
  },
  stateDot: {
    width: "7px",
    height: "7px",
    borderRadius: "50%",
  },
  stateLabel: {
    fontSize: "0.75rem",
    color: "#52525b",
    fontWeight: 500,
  },
  threadKey: {
    fontSize: "0.8125rem",
    color: "#a1a1aa",
    fontFamily: '"JetBrains Mono", "SF Mono", "Fira Code", monospace',
    marginBottom: "0.25rem",
    overflow: "hidden",
    textOverflow: "ellipsis",
    whiteSpace: "nowrap",
    fontWeight: 400,
  },
  agentId: {
    fontSize: "0.75rem",
    color: "#3f3f46",
    fontFamily: '"JetBrains Mono", "SF Mono", "Fira Code", monospace',
    marginBottom: "0.5rem",
  },
  cardMeta: {
    display: "flex",
    gap: "0.375rem",
    fontSize: "0.8125rem",
    color: "#52525b",
    marginBottom: "0.5rem",
    fontWeight: 500,
  },
  metaSep: {
    color: "#27272a",
  },
  lastResult: {
    fontSize: "0.8125rem",
    color: "#3f3f46",
    lineHeight: 1.5,
    borderTop: "1px solid #1c1c1e",
    paddingTop: "0.625rem",
    marginTop: "0.25rem",
    overflow: "hidden",
    textOverflow: "ellipsis",
    display: "-webkit-box",
    WebkitLineClamp: 2,
    WebkitBoxOrient: "vertical" as const,
  },
};
