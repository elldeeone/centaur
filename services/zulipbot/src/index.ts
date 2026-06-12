import { Hono } from 'hono'
import { loadConfig } from './config'
import { CentaurHandoff } from './centaur/handoff'
import { startFinalDeliveryPoller } from './centaur/final-delivery'
import { logError, logInfo, logWarn } from './logging'
import { ZulipClient } from './zulip/client'
import { normalizeZulipWebhookPayload } from './zulip/normalize'
import { ZulipProgressTracker } from './zulip/progress'
import type { ZulipOutgoingWebhookPayload } from './zulip/types'

const config = loadConfig()
const app = new Hono()
const handoff = new CentaurHandoff(config)
const zulipClient = new ZulipClient(config)
const progress = new ZulipProgressTracker(config, zulipClient)

app.get('/health', c =>
  c.json({
    ok: true,
    service: 'zulipbot'
  })
)

app.post(config.ZULIP_EVENTS_PATH, async c => {
  let payload: ZulipOutgoingWebhookPayload
  try {
    payload = await c.req.json<ZulipOutgoingWebhookPayload>()
  } catch {
    return c.json({ ok: false, error: 'invalid_json' }, 400)
  }

  if (config.ZULIP_WEBHOOK_TOKEN && payload.token !== config.ZULIP_WEBHOOK_TOKEN) {
    logWarn('zulip_webhook_token_rejected', {
      message_id: payload.message?.id,
      sender_id: payload.message?.sender_id
    })
    return c.json({ ok: false, error: 'invalid_token' }, 401)
  }

  const normalized = normalizeZulipWebhookPayload(payload, {
    defaultRealm: config.ZULIP_SITE,
    botEmail: config.ZULIP_BOT_EMAIL
  })
  if (!normalized) return c.json({ ok: true, ignored: true })

  await progress.start(normalized)
  const result = await handoff.emit(normalized)
  if (!result.ok) {
    await progress.failThread(normalized.thread_key, 'Centaur could not start this turn.')
    logError('centaur_zulip_handoff_failed', {
      status: result.status,
      body: result.body,
      thread_key: normalized.thread_key
    })
    return c.json({ ok: false, error: 'centaur_handoff_failed' }, 502)
  }
  logInfo('centaur_zulip_handoff_ok', {
    thread_key: normalized.thread_key,
    message_id: normalized.message_id
  })
  progress.attachExecution(normalized.thread_key, result.body)
  return c.json({ ok: true })
})

startFinalDeliveryPoller(config, zulipClient, progress)

export default {
  port: config.PORT,
  fetch: app.fetch
}
