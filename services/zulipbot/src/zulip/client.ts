import type { ZulipDisplayRecipient, ZulipbotFetch } from '../types'

export type ZulipSendMessage = {
  type: 'stream' | 'channel' | 'direct' | 'private'
  to: number | number[] | string | string[]
  topic?: string
  content: string
}

export type ZulipTypingStatus = {
  type?: 'direct' | 'stream' | 'channel'
  op: 'start' | 'stop'
  stream_id?: number
  topic?: string
  to?: number[]
}

export type ZulipNarrowTerm = {
  operator: string
  operand: string | number | string[] | number[]
}

export type ZulipGetMessagesRequest = {
  anchor: number | string
  num_before: number
  num_after: number
  narrow?: ZulipNarrowTerm[]
  include_anchor?: boolean
  apply_markdown?: boolean
}

export type ZulipFetchedMessage = {
  id: number
  type?: string
  stream_id?: number
  subject?: string
  topic?: string
  display_recipient?: ZulipDisplayRecipient
  recipient_id?: number
  sender_id?: number
  sender_email?: string
  sender_full_name?: string
  timestamp?: number
  content?: string
  content_type?: string
}

export class ZulipClient {
  readonly apiKey: string
  readonly botEmail: string
  readonly fetchFn: ZulipbotFetch
  readonly site: string

  constructor(input: {
    apiKey: string
    botEmail: string
    fetchFn?: ZulipbotFetch
    site: string
  }) {
    this.apiKey = input.apiKey
    this.botEmail = input.botEmail
    this.fetchFn = input.fetchFn ?? fetch
    this.site = input.site
  }

  async sendMessage(message: ZulipSendMessage): Promise<{ id?: number }> {
    const url = new URL('/api/v1/messages', this.site)
    const body = new URLSearchParams()
    body.set('type', message.type)
    body.set('to', JSON.stringify(message.to))
    if (message.topic) body.set('topic', message.topic)
    body.set('content', message.content)
    const response = await this.fetchFn(url, {
      method: 'POST',
      headers: {
        Authorization: this.authHeader(),
        'Content-Type': 'application/x-www-form-urlencoded'
      },
      body
    })
    const parsed = await parseZulipResponse(response, `zulip_send_failed_${response.status}`)
    return { id: typeof parsed.id === 'number' ? parsed.id : undefined }
  }

  async getMessages(request: ZulipGetMessagesRequest): Promise<ZulipFetchedMessage[]> {
    const url = new URL('/api/v1/messages', this.site)
    url.searchParams.set('anchor', String(request.anchor))
    url.searchParams.set('num_before', String(request.num_before))
    url.searchParams.set('num_after', String(request.num_after))
    if (request.narrow?.length) url.searchParams.set('narrow', JSON.stringify(request.narrow))
    if (request.include_anchor !== undefined) {
      url.searchParams.set('include_anchor', String(request.include_anchor))
    }
    if (request.apply_markdown !== undefined) {
      url.searchParams.set('apply_markdown', String(request.apply_markdown))
    }

    const response = await this.fetchFn(url, {
      method: 'GET',
      headers: { Authorization: this.authHeader() }
    })
    const parsed = await parseZulipResponse(response, `zulip_get_messages_failed_${response.status}`)
    const messages = parsed.messages
    return Array.isArray(messages) ? (messages as ZulipFetchedMessage[]) : []
  }

  async updateMessage(messageId: number, content: string): Promise<void> {
    const url = new URL(`/api/v1/messages/${encodeURIComponent(String(messageId))}`, this.site)
    const body = new URLSearchParams()
    body.set('content', content)
    const response = await this.fetchFn(url, {
      method: 'PATCH',
      headers: {
        Authorization: this.authHeader(),
        'Content-Type': 'application/x-www-form-urlencoded'
      },
      body
    })
    await parseZulipResponse(response, `zulip_update_failed_${response.status}`)
  }

  async setTypingStatus(status: ZulipTypingStatus): Promise<void> {
    const url = new URL('/api/v1/typing', this.site)
    const body = new URLSearchParams()
    if (status.type) body.set('type', status.type)
    body.set('op', status.op)
    if (status.stream_id !== undefined) body.set('stream_id', String(status.stream_id))
    if (status.topic !== undefined) body.set('topic', status.topic)
    if (status.to?.length) body.set('to', JSON.stringify(status.to))
    const response = await this.fetchFn(url, {
      method: 'POST',
      headers: {
        Authorization: this.authHeader(),
        'Content-Type': 'application/x-www-form-urlencoded'
      },
      body
    })
    await parseZulipResponse(response, `zulip_typing_failed_${response.status}`)
  }

  private authHeader(): string {
    return `Basic ${Buffer.from(`${this.botEmail}:${this.apiKey}`).toString('base64')}`
  }
}

async function parseZulipResponse(
  response: Response,
  fallbackError: string
): Promise<Record<string, unknown>> {
  const text = await response.text()
  let parsed: Record<string, unknown>
  try {
    parsed = (text ? JSON.parse(text) : {}) as Record<string, unknown>
  } catch {
    parsed = {}
  }
  if (!response.ok || parsed.result === 'error') {
    throw new Error(String(parsed.msg ?? parsed.code ?? fallbackError))
  }
  return parsed
}
