import { Hono } from 'hono'
import { errorMessage, noopLogger, nowMs, traceLog } from './logging'
import { forwardToSessionApi, isRetryableSessionApiError } from './session-api'
import type {
  ForwardSessionInput,
  JsonObject,
  JsonValue,
  NormalizedHistoryMessage,
  NormalizedZulipEvent,
  ZulipOutgoingWebhookPayload,
  Zulipbot,
  ZulipbotApiMessage,
  ZulipbotOptions
} from './types'
import { ZulipClient } from './zulip/client'
import { hydrateZulipHistory } from './zulip/history'
import { normalizeZulipWebhookPayload } from './zulip/normalize'
import { sendMessageFromTarget, targetFromEvent, ZulipProgressTracker } from './zulip/progress'
import type { ZulipProgressHandle } from './zulip/progress'

export type {
  ForwardSessionInput,
  NormalizedZulipEvent,
  ZulipOutgoingWebhookPayload,
  Zulipbot,
  ZulipbotOptions
} from './types'

const DEFAULT_DELIVERY_CHUNK_CHARS = 9000
const DEFAULT_STREAM_UPDATE_MS = 1500

export function createZulipbot(options: ZulipbotOptions): Zulipbot {
  const logger = options.logger ?? noopLogger
  const zulipClient = new ZulipClient({
    apiKey: options.zulipApiKey,
    botEmail: options.botEmail,
    fetchFn: options.zulipFetch,
    site: options.site
  })
  const progress = new ZulipProgressTracker(
    {
      maxMs: options.progressMaxMs ?? 120_000,
      placeholder: options.progressPlaceholder ?? true,
      progressText: options.progressText ?? 'Working...',
      updateMs: options.progressUpdateMs ?? 10_000
    },
    zulipClient,
    logger
  )

  const app = new Hono()
  app.get('/health', c => c.json({ ok: true, service: 'zulipbot' }))
  app.post(options.eventsPath ?? '/api/webhooks/zulip', async c => {
    let payload: ZulipOutgoingWebhookPayload
    try {
      payload = await c.req.json<ZulipOutgoingWebhookPayload>()
    } catch {
      return c.json({ ok: false, error: 'invalid_json' }, 400)
    }

    if (options.webhookToken && payload.token !== options.webhookToken) {
      logger.warn('zulip_webhook_token_rejected', {
        message_id: payload.message?.id,
        sender_id: payload.message?.sender_id
      })
      return c.json({ ok: false, error: 'invalid_token' }, 401)
    }

    const normalized = normalizeZulipWebhookPayload(payload, {
      defaultRealm: options.site,
      botEmail: options.botEmail
    })
    if (!normalized) return c.json({ ok: true, ignored: true })

    const event = await withFreshZulipMessageDetails(normalized, zulipClient, logger)
    const progressHandle = await progress.start(event)
    backgroundWaitUntil(
      processZulipEvent({
        event,
        options,
        progress,
        progressHandle,
        zulipClient
      })
    )
    return c.json({ ok: true })
  })

  return { app }
}

async function withFreshZulipMessageDetails(
  event: NormalizedZulipEvent,
  zulipClient: ZulipClient,
  logger: ZulipbotOptions['logger']
): Promise<NormalizedZulipEvent> {
  if (event.delivery.message_type !== 'stream') return event
  if (event.delivery.topic?.trim()) return event
  const messageId = event.zulip.message_id
  if (messageId === undefined) return event

  try {
    const channel = event.zulip.stream_name?.trim()
    const messages = await zulipClient.getMessages({
      anchor: messageId,
      apply_markdown: false,
      include_anchor: true,
      narrow: channel ? [{ operator: 'stream', operand: channel }] : undefined,
      num_after: 5,
      num_before: 5
    })
    const message = messages.find(item => item.id === messageId)
    const topic = message ? message.topic ?? message.subject : undefined
    if (!message || !topic?.trim()) {
      traceLog(logger, 'zulipbot_topic_hydration_skipped', {
        message_id: event.message_id,
        reason: message ? 'missing_topic' : 'message_not_found',
        thread_id: event.thread_key
      })
      return event
    }

    const streamId = message.stream_id ?? event.delivery.stream_id
    if (streamId === undefined) return event
    const streamName =
      typeof message.display_recipient === 'string'
        ? message.display_recipient
        : event.zulip.stream_name
    traceLog(logger, 'zulipbot_topic_hydrated', {
      message_id: event.message_id,
      topic,
      thread_id: event.thread_key
    })
    return {
      ...event,
      thread_key: `zulip:${event.realm}:${streamId}:${encodeURIComponent(topic.trim())}`,
      zulip: {
        ...event.zulip,
        stream_id: streamId,
        stream_name: streamName,
        topic
      },
      delivery: {
        ...event.delivery,
        stream_id: streamId,
        topic
      }
    }
  } catch (error) {
    logger?.warn('zulipbot_topic_hydration_failed', {
      error: errorMessage(error),
      message_id: event.message_id,
      thread_id: event.thread_key
    })
    return event
  }
}

async function processZulipEvent(input: {
  event: NormalizedZulipEvent
  options: ZulipbotOptions
  progress: ZulipProgressTracker
  progressHandle: ZulipProgressHandle | null
  zulipClient: ZulipClient
}): Promise<void> {
  const startedAtMs = nowMs()
  const logger = input.options.logger ?? noopLogger
  let event = input.event
  try {
    event = await withHistory(input)
    if (hasAssistantHistory(event)) input.progress.markWarmThread(event.thread_key, input.progressHandle)

    const { executeMessage, messages } = toSessionMessages(event, input.options)
    const trace = {
      includeContext: (event.history_messages?.length ?? 0) > 0,
      messageId: event.message_id,
      openStream: true,
      startedAtMs,
      threadId: event.thread_key
    }
    let lastStreamUpdateAt = 0
    const forwardInput: ForwardSessionInput = {
      afterEventId: 0,
      executeMessage,
      harnessType: input.options.harnessType,
      messages,
      onAnswerUpdate: async answerMarkdown => {
        const elapsed = nowMs() - lastStreamUpdateAt
        if (elapsed < (input.options.streamUpdateMs ?? DEFAULT_STREAM_UPDATE_MS)) return
        lastStreamUpdateAt = nowMs()
        await input.progressHandle?.update(firstChunk(answerMarkdown, input.options))
      },
      onEventId: eventId => {
        traceLog(logger, 'zulipbot_session_event_seen', {
          event_id: eventId,
          thread_id: event.thread_key
        })
      },
      openStream: true,
      threadId: event.thread_key,
      trace
    }
    const result = await forwardToSessionApi(input.options, forwardInput)
    await deliverAnswer(input, result.answerMarkdown)
    traceLog(logger, 'zulipbot_turn_complete', {
      elapsed_ms: Math.round(nowMs() - startedAtMs),
      execution_id: result.execution.execution_id,
      history_messages: event.history_messages?.length ?? 0,
      message_id: event.message_id,
      thread_id: event.thread_key
    })
  } catch (error) {
    logger.error('zulipbot_turn_failed', {
      error: errorMessage(error),
      retryable: isRetryableSessionApiError(error),
      thread_id: event.thread_key
    })
    await input.progressHandle?.fail('Centaur could not complete this turn.')
  }
}

async function withHistory(input: {
  event: NormalizedZulipEvent
  options: ZulipbotOptions
  zulipClient: ZulipClient
}): Promise<NormalizedZulipEvent> {
  try {
    return await hydrateZulipHistory(
      {
        botEmail: input.options.botEmail,
        botUserId: input.options.botUserId,
        historyLimit: input.options.historyLimit ?? 50,
        progressText: input.options.progressText ?? 'Working...'
      },
      input.zulipClient,
      input.event
    )
  } catch (error) {
    ;(input.options.logger ?? noopLogger).warn('zulip_history_fetch_failed', {
      thread_key: input.event.thread_key,
      message_id: input.event.message_id,
      error: errorMessage(error)
    })
    return input.event
  }
}

function toSessionMessages(
  event: NormalizedZulipEvent,
  options: ZulipbotOptions
): { executeMessage: ZulipbotApiMessage; messages: ZulipbotApiMessage[] } {
  const history = (event.history_messages ?? []).map(message =>
    historyToApiMessage(message, event.thread_key)
  )
  const executeMessage = eventToApiMessage(event, options, history)
  return {
    executeMessage,
    messages: [...history, executeMessage]
  }
}

function historyToApiMessage(
  message: NormalizedHistoryMessage,
  threadKey: string
): ZulipbotApiMessage {
  const userName = stringValue(message.metadata.user_name)
  const userEmail = stringValue(message.metadata.user_email)
  const userId = stringValue(message.metadata.user_id) ?? message.user_id
  const timestamp = stringValue(message.metadata.timestamp)
  const text = message.parts.map(part => part.text).join('\n')
  return {
    author: {
      email: userEmail,
      fullName: userName,
      isBot: message.role === 'assistant',
      isMe: message.role === 'assistant',
      userId,
      userName
    },
    id: message.message_id,
    isMention: false,
    metadata: message.metadata,
    parts: message.parts,
    role: message.role,
    text,
    threadId: threadKey,
    timestamp
  }
}

function eventToApiMessage(
  event: NormalizedZulipEvent,
  options: ZulipbotOptions,
  context: ZulipbotApiMessage[]
): ZulipbotApiMessage {
  const timestamp = event.zulip.timestamp !== undefined
    ? new Date(event.zulip.timestamp * 1000).toISOString()
    : undefined
  const text = event.parts.map(part => part.text).join('\n')
  const authorName = event.zulip.sender_full_name ?? event.zulip.sender_email ?? event.user_id
  const line = `${authorName}${timestamp ? ` at ${timestamp}` : ''}: ${text}`
  return {
    author: {
      email: event.zulip.sender_email,
      fullName: event.zulip.sender_full_name,
      isBot: false,
      isMe: false,
      userId: event.user_id,
      userName: authorName
    },
    context,
    id: event.message_id,
    isMention: event.is_mention,
    metadata: {
      source: 'zulipbot',
      platform: 'zulip',
      zulip_site: options.site,
      zulip: event.zulip as JsonObject
    },
    parts: [{ type: 'text', text: line }],
    raw: event,
    role: 'user',
    text,
    threadId: event.thread_key,
    timestamp
  }
}

async function deliverAnswer(
  input: {
    event: NormalizedZulipEvent
    options: ZulipbotOptions
    progressHandle: ZulipProgressHandle | null
    zulipClient: ZulipClient
  },
  answerMarkdown: string
): Promise<void> {
  const content = answerMarkdown.trim() || 'Execution completed, but no final text was captured.'
  const chunks = splitForZulip(content, input.options.deliveryChunkChars ?? DEFAULT_DELIVERY_CHUNK_CHARS)
  const deliveredFirst = await input.progressHandle?.complete(chunks[0] ?? content)
  const target = targetFromEvent(input.event)
  if (!target) return

  const remaining = deliveredFirst ? chunks.slice(1) : chunks
  for (const chunk of remaining) {
    await input.zulipClient.sendMessage(sendMessageFromTarget(target, chunk))
  }
}

function hasAssistantHistory(event: NormalizedZulipEvent): boolean {
  return event.history_messages?.some(message => message.role === 'assistant') ?? false
}

function firstChunk(answerMarkdown: string, options: ZulipbotOptions): string {
  return splitForZulip(
    answerMarkdown,
    options.deliveryChunkChars ?? DEFAULT_DELIVERY_CHUNK_CHARS
  )[0] ?? answerMarkdown
}

function splitForZulip(content: string, maxChars: number): string[] {
  const normalized = content.trim()
  if (!normalized) return ['']
  if (normalized.length <= maxChars) return [normalized]
  const chunks: string[] = []
  let remaining = normalized
  while (remaining.length > maxChars) {
    const at = splitIndex(remaining, maxChars)
    chunks.push(remaining.slice(0, at).trimEnd())
    remaining = remaining.slice(at).trimStart()
  }
  if (remaining) chunks.push(remaining)
  return chunks
}

function splitIndex(value: string, maxChars: number): number {
  const newline = value.lastIndexOf('\n', maxChars)
  if (newline > maxChars * 0.5) return newline
  const space = value.lastIndexOf(' ', maxChars)
  if (space > maxChars * 0.5) return space
  return maxChars
}

function stringValue(value: unknown): string | undefined {
  return typeof value === 'string' && value.trim() ? value.trim() : undefined
}

function backgroundWaitUntil(promise: Promise<unknown>): void {
  void promise.catch(() => undefined)
}
