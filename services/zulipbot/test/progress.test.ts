import { describe, expect, test } from 'bun:test'
import type { NormalizedZulipEvent } from '../src/types'
import { progressText, ZulipProgressTracker } from '../src/zulip/progress'
import { noopLogger } from '../src/logging'

const event: NormalizedZulipEvent = {
  thread_key: 'zulip:staghunt:7:Test%20Topic',
  message_id: 'zulip:staghunt:42',
  realm: 'staghunt',
  user_id: '5',
  is_mention: true,
  parts: [{ type: 'text', text: 'hello' }],
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

describe('Zulip progress', () => {
  test('formats cold-start heartbeat text', () => {
    expect(
      progressText(
        { coldStart: true, coldStartNoticeSent: false },
        { progressText: 'Working...' },
        13
      )
    ).toBe('Starting Centaur runtime.\n\nFirst reply can take a moment.')
  })

  test('starts typing and placeholder message', async () => {
    const calls: string[] = []
    const tracker = new ZulipProgressTracker(
      {
        maxMs: 1000,
        placeholder: true,
        progressText: 'Working...',
        updateMs: 1000
      },
      {
        sendMessage: async message => {
          calls.push(`send:${message.type}:${message.content}`)
          return { id: 99 }
        },
        updateMessage: async (id, content) => {
          calls.push(`update:${id}:${content}`)
        },
        setTypingStatus: async status => {
          calls.push(`typing:${status.op}`)
        }
      },
      noopLogger
    )

    const handle = await tracker.start(event)
    await handle?.complete('done')

    expect(calls).toEqual(['typing:start', 'send:stream:Working...', 'typing:stop', 'update:99:done'])
  })
})
