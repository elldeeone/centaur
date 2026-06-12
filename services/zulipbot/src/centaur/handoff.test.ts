import { describe, expect, it, mock } from 'bun:test'
import type { AppConfig } from '../config'
import { CentaurHandoff } from './handoff'

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
  ZULIP_PERSONA: 'morphy',
  ZULIP_HARNESS: 'codex',
  ZULIP_FINAL_DELIVERY_LIMIT: 5,
  ZULIP_DELIVERY_CHUNK_CHARS: 9000,
  ZULIP_PROGRESS_PLACEHOLDER: true,
  ZULIP_PROGRESS_TEXT: 'Working...',
  ZULIP_PROGRESS_UPDATE_MS: 12_000,
  ZULIP_PROGRESS_MAX_MS: 120_000
}

describe('CentaurHandoff', () => {
  it('emits a provider-neutral agent_turn with Zulip delivery metadata', async () => {
    const originalFetch = globalThis.fetch
    let capturedInit: RequestInit | undefined
    globalThis.fetch = mock(async (_input: string | URL | Request, init?: RequestInit) => {
      capturedInit = init
      return new Response(JSON.stringify({ ok: true }), { status: 200 })
    }) as unknown as typeof fetch

    try {
      const result = await new CentaurHandoff(config).emit({
        thread_key: 'zulip:intendo:123:Deploy%20plan',
        message_id: 'zulip:intendo:42',
        realm: 'intendo',
        user_id: '7',
        is_mention: true,
        parts: [{ type: 'text', text: 'hello' }],
        zulip: {
          message_id: 42,
          message_type: 'stream',
          stream_id: 123,
          topic: 'Deploy plan',
          trigger: 'mention'
        },
        delivery: {
          platform: 'zulip',
          message_type: 'stream',
          stream_id: 123,
          topic: 'Deploy plan'
        }
      })

      expect(result.ok).toBe(true)
      expect(capturedInit?.headers).toMatchObject({
        'Content-Type': 'application/json',
        'X-Centaur-Thread-Key': 'zulip:intendo:123:Deploy%20plan',
        Authorization: 'Bearer centaur-test-key'
      })
      const body = JSON.parse(String(capturedInit?.body)) as any
      expect(body.workflow_name).toBe('agent_turn')
      expect(body.trigger_key).toBe('zulip:intendo:42')
      expect(body.input).toMatchObject({
        thread_key: 'zulip:intendo:123:Deploy%20plan',
        parts: [{ type: 'text', text: 'hello' }],
        metadata: {
          source: 'zulipbot',
          platform: 'zulip',
          is_mention: true
        },
        delivery: {
          platform: 'zulip',
          message_type: 'stream',
          stream_id: 123,
          topic: 'Deploy plan'
        },
        harness: 'codex',
        persona: 'morphy'
      })
    } finally {
      globalThis.fetch = originalFetch
    }
  })
})
