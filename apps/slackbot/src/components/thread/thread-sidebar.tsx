"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import {
  forwardRef,
  type KeyboardEvent as ReactKeyboardEvent,
  memo,
  useCallback,
  useEffect,
  useId,
  useImperativeHandle,
  useMemo,
  useRef,
  useState,
} from "react";
import { useVirtualizer } from "@tanstack/react-virtual";
import { Check, ChevronLeft, ChevronRight, ExternalLink, LinkIcon, LoaderCircle, RefreshCw } from "lucide-react";
import { ParticipantAvatars } from "@/components/thread/participant-avatars";
import { Progress } from "@/components/ui/progress";
import { HarnessBadge } from "@/components/ui/harness-badge";
import { StateDot } from "@/components/ui/state-dot";
import { useElapsed } from "@/hooks/use-elapsed";

import { BASE } from "@/lib/constants";
import { absoluteTime } from "@/lib/format";
import { threadName } from "@/lib/thread-name";
import { PHASES, type ThreadSummary } from "@/lib/types";
import { ScrollArea } from "@/components/ui/scroll-area";
import { cn } from "@/lib/utils";

type ThreadStatusFilter = "all" | "active" | "idle" | "error";

export type ThreadSidebarHandle = {
  focusSearch: () => void;
  focusSidebar: () => void;
};

type ThreadSidebarProps = {
  selectedThreadKey: string | null;
  collapsed: boolean;
  onCollapsedChange?: (collapsed: boolean) => void;
  onNavigate?: () => void;
  showCollapseToggle?: boolean;
  active?: boolean;
};

function parsePhaseFromMessage(message: string | undefined): string | null {
  const text = message ?? "";
  const match = text.match(/^\[([^\]]+)\]/);
  return match ? match[1].trim().toLowerCase() : null;
}

function parseActivePhase(thread: ThreadSummary): string | null {
  return parsePhaseFromMessage(thread.last_user_message) ?? parsePhaseFromMessage(thread.first_message);
}

function isRunningState(state: string): boolean {
  return state === "working" || state === "running";
}

function isTextInputTarget(target: EventTarget | null): boolean {
  return target instanceof HTMLElement && !!target.closest("input, textarea, select, [contenteditable='true']");
}

function runningSubtitle(thread: ThreadSummary): string | null {
  if (!isRunningState(thread.state)) return null;
  const phase = parseActivePhase(thread);
  if (phase) return `Working on ${phase}...`;
  return "Working...";
}

function ThreadAge({ thread }: { thread: ThreadSummary }) {
  const elapsed = useElapsed(thread.last_activity, isRunningState(thread.state));
  return <span title={absoluteTime(thread.last_activity ?? 0)}>{elapsed}</span>;
}

type DateGroup = "Today" | "Yesterday" | "This Week" | "Older";

function getDateGroup(epochSeconds: number): DateGroup {
  const now = new Date();
  const date = new Date(epochSeconds * 1000);
  const todayStart = new Date(now.getFullYear(), now.getMonth(), now.getDate());
  const yesterdayStart = new Date(todayStart.getTime() - 86400000);
  const weekStart = new Date(todayStart.getTime() - todayStart.getDay() * 86400000);

  if (date >= todayStart) return "Today";
  if (date >= yesterdayStart) return "Yesterday";
  if (date >= weekStart) return "This Week";
  return "Older";
}

function groupThreadsByDate(threads: ThreadSummary[]): { label: DateGroup; threads: ThreadSummary[] }[] {
  const order: DateGroup[] = ["Today", "Yesterday", "This Week", "Older"];
  const groups = new Map<DateGroup, ThreadSummary[]>();
  for (const thread of threads) {
    const group = getDateGroup(thread.last_activity ?? 0);
    const list = groups.get(group);
    if (list) {
      list.push(thread);
    } else {
      groups.set(group, [thread]);
    }
  }
  return order.filter((label) => groups.has(label)).map((label) => ({ label, threads: groups.get(label)! }));
}

type ThreadCardProps = {
  thread: ThreadSummary;
  isSelected: boolean;
  isFocused: boolean;
  statusSubtitle: string | null;
  cardRef: (node: HTMLAnchorElement | null) => void;
  onMouseEnter: () => void;
  onFocus: () => void;
  onClick: () => void;
};

const ThreadCard = memo(function ThreadCard({
  thread,
  isSelected,
  isFocused,
  statusSubtitle,
  cardRef,
  onMouseEnter,
  onFocus,
  onClick,
}: ThreadCardProps) {
  const name = thread.thread_name || threadName(thread.slack_thread_key);
  const href = `/${encodeURIComponent(thread.slack_thread_key)}`;
  const rawTask =
    thread.last_user_message || thread.first_message || thread.last_result || "";
  const taskPreview = rawTask
    .replace(/^\[[\w]+\]\s*/, "")
    .replace(/<([^|>]+)\|([^>]+)>/g, "$2")
    .replace(/<([^>]+)>/g, "$1")
    .replace(/\s+/g, " ")
    .slice(0, 120);
  const activeState = isRunningState(thread.state);
  const activePhase = parseActivePhase(thread);
  const phaseIndex = activePhase
    ? PHASES.indexOf(activePhase as (typeof PHASES)[number])
    : -1;
  const progress = phaseIndex >= 0 ? ((phaseIndex + 1) / PHASES.length) * 100 : 0;

  return (
    <Link
      ref={cardRef}
      href={href}
      prefetch={false}
      role="option"
      aria-selected={isSelected}
      aria-current={isSelected ? "page" : undefined}
      tabIndex={isFocused ? 0 : -1}
      onMouseEnter={onMouseEnter}
      onFocus={onFocus}
      onClick={onClick}
      className={cn(
        "group/card relative thread-sidebar-card block rounded-sm border border-border bg-card px-2.5 py-2 no-underline outline-none transition-colors hover:bg-accent/50 focus-visible:ring-1 focus-visible:ring-ring",
        isSelected && "border-l-2 border-l-primary bg-accent",
        activeState && "border-l-2 border-l-primary/70",
      )}
    >
      <ThreadCardActions threadKey={thread.slack_thread_key} />
      <div className="flex min-w-0 items-start justify-between gap-2">
        <div className="min-w-0">
          <div className="flex min-w-0 items-center gap-1.5">
            <HarnessBadge harness={thread.harness} className="h-5 px-1.5 text-[9px]" />
            <span className="truncate text-xs font-medium text-foreground">{name}</span>
          </div>
          <div className="mt-1 flex items-center gap-1 text-[11px] text-muted-foreground">
            <span>
              {thread.turn_count} turn{thread.turn_count === 1 ? "" : "s"}
            </span>
            <span>·</span>
            <ThreadAge thread={thread} />
            {thread.participants && thread.participants.length > 0 ? (
              <>
                <span>·</span>
                <ParticipantAvatars participants={thread.participants} size={16} />
              </>
            ) : null}
          </div>
        </div>
        <div className="inline-flex items-center gap-1 text-[10px] text-muted-foreground">
          <StateDot state={thread.state} className="size-2.5" />
          <span>{thread.state}</span>
        </div>
      </div>

      {statusSubtitle ? (
        <div className="mt-1 line-clamp-1 text-[11px] text-muted-foreground">{statusSubtitle}</div>
      ) : null}
      {taskPreview ? (
        <div className="mt-1.5 line-clamp-1 text-[11px] leading-relaxed text-muted-foreground/90">
          {taskPreview}
        </div>
      ) : null}
      {activeState && activePhase ? <Progress value={progress} className="mt-2 h-0.5 bg-muted" /> : null}
    </Link>
  );
});

function ThreadCardActions({ threadKey }: { threadKey: string }) {
  const [copied, setCopied] = useState(false);
  const slackKey = threadKey.startsWith("slack:") ? threadKey.slice(6) : null;

  function copyLink(e: React.MouseEvent) {
    e.preventDefault();
    e.stopPropagation();
    const url = `${window.location.origin}/${encodeURIComponent(threadKey)}`;
    void navigator.clipboard.writeText(url).then(() => {
      setCopied(true);
      setTimeout(() => setCopied(false), 1200);
    });
  }

  function openInSlack(e: React.MouseEvent) {
    e.preventDefault();
    e.stopPropagation();
    if (!slackKey) return;
    const parts = slackKey.split("-");
    if (parts.length >= 2) {
      const channel = parts[0];
      const ts = parts.slice(1).join("-");
      window.open(`https://slack.com/app_redirect?channel=${channel}&message_ts=${ts}`, "_blank");
    }
  }

  return (
    <div className="absolute right-1.5 top-1.5 hidden group-hover/card:flex items-center gap-0.5 z-10">
      <button
        type="button"
        onClick={copyLink}
        aria-label="Copy link"
        className="inline-flex size-6 items-center justify-center rounded-sm bg-card border border-border text-muted-foreground transition-colors hover:text-foreground hover:bg-accent"
      >
        {copied ? <Check className="size-3 text-green-500" /> : <LinkIcon className="size-3" />}
      </button>
      {slackKey && (
        <button
          type="button"
          onClick={openInSlack}
          aria-label="Open in Slack"
          className="inline-flex size-6 items-center justify-center rounded-sm bg-card border border-border text-muted-foreground transition-colors hover:text-foreground hover:bg-accent"
        >
          <ExternalLink className="size-3" />
        </button>
      )}
    </div>
  );
}

export const ThreadSidebar = forwardRef<ThreadSidebarHandle, ThreadSidebarProps>(function ThreadSidebar(
  {
    selectedThreadKey,
    collapsed,
    onCollapsedChange,
    onNavigate,
    showCollapseToggle = true,
    active = true,
  },
  ref,
) {
  const router = useRouter();
  const [threads, setThreads] = useState<ThreadSummary[]>([]);
  const [loading, setLoading] = useState(true);
  const [isRefreshing, setIsRefreshing] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  const [statusFilter, setStatusFilter] = useState<ThreadStatusFilter>("all");
  const [focusedThreadKey, setFocusedThreadKey] = useState<string | null>(null);
  const filterId = useId();
  const searchRef = useRef<HTMLInputElement>(null);
  const toggleRef = useRef<HTMLButtonElement>(null);
  const cardRefs = useRef<Record<string, HTMLAnchorElement | null>>({});
  const latestFetchIdRef = useRef(0);
  const detailPrefetchAtRef = useRef<Record<string, number>>({});

  const fetchThreads = useCallback(
    async (showRefreshIndicator = true) => {
      if (!active) return;
      const fetchId = latestFetchIdRef.current + 1;
      latestFetchIdRef.current = fetchId;
      if (showRefreshIndicator) setIsRefreshing(true);
      try {
        const res = await fetch(`${BASE}/api/threads`);
        if (!res.ok) {
          throw new Error(`threads fetch failed: ${res.status}`);
        }
        const data = (await res.json()) as { threads?: ThreadSummary[] };
        if (latestFetchIdRef.current !== fetchId) return;
        setThreads(Array.isArray(data.threads) ? data.threads : []);
        setError(null);
      } catch {
        if (latestFetchIdRef.current !== fetchId) return;
        setError("Unable to load threads.");
      } finally {
        if (latestFetchIdRef.current !== fetchId) return;
        setLoading(false);
        if (showRefreshIndicator) setIsRefreshing(false);
      }
    },
    [active],
  );

  useEffect(() => {
    if (!active) return;
    void fetchThreads(false);
    const interval = window.setInterval(() => {
      void fetchThreads(false);
    }, 5000);
    return () => window.clearInterval(interval);
  }, [active, fetchThreads]);

  const filteredThreads = useMemo(() => {
    const lowerQuery = query.trim().toLowerCase();
    return threads.filter((thread) => {
      if (statusFilter === "active" && !isRunningState(thread.state)) return false;
      if (statusFilter === "idle" && (isRunningState(thread.state) || thread.state === "error")) return false;
      if (statusFilter === "error" && thread.state !== "error") return false;
      if (!lowerQuery) return true;
      const haystack =
        `${thread.thread_name ?? ""} ${thread.first_message ?? ""} ${thread.last_result ?? ""} ${thread.slack_thread_key}`.toLowerCase();
      return haystack.includes(lowerQuery);
    });
  }, [query, statusFilter, threads]);

  const sortedThreads = useMemo(
    () =>
      [...filteredThreads].sort((a, b) => {
        const aActive = isRunningState(a.state) ? 1 : 0;
        const bActive = isRunningState(b.state) ? 1 : 0;
        if (aActive !== bActive) return bActive - aActive;
        return (b.last_activity ?? 0) - (a.last_activity ?? 0);
      }),
    [filteredThreads],
  );

  useEffect(() => {
    if (sortedThreads.length === 0) {
      setFocusedThreadKey(null);
      return;
    }
    const hasFocused = focusedThreadKey
      ? sortedThreads.some((thread) => thread.slack_thread_key === focusedThreadKey)
      : false;
    if (selectedThreadKey && sortedThreads.some((thread) => thread.slack_thread_key === selectedThreadKey)) {
      setFocusedThreadKey(selectedThreadKey);
      return;
    }
    if (!hasFocused) {
      setFocusedThreadKey(sortedThreads[0].slack_thread_key);
    }
  }, [focusedThreadKey, selectedThreadKey, sortedThreads]);

  const activeCount = useMemo(
    () => threads.filter((thread) => isRunningState(thread.state)).length,
    [threads],
  );

  const groupedThreads = useMemo(() => groupThreadsByDate(sortedThreads), [sortedThreads]);

  type VirtualItem = { kind: "header"; label: DateGroup } | { kind: "thread"; thread: ThreadSummary };
  const virtualItems = useMemo<VirtualItem[]>(() => {
    const items: VirtualItem[] = [];
    for (const group of groupedThreads) {
      items.push({ kind: "header", label: group.label });
      for (const thread of group.threads) {
        items.push({ kind: "thread", thread });
      }
    }
    return items;
  }, [groupedThreads]);

  const scrollContainerRef = useRef<HTMLDivElement>(null);
  const virtualizer = useVirtualizer({
    count: virtualItems.length,
    getScrollElement: () => scrollContainerRef.current,
    estimateSize: (index) => (virtualItems[index].kind === "header" ? 24 : 90),
    overscan: 8,
  });

  const prefetchThread = useCallback(
    (threadKey: string) => {
      const href = `/${encodeURIComponent(threadKey)}`;
      router.prefetch(href);

      // Cache thread summary for instant hydration on navigation
      const thread = sortedThreads.find(t => t.slack_thread_key === threadKey);
      if (thread) {
        try {
          sessionStorage.setItem(
            `thread:${threadKey}`,
            JSON.stringify(thread),
          );
        } catch {}
      }

      const now = Date.now();
      const previousAt = detailPrefetchAtRef.current[threadKey] ?? 0;
      if (now - previousAt < 15000) return;
      detailPrefetchAtRef.current[threadKey] = now;
      void fetch(`${BASE}/api/threads/detail?key=${encodeURIComponent(threadKey)}`, {
        cache: "force-cache",
      }).catch(() => {});
    },
    [router, sortedThreads],
  );

  const openThread = useCallback(
    (threadKey: string) => {
      // Cache summary for instant hydration before navigating
      const thread = sortedThreads.find(t => t.slack_thread_key === threadKey);
      if (thread) {
        try { sessionStorage.setItem(`thread:${threadKey}`, JSON.stringify(thread)); } catch {}
      }
      const href = `/${encodeURIComponent(threadKey)}`;
      router.push(href);
      onNavigate?.();
    },
    [onNavigate, router, sortedThreads],
  );

  const focusThreadAt = useCallback(
    (nextIndex: number) => {
      const next = sortedThreads[nextIndex];
      if (!next) return;
      setFocusedThreadKey(next.slack_thread_key);
      const node = cardRefs.current[next.slack_thread_key];
      node?.focus();
      node?.scrollIntoView({ block: "nearest" });
    },
    [sortedThreads],
  );

  const handleListKeyDown = useCallback(
    (event: ReactKeyboardEvent<HTMLElement>) => {
      if (event.metaKey || event.ctrlKey || event.altKey) return;
      if (isTextInputTarget(event.target)) return;
      if (sortedThreads.length === 0) return;

      const currentIndex = Math.max(
        0,
        sortedThreads.findIndex((thread) => thread.slack_thread_key === focusedThreadKey),
      );
      if (event.key === "ArrowDown" || event.key.toLowerCase() === "j") {
        event.preventDefault();
        focusThreadAt(Math.min(currentIndex + 1, sortedThreads.length - 1));
        return;
      }
      if (event.key === "ArrowUp" || event.key.toLowerCase() === "k") {
        event.preventDefault();
        focusThreadAt(Math.max(currentIndex - 1, 0));
        return;
      }
      if (event.key === "Home") {
        event.preventDefault();
        focusThreadAt(0);
        return;
      }
      if (event.key === "End") {
        event.preventDefault();
        focusThreadAt(sortedThreads.length - 1);
        return;
      }
      if (event.key === "Enter") {
        event.preventDefault();
        const current = sortedThreads[currentIndex];
        if (current) openThread(current.slack_thread_key);
      }
    },
    [focusThreadAt, focusedThreadKey, openThread, sortedThreads],
  );

  useImperativeHandle(
    ref,
    () => ({
      focusSearch: () => {
        if (collapsed) {
          toggleRef.current?.focus();
          return;
        }
        searchRef.current?.focus();
      },
      focusSidebar: () => {
        if (collapsed) {
          toggleRef.current?.focus();
          return;
        }
        const selected =
          selectedThreadKey && cardRefs.current[selectedThreadKey]
            ? cardRefs.current[selectedThreadKey]
            : focusedThreadKey
              ? cardRefs.current[focusedThreadKey]
              : null;
        if (selected) {
          selected.focus();
          selected.scrollIntoView({ block: "nearest" });
          return;
        }
        searchRef.current?.focus();
      },
    }),
    [collapsed, focusedThreadKey, selectedThreadKey],
  );

  const canToggle = showCollapseToggle && Boolean(onCollapsedChange);

  if (collapsed) {
    return (
      <div className="flex h-full w-full flex-col items-center justify-start pt-3 p-2">
        {canToggle ? (
          <button
            ref={toggleRef}
            type="button"
            onClick={() => onCollapsedChange?.(false)}
            aria-label="Expand sidebar"
            className="inline-flex size-8 items-center justify-center rounded-sm border border-border text-muted-foreground transition-colors hover:bg-accent hover:text-foreground focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
          >
            <ChevronRight className="size-4" />
          </button>
        ) : null}
      </div>
    );
  }

  return (
    <div className="flex h-full min-h-0 w-full flex-col" onKeyDown={handleListKeyDown}>
      <div className="border-b border-border px-3 py-2">
        <div className="flex items-center justify-between gap-2">
          <div className="min-w-0">
            <h2 className="truncate text-sm font-semibold text-foreground">Threads</h2>
            <p className="mt-0.5 text-[11px] text-muted-foreground">
              {activeCount} active agent{activeCount === 1 ? "" : "s"}
            </p>
          </div>
          <div className="flex items-center gap-1.5">
            <button
              type="button"
              onClick={() => void fetchThreads(true)}
              disabled={isRefreshing || !active}
              className="inline-flex items-center gap-1 rounded-sm border border-border px-2 py-1 text-[11px] text-muted-foreground transition-colors hover:bg-accent hover:text-foreground disabled:cursor-default disabled:opacity-60"
              aria-busy={isRefreshing}
            >
              <RefreshCw className={cn("size-3", isRefreshing ? "animate-spin" : "")} />
              {isRefreshing ? "..." : "Refresh"}
            </button>
            {canToggle ? (
              <button
                ref={toggleRef}
                type="button"
                onClick={() => onCollapsedChange?.(true)}
                aria-label="Collapse sidebar"
                className="inline-flex size-7 items-center justify-center rounded-sm border border-border text-muted-foreground transition-colors hover:bg-accent hover:text-foreground focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
              >
                <ChevronLeft className="size-4" />
              </button>
            ) : null}
          </div>
        </div>
        <div className="mt-2">
          <label htmlFor={filterId} className="sr-only">
            Filter threads
          </label>
          <input
            ref={searchRef}
            id={filterId}
            name={filterId}
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder="Filter threads… (/)"
            autoComplete="off"
            className="h-8 w-full rounded-sm border border-input bg-card px-2.5 text-xs text-foreground placeholder:text-muted-foreground focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
          />
        </div>
        <div className="mt-2 inline-flex w-full rounded-sm border border-border bg-card p-0.5 text-[11px]">
          {([
            { id: "all", label: "All" },
            { id: "active", label: "Active" },
            { id: "idle", label: "Idle" },
            { id: "error", label: "Error" },
          ] as const).map((item) => (
            <button
              key={item.id}
              type="button"
              onClick={() => setStatusFilter(item.id)}
              className={cn(
                "flex-1 rounded-[2px] px-1.5 py-1 text-center text-muted-foreground transition-colors",
                statusFilter === item.id && "bg-accent text-foreground",
              )}
            >
              {item.label}
            </button>
          ))}
        </div>
      </div>

      <ScrollArea viewportRef={scrollContainerRef} className="flex-1 min-h-0" viewportClassName="px-2.5 pt-2 pb-4" role="listbox" aria-label="Thread list">
        {loading ? (
          <div className="inline-flex w-full items-center justify-center gap-2 py-8 text-xs text-muted-foreground">
            <LoaderCircle className="size-3.5 animate-spin text-primary" />
            Loading threads...
          </div>
        ) : error && sortedThreads.length === 0 ? (
          <div className="space-y-2 py-8 text-center">
            <p className="text-xs text-destructive">{error}</p>
            <button
              type="button"
              onClick={() => void fetchThreads(true)}
              className="rounded-sm border border-border px-2 py-1 text-[11px] text-muted-foreground transition-colors hover:bg-accent hover:text-foreground"
            >
              Retry
            </button>
          </div>
        ) : sortedThreads.length === 0 ? (
          <div className="py-10 text-center text-xs text-muted-foreground">
            No threads match your filter.
          </div>
        ) : (
          <div style={{ height: virtualizer.getTotalSize(), position: "relative" }}>
            {virtualizer.getVirtualItems().map((vRow) => {
              const item = virtualItems[vRow.index];
              if (item.kind === "header") {
                return (
                  <div
                    key={`header-${item.label}`}
                    style={{
                      position: "absolute",
                      top: 0,
                      left: 0,
                      width: "100%",
                      height: vRow.size,
                      transform: `translateY(${vRow.start}px)`,
                    }}
                    className="z-10 bg-card/80 backdrop-blur-sm px-1 py-1 text-[10px] uppercase tracking-wider text-muted-foreground font-medium"
                  >
                    {item.label}
                  </div>
                );
              }
              const thread = item.thread;
              return (
                <div
                  key={thread.slack_thread_key}
                  style={{
                    position: "absolute",
                    top: 0,
                    left: 0,
                    width: "100%",
                    transform: `translateY(${vRow.start}px)`,
                  }}
                  ref={virtualizer.measureElement}
                  data-index={vRow.index}
                >
                  <div className="pb-1.5">
                    <ThreadCard
                      thread={thread}
                      isSelected={selectedThreadKey === thread.slack_thread_key}
                      isFocused={focusedThreadKey === thread.slack_thread_key}
                      statusSubtitle={runningSubtitle(thread)}
                      cardRef={(node) => {
                        cardRefs.current[thread.slack_thread_key] = node;
                      }}
                      onMouseEnter={() => prefetchThread(thread.slack_thread_key)}
                      onFocus={() => {
                        setFocusedThreadKey(thread.slack_thread_key);
                        prefetchThread(thread.slack_thread_key);
                      }}
                      onClick={() => {
                        setFocusedThreadKey(thread.slack_thread_key);
                        try { sessionStorage.setItem(`thread:${thread.slack_thread_key}`, JSON.stringify(thread)); } catch {}
                        onNavigate?.();
                      }}
                    />
                  </div>
                </div>
              );
            })}
          </div>
        )}
      </ScrollArea>
    </div>
  );
});
