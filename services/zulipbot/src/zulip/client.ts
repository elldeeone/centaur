import type { AppConfig } from '../config'

export type ZulipSendMessage = {
  type: 'stream' | 'private'
  to: number | number[] | string | string[]
  topic?: string
  content: string
}

export type ZulipTypingStatus = {
  op: 'start' | 'stop'
  stream_id?: number
  topic?: string
  to?: number[]
}

export class ZulipClient {
  readonly config: AppConfig

  constructor(config: AppConfig) {
    this.config = config
  }

  async sendMessage(message: ZulipSendMessage): Promise<{ id?: number }> {
    const apiKey = this.config.ZULIP_API_KEY
    if (!apiKey) throw new Error('missing_zulip_api_key')
    const url = new URL('/api/v1/messages', this.config.ZULIP_SITE)
    const body = new URLSearchParams()
    body.set('type', message.type)
    body.set('to', JSON.stringify(message.to))
    if (message.topic) body.set('topic', message.topic)
    body.set('content', message.content)
    const response = await fetch(url, {
      method: 'POST',
      headers: {
        Authorization: `Basic ${btoa(`${this.config.ZULIP_BOT_EMAIL}:${apiKey}`)}`,
        'Content-Type': 'application/x-www-form-urlencoded'
      },
      body
    })
    const text = await response.text()
    const parsed = (text ? JSON.parse(text) : {}) as Record<string, unknown>
    if (!response.ok || parsed.result === 'error') {
      throw new Error(String(parsed.msg ?? parsed.code ?? `zulip_send_failed_${response.status}`))
    }
    return { id: typeof parsed.id === 'number' ? parsed.id : undefined }
  }

  async updateMessage(messageId: number, content: string): Promise<void> {
    const apiKey = this.config.ZULIP_API_KEY
    if (!apiKey) throw new Error('missing_zulip_api_key')
    const url = new URL(
      `/api/v1/messages/${encodeURIComponent(String(messageId))}`,
      this.config.ZULIP_SITE
    )
    const body = new URLSearchParams()
    body.set('content', content)
    const response = await fetch(url, {
      method: 'PATCH',
      headers: {
        Authorization: `Basic ${btoa(`${this.config.ZULIP_BOT_EMAIL}:${apiKey}`)}`,
        'Content-Type': 'application/x-www-form-urlencoded'
      },
      body
    })
    await parseZulipResponse(response, `zulip_update_failed_${response.status}`)
  }

  async setTypingStatus(status: ZulipTypingStatus): Promise<void> {
    const apiKey = this.config.ZULIP_API_KEY
    if (!apiKey) throw new Error('missing_zulip_api_key')
    const url = new URL('/api/v1/typing', this.config.ZULIP_SITE)
    const body = new URLSearchParams()
    body.set('op', status.op)
    if (status.stream_id !== undefined) body.set('stream_id', String(status.stream_id))
    if (status.topic !== undefined) body.set('topic', status.topic)
    if (status.to?.length) body.set('to', JSON.stringify(status.to))
    const response = await fetch(url, {
      method: 'POST',
      headers: {
        Authorization: `Basic ${btoa(`${this.config.ZULIP_BOT_EMAIL}:${apiKey}`)}`,
        'Content-Type': 'application/x-www-form-urlencoded'
      },
      body
    })
    await parseZulipResponse(response, `zulip_typing_failed_${response.status}`)
  }
}

async function parseZulipResponse(
  response: Response,
  fallbackError: string
): Promise<Record<string, unknown>> {
  const text = await response.text()
  const parsed = (text ? JSON.parse(text) : {}) as Record<string, unknown>
  if (!response.ok || parsed.result === 'error') {
    throw new Error(String(parsed.msg ?? parsed.code ?? fallbackError))
  }
  return parsed
}
