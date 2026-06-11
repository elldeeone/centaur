export type ZulipMessageType = 'stream' | 'private' | 'channel' | 'direct'

export type ZulipDisplayRecipient =
  | string
  | Array<{
      id?: number
      email?: string
      full_name?: string
      short_name?: string
      is_mirror_dummy?: boolean
    }>

export type ZulipOutgoingWebhookPayload = {
  data?: string
  trigger?: string
  token?: string
  bot_email?: string
  bot_full_name?: string
  message?: {
    id?: number
    type?: ZulipMessageType | string
    stream_id?: number
    subject?: string
    topic?: string
    display_recipient?: ZulipDisplayRecipient
    recipient_id?: number
    sender_id?: number
    sender_email?: string
    sender_full_name?: string
    sender_realm_str?: string
    timestamp?: number
    content?: string
    rendered_content?: string
  }
}

export type NormalizedPart = {
  type: 'text'
  text: string
}

export type NormalizedZulipEvent = {
  thread_key: string
  message_id: string
  realm: string
  user_id: string
  is_mention: boolean
  parts: NormalizedPart[]
  zulip: {
    message_id?: number
    message_type?: string
    stream_id?: number
    topic?: string
    recipient_id?: number
    trigger?: string
    sender_email?: string
    sender_full_name?: string
    timestamp?: number
  }
  delivery: {
    platform: 'zulip'
    message_type: 'stream' | 'private'
    stream_id?: number
    topic?: string
    recipient_ids?: number[]
    recipient_emails?: string[]
  }
}
