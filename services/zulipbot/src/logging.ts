import type { ZulipbotLogger } from './types'

export const noopLogger: ZulipbotLogger = {
  debug: () => undefined,
  info: () => undefined,
  warn: () => undefined,
  error: () => undefined,
  child: () => noopLogger
}

export function nowMs(): number {
  return globalThis.performance?.now?.() ?? Date.now()
}

export function elapsedMs(startedAtMs: number): number {
  return Math.max(0, Math.round(nowMs() - startedAtMs))
}

export function errorMessage(error: unknown): string {
  if (error instanceof Error) return error.message
  return String(error)
}

export function traceLog(
  logger: ZulipbotLogger | undefined,
  event: string,
  fields: Record<string, unknown> = {}
): void {
  ;(logger ?? noopLogger).info(event, fields)
}
