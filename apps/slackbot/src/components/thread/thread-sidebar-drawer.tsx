"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import Link from "next/link";
import { Search, X } from "lucide-react";
import { HarnessBadge } from "@/components/ui/harness-badge";
import { StateDot } from "@/components/ui/state-dot";
import { cn } from "@/lib/utils";
import type { ThreadSummary } from "@/lib/types";
import { threadName as fallbackName } from "@/lib/thread-name";
import { absoluteTime, timeAgo } from "@/lib/format";

type StatusFilter = "all" | "active" | "idle" | "error";

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

function isRunningState(state: string): boolean {
  return state === "working" || state === "running";
}

type ThreadSidebarDrawerProps = {
  open: boolean;
  onClose: () => void;
  threads: ThreadSummary[];
  activeKey?: string;
};

function getFocusableElements(container: HTMLElement): HTMLElement[] {
  const candidates = container.querySelectorAll<HTMLElement>(
    "a[href], button:not([disabled]), textarea:not([disabled]), input:not([disabled]), select:not([disabled]), [tabindex]:not([tabindex='-1'])",
  );
  return Array.from(candidates).filter((el) => !el.hasAttribute("disabled") && el.tabIndex >= 0);
}

export function ThreadSidebarDrawer({ open, onClose, threads, activeKey }: ThreadSidebarDrawerProps) {
  const [query, setQuery] = useState("");
  const [statusFilter, setStatusFilter] = useState<StatusFilter>("all");
  const drawerRef = useRef<HTMLDivElement>(null);
  const dragStartXRef = useRef<number | null>(null);
  const [dragX, setDragX] = useState(0);

  const filtered = useMemo(() => {
    return threads.filter((t) => {
      if (statusFilter === "active" && !isRunningState(t.state)) return false;
      if (statusFilter === "idle" && t.state !== "idle" && t.state !== "stopped") return false;
      if (statusFilter === "error" && t.state !== "error") return false;
      if (!query.trim()) return true;
      const q = query.toLowerCase();
      const hay = `${t.thread_name ?? ""} ${t.first_message ?? ""} ${t.slack_thread_key}`.toLowerCase();
      return hay.includes(q);
    });
  }, [query, statusFilter, threads]);

  const dateGroups = useMemo(() => {
    const order: DateGroup[] = ["Today", "Yesterday", "This Week", "Older"];
    const groups = new Map<DateGroup, ThreadSummary[]>();
    for (const thread of filtered) {
      const group = getDateGroup(thread.last_activity ?? 0);
      const list = groups.get(group);
      if (list) list.push(thread);
      else groups.set(group, [thread]);
    }
    return order.filter((label) => groups.has(label)).map((label) => ({ label, threads: groups.get(label)! }));
  }, [filtered]);

  const handleTouchStart = useCallback((e: React.TouchEvent) => {
    dragStartXRef.current = e.touches[0].clientX;
  }, []);

  const handleTouchMove = useCallback((e: React.TouchEvent) => {
    if (dragStartXRef.current === null) return;
    const delta = dragStartXRef.current - e.touches[0].clientX;
    if (delta > 0) setDragX(delta);
  }, []);

  const handleTouchEnd = useCallback(() => {
    if (dragX > 80) {
      onClose();
      if (history.state?.drawer) history.back();
    }
    setDragX(0);
    dragStartXRef.current = null;
  }, [dragX, onClose]);

  const closeDrawer = useCallback(() => {
    onClose();
    if (history.state?.drawer) history.back();
  }, [onClose]);

  function handleSelectThread() {
    closeDrawer();
  }

  const handleBackdropClose = () => {
    closeDrawer();
  };

  useEffect(() => {
    if (!open) {
      setQuery("");
      setStatusFilter("all");
      setDragX(0);
      return;
    }

    const drawer = drawerRef.current;
    const previousFocused = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    if (drawer) {
      const focusable = getFocusableElements(drawer);
      (focusable[0] ?? drawer).focus();
    }

    history.pushState({ drawer: true }, "");
    const onPop = () => onClose();
    window.addEventListener("popstate", onPop);

    const onKey = (e: KeyboardEvent) => {
      if (!drawer) return;
      if (e.key === "Escape") {
        e.preventDefault();
        e.stopPropagation();
        closeDrawer();
        return;
      }
      if (e.key !== "Tab") return;
      const focusable = getFocusableElements(drawer);
      if (focusable.length === 0) {
        e.preventDefault();
        drawer.focus();
        return;
      }
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      const active = document.activeElement;
      if (e.shiftKey) {
        if (active === first || !drawer.contains(active)) {
          e.preventDefault();
          last.focus();
        }
      } else if (active === last || !drawer.contains(active)) {
        e.preventDefault();
        first.focus();
      }
    };
    document.addEventListener("keydown", onKey);

    return () => {
      window.removeEventListener("popstate", onPop);
      document.removeEventListener("keydown", onKey);
      previousFocused?.focus();
    };
  }, [closeDrawer, onClose, open]);

  if (!open) return null;

  return (
    <div className="fixed inset-0 z-50 md:hidden" aria-modal="true" role="dialog" aria-label="Thread list">
      <div className="absolute inset-0 bg-black/40" onClick={handleBackdropClose} />
      <div
        ref={drawerRef}
        tabIndex={-1}
        className="absolute inset-y-0 left-0 bg-background border-r border-border shadow-2xl overflow-y-auto animate-in slide-in-from-left duration-250"
        style={{
          width: "min(300px, 85vw)",
          transform: dragX > 0 ? `translateX(-${dragX}px)` : undefined,
        }}
        onTouchStart={handleTouchStart}
        onTouchMove={handleTouchMove}
        onTouchEnd={handleTouchEnd}
      >
        <div className="sticky top-0 z-10 bg-background p-3 border-b border-border space-y-2">
          <div className="relative">
            <Search className="absolute left-3 top-1/2 -translate-y-1/2 size-4 text-muted-foreground" />
            <input
              type="text"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder="Filter threads…"
              className="w-full bg-secondary/50 border border-border/50 rounded-lg pl-9 pr-8 py-2 text-sm text-foreground placeholder:text-muted-foreground focus:outline-none focus:ring-1 focus:ring-ring"
              autoFocus
            />
            {query && (
              <button
                type="button"
                onClick={() => setQuery("")}
                className="absolute right-2 top-1/2 -translate-y-1/2 size-6 flex items-center justify-center rounded text-muted-foreground"
                aria-label="Clear search"
              >
                <X className="size-3.5" />
              </button>
            )}
          </div>
          <div className="inline-flex w-full rounded-md border border-border bg-card p-0.5 text-[11px]">
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
                  "flex-1 rounded-[3px] px-1.5 py-1.5 text-center text-muted-foreground transition-colors",
                  statusFilter === item.id && "bg-accent text-foreground",
                )}
              >
                {item.label}
              </button>
            ))}
          </div>
        </div>

        <div className="p-2 space-y-1">
          {filtered.length === 0 && (
            <p className="text-sm text-muted-foreground text-center py-8">No threads found</p>
          )}
          {dateGroups.map(({ label, threads: groupThreads }) => (
            <div key={label}>
              <div className="px-1 py-1.5 text-[10px] uppercase tracking-wider text-muted-foreground font-medium">
                {label}
              </div>
              {groupThreads.map((t) => {
                const name = t.thread_name || fallbackName(t.slack_thread_key);
                const href = `/${encodeURIComponent(t.slack_thread_key)}`;
                const isSelected = t.slack_thread_key === activeKey;
                const running = isRunningState(t.state);

                return (
                  <Link
                    key={t.slack_thread_key}
                    href={href}
                    onClick={handleSelectThread}
                    className={cn(
                      "block rounded-lg p-3 no-underline text-inherit transition-colors",
                      isSelected ? "bg-accent border-l-2 border-l-primary" : "active:bg-accent/50",
                      running && !isSelected && "border-l-2 border-l-green-500",
                      t.state === "error" && !isSelected && "border-l-2 border-l-destructive",
                    )}
                  >
                    <div className="flex items-center gap-2 min-w-0">
                      <HarnessBadge harness={t.harness} />
                      <span className="text-sm font-medium truncate flex-1 min-w-0">{name}</span>
                      <StateDot state={t.state} />
                    </div>
                    <div className="flex items-center gap-1.5 mt-1 text-[11px] text-muted-foreground">
                      <span>{t.state}</span>
                      <span>·</span>
                      <span>{t.turn_count}t</span>
                      <span>·</span>
                      <span title={absoluteTime(t.last_activity)}>{timeAgo(t.last_activity)}</span>
                    </div>
                  </Link>
                );
              })}
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}
