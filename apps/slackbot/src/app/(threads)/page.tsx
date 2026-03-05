"use client";

import { useCallback, useState } from "react";
import { useRouter } from "next/navigation";
import { Menu, MessageSquarePlus } from "lucide-react";
import { toast } from "sonner";
import { MessageInput } from "@/components/thread/message-input";
import { MobileTabBar } from "@/components/thread/mobile-tab-bar";
import { useThreadLayout } from "@/components/thread/thread-layout";
import { useHaptics } from "@/components/haptics-provider";

export default function NewSessionPage() {
  const router = useRouter();
  const { openMobileSidebar } = useThreadLayout();
  const { trigger } = useHaptics();
  const [sending, setSending] = useState(false);

  const handleSend = useCallback(
    async (message: string) => {
      const text = message.trim();
      if (!text || sending) return;
      setSending(true);

      const threadKey = `ui:${crypto.randomUUID()}`;
      const encoded = encodeURIComponent(threadKey);

      try {
        const res = await fetch("/api/agent/execute", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            slack_thread_key: threadKey,
            message: text,
            source: "thread_ui",
          }),
        });

        if (!res.ok) {
          const data = await res.json().catch(() => ({}));
          throw new Error(data.error ?? `Failed (${res.status})`);
        }

        router.push(`/${encoded}`);
      } catch (err) {
        toast(err instanceof Error ? err.message : "Failed to start session");
        setSending(false);
      }
    },
    [router, sending],
  );

  return (
    <div className="app-shell h-dvh md:h-full flex flex-col bg-background overflow-hidden">
      <div className="md:hidden flex items-center justify-between border-b border-border/60 bg-background/70 px-3 py-2 backdrop-blur-md">
        <button
          type="button"
          onClick={() => { trigger("light"); openMobileSidebar(); }}
          className="inline-flex size-10 items-center justify-center rounded-lg ui-control-icon"
          aria-label="Open thread list"
          data-touch-target
        >
          <Menu className="size-5" />
        </button>
        <span className="text-sm font-medium text-foreground">New Session</span>
        <span className="size-10" aria-hidden="true" />
      </div>
      <div className="flex-1 flex items-center justify-center px-4">
        <div className="text-center max-w-md">
          <div className="mx-auto mb-4 flex size-12 items-center justify-center rounded-xl border border-border/80 bg-card/60">
            <MessageSquarePlus className="size-6 text-muted-foreground" />
          </div>
          <h1 className="text-lg font-semibold text-foreground">New Session</h1>
          <p className="mt-1.5 text-sm text-muted-foreground">
            Start a conversation with the AI agent. Your session will appear in the sidebar.
          </p>
        </div>
      </div>

      <MessageInput
        mode={sending ? "running" : "idle"}
        onSend={handleSend}
      />

      <MobileTabBar activeThreadHref="/" hasRunningAgent={false} hasError={false} />
    </div>
  );
}
