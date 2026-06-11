import type { AppConfig } from '../config'

export type ZulipSendMessage = {
  type: 'stream' | 'private'
  to: number | number[] | string | string[]
  topic?: string
  content: string
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
}
