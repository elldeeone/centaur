import { describe, expect, it, mock } from 'bun:test'
import type { AppConfig } from '../config'
import { pollFinalDeliveriesOnce } from './final-delivery'

const config: AppConfig = {
  NODE_ENV: 'test',
  PORT: 3002,
  CENTAUR_API_URL: 'http://centaur-api.test',
  CENTAUR_API_KEY: 'centaur-test-key',
  ZULIP_EVENTS_PATH: '/api/webhooks/zulip',
  ZULIP_SITE: 'https://intendo.zulipchat.com',
  ZULIP_BOT_EMAIL: 'centaur@example.com',
  ZULIP_API_KEY: 'zulip-api-key',
  ZULIP_WEBHOOK_TOKEN: 'zulip-webhook-token',
  ZULIP_PERSONA: '',
  ZULIP_HARNESS: '',
  ZULIP_FINAL_DELIVERY_LIMIT: 5,
  ZULIP_DELIVERY_CHUNK_CHARS: 9000,
  ZULIP_HISTORY_LIMIT: 50,
  ZULIP_PROGRESS_PLACEHOLDER: true,
  ZULIP_PROGRESS_TEXT: 'Working...',
  ZULIP_PROGRESS_UPDATE_MS: 12_000,
  ZULIP_PROGRESS_MAX_MS: 120_000
}

describe('pollFinalDeliveriesOnce', () => {
  it('claims only Zulip deliveries and posts them back to their topic', async () => {
    const originalFetch = globalThis.fetch
    const fetchCalls: Array<{ path: string; body: any }> = []
    globalThis.fetch = mock(async (input: string | URL | Request, init?: RequestInit) => {
      const url = new URL(input instanceof Request ? input.url : input)
      const body = init?.body ? JSON.parse(init.body as string) : undefined
      fetchCalls.push({ path: url.pathname, body })
      if (url.pathname === '/agent/final-deliveries/claim') {
        return json({
          deliveries: [
            {
              execution_id: 'exe-zulip',
              thread_key: 'zulip:intendo:123:Deploy%20plan',
              delivery: {
                platform: 'zulip',
                message_type: 'stream',
                stream_id: 123,
                topic: 'Deploy plan'
              },
              final_payload: { result_text: 'done' }
            }
          ]
        })
      }
      if (url.pathname === '/agent/final-deliveries/exe-zulip/delivered') return json({ ok: true })
      throw new Error(`unexpected request: ${url.pathname}`)
    }) as unknown as typeof fetch

    const sent: any[] = []
    try {
      await pollFinalDeliveriesOnce(config, {
        sendMessage: async message => {
          sent.push(message)
          return { id: 100 }
        }
      })

      expect(fetchCalls[0]).toMatchObject({
        path: '/agent/final-deliveries/claim',
        body: {
          platform: 'zulip',
          limit: 5
        }
      })
      expect(sent).toEqual([
        {
          type: 'stream',
          to: 123,
          topic: 'Deploy plan',
          content: 'done'
        }
      ])
      expect(fetchCalls.map(call => call.path)).toContain(
        '/agent/final-deliveries/exe-zulip/delivered'
      )
    } finally {
      globalThis.fetch = originalFetch
    }
  })

  it('falls back to the Zulip thread key when delivery metadata has only the platform', async () => {
    const originalFetch = globalThis.fetch
    globalThis.fetch = mock(async (input: string | URL | Request) => {
      const url = new URL(input instanceof Request ? input.url : input)
      if (url.pathname === '/agent/final-deliveries/claim') {
        return json({
          deliveries: [
            {
              execution_id: 'exe-zulip-thread-key',
              thread_key: 'zulip:intendo:609693:Test%20Topic',
              delivery: { platform: 'zulip' },
              final_payload: { result_text: 'reply from fallback' }
            }
          ]
        })
      }
      if (url.pathname === '/agent/final-deliveries/exe-zulip-thread-key/delivered') {
        return json({ ok: true })
      }
      throw new Error(`unexpected request: ${url.pathname}`)
    }) as unknown as typeof fetch

    const sent: any[] = []
    try {
      await pollFinalDeliveriesOnce(config, {
        sendMessage: async message => {
          sent.push(message)
          return { id: 101 }
        }
      })

      expect(sent).toEqual([
        {
          type: 'stream',
          to: 609693,
          topic: 'Test Topic',
          content: 'reply from fallback'
        }
      ])
    } finally {
      globalThis.fetch = originalFetch
    }
  })

  it('edits a tracked placeholder instead of posting a duplicate final message', async () => {
    const originalFetch = globalThis.fetch
    globalThis.fetch = mock(async (input: string | URL | Request) => {
      const url = new URL(input instanceof Request ? input.url : input)
      if (url.pathname === '/agent/final-deliveries/claim') {
        return json({
          deliveries: [
            {
              execution_id: 'exe-zulip-edit',
              thread_key: 'zulip:intendo:609693:Test%20Topic',
              delivery: { platform: 'zulip' },
              final_payload: { result_text: 'edited final reply' }
            }
          ]
        })
      }
      if (url.pathname === '/agent/final-deliveries/exe-zulip-edit/delivered') {
        return json({ ok: true })
      }
      throw new Error(`unexpected request: ${url.pathname}`)
    }) as unknown as typeof fetch

    const sent: any[] = []
    const completed: Array<{ executionId: string; content: string }> = []
    try {
      await pollFinalDeliveriesOnce(
        config,
        {
          sendMessage: async message => {
            sent.push(message)
            return { id: 101 }
          }
        },
        {
          completeDelivery: async (executionId, threadKey, content) => {
            completed.push({ executionId, content })
            expect(threadKey).toBe('zulip:intendo:609693:Test%20Topic')
            return true
          },
          stopExecution: async () => {}
        }
      )

      expect(completed).toEqual([{ executionId: 'exe-zulip-edit', content: 'edited final reply' }])
      expect(sent).toEqual([])
    } finally {
      globalThis.fetch = originalFetch
    }
  })
})

function json(body: unknown, init: ResponseInit = {}): Response {
  return new Response(JSON.stringify(body), {
    status: init.status ?? 200,
    headers: { 'Content-Type': 'application/json' }
  })
}
