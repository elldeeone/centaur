import { describe, expect, test } from 'bun:test'
import { forwardToSessionApi } from '../src/session-api'
import type { ZulipbotApiMessage, ZulipbotFetch } from '../src/types'

describe('forwardToSessionApi', () => {
  test('creates, appends, executes, and renders a session turn', async () => {
    const calls: Array<{ body?: unknown; method: string; url: string }> = []
    const fetchFn: ZulipbotFetch = async (input, init) => {
      const url = input.toString()
      const method = init?.method ?? 'GET'
      const body = typeof init?.body === 'string' ? JSON.parse(init.body) : undefined
      calls.push({ body, method, url })

      if (url.endsWith('/events?after_event_id=0&execution_id=exe_1')) {
        return new Response(
          new ReadableStream<Uint8Array>({
            start(controller) {
              controller.enqueue(
                new TextEncoder().encode(
                  [
                    'id: 1',
                    'event: session.execution_completed',
                    'data: {"result":"hello from centaur"}',
                    '',
                    ''
                  ].join('\n')
                )
              )
              controller.close()
            }
          }),
          { status: 200 }
        )
      }

      if (url.endsWith('/execute')) {
        return Response.json({
          execution_id: 'exe_1',
          ok: true,
          status: 'queued',
          thread_key: 'zulip:staghunt:7:Test%20Topic'
        })
      }

      return Response.json({ ok: true, message_ids: ['msg_1'] })
    }

    const message: ZulipbotApiMessage = {
      author: {
        email: 'luke@example.com',
        fullName: 'Luke Dunshea',
        isBot: false,
        isMe: false,
        userId: '5',
        userName: 'Luke Dunshea'
      },
      context: [
        {
          author: {
            fullName: 'Luke Dunshea',
            isBot: false,
            isMe: false,
            userId: '5',
            userName: 'Luke Dunshea'
          },
          id: 'zulip:staghunt:1',
          isMention: false,
          metadata: {},
          parts: [{ type: 'text', text: 'Luke Dunshea at 2026-06-09T11:33:20.000Z: hello' }],
          role: 'user',
          text: 'hello',
          threadId: 'zulip:staghunt:7:Test%20Topic',
          timestamp: '2026-06-09T11:33:20.000Z'
        }
      ],
      id: 'zulip:staghunt:42',
      isMention: true,
      metadata: {},
      parts: [{ type: 'text', text: 'Luke Dunshea at 2026-06-09T11:34:20.000Z: current' }],
      role: 'user',
      text: 'current',
      threadId: 'zulip:staghunt:7:Test%20Topic',
      timestamp: '2026-06-09T11:34:20.000Z'
    }

    const result = await forwardToSessionApi(
      {
        apiUrl: 'http://centaur.local',
        botEmail: 'centaur-bot@example.com',
        fetch: fetchFn,
        site: 'https://staghunt.zulipchat.com',
        zulipApiKey: 'zulip-key'
      },
      {
        afterEventId: 0,
        executeMessage: message,
        messages: [message],
        openStream: true,
        threadId: 'zulip:staghunt:7:Test%20Topic'
      }
    )

    expect(result.answerMarkdown).toBe('hello from centaur')
    expect(calls.map(call => call.method)).toEqual(['POST', 'POST', 'POST', 'GET'])
    expect(calls[0]?.body).toMatchObject({
      harness_type: 'codex',
      metadata: { platform: 'zulip', source: 'zulipbot' }
    })
    expect(calls[1]?.body).toMatchObject({
      messages: [{ client_message_id: 'zulip:staghunt:42', role: 'user' }]
    })
    expect(JSON.stringify(calls[2]?.body)).toContain('Zulip topic context before the current message')
  })
})
