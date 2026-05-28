export type ContentBlock = { type: 'text'; text: string }

export interface CentaurClientOptions {
  apiUrl: string
  apiKey: string
  fetchImpl?: typeof fetch
}

export interface SpawnOptions {
  threadKey: string
  harness?: string
  engine?: string
  personaId?: string
}

export interface SpawnResult {
  assignment_generation: number
  runtime_id?: string
  thread_key: string
}

export interface ExecuteResult {
  execution_id: string
  status: string
}

export interface StreamEvent {
  eventId: number
  eventKind: string
  data: Record<string, unknown>
}

export class CentaurClient {
  readonly apiUrl: string
  readonly apiKey: string
  private readonly fetchImpl: typeof fetch

  constructor(options: CentaurClientOptions) {
    this.apiUrl = options.apiUrl.replace(/\/+$/, '')
    this.apiKey = options.apiKey
    this.fetchImpl = options.fetchImpl ?? fetch
  }

  async spawn(options: SpawnOptions): Promise<SpawnResult> {
    return this.request('/agent/spawn', {
      method: 'POST',
      body: {
        thread_key: options.threadKey,
        harness: options.harness,
        engine: options.engine,
        persona_id: options.personaId,
      },
    })
  }

  async message(options: {
    threadKey: string
    assignmentGeneration: number
    parts: ContentBlock[]
  }): Promise<{ ok: boolean; message_id: string }> {
    return this.request('/agent/message', {
      method: 'POST',
      body: {
        thread_key: options.threadKey,
        assignment_generation: options.assignmentGeneration,
        role: 'user',
        parts: options.parts,
        metadata: { platform: 'cli' },
      },
    })
  }

  async execute(options: {
    threadKey: string
    assignmentGeneration: number
    harness?: string
  }): Promise<ExecuteResult> {
    return this.request('/agent/execute', {
      method: 'POST',
      body: {
        thread_key: options.threadKey,
        assignment_generation: options.assignmentGeneration,
        harness: options.harness,
        delivery: { platform: 'cli' },
        metadata: { platform: 'cli' },
      },
    })
  }

  async getExecution(executionId: string): Promise<Record<string, unknown>> {
    return this.request(`/agent/executions/${encodeURIComponent(executionId)}`)
  }

  async *streamEvents(options: {
    threadKey: string
    executionId?: string
    afterEventId?: number
    pollMs?: number
  }): AsyncGenerator<StreamEvent> {
    const params = new URLSearchParams()
    if (options.executionId) params.set('execution_id', options.executionId)
    if (options.afterEventId !== undefined) {
      params.set('after_event_id', String(options.afterEventId))
    }
    if (options.pollMs !== undefined) params.set('poll_ms', String(options.pollMs))

    const response = await this.fetchImpl(
      `${this.apiUrl}/agent/threads/${encodeURIComponent(options.threadKey)}/events?${params}`,
      {
        headers: this.headers({ threadKey: options.threadKey, json: false }),
      },
    )
    if (!response.ok) {
      const text = await response.text().catch(() => '')
      throw new Error(`stream failed (${response.status}): ${text.slice(0, 300)}`)
    }
    if (!response.body) return

    for await (const event of parseSse(response.body)) {
      if (!event.data || event.data === '[DONE]') continue
      let data: Record<string, unknown>
      try {
        data = JSON.parse(event.data) as Record<string, unknown>
      } catch {
        data = { type: 'unknown', raw: event.data }
      }
      yield {
        eventId: Number(event.id || 0),
        eventKind: event.event || 'message',
        data,
      }
    }
  }

  private async request<T>(
    path: string,
    options: { method?: string; body?: Record<string, unknown> } = {},
  ): Promise<T> {
    const response = await this.fetchImpl(`${this.apiUrl}${path}`, {
      method: options.method ?? 'GET',
      headers: this.headers({ json: true }),
      body: options.body === undefined ? undefined : JSON.stringify(options.body),
    })
    if (!response.ok) {
      const text = await response.text().catch(() => '')
      throw new Error(`${path} failed (${response.status}): ${text.slice(0, 300)}`)
    }
    return response.json() as Promise<T>
  }

  private headers(options: { json: boolean; threadKey?: string }) {
    const headers: Record<string, string> = {
      Authorization: `Bearer ${this.apiKey}`,
      'X-Api-Key': this.apiKey,
    }
    if (options.json) headers['Content-Type'] = 'application/json'
    if (options.threadKey) headers['X-Centaur-Thread-Key'] = options.threadKey
    return headers
  }
}

interface SseFrame {
  id?: string
  event?: string
  data: string
}

export async function* parseSse(stream: ReadableStream<Uint8Array>): AsyncGenerator<SseFrame> {
  const reader = stream.getReader()
  const decoder = new TextDecoder()
  let buffer = ''

  for (;;) {
    const { value, done } = await reader.read()
    if (done) break
    buffer += decoder.decode(value, { stream: true })
    let boundary = findFrameBoundary(buffer)
    while (boundary !== -1) {
      const frame = buffer.slice(0, boundary.index)
      buffer = buffer.slice(boundary.index + boundary.length)
      const parsed = parseFrame(frame)
      if (parsed) yield parsed
      boundary = findFrameBoundary(buffer)
    }
  }

  buffer += decoder.decode()
  if (buffer.trim()) {
    const parsed = parseFrame(buffer)
    if (parsed) yield parsed
  }
}

function findFrameBoundary(value: string): { index: number; length: number } | -1 {
  const unix = value.indexOf('\n\n')
  const windows = value.indexOf('\r\n\r\n')
  if (unix === -1 && windows === -1) return -1
  if (windows !== -1 && (unix === -1 || windows < unix)) return { index: windows, length: 4 }
  return { index: unix, length: 2 }
}

function parseFrame(frame: string): SseFrame | undefined {
  const out: SseFrame = { data: '' }
  const data: string[] = []
  for (const rawLine of frame.split(/\r?\n/)) {
    if (!rawLine || rawLine.startsWith(':')) continue
    const colon = rawLine.indexOf(':')
    const field = colon === -1 ? rawLine : rawLine.slice(0, colon)
    const value = colon === -1 ? '' : rawLine.slice(colon + 1).replace(/^ /, '')
    if (field === 'id') out.id = value
    if (field === 'event') out.event = value
    if (field === 'data') data.push(value)
  }
  if (data.length === 0) return undefined
  out.data = data.join('\n')
  return out
}
