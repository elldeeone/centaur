import { centaurApiKey, type AppConfig } from '../config'
import { logError, logWarn } from '../logging'
import { ZulipClient } from '../zulip/client'

const CONSUMER_ID = `zulipbot-${process.pid}`

export function startFinalDeliveryPoller(config: AppConfig, client = new ZulipClient(config)): void {
  if (!centaurApiKey(config)) return
  const tick = async () => {
    try {
      await pollFinalDeliveriesOnce(config, client)
    } catch (error) {
      logError('zulip_final_delivery_poll_failed', error)
    }
  }
  setInterval(tick, 2_000).unref?.()
  void tick()
}

export async function pollFinalDeliveriesOnce(
  config: AppConfig,
  client: Pick<ZulipClient, 'sendMessage'>
): Promise<void> {
  const claimed = await centaur(config, '/agent/final-deliveries/claim', {
    consumer_id: CONSUMER_ID,
    platform: 'zulip',
    limit: config.ZULIP_FINAL_DELIVERY_LIMIT,
    lease_seconds: 60
  })
  const deliveries = Array.isArray(claimed.deliveries) ? claimed.deliveries : []
  for (const delivery of deliveries) {
    const executionId = String(delivery.execution_id)
    try {
      await deliver(config, client, delivery)
      await centaur(config, `/agent/final-deliveries/${encodeURIComponent(executionId)}/delivered`, {
        consumer_id: CONSUMER_ID
      })
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error)
      logWarn('zulip_final_delivery_failed', {
        execution_id: executionId,
        thread_key: delivery.thread_key,
        error: message
      })
      await centaur(config, `/agent/final-deliveries/${encodeURIComponent(executionId)}/failed`, {
        consumer_id: CONSUMER_ID,
        error: message,
        retry_after_seconds: 10,
        non_retryable: nonRetryableZulipError(message),
        ...(nonRetryableZulipError(message) ? { error_class: message } : {})
      }).catch(failError => logError('zulip_final_delivery_mark_failed_failed', failError))
    }
  }
}

async function deliver(
  config: AppConfig,
  client: Pick<ZulipClient, 'sendMessage'>,
  delivery: any
): Promise<void> {
  const meta = delivery.delivery ?? {}
  const target = targetFromDelivery(delivery)
  const text = extractText(delivery.final_payload ?? {})
  const chunks = splitText(text, config.ZULIP_DELIVERY_CHUNK_CHARS)
  for (const chunk of chunks) {
    if (meta.message_type === 'stream' || target.message_type === 'stream') {
      const stream = meta.stream_id ?? meta.channel_id ?? meta.channel ?? target.stream_id
      const topic = typeof meta.topic === 'string' ? meta.topic : target.topic
      if (!stream || typeof topic !== 'string') throw new Error('missing_zulip_stream_target')
      await client.sendMessage({
        type: 'stream',
        to: typeof stream === 'number' ? stream : String(stream),
        topic,
        content: chunk
      })
      continue
    }

    const recipientIds = Array.isArray(meta.recipient_ids) ? meta.recipient_ids : []
    const recipientEmails = Array.isArray(meta.recipient_emails) ? meta.recipient_emails : []
    const to = recipientIds.length
      ? recipientIds
      : recipientEmails.length
        ? recipientEmails
        : target.recipient_ids?.length
          ? target.recipient_ids
          : target.recipient_emails ?? []
    if (!to.length) throw new Error('missing_zulip_dm_target')
    await client.sendMessage({
      type: 'private',
      to,
      content: chunk
    })
  }
}

type ZulipDeliveryTarget = {
  message_type?: 'stream' | 'private'
  stream_id?: number
  topic?: string
  recipient_ids?: number[]
  recipient_emails?: string[]
}

function targetFromDelivery(delivery: any): ZulipDeliveryTarget {
  const threadKey = String(delivery.thread_key ?? '')
  const parts = threadKey.split(':')
  if (parts[0] === 'zulip' && parts.length >= 4) {
    const streamId = Number(parts[2])
    return {
      message_type: 'stream',
      ...(Number.isFinite(streamId) ? { stream_id: streamId } : {}),
      topic: decodeTopic(parts.slice(3).join(':'))
    }
  }
  if (parts[0] === 'zulipdm' && parts.length >= 3) {
    const recipients = parts.slice(2).join(':').split(',').filter(Boolean)
    const ids = recipients.map(value => Number(value)).filter(Number.isInteger)
    const emails = recipients.filter(value => value.includes('@')).map(decodeURIComponent)
    return {
      message_type: 'private',
      ...(ids.length ? { recipient_ids: ids } : {}),
      ...(emails.length ? { recipient_emails: emails } : {})
    }
  }
  return {}
}

function decodeTopic(topic: string): string {
  try {
    return decodeURIComponent(topic)
  } catch {
    return topic
  }
}

function extractText(payload: any): string {
  for (const key of ['result_text', 'text', 'content', 'markdown']) {
    const value = payload?.[key]
    if (typeof value === 'string' && value.trim()) return value.trim()
  }
  return JSON.stringify(payload)
}

function splitText(text: string, chunkChars: number): string[] {
  if (text.length <= chunkChars) return [text]
  const chunks: string[] = []
  let remaining = text
  while (remaining.length > chunkChars) {
    let cut = remaining.lastIndexOf('\n\n', chunkChars)
    if (cut < chunkChars * 0.5) cut = remaining.lastIndexOf('\n', chunkChars)
    if (cut < chunkChars * 0.5) cut = chunkChars
    chunks.push(remaining.slice(0, cut).trim())
    remaining = remaining.slice(cut).trim()
  }
  if (remaining) chunks.push(remaining)
  return chunks
}

function nonRetryableZulipError(message: string): boolean {
  return [
    'missing_zulip_stream_target',
    'missing_zulip_dm_target',
    'missing_zulip_api_key',
    'BAD_REQUEST',
    'CHANNEL_DOES_NOT_EXIST',
    'USER_NOT_AUTHORIZED'
  ].some(candidate => message.includes(candidate))
}

async function centaur(config: AppConfig, path: string, body: unknown): Promise<any> {
  const apiKey = centaurApiKey(config)
  const response = await fetch(new URL(path, config.CENTAUR_API_URL), {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      ...(apiKey ? { Authorization: `Bearer ${apiKey}` } : {})
    },
    body: JSON.stringify(body)
  })
  const text = await response.text()
  const parsed = (text ? JSON.parse(text) : {}) as Record<string, unknown>
  if (!response.ok) throw new Error(String(parsed.detail ?? parsed.error ?? response.status))
  return parsed
}
