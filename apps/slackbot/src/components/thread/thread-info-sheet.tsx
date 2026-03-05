"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import {
  CircleStop,
  Copy,
  ExternalLink,
  RefreshCw,
  X,
} from "lucide-react";
import { HarnessBadge } from "@/components/ui/harness-badge";
import { StateDot } from "@/components/ui/state-dot";
import { ParticipantAvatars } from "@/components/thread/participant-avatars";
import { cn } from "@/lib/utils";
import type { ThreadDetail } from "@/lib/types";

type TokenUsage = {
  input_tokens: number;
  output_tokens: number;
  total_tokens: number;
  cost_usd: number | null;
  estimated: boolean;
  authoritative: boolean;
  model: string | null;
};

type ThreadInfoSheetProps = {
  open: boolean;
  onClose: () => void;
  thread: ThreadDetail;
  tokenUsage: TokenUsage | null;
  elapsed: string;
  onRefresh: () => void;
  onStop?: () => void;
  canStop: boolean;
};

function getFocusableElements(container: HTMLElement): HTMLElement[] {
  const candidates = container.querySelectorAll<HTMLElement>(
    "a[href], button:not([disabled]), textarea:not([disabled]), input:not([disabled]), select:not([disabled]), [tabindex]:not([tabindex='-1'])",
  );
  return Array.from(candidates).filter((el) => !el.hasAttribute("disabled") && el.tabIndex >= 0);
}

function Stat({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div>
      <dt className="text-xs text-muted-foreground">{label}</dt>
      <dd className="text-sm font-mono tabular-nums text-foreground mt-0.5">{children}</dd>
    </div>
  );
}

function ContextBar({ percent }: { percent: number }) {
  const color = percent > 80 ? "bg-destructive" : percent > 50 ? "bg-primary/70" : "bg-primary";
  return (
    <div className="mt-1 h-1.5 overflow-hidden rounded-full bg-secondary">
      <div className={cn("h-full rounded-full transition-[width] duration-[var(--dur-base)] ease-out", color)} style={{ width: `${percent}%` }} />
    </div>
  );
}

export function ThreadInfoSheet({
  open,
  onClose,
  thread,
  tokenUsage,
  elapsed,
  onRefresh,
  onStop,
  canStop,
}: ThreadInfoSheetProps) {
  const sheetRef = useRef<HTMLDivElement>(null);
  const [dragY, setDragY] = useState(0);
  const dragStartRef = useRef<number | null>(null);
  const dragRafRef = useRef<number>(0);
  const dragPendingRef = useRef(0);
  const draggingRef = useRef(false);

  const handleTouchStart = useCallback((e: React.TouchEvent) => {
    const sheet = sheetRef.current;
    if (!sheet) return;
    const touchY = e.touches[0].clientY;
    const fromTop = touchY - sheet.getBoundingClientRect().top;
    if (sheet.scrollTop > 0 || fromTop > 80) {
      dragStartRef.current = null;
      draggingRef.current = false;
      return;
    }
    dragStartRef.current = touchY;
    draggingRef.current = true;
  }, []);

  const handleTouchMove = useCallback((e: React.TouchEvent) => {
    if (dragStartRef.current === null || !draggingRef.current) return;
    const delta = e.touches[0].clientY - dragStartRef.current;
    if (delta <= 0) return;
    e.preventDefault();
    dragPendingRef.current = delta;
    if (dragRafRef.current) return;
    dragRafRef.current = window.requestAnimationFrame(() => {
      dragRafRef.current = 0;
      setDragY(dragPendingRef.current);
    });
  }, []);

  const handleTouchEnd = useCallback(() => {
    const finalDragY = Math.max(dragY, dragPendingRef.current);
    if (dragRafRef.current) {
      window.cancelAnimationFrame(dragRafRef.current);
      dragRafRef.current = 0;
    }
    if (finalDragY > 100) {
      onClose();
    }
    setDragY(0);
    dragStartRef.current = null;
    dragPendingRef.current = 0;
    draggingRef.current = false;
  }, [dragY, onClose]);

  useEffect(() => {
    return () => {
      if (dragRafRef.current) {
        window.cancelAnimationFrame(dragRafRef.current);
      }
    };
  }, []);

  useEffect(() => {
    if (!open) {
      setDragY(0);
      return;
    }
    const sheet = sheetRef.current;
    const previousFocused = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    if (sheet) {
      const focusable = getFocusableElements(sheet);
      (focusable[0] ?? sheet).focus();
    }

    const onKey = (e: KeyboardEvent) => {
      if (!sheet) return;
      if (e.key === "Escape") {
        e.preventDefault();
        e.stopPropagation();
        onClose();
        return;
      }
      if (e.key !== "Tab") return;
      const focusable = getFocusableElements(sheet);
      if (focusable.length === 0) {
        e.preventDefault();
        sheet.focus();
        return;
      }
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      const active = document.activeElement;
      if (e.shiftKey) {
        if (active === first || !sheet.contains(active)) {
          e.preventDefault();
          last.focus();
        }
      } else if (active === last || !sheet.contains(active)) {
        e.preventDefault();
        first.focus();
      }
    };
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("keydown", onKey);
      previousFocused?.focus();
    };
  }, [open, onClose]);

  const contextPercent = tokenUsage
    ? Math.max(0, Math.min(100, Math.round((tokenUsage.total_tokens / 200_000) * 100)))
    : 0;

  const keyParts = thread.slack_thread_key.startsWith("slack:")
    ? thread.slack_thread_key.replace(/^slack:/, "").split(":")
    : [];
  const channelId = keyParts[0] ?? "";
  const threadTs = keyParts[1] ?? "";
  const slackUrl =
    channelId && threadTs
      ? `slack://app_redirect?channel=${encodeURIComponent(channelId)}&thread_ts=${encodeURIComponent(threadTs)}`
      : "";

  function copyLink() {
    if (typeof window === "undefined") return;
    if (!navigator.clipboard?.writeText) return;
    const viewerUrl = `${window.location.origin}/${encodeURIComponent(thread.slack_thread_key)}`;
    void navigator.clipboard
      .writeText(viewerUrl)
      .then(() => onClose())
      .catch(() => {});
  }

  if (!open) return null;

  return (
    <div className="fixed inset-0 z-50 md:hidden" aria-modal="true" role="dialog" aria-label="Thread details">
      <div className="absolute inset-0 bg-black/60 backdrop-blur-[1px] animate-in fade-in duration-[var(--dur-base)] motion-reduce:animate-none" onClick={onClose} />
      <div
        ref={sheetRef}
        tabIndex={-1}
        className={cn(
          "absolute inset-x-0 bottom-0 max-h-[82dvh] overflow-y-auto overscroll-contain rounded-t-2xl border-t border-border/80 bg-card shadow-[0_-12px_36px_rgba(0,0,0,0.38)] will-change-transform animate-in slide-in-from-bottom duration-[var(--dur-slow)] ease-[var(--ease-emphasized)] motion-reduce:animate-none",
          dragY > 0 ? "transition-none" : "transition-transform duration-[var(--dur-slow)] ease-[var(--ease-emphasized)]",
        )}
        style={{ transform: dragY > 0 ? `translateY(${dragY}px)` : undefined }}
        onTouchStart={handleTouchStart}
        onTouchMove={handleTouchMove}
        onTouchEnd={handleTouchEnd}
      >
        <div className="flex justify-center pt-3 pb-1">
          <div className="w-8 h-1 bg-border rounded-full" />
        </div>

        <div className="px-4 sm:px-5 pb-[max(1.25rem,env(safe-area-inset-bottom))]">
          <div className="flex items-center justify-between mt-2">
            <h2 className="text-lg font-semibold text-foreground">
              {thread.thread_name || thread.slack_thread_key}
            </h2>
            <button
              type="button"
              onClick={onClose}
              className="flex size-11 items-center justify-center rounded-md text-muted-foreground transition-colors duration-[var(--dur-fast)] hover:bg-accent hover:text-foreground"
              aria-label="Close"
              data-touch-target
            >
              <X className="size-4" />
            </button>
          </div>

          <div className="mt-1 flex items-center gap-2 text-sm text-muted-foreground">
            <HarnessBadge harness={thread.harness} />
            <span>·</span>
            <StateDot state={thread.state} />
            <span>{thread.state}</span>
            <span>·</span>
            <span>{elapsed}</span>
          </div>

          <dl className="mt-5 grid grid-cols-1 gap-3 sm:grid-cols-2 sm:gap-x-4 sm:gap-y-4">
            <Stat label="Tokens in">{tokenUsage?.input_tokens.toLocaleString() ?? "--"}</Stat>
            <Stat label="Tokens out">{tokenUsage?.output_tokens.toLocaleString() ?? "--"}</Stat>
            <Stat label="Cost">
              {tokenUsage?.cost_usd !== null && tokenUsage?.cost_usd !== undefined
                ? `$${tokenUsage.cost_usd.toFixed(4)}${tokenUsage.estimated ? "~" : ""}`
                : "--"}
            </Stat>
            <Stat label="Model">{tokenUsage?.model ?? "--"}</Stat>
            <Stat label="Turns">{thread.turns.length}</Stat>
            <div>
              <dt className="text-xs text-muted-foreground">Context</dt>
              <dd className="text-sm font-mono tabular-nums text-foreground mt-0.5">{contextPercent}%</dd>
              <ContextBar percent={contextPercent} />
            </div>
          </dl>

          {thread.participants && thread.participants.length > 0 && (
            <div className="mt-5 border-t border-border pt-4">
              <h3 className="mb-2 text-xs font-medium text-muted-foreground">Participants</h3>
              <ParticipantAvatars participants={thread.participants} size={28} max={10} decorative={false} />
            </div>
          )}

          <div className="mt-5 space-y-2 border-t border-border pt-4">
            <h3 className="mb-2 text-xs font-medium text-muted-foreground">Actions</h3>

            <button
              type="button"
              onClick={() => { onRefresh(); onClose(); }}
              className="flex min-h-11 w-full items-center gap-3 rounded-md px-2 py-3 text-left text-sm text-foreground transition-colors duration-[var(--dur-fast)] hover:bg-accent/70 active:bg-accent"
              data-touch-target
            >
              <RefreshCw className="size-5 text-muted-foreground" />
              Refresh thread
            </button>

            {canStop && onStop && (
              <button
                type="button"
                onClick={() => { onStop(); onClose(); }}
                className="flex min-h-11 w-full items-center gap-3 rounded-md px-2 py-3 text-left text-sm text-destructive transition-colors duration-[var(--dur-fast)] hover:bg-destructive/10 active:bg-accent"
                data-touch-target
              >
                <CircleStop className="size-5" />
                Stop agent
              </button>
            )}

            <button
              type="button"
              onClick={copyLink}
              className="flex min-h-11 w-full items-center gap-3 rounded-md px-2 py-3 text-left text-sm text-foreground transition-colors duration-[var(--dur-fast)] hover:bg-accent/70 active:bg-accent"
              data-touch-target
            >
              <Copy className="size-5 text-muted-foreground" />
              Copy link
            </button>

            {slackUrl ? (
              <a
                href={slackUrl}
                className="flex min-h-11 w-full items-center gap-3 rounded-md px-2 py-3 text-sm text-foreground no-underline transition-colors duration-[var(--dur-fast)] hover:bg-accent/70 active:bg-accent"
                data-touch-target
              >
                <ExternalLink className="size-5 text-muted-foreground" />
                Open in Slack
              </a>
            ) : null}
          </div>
        </div>
      </div>
    </div>
  );
}
