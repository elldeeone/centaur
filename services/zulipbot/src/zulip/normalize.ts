import type {
  NormalizedZulipEvent,
  ZulipDisplayRecipient,
  ZulipOutgoingWebhookPayload
} from './types'

export function normalizeZulipWebhookPayload(
  payload: ZulipOutgoingWebhookPayload,
  opts: { defaultRealm: string; botEmail: string }
): NormalizedZulipEvent | null {
  const message = payload.message
  if (!message) return null
  if (message.sender_email && sameEmail(message.sender_email, opts.botEmail)) return null

  const rawText = (payload.data ?? message.content ?? '').trim()
  if (!rawText) return null

  const realm = cleanSegment(message.sender_realm_str || hostFromUrl(opts.defaultRealm))
  const messageId = message.id
  const userId = String(message.sender_id ?? message.sender_email ?? 'unknown')
  const trigger = (payload.trigger ?? '').trim()
  const isMention = trigger === 'mention'
  const normalizedType = normalizeMessageType(message.type)

  if (normalizedType === 'stream') {
    if (!message.stream_id) return null
    const topic = message.topic ?? message.subject ?? ''
    return {
      thread_key: `zulip:${realm}:${message.stream_id}:${topicKey(topic)}`,
      message_id: `zulip:${realm}:${messageId ?? message.timestamp ?? Date.now()}`,
      realm,
      user_id: userId,
      is_mention: isMention,
      parts: [{ type: 'text', text: rawText }],
      zulip: {
        message_id: messageId,
        message_type: message.type,
        stream_id: message.stream_id,
        topic,
        recipient_id: message.recipient_id,
        trigger,
        sender_email: message.sender_email,
        sender_full_name: message.sender_full_name,
        timestamp: message.timestamp
      },
      delivery: {
        platform: 'zulip',
        message_type: 'stream',
        stream_id: message.stream_id,
        topic
      }
    }
  }

  const recipients = directRecipients(message.display_recipient)
  return {
    thread_key: `zulipdm:${realm}:${directKey(recipients.ids, recipients.emails, message.recipient_id)}`,
    message_id: `zulip:${realm}:${messageId ?? message.timestamp ?? Date.now()}`,
    realm,
    user_id: userId,
    is_mention: true,
    parts: [{ type: 'text', text: rawText }],
    zulip: {
      message_id: messageId,
      message_type: message.type,
      recipient_id: message.recipient_id,
      trigger,
      sender_email: message.sender_email,
      sender_full_name: message.sender_full_name,
      timestamp: message.timestamp
    },
    delivery: {
      platform: 'zulip',
      message_type: 'private',
      recipient_ids: recipients.ids.length ? recipients.ids : undefined,
      recipient_emails: recipients.emails.length ? recipients.emails : undefined
    }
  }
}

function normalizeMessageType(type: string | undefined): 'stream' | 'private' {
  if (type === 'stream' || type === 'channel') return 'stream'
  return 'private'
}

function directRecipients(
  displayRecipient: ZulipDisplayRecipient | undefined
): { ids: number[]; emails: string[] } {
  if (!Array.isArray(displayRecipient)) return { ids: [], emails: [] }
  const ids = displayRecipient
    .map(recipient => recipient.id)
    .filter((id): id is number => typeof id === 'number')
    .sort((a, b) => a - b)
  const emails = displayRecipient
    .map(recipient => recipient.email)
    .filter((email): email is string => Boolean(email))
    .sort()
  return { ids, emails }
}

function directKey(ids: number[], emails: string[], recipientId: number | undefined): string {
  if (ids.length) return ids.join(',')
  if (emails.length) return emails.map(cleanSegment).join(',')
  if (recipientId) return `recipient-${recipientId}`
  return 'unknown'
}

function topicKey(topic: string): string {
  return encodeURIComponent(topic.trim() || '(no-topic)')
}

function cleanSegment(value: string): string {
  return value.trim().toLowerCase().replace(/[^a-z0-9._@-]+/g, '-')
}

function hostFromUrl(value: string): string {
  try {
    return new URL(value).host
  } catch {
    return value
  }
}

function sameEmail(a: string, b: string): boolean {
  return a.trim().toLowerCase() === b.trim().toLowerCase()
}
