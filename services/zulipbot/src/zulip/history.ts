import type { JsonObject, NormalizedHistoryMessage, NormalizedZulipEvent } from '../types'
import type { ZulipClient, ZulipFetchedMessage, ZulipNarrowTerm } from './client'

export async function hydrateZulipHistory(
  input: {
    botEmail: string
    botUserId?: number
    historyLimit: number
    progressText: string
  },
  client: ZulipClient,
  event: NormalizedZulipEvent
): Promise<NormalizedZulipEvent> {
  if (input.historyLimit <= 0) return event
  const narrow = narrowForEvent(event, { botUserId: input.botUserId })
  if (!narrow) return event

  const messages = await client.getMessages({
    anchor: event.zulip.message_id ?? 'newest',
    num_before: input.historyLimit,
    num_after: 0,
    narrow,
    include_anchor: false,
    apply_markdown: false
  })
  return {
    ...event,
    history_messages: toHistoryMessages(messages, event, {
      botEmail: input.botEmail,
      progressText: input.progressText
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

export function narrowForEvent(
  event: NormalizedZulipEvent,
  opts: { botUserId?: number } = {}
): ZulipNarrowTerm[] | undefined {
  if (event.delivery.message_type === 'stream') {
    const channel = event.zulip.stream_name?.trim() || event.delivery.stream_id
    const topic = event.zulip.topic ?? event.delivery.topic
    if (!channel || topic === undefined) return undefined
    return [
      { operator: 'channel', operand: channel },
      { operator: 'topic', operand: topic }
    ]
  }

  const senderId = numberFromString(event.user_id)
  const recipientIds = (event.delivery.recipient_ids ?? []).filter(id => id !== opts.botUserId)
  if (senderId !== undefined && recipientIds.length <= 2) {
    return [{ operator: 'dm', operand: senderId }]
  }
  if (recipientIds.length > 0) return [{ operator: 'dm', operand: recipientIds }]
  return undefined
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
  const speakerText = speakerLine(message, role, text)
  return {
    message_id: `zulip:${event.realm}:${message.id}`,
    role,
    user_id: message.sender_id !== undefined ? String(message.sender_id) : message.sender_email,
    parts: [{ type: 'text', text: speakerText }],
    metadata: {
      source: 'zulip_history',
      platform: 'zulip',
      message_id: `zulip:${event.realm}:${message.id}`,
      user_id: message.sender_id !== undefined ? String(message.sender_id) : message.sender_email,
      user_name: message.sender_full_name ?? message.sender_email ?? role,
      timestamp: message.timestamp !== undefined ? timestampIso(message.timestamp) : undefined,
      zulip: zulipMetadata(message)
    }
  }
}

function speakerLine(message: ZulipFetchedMessage, role: string, text: string): string {
  const speaker = (message.sender_full_name || message.sender_email || role).trim()
  const time = message.timestamp !== undefined ? ` at ${timestampIso(message.timestamp)}` : ''
  return `${speaker}${time}: ${text}`
}

function zulipMetadata(message: ZulipFetchedMessage): JsonObject {
  return {
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
  if (normalized === expected) return true
  if (normalized.startsWith('Starting Centaur runtime')) return true
  const escaped = expected.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
  return new RegExp(`^${escaped}\\s+Still working \\(`).test(normalized)
}

function timestampIso(timestamp: number): string {
  return new Date(timestamp * 1000).toISOString()
}

function sameEmail(a: string | undefined, b: string): boolean {
  return (a ?? '').trim().toLowerCase() === b.trim().toLowerCase()
}

function numberFromString(value: string): number | undefined {
  const parsed = Number.parseInt(value, 10)
  return Number.isFinite(parsed) ? parsed : undefined
}
