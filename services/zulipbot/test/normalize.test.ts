import { describe, expect, test } from 'bun:test'
import { normalizeZulipWebhookPayload } from '../src/zulip/normalize'

describe('normalizeZulipWebhookPayload', () => {
  test('normalizes stream mentions into stable topic thread keys', () => {
    const event = normalizeZulipWebhookPayload(
      {
        data: '@centaur hello',
        trigger: 'mention',
        message: {
          id: 42,
          type: 'stream',
          stream_id: 7,
          display_recipient: 'general',
          sender_id: 5,
          sender_email: 'luke@example.com',
          sender_full_name: 'Luke Dunshea',
          sender_realm_str: 'staghunt',
          timestamp: 1_781_000_000,
          topic: 'Test Topic'
        }
      },
      { botEmail: 'centaur-bot@example.com', defaultRealm: 'https://staghunt.zulipchat.com' }
    )

    expect(event?.thread_key).toBe('zulip:staghunt:7:Test%20Topic')
    expect(event?.message_id).toBe('zulip:staghunt:42')
    expect(event?.is_mention).toBe(true)
    expect(event?.delivery).toEqual({
      platform: 'zulip',
      message_type: 'stream',
      stream_id: 7,
      topic: 'Test Topic'
    })
  })

  test('ignores messages authored by the bot', () => {
    const event = normalizeZulipWebhookPayload(
      {
        data: 'loop',
        message: {
          id: 44,
          type: 'stream',
          stream_id: 7,
          sender_email: 'centaur-bot@example.com',
          topic: 'Test Topic'
        }
      },
      { botEmail: 'centaur-bot@example.com', defaultRealm: 'https://staghunt.zulipchat.com' }
    )

    expect(event).toBeNull()
  })

  test('normalizes direct messages', () => {
    const event = normalizeZulipWebhookPayload(
      {
        data: 'hello',
        message: {
          id: 45,
          type: 'private',
          display_recipient: [
            { id: 5, email: 'luke@example.com' },
            { id: 9, email: 'centaur-bot@example.com' }
          ],
          sender_id: 5,
          sender_email: 'luke@example.com',
          sender_realm_str: 'staghunt'
        }
      },
      { botEmail: 'centaur-bot@example.com', defaultRealm: 'https://staghunt.zulipchat.com' }
    )

    expect(event?.thread_key).toBe('zulipdm:staghunt:5,9')
    expect(event?.delivery.message_type).toBe('direct')
    expect(event?.delivery.recipient_ids).toEqual([5, 9])
  })
})
