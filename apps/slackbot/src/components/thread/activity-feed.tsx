"use client";

import {
  AlertTriangle,
  Check,
  ChevronRight,
  Copy,
  FileDiff,
  FilePenLine,
  MessagesSquare,
  TerminalSquare,
} from "lucide-react";
import { lazy, Suspense, useState } from "react";
import {
  Conversation,
  ConversationContent,
  ConversationScrollButton,
} from "@/components/ai-elements/conversation";
import {
  Message,
  MessageContent,
  MessageResponse,
} from "@/components/ai-elements/message";
import {
  Reasoning,
  ReasoningTrigger,
  ReasoningContent,
} from "@/components/ai-elements/reasoning";
import {
  Terminal,
  TerminalHeader,
  TerminalTitle,
  TerminalContent,
  TerminalCopyButton,
} from "@/components/ai-elements/terminal";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import type { Step } from "@/lib/describe";
import type { Participant } from "@/lib/types";
import { StepGroup } from "@/components/thread/step-group";

const DiffCard = lazy(() =>
  import("@/components/thread/diff-card").then((m) => ({ default: m.DiffCard })),
);

function CopyResultButton({ text }: { text: string }) {
  const [copied, setCopied] = useState(false);

  async function copyResult() {
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
      setTimeout(() => setCopied(false), 1400);
    } catch {
      setCopied(false);
    }
  }

  return (
    <button
      type="button"
      onClick={() => void copyResult()}
      aria-label="Copy result text"
      className="copy-btn ml-auto inline-flex items-center gap-1 rounded bg-secondary/80 text-muted-foreground text-[10px] px-2 py-1 transition-colors hover:text-foreground"
    >
      {copied ? <Check className="size-3 text-green-500" /> : <Copy className="size-3" />}
      <span className="md:hidden">{copied ? "Copied" : "Copy"}</span>
    </button>
  );
}

function sourceLabel(source?: string): string {
  const normalized = (source ?? "").trim().toLowerCase();
  if (!normalized) return "Unknown";
  if (normalized === "thread_ui") return "Thread Viewer";
  if (normalized === "slack") return "Slack";
  if (normalized === "slack_subscribed_message") return "Slack Thread";
  if (normalized === "api") return "API";
  return normalized.replace(/_/g, " ");
}

function initials(name: string): string {
  const words = name.trim().split(/\s+/).filter(Boolean);
  if (words.length === 0) return "?";
  if (words.length === 1) return words[0].slice(0, 2).toUpperCase();
  return `${words[0][0]}${words[1][0]}`.toUpperCase();
}

function renderStep(
  step: Step,
  key: string,
  participantsById: Map<string, Participant>,
  threadStopped?: boolean,
): React.ReactNode {
  if (step.type === "phase") {
    return (
      <div key={key} className="flex items-center gap-2 text-[10px] uppercase tracking-wider text-muted-foreground">
        <FileDiff aria-hidden="true" className="size-3 text-primary" />
        {step.phase}
      </div>
    );
  }

  if (step.type === "thinking") {
    return (
      <Reasoning
        key={key}
        duration={step.durationS}
        isStreaming={!step.durationS}
        defaultOpen={!step.durationS}
      >
        <ReasoningTrigger />
        <ReasoningContent>{step.text}</ReasoningContent>
      </Reasoning>
    );
  }

  if (step.type === "tool-group") {
    return <StepGroup key={key} icon={step.icon} summary={step.summary} calls={step.calls} threadStopped={threadStopped} />;
  }

  if (step.type === "diff") {
    return (
      <Suspense key={key} fallback={<div className="step-item h-16 rounded-sm border border-border bg-card animate-pulse" />}>
        <DiffCard file={step.file} lang={step.lang} oldStr={step.oldStr} newStr={step.newStr} />
      </Suspense>
    );
  }

  if (step.type === "terminal") {
    const output = [
      `$ ${step.command}`,
      step.output ?? "",
    ].filter(Boolean).join("\n");

    return (
      <Terminal key={key} output={output} isStreaming={false} className="step-item">
        <TerminalHeader>
          <TerminalTitle>{step.description}</TerminalTitle>
          <TerminalCopyButton />
        </TerminalHeader>
        <TerminalContent className="max-h-[240px] md:max-h-[320px]" />
      </Terminal>
    );
  }

  if (step.type === "file-changes") {
    return (
      <div key={key} className="step-item rounded-sm border border-border bg-card px-3 py-2">
        <div className="flex items-center gap-2 text-xs text-muted-foreground mb-1.5">
          <FilePenLine aria-hidden="true" className="size-3.5 text-primary" />
          File changes
        </div>
        <div className="space-y-1">
          {step.changes.map((change, index) => (
            <div key={`${change.path}-${index}`} className="text-xs font-mono text-muted-foreground">
              {change.kind} {change.path}
            </div>
          ))}
        </div>
      </div>
    );
  }

  if (step.type === "error") {
    return (
      <div key={key} className="step-item rounded-sm border border-destructive/30 bg-destructive/10 px-3 py-2 text-sm text-destructive flex items-center gap-2">
        <AlertTriangle aria-hidden="true" className="size-4 shrink-0" />
        {step.message}
      </div>
    );
  }

  if (step.type === "user-message") {
    const participant = step.userId ? participantsById.get(step.userId) : undefined;
    const displayName = participant?.name || step.userId || "User";
    const avatar = participant?.avatar_url ? (
      <img src={participant.avatar_url} alt="" width={28} height={28} className="size-7 rounded-full shrink-0" />
    ) : (
      <div className="flex size-7 items-center justify-center rounded-full bg-muted text-[11px] font-medium text-muted-foreground shrink-0">
        {initials(displayName)}
      </div>
    );
    return (
      <div key={key} className="step-item relative">
        <Tooltip>
          <TooltipTrigger asChild>
            <div className="absolute -left-9 top-2.5 cursor-default hidden md:block">{avatar}</div>
          </TooltipTrigger>
          <TooltipContent side="left" className="text-xs">{displayName}</TooltipContent>
        </Tooltip>
        <div className="rounded-lg border border-border/40 bg-secondary/40 px-3.5 py-2.5">
          <div className="whitespace-pre-wrap text-sm text-foreground">{step.text}</div>
        </div>
      </div>
    );
  }

  if (step.type === "context-group") {
    return (
      <details key={key} className="group step-item rounded-lg border border-border/40 bg-card/40">
        <summary className="list-none cursor-pointer px-3 py-2 min-h-[44px] flex items-center gap-2 text-xs text-muted-foreground [&::-webkit-details-marker]:hidden">
          <ChevronRight className="size-3.5 transition-transform group-open:rotate-90" />
          {step.items.length} message{step.items.length === 1 ? "" : "s"} in thread
        </summary>
        <div className="space-y-2 px-3 pb-3">
          {step.items.map((item) => {
            const participant = item.userId ? participantsById.get(item.userId) : undefined;
            const displayName = participant?.name || item.userId || "Thread participant";
            return (
              <div key={item.id} className="rounded border border-border/50 bg-background px-2 py-1.5">
                <div className="mb-1 flex items-center gap-2 text-[11px] text-muted-foreground">
                  <span className="text-foreground">{displayName}</span>
                  <span>{sourceLabel(item.source)}</span>
                </div>
                <div className="whitespace-pre-wrap text-xs text-muted-foreground">{item.text}</div>
              </div>
            );
          })}
        </div>
      </details>
    );
  }

  if (step.type === "result") {
    return (
      <Message key={key} from="assistant">
        <MessageContent>
          <div className="flex items-center gap-2 mb-1 text-xs text-muted-foreground">
            <MessagesSquare aria-hidden="true" className="size-3.5 text-primary" />
            Result
            <CopyResultButton text={step.text} />
          </div>
          <div className="relative prose-console">
            <MessageResponse>{step.text}</MessageResponse>
          </div>
        </MessageContent>
      </Message>
    );
  }

  return null;
}

export function ActivityFeed({
  steps,
  state,
  isStreaming,
  participants,
  chatStatus,
}: {
  steps: Step[];
  state?: string;
  isStreaming?: boolean;
  participants?: Participant[];
  chatStatus?: string;
}) {
  const activeCount = steps.length;
  const participantsById = new Map((participants || []).map((participant) => [participant.id, participant]));

  const ariaLive = isStreaming ? "off" : "polite";
  const showThinking = chatStatus === "submitted";

  return (
    <div className="relative flex-1 min-h-0 flex flex-col">
      <Conversation aria-live={ariaLive}>
        <ConversationContent className="gap-1.5 md:gap-4 px-4 md:pl-14 md:pr-5 pt-3 md:pt-4 pb-16 md:pb-20">
          {activeCount === 0 && !showThinking ? (
            <div className="h-full flex items-center justify-center text-sm text-muted-foreground gap-2">
              <TerminalSquare aria-hidden="true" className="size-4 text-primary" />
              {state === "idle" ? "No events yet. This thread is idle." : "Waiting for events…"}
            </div>
          ) : (
            <>
              {steps.map((step, index) => renderStep(step, `live-${index}`, participantsById, !isStreaming && state !== "running" && state !== "working"))}
              {showThinking && (
                <div className="step-item flex items-center gap-2 text-sm text-muted-foreground animate-pulse">
                  <TerminalSquare aria-hidden="true" className="size-4 text-primary" />
                  Thinking…
                </div>
              )}
            </>
          )}
        </ConversationContent>
        <ConversationScrollButton />
      </Conversation>
    </div>
  );
}