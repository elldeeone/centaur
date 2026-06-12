import type { RustSessionStreamEvent } from '@centaur/harness-events'
import type { Hono } from 'hono'

export type JsonPrimitive = string | number | boolean | null
export type JsonValue = JsonPrimitive | JsonObject | JsonValue[]
export type JsonObject = { [key: string]: JsonValue | undefined }

export type ZulipbotFetch = (input: RequestInfo | URL, init?: RequestInit) => Promise<Response>

export type ZulipbotLogger = {
  debug(message: string, data?: Record<string, unknown>): void
  info(message: string, data?: Record<string, unknown>): void
  warn(message: string, data?: Record<string, unknown>): void
  error(message: string, data?: Record<string, unknown>): void
  child?(): ZulipbotLogger
}

export type ZulipbotOptions = {
  apiKey?: string
  apiUrl: string
  botEmail: string
  botUserId?: number
  deliveryChunkChars?: number
  eventsPath?: string
  fetch?: ZulipbotFetch
  harnessType?: string
  historyLimit?: number
  idleTimeoutMs?: number
  logger?: ZulipbotLogger
  maxDurationMs?: number
  personaId?: string
  progressMaxMs?: number
  progressPlaceholder?: boolean
  progressText?: string
  progressUpdateMs?: number
  site: string
  streamUpdateMs?: number
  webhookToken?: string
  zulipApiKey: string
  zulipFetch?: ZulipbotFetch
}

export type Zulipbot = {
  app: Hono
}

export type ZulipMessageType = 'stream' | 'private' | 'channel' | 'direct'

export type ZulipDisplayRecipient =
  | string
  | Array<{
      id?: number
      email?: string
      full_name?: string
      short_name?: string
      is_mirror_dummy?: boolean
    }>

export type ZulipOutgoingWebhookPayload = {
  data?: string
  trigger?: string
  token?: string
  bot_email?: string
  bot_full_name?: string
  message?: {
    id?: number
    type?: ZulipMessageType | string
    stream_id?: number
    subject?: string
    topic?: string
    display_recipient?: ZulipDisplayRecipient
    recipient_id?: number
    sender_id?: number
    sender_email?: string
    sender_full_name?: string
    sender_realm_str?: string
    timestamp?: number
    content?: string
    rendered_content?: string
  }
}

export type NormalizedPart = {
  type: 'text'
  text: string
}

export type NormalizedHistoryMessage = {
  message_id: string
  role: 'user' | 'assistant'
  user_id?: string
  parts: NormalizedPart[]
  metadata: JsonObject
}

export type NormalizedZulipEvent = {
  thread_key: string
  message_id: string
  realm: string
  user_id: string
  is_mention: boolean
  parts: NormalizedPart[]
  history_messages?: NormalizedHistoryMessage[]
  zulip: {
    message_id?: number
    message_type?: string
    stream_id?: number
    stream_name?: string
    topic?: string
    recipient_id?: number
    trigger?: string
    sender_email?: string
    sender_full_name?: string
    timestamp?: number
  }
  delivery: {
    platform: 'zulip'
    message_type: 'stream' | 'direct'
    stream_id?: number
    topic?: string
    recipient_ids?: number[]
    recipient_emails?: string[]
  }
}

export type ZulipbotApiAuthor = {
  email?: string
  fullName?: string
  isBot: boolean
  isMe: boolean
  userId?: string
  userName?: string
}

export type ZulipbotApiMessage = {
  author: ZulipbotApiAuthor
  context?: ZulipbotApiMessage[]
  id: string
  isMention: boolean
  metadata: JsonObject
  parts: JsonValue[]
  raw?: unknown
  role: ZulipbotSessionMessageRole
  text: string
  threadId: string
  timestamp?: string
}

export type ZulipbotSessionMessageRole = 'user' | 'assistant' | 'system' | 'tool'

export type ZulipbotSessionMessage = {
  client_message_id?: string
  metadata: JsonObject
  parts: JsonValue[]
  role: ZulipbotSessionMessageRole
}

export type ZulipbotAppendMessagesRequest = {
  messages: ZulipbotSessionMessage[]
}

export type ZulipbotCreateSessionRequest = {
  harness_type: string
  metadata: JsonObject
  persona_id?: string
}

export type ZulipbotExecuteSessionRequest = {
  idempotency_key?: string
  idle_timeout_ms?: number
  input_lines: string[]
  max_duration_ms?: number
  metadata: JsonObject
}

export type ZulipbotExecuteSessionResponse = {
  execution_id: string
  ok: boolean
  status: string
  thread_key: string
}

export type ZulipbotRendererSource = RustSessionStreamEvent | JsonObject

export type ZulipbotTrace = {
  includeContext: boolean
  messageId: string
  openStream: boolean
  startedAtMs: number
  threadId: string
}

export type ForwardSessionInput = {
  afterEventId: number
  executeMessage: ZulipbotApiMessage
  harnessType?: string
  messages: ZulipbotApiMessage[]
  onAnswerUpdate?(answerMarkdown: string): Promise<void> | void
  onEventId?(eventId: number): void
  onExecutionStarted?(execution: ZulipbotExecuteSessionResponse): Promise<void> | void
  openStream: boolean
  threadId: string
  trace?: ZulipbotTrace
}
