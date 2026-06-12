import { describe, expect, it } from 'bun:test'
import type { AppConfig } from '../config'
import { ZulipProgressTracker } from './progress'

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
  ZULIP_PROGRESS_PLACEHOLDER: true,
  ZULIP_PROGRESS_TEXT: 'Working...',
  ZULIP_PROGRESS_UPDATE_MS: 60_000,
  ZULIP_PROGRESS_MAX_MS: 120_000
}

describe('ZulipProgressTracker', () => {
  it('starts typing, sends a placeholder, then edits it with the final answer', async () => {
    const calls: any[] = []
    const tracker = new ZulipProgressTracker(config, {
      sendMessage: async message => {
        calls.push({ method: 'sendMessage', message })
        return { id: 1234 }
      },
      updateMessage: async (messageId, content) => {
        calls.push({ method: 'updateMessage', messageId, content })
      },
      setTypingStatus: async status => {
        calls.push({ method: 'setTypingStatus', status })
      }
    })

    await tracker.start({
      thread_key: 'zulip:intendo:609693:Test%20Topic',
      message_id: 'zulip:intendo:42',
      realm: 'intendo',
      user_id: '7',
      is_mention: true,
      parts: [{ type: 'text', text: 'hello' }],
      zulip: {
        message_id: 42,
        message_type: 'stream',
        stream_id: 609693,
        topic: 'Test Topic'
      },
      delivery: {
        platform: 'zulip',
        message_type: 'stream',
        stream_id: 609693,
        topic: 'Test Topic'
      }
    })
    const executionId = tracker.attachExecution('zulip:intendo:609693:Test%20Topic', {
      execution_id: 'exe-test'
    })
    const edited = await tracker.completeDelivery(
      'exe-test',
      'zulip:intendo:609693:Test%20Topic',
      'final answer'
    )

    expect(executionId).toBe('exe-test')
    expect(edited).toBe(true)
    expect(calls).toEqual([
      {
        method: 'setTypingStatus',
        status: { type: 'stream', op: 'start', stream_id: 609693, topic: 'Test Topic' }
      },
      {
        method: 'sendMessage',
        message: {
          type: 'stream',
          to: 609693,
          topic: 'Test Topic',
          content: 'Working...'
        }
      },
      {
        method: 'setTypingStatus',
        status: { type: 'stream', op: 'stop', stream_id: 609693, topic: 'Test Topic' }
      },
      {
        method: 'updateMessage',
        messageId: 1234,
        content: 'final answer'
      }
    ])
  })

  it('edits a pending thread placeholder when the handoff did not expose an execution id', async () => {
    const calls: any[] = []
    const tracker = new ZulipProgressTracker(config, {
      sendMessage: async message => {
        calls.push({ method: 'sendMessage', message })
        return { id: 5678 }
      },
      updateMessage: async (messageId, content) => {
        calls.push({ method: 'updateMessage', messageId, content })
      },
      setTypingStatus: async status => {
        calls.push({ method: 'setTypingStatus', status })
      }
    })

    await tracker.start({
      thread_key: 'zulip:intendo:609693:Test%20Topic',
      message_id: 'zulip:intendo:43',
      realm: 'intendo',
      user_id: '7',
      is_mention: true,
      parts: [{ type: 'text', text: 'hello' }],
      zulip: {
        message_id: 43,
        message_type: 'stream',
        stream_id: 609693,
        topic: 'Test Topic'
      },
      delivery: {
        platform: 'zulip',
        message_type: 'stream',
        stream_id: 609693,
        topic: 'Test Topic'
      }
    })

    const edited = await tracker.completeDelivery(
      'exe-late',
      'zulip:intendo:609693:Test%20Topic',
      'late final answer'
    )

    expect(edited).toBe(true)
    expect(calls.at(-1)).toEqual({
      method: 'updateMessage',
      messageId: 5678,
      content: 'late final answer'
    })
  })
})
