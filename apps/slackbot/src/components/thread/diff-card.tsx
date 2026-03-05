"use client";

import { useMemo, useState } from "react";
import { diffLines } from "diff";
import { Badge } from "@/components/ui/badge";
import { MessageAction, MessageActions } from "@/components/ai-elements/message";
import { CopyIcon } from "lucide-react";
import { toast } from "sonner";

const LANGUAGE_CLASSES: Record<string, string> = {
  ts: "bg-primary/10 text-primary",
  tsx: "bg-primary/10 text-primary",
  js: "bg-primary/10 text-primary",
  jsx: "bg-primary/10 text-primary",
  py: "bg-primary/10 text-primary",
  css: "bg-primary/10 text-primary",
  json: "bg-primary/10 text-primary",
  md: "bg-secondary text-muted-foreground",
  sh: "bg-primary/10 text-primary",
};

export function DiffCard({
  file,
  lang,
  oldStr,
  newStr,
  result,
}: {
  file: string;
  lang: string;
  oldStr: string;
  newStr: string;
  result?: string;
}) {
  const didFail = result != null && result.toLowerCase().includes("error");
  const [expanded, setExpanded] = useState(false);
  const chunks = useMemo(() => diffLines(oldStr, newStr), [oldStr, newStr]);
  const rows = useMemo(() => {
    const collected: Array<{
      kind: "add" | "remove" | "context";
      text: string;
      oldLine: number | null;
      newLine: number | null;
    }> = [];
    let oldLine = 1;
    let newLine = 1;
    for (const chunk of chunks) {
      const kind = chunk.added ? "add" : chunk.removed ? "remove" : "context";
      const lines = chunk.value
        .split("\n")
        .filter((line, index, arr) => index < arr.length - 1 || line.length > 0);
      for (const line of lines) {
        if (kind === "add") {
          collected.push({ kind, text: line, oldLine: null, newLine });
          newLine += 1;
        } else if (kind === "remove") {
          collected.push({ kind, text: line, oldLine, newLine: null });
          oldLine += 1;
        } else {
          collected.push({ kind, text: line, oldLine, newLine });
          oldLine += 1;
          newLine += 1;
        }
      }
    }
    return collected;
  }, [chunks]);
  const canToggle = rows.length > 80;
  const visibleRows = expanded ? rows : rows.slice(0, 80);
  const hiddenCount = Math.max(0, rows.length - visibleRows.length);

  return (
    <div className={`overflow-hidden rounded-md border border-border/80 bg-card/55 shadow-[0_0_0_1px_rgba(255,255,255,0.02)] ${didFail ? "border-destructive/30" : ""}`}>
      <div className="flex items-center justify-between border-b border-border/80 bg-background/60 px-3 py-2">
        <div className="flex items-center gap-2 min-w-0">
          <Badge className={LANGUAGE_CLASSES[lang] ?? "bg-secondary text-muted-foreground"}>{lang}</Badge>
          <span className="font-mono text-xs text-foreground truncate">{file}</span>
          {didFail && <Badge variant="destructive" className="text-xs">failed</Badge>}
        </div>
        <MessageActions>
          <MessageAction
            tooltip="Copy diff"
            onClick={() => {
              void navigator.clipboard
                ?.writeText(`--- old\n${oldStr}\n+++ new\n${newStr}`)
                .then(() => toast("Diff copied"))
                .catch(() => {});
            }}
          >
            <CopyIcon className="size-3.5" />
          </MessageAction>
        </MessageActions>
      </div>
      <pre className="max-h-[360px] overflow-auto overscroll-contain p-3 text-xs font-mono">
        {visibleRows.map((row, i) => (
          <span
            key={`${i}-${row.oldLine ?? "x"}-${row.newLine ?? "x"}`}
            className={
              row.kind === "add"
                ? "grid grid-cols-[52px_1fr] bg-primary/10 text-foreground"
                : row.kind === "remove"
                  ? "grid grid-cols-[52px_1fr] bg-destructive/10 text-destructive"
                  : "grid grid-cols-[52px_1fr] text-muted-foreground"
            }
          >
            <span className="select-none pr-2 text-right text-xs text-muted-foreground/70">
              {row.newLine ?? row.oldLine ?? ""}
            </span>
            <span>{row.kind === "add" ? "+" : row.kind === "remove" ? "-" : " "} {row.text}</span>
          </span>
        ))}
      </pre>
      {canToggle && (
        <button
          type="button"
          onClick={() => setExpanded((value) => !value)}
          aria-expanded={expanded}
          className="w-full cursor-pointer border-t border-border/80 px-3 py-1.5 text-left text-xs text-muted-foreground transition-colors duration-[var(--dur-fast)] hover:bg-accent/50 hover:text-foreground"
        >
          {expanded ? "Show less context" : `Show ${hiddenCount} more lines`}
        </button>
      )}
    </div>
  );
}
