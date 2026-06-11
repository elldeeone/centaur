import { z } from 'zod'

const EnvSchema = z.object({
  NODE_ENV: z.string().default('development'),
  PORT: z.coerce.number().int().positive().default(3002),
  CENTAUR_API_URL: z.string().url().default('http://localhost:8000'),
  CENTAUR_API_KEY: z.string().optional(),
  ZULIP_EVENTS_PATH: z.string().default('/api/webhooks/zulip'),
  ZULIP_SITE: z.string().url(),
  ZULIP_BOT_EMAIL: z.string().email(),
  ZULIP_API_KEY: z.string().optional(),
  ZULIP_WEBHOOK_TOKEN: z.string().optional(),
  ZULIP_PERSONA: z.string().default(''),
  ZULIP_HARNESS: z.string().default(''),
  ZULIP_FINAL_DELIVERY_LIMIT: z.coerce.number().int().positive().max(20).default(5),
  ZULIP_DELIVERY_CHUNK_CHARS: z.coerce.number().int().positive().default(9000)
})

export type AppConfig = z.infer<typeof EnvSchema>

export function loadConfig(env: NodeJS.ProcessEnv = process.env): AppConfig {
  return EnvSchema.parse(env)
}

export function centaurApiKey(config: AppConfig): string | undefined {
  return config.CENTAUR_API_KEY || undefined
}

export function zulipApiKey(config: AppConfig): string | undefined {
  return config.ZULIP_API_KEY || undefined
}
