import React from "react";

type ThreadDetailTelemetryProps = {
  state: string;
  turnCount: number;
  elapsed: string;
  activePhase: string | null;
};

export function ThreadDetailTelemetry({
  state,
  turnCount,
  elapsed,
  activePhase,
}: ThreadDetailTelemetryProps) {
  return (
    <div className="mb-3 grid grid-cols-2 gap-2 md:grid-cols-4">
      <div className="rounded-lg border border-border/70 bg-card/45 px-3 py-2">
        <p className="text-[11px] uppercase tracking-wide text-muted-foreground">State</p>
        <p className="mt-0.5 text-sm font-medium text-foreground">{state}</p>
      </div>
      <div className="rounded-lg border border-border/70 bg-card/45 px-3 py-2">
        <p className="text-[11px] uppercase tracking-wide text-muted-foreground">Turns</p>
        <p className="mt-0.5 text-sm font-medium text-foreground">{turnCount}</p>
      </div>
      <div className="rounded-lg border border-border/70 bg-card/45 px-3 py-2">
        <p className="text-[11px] uppercase tracking-wide text-muted-foreground">Elapsed</p>
        <p className="mt-0.5 text-sm font-medium text-foreground">{elapsed}</p>
      </div>
      <div className="rounded-lg border border-border/70 bg-card/45 px-3 py-2">
        <p className="text-[11px] uppercase tracking-wide text-muted-foreground">Phase</p>
        <p className="mt-0.5 line-clamp-1 text-sm font-medium text-foreground">{activePhase ?? "Idle"}</p>
      </div>
    </div>
  );
}
