const EMAIL_RE = /\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b/gi
const BEARER_TOKEN_RE = /\bbearer\s+[A-Z0-9._~+/=-]+/gi
const SECRET_FIELD_TOKENS = new Set(['apikey', 'authorization', 'secret', 'token'])

export function sanitizeLogValue(value: unknown, fieldName?: string): unknown {
  if (value === null || value === undefined) return value
  if (typeof value === 'boolean' || typeof value === 'number') return value
  if (typeof value === 'string') {
    const normalized = (fieldName ?? '').toLowerCase().replace(/[^a-z0-9]/g, '')
    if ([...SECRET_FIELD_TOKENS].some(token => normalized.includes(token))) {
      return '[REDACTED:secret]'
    }
    return value.replace(BEARER_TOKEN_RE, 'Bearer [REDACTED:secret]').replace(EMAIL_RE, '[REDACTED:email]')
  }
  if (typeof value !== 'object') return String(value)
  if (Array.isArray(value)) return value.map(item => sanitizeLogValue(item, fieldName))
  if (value instanceof Error) return { name: value.name, message: sanitizeLogValue(value.message) }
  return Object.fromEntries(
    Object.entries(value).map(([key, item]) => [key, sanitizeLogValue(item, key)])
  )
}

export function logInfo(event: string, ...values: unknown[]): void {
  console.log(event, ...values.map(value => sanitizeLogValue(value)))
}

export function logWarn(event: string, ...values: unknown[]): void {
  console.warn(event, ...values.map(value => sanitizeLogValue(value)))
}

export function logError(event: string, ...values: unknown[]): void {
  console.error(event, ...values.map(value => sanitizeLogValue(value)))
}
