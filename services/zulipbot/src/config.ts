import type { ZulipbotOptions } from './types'

export function loadOptions(env: NodeJS.ProcessEnv = process.env): ZulipbotOptions {
  return {
    apiKey: optionalEnv(env, 'ZULIPBOT_API_KEY') ?? optionalEnv(env, 'CENTAUR_API_KEY'),
    apiUrl: stringEnv(env, 'CENTAUR_API_URL', 'http://127.0.0.1:8080'),
    botEmail: requiredEnv(env, 'ZULIP_BOT_EMAIL'),
    botUserId: optionalNumberEnv(env, 'ZULIP_BOT_USER_ID'),
    deliveryChunkChars: numberEnv(env, 'ZULIP_DELIVERY_CHUNK_CHARS', 9000),
    eventsPath: stringEnv(env, 'ZULIP_EVENTS_PATH', '/api/webhooks/zulip'),
    harnessType: stringEnv(env, 'ZULIP_HARNESS', 'codex'),
    historyLimit: clamp(numberEnv(env, 'ZULIP_HISTORY_LIMIT', 50), 0, 200),
    idleTimeoutMs: optionalNumberEnv(env, 'SESSION_IDLE_TIMEOUT_MS'),
    maxDurationMs: optionalNumberEnv(env, 'SESSION_MAX_DURATION_MS'),
    personaId: optionalEnv(env, 'ZULIP_PERSONA_ID'),
    progressMaxMs: numberEnv(env, 'ZULIP_PROGRESS_MAX_MS', 120_000),
    progressPlaceholder: booleanEnv(env, 'ZULIP_PROGRESS_PLACEHOLDER', true),
    progressText: stringEnv(env, 'ZULIP_PROGRESS_TEXT', 'Working...'),
    progressUpdateMs: numberEnv(env, 'ZULIP_PROGRESS_UPDATE_MS', 10_000),
    site: requiredUrlEnv(env, 'ZULIP_SITE'),
    streamUpdateMs: numberEnv(env, 'ZULIP_STREAM_UPDATE_MS', 1500),
    webhookToken: optionalEnv(env, 'ZULIP_WEBHOOK_TOKEN'),
    zulipApiKey: requiredEnv(env, 'ZULIP_API_KEY')
  }
}

function optionalEnv(env: NodeJS.ProcessEnv, name: string): string | undefined {
  const value = env[name]?.trim()
  return value ? value : undefined
}

function requiredEnv(env: NodeJS.ProcessEnv, name: string): string {
  const value = optionalEnv(env, name)
  if (!value) throw new Error(`${name} is required`)
  return value
}

function requiredUrlEnv(env: NodeJS.ProcessEnv, name: string): string {
  const value = requiredEnv(env, name)
  try {
    return new URL(value).toString()
  } catch {
    throw new Error(`${name} must be a URL`)
  }
}

function stringEnv(env: NodeJS.ProcessEnv, name: string, fallback: string): string {
  return optionalEnv(env, name) ?? fallback
}

function numberEnv(env: NodeJS.ProcessEnv, name: string, fallback: number): number {
  return optionalNumberEnv(env, name) ?? fallback
}

function optionalNumberEnv(env: NodeJS.ProcessEnv, name: string): number | undefined {
  const value = optionalEnv(env, name)
  if (!value) return undefined
  const parsed = Number.parseInt(value, 10)
  if (!Number.isFinite(parsed) || parsed < 0) {
    throw new Error(`${name} must be a non-negative integer`)
  }
  return parsed
}

function booleanEnv(env: NodeJS.ProcessEnv, name: string, fallback: boolean): boolean {
  const value = optionalEnv(env, name)
  if (!value) return fallback
  if (['1', 'true', 'yes', 'on'].includes(value.toLowerCase())) return true
  if (['0', 'false', 'no', 'off'].includes(value.toLowerCase())) return false
  throw new Error(`${name} must be a boolean`)
}

function clamp(value: number, min: number, max: number): number {
  return Math.min(max, Math.max(min, value))
}
