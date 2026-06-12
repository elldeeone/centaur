import { describe, expect, test } from 'bun:test'
import type { NormalizedZulipEvent } from '../src/types'
import { narrowForEvent, toHistoryMessages } from '../src/zulip/history'

const streamEvent: NormalizedZulipEvent = {
  thread_key: 'zulip:staghunt:7:Test%20Topic',
  message_id: 'zulip:staghunt:42',
  realm: 'staghunt',
  user_id: '5',
  is_mention: true,
  parts: [{ type: 'text', text: 'current' }],
  zulip: {
    message_id: 42,
    message_type: 'stream',
    stream_id: 7,
    stream_name: 'general',
    topic: 'Test Topic'
  },
  delivery: {
    platform: 'zulip',
    message_type: 'stream',
    stream_id: 7,
    topic: 'Test Topic'
  }
}

describe('Zulip history', () => {
  test('builds a topic narrow', () => {
    expect(narrowForEvent(streamEvent)).toEqual([
      { operator: 'channel', operand: 'general' },
      { operator: 'topic', operand: 'Test Topic' }
    ])
  })

  test('builds a direct-message narrow from sender id for 1:1 DMs', () => {
    expect(
      narrowForEvent(
        {
          ...streamEvent,
          delivery: {
            platform: 'zulip',
            message_type: 'direct',
            recipient_ids: [5, 9]
          },
          user_id: '5'
        },
        { botUserId: 9 }
      )
    ).toEqual([{ operator: 'dm', operand: 5 }])
  })

  test('converts history into speaker/timestamp lines and filters placeholders', () => {
    const messages = toHistoryMessages(
      [
        {
          id: 1,
          type: 'stream',
          sender_id: 5,
          sender_email: 'luke@example.com',
          sender_full_name: 'Luke Dunshea',
          timestamp: 1_781_000_000,
          content: 'hello',
          content_type: 'text/plain'
        },
        {
          id: 2,
          type: 'stream',
          sender_id: 9,
          sender_email: 'centaur-bot@example.com',
          sender_full_name: 'centaur',
          timestamp: 1_781_000_001,
          content: 'Working...\n\nStill working (13s).',
          content_type: 'text/plain'
        },
        {
          id: 3,
          type: 'stream',
          sender_id: 9,
          sender_email: 'centaur-bot@example.com',
          sender_full_name: 'centaur',
          timestamp: 1_781_000_002,
          content: 'answer',
          content_type: 'text/plain'
        }
      ],
      streamEvent,
      { botEmail: 'centaur-bot@example.com', progressText: 'Working...' }
    )

    expect(messages).toHaveLength(2)
    expect(messages[0]?.parts[0]?.text).toBe('Luke Dunshea at 2026-06-09T10:13:20.000Z: hello')
    expect(messages[1]?.role).toBe('assistant')
    expect(messages[1]?.metadata.timestamp).toBe('2026-06-09T10:13:22.000Z')
  })
})
