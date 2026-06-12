import type { AppConfig } from '../config'
import type { ZulipClient, ZulipFetchedMessage, ZulipNarrowTerm } from './client'
import type { NormalizedHistoryMessage, NormalizedZulipEvent } from './types'

export async function hydrateZulipHistory(
  config: AppConfig,
  client: ZulipClient,
  event: NormalizedZulipEvent
): Promise<NormalizedZulipEvent> {
  if (config.ZULIP_HISTORY_LIMIT <= 0) return event
  const narrow = narrowForEvent(event)
  if (!narrow) return event

  const messages = await client.getMessages({
    anchor: event.zulip.message_id ?? 'newest',
    num_before: config.ZULIP_HISTORY_LIMIT,
    num_after: 0,
    narrow,
    include_anchor: false,
    apply_markdown: false
  })
  return {
    ...event,
    history_messages: toHistoryMessages(messages, event, {
      botEmail: config.ZULIP_BOT_EMAIL,
      progressText: config.ZULIP_PROGRESS_TEXT
    })
  }
}

export function toHistoryMessages(
  messages: ZulipFetchedMessage[],
  event: NormalizedZulipEvent,
  opts: { botEmail: string; progressText: string }
): NormalizedHistoryMessage[] {
  const currentMessageId = event.zulip.message_id
  return messages
    .filter(message => message.id !== currentMessageId)
    .map(message => toHistoryMessage(message, event, opts))
    .filter((message): message is NormalizedHistoryMessage => Boolean(message))
}

function narrowForEvent(event: NormalizedZulipEvent): ZulipNarrowTerm[] | undefined {
  if (event.delivery.message_type !== 'stream') return undefined
  const streamName = event.zulip.stream_name?.trim()
  const topic = event.zulip.topic?.trim()
  if (!streamName || !topic) return undefined
  return [
    { operator: 'channel', operand: streamName },
    { operator: 'topic', operand: topic }
  ]
}

function toHistoryMessage(
  message: ZulipFetchedMessage,
  event: NormalizedZulipEvent,
  opts: { botEmail: string; progressText: string }
): NormalizedHistoryMessage | undefined {
  if (!message.id) return undefined
  const text = normalizedContent(message)
  if (!text) return undefined
  const role = sameEmail(message.sender_email, opts.botEmail) ? 'assistant' : 'user'
  if (role === 'assistant' && isProgressPlaceholder(text, opts.progressText)) return undefined
  const speaker = (message.sender_full_name || message.sender_email || role).trim()
  const speakerText = speaker ? `${speaker}: ${text}` : text
  return {
    message_id: `zulip:${event.realm}:${message.id}`,
    role,
    user_id: message.sender_id !== undefined ? String(message.sender_id) : message.sender_email,
    parts: [{ type: 'text', text: speakerText }],
    metadata: {
      source: 'zulip_history',
      platform: 'zulip',
      zulip: {
        message_id: message.id,
        message_type: message.type,
        stream_id: message.stream_id,
        topic: message.topic ?? message.subject,
        recipient_id: message.recipient_id,
        sender_email: message.sender_email,
        sender_full_name: message.sender_full_name,
        timestamp: message.timestamp
      }
    }
  }
}

function normalizedContent(message: ZulipFetchedMessage): string {
  const raw = (message.content ?? '').trim()
  if (!raw) return ''
  if (message.content_type !== 'text/html') return raw
  return decodeHtml(
    raw
      .replace(/<br\s*\/?>/gi, '\n')
      .replace(/<\/p>\s*<p[^>]*>/gi, '\n\n')
      .replace(/<[^>]+>/g, '')
  ).trim()
}

function decodeHtml(value: string): string {
  return value
    .replace(/&nbsp;/g, ' ')
    .replace(/&amp;/g, '&')
    .replace(/&lt;/g, '<')
    .replace(/&gt;/g, '>')
    .replace(/&quot;/g, '"')
    .replace(/&#39;/g, "'")
    .replace(/&#x27;/g, "'")
}

function isProgressPlaceholder(text: string, progressText: string): boolean {
  const normalized = text.trim()
  const expected = progressText.trim()
  return normalized === expected || normalized.startsWith(`${expected}\nStill working (`)
}

function sameEmail(a: string | undefined, b: string): boolean {
  return (a ?? '').trim().toLowerCase() === b.trim().toLowerCase()
}
