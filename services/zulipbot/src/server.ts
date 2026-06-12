import { loadOptions } from './config'
import { createZulipbot } from './index'

const LOG_LEVELS = ['debug', 'info', 'warn', 'error'] as const
const minLogLevel: (typeof LOG_LEVELS)[number] = (() => {
  const value = process.env.ZULIPBOT_LOG_LEVEL?.trim().toLowerCase()
  return (LOG_LEVELS as readonly string[]).includes(value ?? '')
    ? (value as (typeof LOG_LEVELS)[number])
    : 'info'
})()

const consoleLogger = {
  debug: (message: string, data?: Record<string, unknown>) => log('debug', message, data),
  info: (message: string, data?: Record<string, unknown>) => log('info', message, data),
  warn: (message: string, data?: Record<string, unknown>) => log('warn', message, data),
  error: (message: string, data?: Record<string, unknown>) => log('error', message, data),
  child: () => consoleLogger
}

const options = {
  ...loadOptions(),
  logger: consoleLogger
}
const port = Number.parseInt(process.env.PORT ?? '3002', 10)
const { app } = createZulipbot(options)
const server = Bun.serve({
  port,
  fetch: app.fetch
})

console.log(
  JSON.stringify({
    timestamp: new Date().toISOString(),
    level: 'info',
    event: 'zulipbot_started',
    service: 'zulipbot',
    port: server.port,
    api_url: options.apiUrl,
    zulip_site: options.site
  })
)

function log(level: (typeof LOG_LEVELS)[number], message: string, data?: Record<string, unknown>): void {
  if (LOG_LEVELS.indexOf(level) < LOG_LEVELS.indexOf(minLogLevel)) return
  console.log(
    JSON.stringify({
      level,
      service: 'zulipbot',
      timestamp: new Date().toISOString(),
      event: message,
      ...(data && typeof data === 'object' ? data : {})
    })
  )
}
