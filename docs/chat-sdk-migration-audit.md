# Chat SDK Migration Audit

## Summary

The current thread viewer uses a custom dual-path rendering pipeline:
1. **Historical path**: Postgres `agent_turns` → `stepsFromTurns()` → `Step[]` → `MessagePartRenderer`
2. **Live path**: SSE → `useChat` → `UIMessage[]` → `stepsFromUiMessages()` → `Step[]` → `MessagePartRenderer`

Both paths converge on the same `Step[]` type and the same renderer. The Chat SDK already handles
the dual-path merge natively via `initialMessages` + `resumeStream()`, making ~1000 lines of custom
step-extraction code unnecessary.

## Current Frontend → Chat SDK Mapping

### Hooks

| File | Current Logic | Chat SDK Equivalent | Action |
|------|--------------|---------------------|--------|
| `hooks/use-thread-stream.ts` (564 lines) | Dual-path merge: `stepsFromTurns(historicalTurns)` + `stepsFromUiMessages(chat.messages)` with semantic dedup via `mergeStepsPreferLive()` | `useChat({ initialMessages, resume: true })` — SDK merges natively | **Replace** — keep `useChat` setup, delete merge logic |
| Token usage parsing + merging (lines 14-97) | Custom `parseTokenUsage()`, `mergeTokenUsageSnapshots()` | `data-token-usage` data part (already working via `onData`) | **Keep** — data part handling stays |
| Reconnect retry (lines 457-495) | Manual 3-attempt retry with exponential backoff, fallback to polling | Transport's `reconnectToStream()` + SDK retry | **Simplify** — transport handles reconnect |
| Visibility handler (lines 498-515) | Refetch + reconnect after 30s hidden | Keep (SDK doesn't handle tab visibility) | **Keep** |

### Lib (Step Extraction)

| File | Lines | Purpose | Action |
|------|-------|---------|--------|
| `lib/turn-steps.ts` | 521 | Convert Postgres `Turn[]` → `Step[]` — massive switch on event types | **Delete** — once persistence stores `UIMessage[]`, render from parts directly |
| `lib/chat-steps.ts` | 519 | Convert `UIMessage[]` → `Step[]` — another massive switch | **Delete** — render from `message.parts` directly |
| `lib/describe.ts` | 456 | `Step` type union, `categorizeToolCall()`, `summarizeGroup()`, `describeToolCall()` | **Keep** — tool categorization/summarization still needed for grouped tool display |
| `lib/thread-hierarchy.ts` | 74 | `groupStepsByTurn()` — groups steps by turn ID | **Replace** — group by `step-start` part boundaries or by message |
| `lib/agent-transport.ts` | 138 | `AgentThreadTransport` implementing `ChatTransport` | **Simplify** — already correct pattern, minor cleanup |
| `lib/types.ts` | 77 | `Turn`, `ThreadDetail`, `ThreadTokenUsage`, `Participant` | **Delete** `Turn` type; keep rest |

### Components

| Component | File | Purpose | Chat SDK Part Type | Action |
|-----------|------|---------|-------------------|--------|
| `Conversation` | `ai-elements/conversation.tsx` | `StickToBottom` scroll wrapper | None (SDK has no scroll mgmt) | **Keep as-is** |
| `MessageResponse` | `ai-elements/message.tsx` | Markdown rendering (Streamdown) | `text` part | **Keep** — wire to `part.type === 'text'` |
| `Reasoning` | `ai-elements/reasoning.tsx` | Collapsible thinking block | `reasoning` part | **Keep** — wire to `part.type === 'reasoning'` |
| `Terminal` | `ai-elements/terminal.tsx` | Shell command + ANSI output | `data-shell-command` data part | **Keep** — wire to data part |
| `MessagePartRenderer` | `ai-elements/message-part-renderer.tsx` | `Step` → component switch | `message.parts.map()` | **Rewrite** — iterate over parts not steps |
| `StepGroup` | `thread/step-group.tsx` | Collapsed tool call list | `dynamic-tool` / `tool-*` parts | **Keep** — group consecutive tool parts |
| `ActivityFeed` | `thread/activity-feed.tsx` | Main feed: turns → groups → renderer | `messages.map(msg => msg.parts.map())` | **Rewrite** — render messages/parts directly |
| `DiffCard` | `thread/diff-card.tsx` | Inline diff display | `dynamic-tool` (str_replace) | **Keep** — render when tool part is str_replace |
| `Agent` (subagent) | `ai-elements/agent.tsx` | Subagent status card | `data-subagent` data part | **Keep** — wire to data part |
| `PhaseProgress` | `thread/phase-progress.tsx` | Phase indicator bar | `data-phase-progress` data part | **Keep** — wire to data part |

### Backend (Python)

| File | Current Role | End-State Role | Action |
|------|-------------|---------------|--------|
| `harness_events.py` | Raw harness → canonical events | Raw harness → UI Message Stream chunks | **Refactor** — output protocol chunks directly |
| `threads.py` `_ui_stream_chunks_for_event()` | Canonical events → protocol chunks | Merge with above | **Merge** into single conversion |
| `threads.py` `stream_ui` | Polling loop with dedup pre-scan | Simpler pass-through loop | **Simplify** — remove pre-scan, emit chunks inline |
| `agent_transport.py` | Turn tracking + result extraction + pipe | Pure stdin/stdout pipe | **Strip** — remove `_is_result_event`, `_extract_thread_id`, `_extract_result_text` |
| `agent.py` | Thread naming, persistence, slack posting | Container lifecycle only | **Strip** after webapp migration |

## Architecture After Migration

```
useChat({ initialMessages, transport, resume: true })
    │
    │ sendMessage() → POST /api/agent/execute
    │ reconnectToStream() → GET /api/threads/stream-ui
    │
    ▼
messages.map(message =>
  message.parts.map(part => {
    switch(part.type) {
      case 'text':           → <MessageResponse>
      case 'reasoning':      → <Reasoning>
      case 'dynamic-tool':   → <StepGroup> or <DiffCard> or <Terminal>
      case 'data-*':         → custom data components
      case 'step-start':     → turn boundary marker
      case 'error':          → <ErrorAlert>
    }
  })
)
```

## Execution Order

1. ✅ **Audit** (this document)
2. **Strip `agent_transport.py`** — make pure pipe (no turn tracking)
3. **Simplify SSE stream** — merge `harness_events.py` normalization with protocol chunk emission
4. **New part-based renderer** — rewrite `MessagePartRenderer` to consume `UIMessage.parts`
5. **New `useThreadStream`** — delete step extraction, use `initialMessages` + parts directly
6. **Persistence migration** — store `UIMessage[]` in Postgres, load as `initialMessages`
7. **Stream resumption** — wire `reconnectToStream` properly
8. **Cleanup** — delete `turn-steps.ts`, `chat-steps.ts`, old Step types
