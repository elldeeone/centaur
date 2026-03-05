"use client";

import type { ComponentProps, Ref } from "react";
import Link from "next/link";
import { HarnessBadge } from "@/components/ui/harness-badge";
import { StateDot } from "@/components/ui/state-dot";
import { ParticipantAvatars } from "@/components/thread/participant-avatars";
import { Progress } from "@/components/ui/progress";
import { PHASES, type ThreadSummary } from "@/lib/types";
import { useElapsed } from "@/hooks/use-elapsed";
import { getThreadDisplayName, parseActivePhase, runningSubtitle } from "@/lib/thread-selectors";
import { isRunningState } from "@/lib/thread-ordering";
import { cn } from "@/lib/utils";

type ThreadSummaryCardProps = {
  thread: ThreadSummary;
  href: string;
  statusSubtitle?: string | null;
  density?: "compact" | "comfortable";
  isSelected?: boolean;
  className?: string;
  linkRef?: Ref<HTMLAnchorElement>;
  linkProps?: Omit<ComponentProps<typeof Link>, "href" | "className" | "children" | "prefetch"> & {
    [key: `data-${string}`]: string | undefined;
  };
};

function ThreadAge({ thread }: { thread: ThreadSummary }) {
  const elapsed = useElapsed(thread.last_activity, isRunningState(thread.state));
  return <span>{elapsed}</span>;
}

export function ThreadSummaryCard({
  thread,
  href,
  statusSubtitle,
  density = "comfortable",
  isSelected = false,
  className,
  linkRef,
  linkProps,
}: ThreadSummaryCardProps) {
  const compact = density === "compact";
  const activeState = isRunningState(thread.state);
  const resolvedStatusSubtitle = statusSubtitle ?? runningSubtitle(thread);
  const activePhase = parseActivePhase(thread);
  const phaseIndex = activePhase ? PHASES.indexOf(activePhase as (typeof PHASES)[number]) : -1;
  const progress = phaseIndex >= 0 ? ((phaseIndex + 1) / PHASES.length) * 100 : 0;
  const name = getThreadDisplayName(thread);
  const rawTask = thread.last_user_message || thread.first_message || thread.last_result || "";
  const taskPreview = rawTask.replace(/^\[[\w]+\]\s*/, "").replace(/\s+/g, " ").slice(0, compact ? 120 : 100);

  return (
    <Link
      href={href}
      prefetch={false}
      ref={linkRef}
      className={cn(
        "thread-surface-soft thread-action-transition group block rounded-xl no-underline text-inherit hover:border-border/70 hover:bg-accent/35 hover:shadow-sm active:scale-[0.998] focus-visible:ring-1 focus-visible:ring-ring",
        compact ? "px-3.5 py-3" : "p-4",
        activeState && "border-primary/40",
        thread.state === "error" && "border-destructive/45",
        isSelected && "border-primary/50 bg-accent/50",
        className,
      )}
      {...linkProps}
    >
      <div className="flex min-w-0 items-start justify-between gap-2">
        <div className="min-w-0">
          <div className="flex min-w-0 items-center gap-1.5">
            <HarnessBadge harness={thread.harness} className={compact ? "h-5 px-1.5 text-xs" : undefined} />
            <span className="truncate font-medium text-sm text-foreground text-balance">
              {name}
            </span>
            {!compact && thread.participants && thread.participants.length > 0 ? (
              <span className="hidden lg:inline-flex" aria-hidden="true">
                <ParticipantAvatars participants={thread.participants} size={20} decorative />
              </span>
            ) : null}
          </div>
          <div className="mt-1.5 flex items-center gap-1.5 text-[13px] text-muted-foreground">
            <span>
              {thread.turn_count} turn{thread.turn_count === 1 ? "" : "s"}
            </span>
            <span>·</span>
            <ThreadAge thread={thread} />
          </div>
        </div>
        <div
          className={cn(
            "inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-[11px] uppercase tracking-wide",
            thread.state === "error"
              ? "border-destructive/45 bg-destructive/10 text-destructive"
              : activeState
                ? "border-primary/45 bg-primary/10 text-primary"
                : "border-border/80 bg-background/60 text-muted-foreground",
          )}
        >
          <StateDot state={thread.state} className="size-2.5" />
          <span>{thread.state}</span>
        </div>
      </div>

      {resolvedStatusSubtitle ? (
        <div className="mt-1.5 line-clamp-1 text-xs text-muted-foreground text-pretty">
          {resolvedStatusSubtitle}
        </div>
      ) : null}
      {taskPreview ? (
        <div
          className={cn(
            "text-xs leading-relaxed text-muted-foreground/90",
            compact ? "mt-1 line-clamp-2" : "mt-1.5 line-clamp-1",
          )}
        >
          {taskPreview}
        </div>
      ) : null}
      {activePhase ? <Progress value={progress} className={cn("bg-muted/70", compact ? "mt-2 h-1" : "mt-3 h-1")} /> : null}
    </Link>
  );
}
