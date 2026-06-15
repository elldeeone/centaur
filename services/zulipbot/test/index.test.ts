import { describe, expect, test } from 'bun:test'
import { createZulipbot } from '../src/index'
import type { ZulipbotFetch } from '../src/types'

describe('createZulipbot', () => {
  test('hydrates a missing stream topic from the live Zulip message before delivery', async () => {
    const apiCalls: Array<{ body?: unknown; method: string; url: string }> = []
    const zulipCalls: Array<{ body?: string; method: string; url: string }> = []

    const apiFetch: ZulipbotFetch = async (input, init) => {
      const url = input.toString()
      const method = init?.method ?? 'GET'
      const body = typeof init?.body === 'string' ? JSON.parse(init.body) : undefined
      apiCalls.push({ body, method, url })

      if (url.endsWith('/events?after_event_id=0&execution_id=exe_1')) {
        return Response.json({ result: 'hydrated reply' })
      }
      if (url.endsWith('/execute')) {
        return Response.json({
          execution_id: 'exe_1',
          ok: true,
          status: 'queued',
          thread_key: 'zulip:staghunt:609620:general%20chat'
        })
      }
      return Response.json({ ok: true })
    }

    const zulipFetch: ZulipbotFetch = async (input, init) => {
      const url = input.toString()
      const method = init?.method ?? 'GET'
      const body = init?.body instanceof URLSearchParams ? init.body.toString() : undefined
      zulipCalls.push({ body, method, url })

      if (method === 'GET' && url.includes('/api/v1/messages')) {
        return Response.json({
          messages: [
            {
              content: '@**centaur** dead?',
              display_recipient: 'STAGHUNT',
              id: 603107203,
              sender_email: 'luke@example.com',
              sender_full_name: 'Luke Dunshea',
              sender_id: 5,
              stream_id: 609620,
              subject: 'general chat',
              timestamp: 1_781_459_535,
              type: 'stream'
            }
          ],
          result: 'success'
        })
      }
      if (method === 'POST' && url.includes('/api/v1/messages')) {
        return Response.json({ id: 99, result: 'success' })
      }
      return Response.json({ result: 'success' })
    }

    const bot = createZulipbot({
      apiUrl: 'http://centaur.local',
      botEmail: 'centaur-bot@staghunt.zulipchat.com',
      fetch: apiFetch,
      site: 'https://staghunt.zulipchat.com',
      webhookToken: 'token',
      zulipApiKey: 'zulip-key',
      zulipFetch
    })

    const response = await bot.app.request('/api/webhooks/zulip', {
      body: JSON.stringify({
        data: '@**centaur** dead?',
        message: {
          id: 603107203,
          sender_email: 'luke@example.com',
          sender_full_name: 'Luke Dunshea',
          sender_id: 5,
          sender_realm_str: 'staghunt',
          stream_id: 609620,
          subject: '',
          timestamp: 1_781_459_535,
          type: 'stream'
        },
        token: 'token',
        trigger: 'mention'
      }),
      headers: { 'content-type': 'application/json' },
      method: 'POST'
    })

    expect(response.status).toBe(200)
    await waitFor(() => apiCalls.some(call => call.url.endsWith('/execute')))

    const executeCall = apiCalls.find(call => call.url.endsWith('/execute'))
    expect(executeCall?.body).toMatchObject({
      metadata: { thread_id: 'zulip:staghunt:609620:general%20chat' }
    })
    expect(JSON.stringify(apiCalls)).toContain('general%20chat')
    const progressPost = zulipCalls.find(
      call => call.method === 'POST' && call.url.includes('/api/v1/messages')
    )
    expect(progressPost?.body).toContain('topic=general+chat')
  })

  test('does not hydrate a missing topic from a neighboring Zulip message', async () => {
    const apiCalls: Array<{ body?: unknown; method: string; url: string }> = []
    const zulipCalls: Array<{ body?: string; method: string; url: string }> = []

    const apiFetch: ZulipbotFetch = async (input, init) => {
      const url = input.toString()
      const method = init?.method ?? 'GET'
      const body = typeof init?.body === 'string' ? JSON.parse(init.body) : undefined
      apiCalls.push({ body, method, url })

      if (url.endsWith('/events?after_event_id=0&execution_id=exe_1')) {
        return Response.json({ result: 'fallback reply' })
      }
      if (url.endsWith('/execute')) {
        return Response.json({
          execution_id: 'exe_1',
          ok: true,
          status: 'queued',
          thread_key: 'zulip:staghunt:609620:(no-topic)'
        })
      }
      return Response.json({ ok: true })
    }

    const zulipFetch: ZulipbotFetch = async (input, init) => {
      const url = input.toString()
      const method = init?.method ?? 'GET'
      const body = init?.body instanceof URLSearchParams ? init.body.toString() : undefined
      zulipCalls.push({ body, method, url })

      if (method === 'GET' && url.includes('/api/v1/messages')) {
        return Response.json({
          messages: [
            {
              content: 'nearby but not the anchor',
              display_recipient: 'STAGHUNT',
              id: 603107204,
              sender_email: 'luke@example.com',
              sender_full_name: 'Luke Dunshea',
              sender_id: 5,
              stream_id: 609620,
              subject: 'wrong topic',
              timestamp: 1_781_459_536,
              type: 'stream'
            }
          ],
          result: 'success'
        })
      }
      if (method === 'POST' && url.includes('/api/v1/messages')) {
        return Response.json({ msg: 'Missing topic', result: 'error' }, { status: 400 })
      }
      return Response.json({ result: 'success' })
    }

    const logs: Array<{ event: string; fields: Record<string, unknown> }> = []
    const bot = createZulipbot({
      apiUrl: 'http://centaur.local',
      botEmail: 'centaur-bot@staghunt.zulipchat.com',
      fetch: apiFetch,
      logger: {
        debug: (event, fields) => logs.push({ event, fields: fields ?? {} }),
        error: (event, fields) => logs.push({ event, fields: fields ?? {} }),
        info: (event, fields) => logs.push({ event, fields: fields ?? {} }),
        warn: (event, fields) => logs.push({ event, fields: fields ?? {} })
      },
      site: 'https://staghunt.zulipchat.com',
      webhookToken: 'token',
      zulipApiKey: 'zulip-key',
      zulipFetch
    })

    const response = await bot.app.request('/api/webhooks/zulip', {
      body: JSON.stringify({
        data: '@**centaur** dead?',
        message: {
          id: 603107203,
          sender_email: 'luke@example.com',
          sender_full_name: 'Luke Dunshea',
          sender_id: 5,
          sender_realm_str: 'staghunt',
          stream_id: 609620,
          subject: '',
          timestamp: 1_781_459_535,
          type: 'stream'
        },
        token: 'token',
        trigger: 'mention'
      }),
      headers: { 'content-type': 'application/json' },
      method: 'POST'
    })

    expect(response.status).toBe(200)
    await waitFor(() => apiCalls.some(call => call.url.endsWith('/execute')))

    const executeCall = apiCalls.find(call => call.url.endsWith('/execute'))
    expect(executeCall?.body).toMatchObject({
      metadata: { thread_id: 'zulip:staghunt:609620:(no-topic)' }
    })
    expect(JSON.stringify(apiCalls)).not.toContain('wrong%20topic')
    expect(JSON.stringify(zulipCalls)).not.toContain('topic=wrong+topic')
    expect(logs).toContainEqual(
      expect.objectContaining({
        event: 'zulipbot_topic_hydration_skipped',
        fields: expect.objectContaining({ reason: 'message_not_found' })
      })
    )
  })
})

async function waitFor(predicate: () => boolean): Promise<void> {
  const deadline = Date.now() + 1000
  while (Date.now() < deadline) {
    if (predicate()) return
    await new Promise(resolve => setTimeout(resolve, 10))
  }
  throw new Error('timed out waiting for condition')
}
