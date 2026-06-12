import { describe, expect, it } from 'bun:test'
import { normalizeZulipWebhookPayload } from './normalize'

describe('normalizeZulipWebhookPayload', () => {
  it('maps channel mentions to a Zulip thread key and stream delivery target', () => {
    const event = normalizeZulipWebhookPayload(
      {
        data: '@**Centaur** check this',
        trigger: 'mention',
        token: 'secret',
        message: {
          id: 42,
          type: 'stream',
          stream_id: 123,
          display_recipient: 'general',
          subject: 'Deploy plan',
          recipient_id: 99,
          sender_id: 7,
          sender_email: 'luke@example.com',
          sender_full_name: 'Luke',
          sender_realm_str: 'intendo',
          timestamp: 1781234567
        }
      },
      { defaultRealm: 'https://intendo.zulipchat.com', botEmail: 'centaur@example.com' }
    )

    expect(event?.thread_key).toBe('zulip:intendo:123:Deploy%20plan')
    expect(event?.message_id).toBe('zulip:intendo:42')
    expect(event?.zulip.stream_name).toBe('general')
    expect(event?.parts).toEqual([{ type: 'text', text: '@**Centaur** check this' }])
    expect(event?.delivery).toEqual({
      platform: 'zulip',
      message_type: 'stream',
      stream_id: 123,
      topic: 'Deploy plan'
    })
  })

  it('maps direct messages to a separate Zulip DM namespace', () => {
    const event = normalizeZulipWebhookPayload(
      {
        data: 'hello',
        trigger: 'direct_message',
        message: {
          id: 43,
          type: 'private',
          sender_id: 7,
          sender_email: 'luke@example.com',
          sender_realm_str: 'intendo',
          display_recipient: [
            { id: 7, email: 'luke@example.com' },
            { id: 22, email: 'centaur@example.com' }
          ]
        }
      },
      { defaultRealm: 'https://intendo.zulipchat.com', botEmail: 'centaur@example.com' }
    )

    expect(event?.thread_key).toBe('zulipdm:intendo:7,22')
    expect(event?.delivery).toEqual({
      platform: 'zulip',
      message_type: 'private',
      recipient_ids: [7, 22],
      recipient_emails: ['centaur@example.com', 'luke@example.com']
    })
  })

  it('ignores messages sent by the bot itself', () => {
    const event = normalizeZulipWebhookPayload(
      {
        data: 'loop',
        trigger: 'direct_message',
        message: {
          id: 44,
          type: 'private',
          sender_email: 'centaur@example.com'
        }
      },
      { defaultRealm: 'https://intendo.zulipchat.com', botEmail: 'centaur@example.com' }
    )

    expect(event).toBeNull()
  })
})
