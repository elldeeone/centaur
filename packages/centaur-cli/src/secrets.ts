import { chmodSync, mkdirSync, mkdtempSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { dirname, join } from 'node:path'
import { spawnSync } from 'node:child_process'

import type { SecretBackend } from './constants.js'
import { expandPath } from './state.js'

export type SecretMap = Record<string, string>

export type SecretBackendOptions = {
  localEnvPath?: string
  kubernetesNamespace?: string
  kubernetesSecretName?: string
  onePasswordVault?: string
  sopsPath?: string
  vaultPath?: string
}

export type SecretWriteResult = {
  backend: SecretBackend
  target: string
  writtenKeys: string[]
  command: string
}

function shellQuote(value: string) {
  return `'${value.replaceAll("'", "'\\''")}'`
}

function run(command: string[], options: { input?: Buffer | string } = {}) {
  const proc = spawnSync(command[0]!, command.slice(1), {
    encoding: 'utf8',
    input: options.input,
    stdio: options.input ? ['pipe', 'pipe', 'pipe'] : ['ignore', 'pipe', 'pipe'],
  })
  if (proc.status !== 0) {
    const stderr = proc.stderr || proc.stdout || ''
    throw new Error(`${command.join(' ')} failed: ${stderr.trim()}`)
  }
  return proc.stdout
}

function dotenvValue(value: string) {
  if (/^[A-Za-z0-9_./:@%+=,-]+$/.test(value)) return value
  return JSON.stringify(value)
}

function dotenv(secrets: SecretMap) {
  return `${Object.entries(secrets)
    .sort(([a], [b]) => a.localeCompare(b))
    .map(([key, value]) => `${key}=${dotenvValue(value)}`)
    .join('\n')}\n`
}

export function kubernetesEnvFile(secrets: SecretMap) {
  for (const [key, value] of Object.entries(secrets)) {
    if (value.includes('\n')) {
      throw new Error(`${key} contains a newline and cannot be written via kubectl --from-env-file`)
    }
  }
  return `${Object.entries(secrets)
    .sort(([a], [b]) => a.localeCompare(b))
    .map(([key, value]) => `${key}=${value}`)
    .join('\n')}\n`
}

function tempSecretFile(secrets: SecretMap, format: 'dotenv' | 'kubernetes-env' = 'dotenv') {
  const dir = mkdtempSync(join(tmpdir(), 'centaur-secrets-'))
  const path = join(dir, 'secrets.env')
  writeFileSync(path, format === 'kubernetes-env' ? kubernetesEnvFile(secrets) : dotenv(secrets), {
    mode: 0o600,
  })
  chmodSync(path, 0o600)
  return { dir, path }
}

function onePasswordTemplate(title: string, value: string, existing?: unknown) {
  const item: Record<string, unknown> =
    existing && typeof existing === 'object'
      ? { ...(existing as Record<string, unknown>), title }
      : { title, category: 'PASSWORD' }
  const fields = Array.isArray(item.fields) ? [...item.fields] : []
  const passwordFieldIndex = fields.findIndex(field => {
    if (!field || typeof field !== 'object') return false
    const candidate = field as Record<string, unknown>
    return candidate.purpose === 'PASSWORD' || candidate.id === 'password' || candidate.label === 'password'
  })
  const passwordField = {
    id: 'password',
    type: 'CONCEALED',
    purpose: 'PASSWORD',
    label: 'password',
    value,
  }
  if (passwordFieldIndex >= 0) {
    fields[passwordFieldIndex] = {
      ...(fields[passwordFieldIndex] as Record<string, unknown>),
      ...passwordField,
    }
  } else {
    fields.push(passwordField)
  }
  return { ...item, fields }
}

function writeOnePasswordTemplate(dir: string, title: string, value: string, existing?: unknown) {
  const path = join(dir, `${title.replace(/[^A-Za-z0-9_.-]/g, '_')}.json`)
  writeFileSync(path, JSON.stringify(onePasswordTemplate(title, value, existing), null, 2), {
    mode: 0o600,
  })
  chmodSync(path, 0o600)
  return path
}

export function writeSecrets(
  backend: SecretBackend,
  secrets: SecretMap,
  options: SecretBackendOptions,
): SecretWriteResult {
  const keys = Object.keys(secrets).sort()
  if (keys.length === 0) {
    return { backend, target: 'none', writtenKeys: [], command: 'none' }
  }

  if (backend === 'local-env') {
    const target = expandPath(options.localEnvPath || 'org/secrets.local.env')
    mkdirSync(dirname(target), { recursive: true })
    writeFileSync(target, dotenv(secrets), { mode: 0o600 })
    chmodSync(target, 0o600)
    return {
      backend,
      target,
      writtenKeys: keys,
      command: `write ${target}`,
    }
  }

  if (backend === 'kubernetes') {
    const namespace = options.kubernetesNamespace || 'centaur'
    const secretName = options.kubernetesSecretName || 'centaur-infra-env'
    const temp = tempSecretFile(secrets, 'kubernetes-env')
    try {
      const namespaceYaml = run(['kubectl', 'create', 'namespace', namespace, '--dry-run=client', '-o', 'yaml'])
      run(['kubectl', 'apply', '-f', '-'], { input: namespaceYaml })
      const yaml = run([
        'kubectl',
        'create',
        'secret',
        'generic',
        secretName,
        '-n',
        namespace,
        '--from-env-file',
        temp.path,
        '--dry-run=client',
        '-o',
        'yaml',
      ])
      run(['kubectl', 'apply', '-f', '-'], { input: yaml })
    } finally {
      rmSync(temp.dir, { recursive: true, force: true })
    }
    return {
      backend,
      target: `${namespace}/${secretName}`,
      writtenKeys: keys,
      command: `kubectl create secret generic ${secretName} -n ${namespace} --from-env-file <temp> --dry-run=client -o yaml | kubectl apply -f -`,
    }
  }

  if (backend === 'onepassword' || backend === 'onepassword-connect') {
    const vault = options.onePasswordVault || process.env.OP_VAULT
    if (!vault) throw new Error('OP_VAULT or onePasswordVault is required for 1Password backends')
    const temp = mkdtempSync(join(tmpdir(), 'centaur-op-'))
    try {
      for (const [key, value] of Object.entries(secrets)) {
        const existing = spawnSync('op', ['item', 'get', key, '--vault', vault, '--format', 'json'], {
          encoding: 'utf8',
          stdio: ['ignore', 'pipe', 'ignore'],
        })
        const existingItem = existing.status === 0 ? JSON.parse(existing.stdout) : undefined
        const template = writeOnePasswordTemplate(temp, key, value, existingItem)
        if (existing.status === 0) {
          run(['op', 'item', 'edit', key, '--vault', vault, `--template=${template}`])
        } else {
          run(['op', 'item', 'create', '--vault', vault, `--template=${template}`])
        }
      }
    } finally {
      rmSync(temp, { recursive: true, force: true })
    }
    return {
      backend,
      target: `1Password vault ${vault}`,
      writtenKeys: keys,
      command: `op item create/edit <SECRET_NAME> --vault ${shellQuote(vault)} --template <temp-json-file>`,
    }
  }

  if (backend === 'doppler') {
    const temp = tempSecretFile(secrets)
    try {
      run(['doppler', 'secrets', 'upload', temp.path, '--format', 'env', '--yes'])
    } finally {
      rmSync(temp.dir, { recursive: true, force: true })
    }
    return {
      backend,
      target: 'current Doppler project/config',
      writtenKeys: keys,
      command: 'doppler secrets upload <temp-env-file> --format env --yes',
    }
  }

  if (backend === 'vault') {
    const path = options.vaultPath || 'secret/centaur'
    const temp = mkdtempSync(join(tmpdir(), 'centaur-vault-'))
    const file = join(temp, 'secrets.json')
    try {
      writeFileSync(file, JSON.stringify(secrets, null, 2), { mode: 0o600 })
      chmodSync(file, 0o600)
      run(['vault', 'kv', 'put', path, `@${file}`])
    } finally {
      rmSync(temp, { recursive: true, force: true })
    }
    return {
      backend,
      target: path,
      writtenKeys: keys,
      command: `vault kv put ${shellQuote(path)} @<temp-json-file>`,
    }
  }

  const temp = tempSecretFile(secrets)
  const target = expandPath(options.sopsPath || 'org/secrets.sops.env')
  try {
    const encrypted = run([
      'sops',
      '--encrypt',
      '--input-type',
      'dotenv',
      '--output-type',
      'dotenv',
      temp.path,
    ])
    mkdirSync(dirname(target), { recursive: true })
    writeFileSync(target, encrypted, { mode: 0o600 })
    chmodSync(target, 0o600)
  } finally {
    rmSync(temp.dir, { recursive: true, force: true })
  }
  return {
    backend,
    target,
    writtenKeys: keys,
    command: `sops --encrypt --input-type dotenv --output-type dotenv <temp-env-file> > ${target}`,
  }
}

export function defaultSecretTarget(backend: SecretBackend, overlayPath: string) {
  if (backend === 'local-env') return join(expandPath(overlayPath), 'secrets.local.env')
  if (backend === 'sops') return join(expandPath(overlayPath), 'secrets.sops.env')
  if (backend === 'kubernetes') return 'centaur/centaur-infra-env'
  if (backend === 'vault') return 'secret/centaur'
  if (backend === 'doppler') return 'current Doppler project/config'
  return '1Password vault'
}
