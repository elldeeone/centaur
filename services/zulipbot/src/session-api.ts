import { CodexAppServerRendererEventMapper, type RendererEvent } from '@centaur/rendering'
import type { RustSessionStreamEvent } from '@centaur/harness-events'
import { elapsedMs, errorMessage, nowMs, traceLog } from './logging'
import type {
  ForwardSessionInput,
  JsonObject,
  JsonValue,
  ZulipbotAppendMessagesRequest,
  ZulipbotApiMessage,
  ZulipbotCreateSessionRequest,
  ZulipbotExecuteSessionRequest,
  ZulipbotExecuteSessionResponse,
  ZulipbotOptions,
  ZulipbotRendererSource,
  ZulipbotSessionMessage
} from './types'

export class SessionApiError extends Error {
  readonly action: string
  readonly body: string
  readonly retryable: boolean
  readonly status: number
  readonly statusText: string

  constructor(input: {
    action: string
    body: string
    retryable: boolean
    status: number
    statusText: string
  }) {
    const suffix = input.body ? `: ${input.body}` : ''
    super(`Centaur session ${input.action} failed: ${input.status} ${input.statusText}${suffix}`)
    this.name = 'SessionApiError'
    this.action = input.action
    this.body = input.body
    this.retryable = input.retryable
    this.status = input.status
    this.statusText = input.statusText
  }
}

export function isRetryableSessionApiError(error: unknown): boolean {
  if (error instanceof SessionApiError) return error.retryable
  if (!(error instanceof Error)) return false
  return error.name === 'AbortError' || error.name === 'TypeError'
}

export async function forwardToSessionApi(
  options: ZulipbotOptions,
  input: ForwardSessionInput
): Promise<{ answerMarkdown: string; execution: ZulipbotExecuteSessionResponse }> {
  const createStartedAtMs = nowMs()
  await createSession(options, input.threadId, input.harnessType)
  traceLog(options.logger, 'zulipbot_session_create_complete', {
    phase_ms: elapsedMs(createStartedAtMs),
    thread_id: input.threadId
  })

  if (input.messages.length > 0) {
    const appendStartedAtMs = nowMs()
    await appendSessionMessages(options, input.threadId, input.messages)
    traceLog(options.logger, 'zulipbot_session_append_complete', {
      message_count: input.messages.length,
      phase_ms: elapsedMs(appendStartedAtMs),
      thread_id: input.threadId
    })
  }

  const executeStartedAtMs = nowMs()
  const execution = await executeSession(options, input.threadId, input.executeMessage)
  traceLog(options.logger, 'zulipbot_session_execute_complete', {
    execution_id: execution.execution_id,
    phase_ms: elapsedMs(executeStartedAtMs),
    thread_id: input.threadId
  })
  await input.onExecutionStarted?.(execution)

  if (!input.openStream) return { answerMarkdown: '', execution }

  const streamStartedAtMs = nowMs()
  const stream = await openSessionEventStream(options, {
    afterEventId: input.afterEventId,
    executionId: execution.execution_id,
    onEventId: input.onEventId,
    threadId: input.threadId
  })
  traceLog(options.logger, 'zulipbot_session_events_opened', {
    execution_id: execution.execution_id,
    phase_ms: elapsedMs(streamStartedAtMs),
    thread_id: input.threadId
  })

  const answerMarkdown = await collectRenderedAnswer(stream, {
    onAnswerUpdate: input.onAnswerUpdate,
    threadId: input.threadId
  })
  return { answerMarkdown, execution }
}

export function sessionStreamError(error: unknown): RustSessionStreamEvent {
  return {
    data: { error: errorMessage(error) },
    event: 'session.stream_error',
    eventKind: 'session.stream_error'
  }
}

async function createSession(
  options: ZulipbotOptions,
  threadId: string,
  harnessType?: string
): Promise<void> {
  const requested = harnessType || options.harnessType || 'codex'
  const response = await postCreateSession(options, threadId, requested)
  if (response.ok) return

  let body = ''
  try {
    body = await response.text()
  } catch {
    body = ''
  }
  const existing = response.status === 409 ? existingHarnessFromConflict(body) : undefined
  if (existing && existing !== requested) {
    const retry = await postCreateSession(options, threadId, existing)
    await ensureApiOk(retry, 'create session')
    return
  }
  throw new SessionApiError({
    action: 'create session',
    body,
    retryable: isRetryableApiStatus(response.status),
    status: response.status,
    statusText: response.statusText
  })
}

async function postCreateSession(
  options: ZulipbotOptions,
  threadId: string,
  harnessType: string
): Promise<Response> {
  const fetchFn = options.fetch ?? fetch
  const body: ZulipbotCreateSessionRequest = {
    harness_type: harnessType,
    metadata: {
      source: 'zulipbot',
      platform: 'zulip',
      thread_id: threadId,
      zulip_site: options.site
    },
    ...(options.personaId ? { persona_id: options.personaId } : {})
  }
  return fetchFn(apiSessionUrl(options.apiUrl, threadId), {
    method: 'POST',
    headers: apiHeaders(options),
    body: JSON.stringify(body)
  })
}

function existingHarnessFromConflict(body: string): string | undefined {
  try {
    const payload = JSON.parse(body)
    if (isJsonObject(payload)) {
      const existing = stringValue(payload.existing_harness)
      if (existing) return existing
    }
  } catch {
    // Fall through to message parsing.
  }
  return /already exists with harness_type ([A-Za-z0-9_-]+)/.exec(body)?.[1]
}

async function appendSessionMessages(
  options: ZulipbotOptions,
  threadId: string,
  messages: ZulipbotApiMessage[]
): Promise<void> {
  const fetchFn = options.fetch ?? fetch
  const body: ZulipbotAppendMessagesRequest = {
    messages: messages.map(toSessionMessage)
  }
  const response = await fetchFn(apiSessionUrl(options.apiUrl, threadId, 'messages'), {
    method: 'POST',
    headers: apiHeaders(options),
    body: JSON.stringify(body)
  })
  await ensureApiOk(response, 'append session messages')
}

async function executeSession(
  options: ZulipbotOptions,
  threadId: string,
  message: ZulipbotApiMessage
): Promise<ZulipbotExecuteSessionResponse> {
  const fetchFn = options.fetch ?? fetch
  const body: ZulipbotExecuteSessionRequest = {
    idempotency_key: message.id,
    metadata: sessionMetadata(message, { action: 'execute' }),
    input_lines: toCodexInputLines(message, threadId),
    ...(options.idleTimeoutMs === undefined ? {} : { idle_timeout_ms: options.idleTimeoutMs }),
    ...(options.maxDurationMs === undefined ? {} : { max_duration_ms: options.maxDurationMs })
  }
  const response = await fetchFn(apiSessionUrl(options.apiUrl, threadId, 'execute'), {
    method: 'POST',
    headers: apiHeaders(options),
    body: JSON.stringify(body)
  })
  await ensureApiOk(response, 'execute session')
  return (await response.json()) as ZulipbotExecuteSessionResponse
}

async function ensureApiOk(response: Response, action: string): Promise<void> {
  if (response.ok) return
  let body = ''
  try {
    body = await response.text()
  } catch {
    body = ''
  }
  throw new SessionApiError({
    action,
    body,
    retryable: isRetryableApiStatus(response.status),
    status: response.status,
    statusText: response.statusText
  })
}

function isRetryableApiStatus(status: number): boolean {
  return status === 408 || status === 425 || status === 429 || status >= 500
}

async function openSessionEventStream(
  options: ZulipbotOptions,
  input: {
    afterEventId: number
    executionId?: string
    onEventId?: (eventId: number) => void
    threadId: string
  }
): Promise<AsyncIterable<ZulipbotRendererSource>> {
  const fetchFn = options.fetch ?? fetch
  const url = new URL(apiSessionUrl(options.apiUrl, input.threadId, 'events'))
  url.searchParams.set('after_event_id', String(input.afterEventId))
  if (input.executionId) url.searchParams.set('execution_id', input.executionId)
  const response = await fetchFn(url, {
    method: 'GET',
    headers: apiHeaders(options, false)
  })
  await ensureApiOk(response, 'stream events')
  if (!response.body) return toAsyncIterable([])
  return parseSessionEventStream(response.body, input.onEventId ?? (() => undefined))
}

async function collectRenderedAnswer(
  stream: AsyncIterable<ZulipbotRendererSource>,
  input: {
    onAnswerUpdate?: (answerMarkdown: string) => Promise<void> | void
    threadId: string
  }
): Promise<string> {
  const mapper = new CodexAppServerRendererEventMapper({ sessionId: input.threadId })
  let answerMarkdown = ''

  const processEvents = async (events: RendererEvent[]): Promise<void> => {
    for (const event of events) {
      if (event.type === 'renderer.message.delta' && event.delta) {
        answerMarkdown += event.delta
        await input.onAnswerUpdate?.(answerMarkdown)
      }
      if (event.type === 'renderer.done') {
        answerMarkdown = event.answerMarkdown || answerMarkdown
      }
    }
  }

  for await (const event of stream) {
    await processEvents(mapper.process(event))
    if (mapper.isDone()) break
  }
  await processEvents(mapper.flush())
  return answerMarkdown.trim()
}

function apiSessionUrl(
  apiUrl: string,
  threadId: string,
  suffix?: 'messages' | 'execute' | 'events'
): string {
  const path = `/api/session/${encodeURIComponent(threadId)}${suffix ? `/${suffix}` : ''}`
  return new URL(path, ensureTrailingSlash(apiUrl)).toString()
}

function ensureTrailingSlash(value: string): string {
  return value.endsWith('/') ? value : `${value}/`
}

function apiHeaders(options: ZulipbotOptions, jsonBody = true): HeadersInit {
  return {
    ...(jsonBody ? { 'content-type': 'application/json' } : {}),
    ...(options.apiKey ? { authorization: `Bearer ${options.apiKey}` } : {})
  }
}

function toSessionMessage(message: ZulipbotApiMessage): ZulipbotSessionMessage {
  return {
    client_message_id: message.id,
    role: message.role,
    parts: sessionMessageParts(message),
    metadata: sessionMetadata(message)
  }
}

function sessionMessageParts(message: ZulipbotApiMessage): JsonValue[] {
  return message.parts.length > 0 ? message.parts : [{ type: 'text', text: message.text }]
}

function sessionMetadata(message: ZulipbotApiMessage, extra: JsonObject = {}): JsonObject {
  return {
    source: 'zulipbot',
    platform: 'zulip',
    message_id: message.id,
    thread_id: message.threadId,
    is_mention: message.isMention,
    timestamp: message.timestamp,
    user_id: message.author.userId,
    user_name: message.author.userName,
    user_email: message.author.email,
    ...message.metadata,
    ...extra
  }
}

function toCodexInputLines(message: ZulipbotApiMessage, threadId: string): string[] {
  return [
    JSON.stringify({
      type: 'user',
      thread_key: threadId,
      trace_metadata: sessionMetadata(message, { action: 'execute' }),
      message: {
        role: 'user',
        content: codexInputContent(message)
      }
    })
  ]
}

function codexInputContent(message: ZulipbotApiMessage): JsonValue[] {
  const content: JsonValue[] = []
  const contextText = compactContextText(message)
  if (contextText) content.push({ type: 'text', text: contextText })
  content.push({ type: 'text', text: currentMessageText(message) })
  return content
}

function compactContextText(message: ZulipbotApiMessage): string {
  const context = message.context ?? []
  if (context.length === 0) return ''
  const lines = context.map(item => messageLine(item)).filter(Boolean)
  if (lines.length === 0) return ''
  return `Zulip topic context before the current message, oldest to newest:\n${lines.join('\n')}`
}

function currentMessageText(message: ZulipbotApiMessage): string {
  return `Current Zulip message:\n${messageLine(message)}`
}

function messageLine(message: ZulipbotApiMessage): string {
  const speaker = message.author.fullName || message.author.email || message.author.userName || 'unknown'
  const time = message.timestamp ? ` at ${message.timestamp}` : ''
  const text = message.text || plainTextFromParts(message.parts)
  return `${speaker}${time}: ${text}`
}

function plainTextFromParts(parts: JsonValue[]): string {
  return parts
    .map(part => {
      if (!isJsonObject(part)) return ''
      const text = part.text
      return typeof text === 'string' ? text : ''
    })
    .filter(Boolean)
    .join('\n')
}

type ParsedSessionEvent = {
  data: string
  event?: string
  id?: number
}

async function* parseSessionEventStream(
  stream: ReadableStream<Uint8Array>,
  onEventId: (eventId: number) => void
): AsyncIterable<ZulipbotRendererSource> {
  for await (const event of parseSseEvents(stream)) {
    if (typeof event.id === 'number') onEventId(event.id)
    if (event.event === 'session.output.line') {
      yield {
        data: event.data,
        event: event.event,
        eventId: event.id,
        eventKind: event.event
      } satisfies RustSessionStreamEvent
      if (isTerminalCodexOutputLine(event.data)) return
      continue
    }
    if (event.event === 'session.execution_failed' || event.event === 'session.stream_error') {
      yield {
        data: { error: sessionErrorMessage(event) },
        event: event.event,
        eventId: event.id,
        eventKind: event.event
      } satisfies RustSessionStreamEvent
      return
    }
    if (event.event === 'session.execution_cancelled') {
      yield {
        data: { error: sessionErrorMessage(event, 'Execution cancelled') },
        event: event.event,
        eventId: event.id,
        eventKind: event.event
      } satisfies RustSessionStreamEvent
      return
    }
    if (event.event === 'session.execution_completed') {
      yield {
        data: sessionEventData(event),
        event: event.event,
        eventId: event.id,
        eventKind: event.event
      } satisfies RustSessionStreamEvent
      return
    }
  }
}

async function* parseSseEvents(stream: ReadableStream<Uint8Array>): AsyncIterable<ParsedSessionEvent> {
  const reader = stream.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  let eventName: string | undefined
  let eventId: number | undefined
  let data: string[] = []

  while (true) {
    const { done, value } = await reader.read()
    if (done) break
    buffer += decoder.decode(value, { stream: true })
    const lines = buffer.split(/\r?\n/)
    buffer = lines.pop() ?? ''

    for (const line of lines) {
      const emitted = parseSseLine(line, { data, eventId, eventName })
      data = emitted.state.data
      eventId = emitted.state.eventId
      eventName = emitted.state.eventName
      if (emitted.event) yield emitted.event
    }
  }

  buffer += decoder.decode()
  if (buffer) {
    const emitted = parseSseLine(buffer, { data, eventId, eventName })
    data = emitted.state.data
    eventId = emitted.state.eventId
    eventName = emitted.state.eventName
    if (emitted.event) yield emitted.event
  }
  if (data.length > 0) {
    yield { data: data.join('\n'), event: eventName, id: eventId }
  }
}

function parseSseLine(
  line: string,
  state: {
    data: string[]
    eventId?: number
    eventName?: string
  }
): {
  event?: ParsedSessionEvent
  state: { data: string[]; eventId?: number; eventName?: string }
} {
  if (!line.trim()) {
    const event =
      state.data.length > 0
        ? { data: state.data.join('\n'), event: state.eventName, id: state.eventId }
        : undefined
    return { event, state: { data: [] } }
  }
  if (line.startsWith(':')) return { state }

  const separator = line.indexOf(':')
  const field = separator >= 0 ? line.slice(0, separator) : line
  const value = separator >= 0 ? line.slice(separator + 1).replace(/^ /, '') : ''
  if (field === 'event') return { state: { ...state, eventName: value } }
  if (field === 'id') {
    const id = Number.parseInt(value, 10)
    return { state: { ...state, eventId: Number.isFinite(id) ? id : undefined } }
  }
  if (field === 'data' && value !== '[DONE]') {
    return { state: { ...state, data: [...state.data, value] } }
  }

  return { state }
}

function isTerminalCodexOutputLine(line: string): boolean {
  let payload: unknown
  try {
    payload = JSON.parse(line)
  } catch {
    return false
  }
  if (!isJsonObject(payload)) return false

  return (
    payload.type === 'turn.completed' ||
    payload.type === 'turn.failed' ||
    payload.type === 'turn.done' ||
    payload.method === 'error' ||
    payload.method === 'turn/completed'
  )
}

function sessionEventData(event: ParsedSessionEvent): unknown {
  try {
    return JSON.parse(event.data)
  } catch {
    return event.data
  }
}

function sessionErrorMessage(event: ParsedSessionEvent, fallback?: string): string {
  let message = fallback ?? `${event.event ?? 'session error'}`
  try {
    const payload = JSON.parse(event.data)
    if (isJsonObject(payload)) {
      message = stringValue(payload.error) ?? stringValue(payload.message) ?? message
    }
  } catch {
    if (event.data.trim()) message = event.data.trim()
  }
  return message
}

function isJsonObject(value: unknown): value is JsonObject {
  return Boolean(value && typeof value === 'object' && !Array.isArray(value))
}

function stringValue(value: unknown): string | undefined {
  return typeof value === 'string' && value.trim() ? value.trim() : undefined
}

async function* toAsyncIterable<T>(source: Iterable<T>): AsyncIterable<T> {
  for await (const item of source) yield item
}
