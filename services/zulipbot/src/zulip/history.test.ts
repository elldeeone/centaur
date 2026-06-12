import { describe, expect, it } from 'bun:test'
import { toHistoryMessages } from './history'
import type { NormalizedZulipEvent } from './types'

const event: NormalizedZulipEvent = {
  thread_key: 'zulip:intendo:609693:Test%20Topic',
  message_id: 'zulip:intendo:50',
  realm: 'intendo',
  user_id: '7',
  is_mention: true,
  parts: [{ type: 'text', text: '@centaur what was first?' }],
  zulip: {
    message_id: 50,
    message_type: 'stream',
    stream_id: 609693,
    stream_name: 'general',
    topic: 'Test Topic'
  },
  delivery: {
    platform: 'zulip',
    message_type: 'stream',
    stream_id: 609693,
    topic: 'Test Topic'
  }
}

describe('toHistoryMessages', () => {
  it('maps older Zulip topic messages into Centaur history messages', () => {
    const history = toHistoryMessages(
      [
        {
          id: 40,
          type: 'stream',
          stream_id: 609693,
          topic: 'Test Topic',
          sender_id: 7,
          sender_email: 'luke@example.com',
          sender_full_name: 'Luke Dunshea',
          content: 'hello',
          content_type: 'text/x-markdown'
        },
        {
          id: 41,
          type: 'stream',
          stream_id: 609693,
          topic: 'Test Topic',
          sender_id: 22,
          sender_email: 'centaur@example.com',
          sender_full_name: 'centaur',
          content: 'Yes, I am here.',
          content_type: 'text/x-markdown'
        },
        {
          id: 50,
          type: 'stream',
          stream_id: 609693,
          topic: 'Test Topic',
          sender_id: 7,
          sender_email: 'luke@example.com',
          sender_full_name: 'Luke Dunshea',
          content: '@centaur what was first?',
          content_type: 'text/x-markdown'
        }
      ],
      event,
      { botEmail: 'centaur@example.com', progressText: 'Working...' }
    )

    expect(history).toEqual([
      {
        message_id: 'zulip:intendo:40',
        role: 'user',
        user_id: '7',
        parts: [{ type: 'text', text: 'Luke Dunshea: hello' }],
        metadata: {
          source: 'zulip_history',
          platform: 'zulip',
          zulip: {
            message_id: 40,
            message_type: 'stream',
            stream_id: 609693,
            topic: 'Test Topic',
            recipient_id: undefined,
            sender_email: 'luke@example.com',
            sender_full_name: 'Luke Dunshea',
            timestamp: undefined
          }
        }
      },
      {
        message_id: 'zulip:intendo:41',
        role: 'assistant',
        user_id: '22',
        parts: [{ type: 'text', text: 'centaur: Yes, I am here.' }],
        metadata: {
          source: 'zulip_history',
          platform: 'zulip',
          zulip: {
            message_id: 41,
            message_type: 'stream',
            stream_id: 609693,
            topic: 'Test Topic',
            recipient_id: undefined,
            sender_email: 'centaur@example.com',
            sender_full_name: 'centaur',
            timestamp: undefined
          }
        }
      }
    ])
  })

  it('skips old progress placeholders from the bot', () => {
    const history = toHistoryMessages(
      [
        {
          id: 42,
          sender_id: 22,
          sender_email: 'centaur@example.com',
          sender_full_name: 'centaur',
          content: 'Working...\n\nStill working (109s).',
          content_type: 'text/x-markdown'
        }
      ],
      event,
      { botEmail: 'centaur@example.com', progressText: 'Working...' }
    )

    expect(history).toEqual([])
  })

  it('turns rendered Zulip HTML into text if the server returns HTML', () => {
    const history = toHistoryMessages(
      [
        {
          id: 43,
          sender_id: 7,
          sender_email: 'luke@example.com',
          sender_full_name: 'Luke Dunshea',
          content: '<p><span class="user-mention">@centaur</span> fish &amp; chips<br>please</p>',
          content_type: 'text/html'
        }
      ],
      event,
      { botEmail: 'centaur@example.com', progressText: 'Working...' }
    )

    expect(history[0]?.parts).toEqual([
      { type: 'text', text: 'Luke Dunshea: @centaur fish & chips\nplease' }
    ])
  })
})
