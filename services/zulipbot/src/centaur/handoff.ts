import { centaurApiKey, type AppConfig } from '../config'
import type { NormalizedZulipEvent } from '../zulip/types'

export type CentaurHandoffResult =
  | { ok: true; status: number; body: unknown }
  | { ok: false; status: number; body: unknown }

export class CentaurHandoff {
  readonly config: AppConfig

  constructor(config: AppConfig) {
    this.config = config
  }

  async emit(event: NormalizedZulipEvent): Promise<CentaurHandoffResult> {
    const url = new URL('/workflows/runs', this.config.CENTAUR_API_URL)
    const apiKey = centaurApiKey(this.config)
    const response = await fetch(url, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'X-Centaur-Thread-Key': event.thread_key,
        ...(apiKey ? { Authorization: `Bearer ${apiKey}` } : {})
      },
      body: JSON.stringify({
        workflow_name: 'agent_turn',
        trigger_key: event.message_id,
        eager_start: true,
        input: {
          thread_key: event.thread_key,
          parts: event.parts,
          message_id: event.message_id,
          user_id: event.user_id,
          metadata: {
            source: 'zulipbot',
            platform: 'zulip',
            zulip: event.zulip,
            is_mention: event.is_mention
          },
          delivery: event.delivery,
          ...(this.config.ZULIP_HARNESS ? { harness: this.config.ZULIP_HARNESS } : {}),
          ...(this.config.ZULIP_PERSONA ? { persona: this.config.ZULIP_PERSONA } : {})
        }
      })
    })
    const body = await readResponseBody(response)
    return { ok: response.ok, status: response.status, body }
  }
}

async function readResponseBody(response: Response): Promise<unknown> {
  const text = await response.text()
  if (!text) return null
  try {
    return JSON.parse(text)
  } catch {
    return text
  }
}
