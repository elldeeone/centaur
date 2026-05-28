import { spawnSync } from 'node:child_process'
import { randomBytes } from 'node:crypto'
import { createInterface } from 'node:readline/promises'
import { stdin as input, stdout as output } from 'node:process'
import { existsSync, readFileSync } from 'node:fs'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

import { Cli, z } from 'incur'

import {
  AUTH_MODES,
  CLAUDE_CODE_CLIENT_ID,
  HARNESSES,
  INSTALL_MODES,
  OPENAI_CODEX_CLIENT_ID,
  SECRET_BACKENDS,
  VERSION,
  type Harness,
} from './constants.js'
import {
  binaryChecks,
  commandCheck,
  dockerDaemonCheck,
  envChecks,
  overlayChecks,
  type CheckResult,
} from './checks.js'
import { DEFAULT_HOME, emptyState, expandPath, loadState, markDone, saveState } from './state.js'
import {
  SLACK_SCOPES,
  harnessAuthPlan,
  slackManifest,
  writeOverlay,
  writeSlackManifest,
} from './templates.js'
import {
  defaultSecretTarget,
  writeSecrets,
  type SecretBackendOptions,
  type SecretMap,
} from './secrets.js'
import { runAgent } from './run.js'

const authModeSchema = z.enum(AUTH_MODES)
const harnessSchema = z.enum(HARNESSES)
const installModeSchema = z.enum(INSTALL_MODES)
const secretBackendSchema = z.enum(SECRET_BACKENDS)

function allOk(results: CheckResult[]) {
  return results.every(result => result.ok)
}

function setFailedExit(ok: boolean) {
  if (!ok) process.exitCode = 1
}

async function ask(prompt: string, defaultValue: string, nonInteractive: boolean) {
  if (nonInteractive) return defaultValue
  const rl = createInterface({ input, output })
  try {
    const answer = await rl.question(`${prompt} [${defaultValue}]: `)
    return answer.trim() || defaultValue
  } finally {
    rl.close()
  }
}

async function askSecret(
  prompt: string,
  options: { nonInteractive: boolean; defaultValue?: string; required?: boolean } = { nonInteractive: false },
) {
  if (options.nonInteractive) {
    if (options.defaultValue) return options.defaultValue
    if (options.required) throw new Error(`${prompt} is required; run in a TTY or use environment-backed secret collection`)
    return ''
  }
  if (!input.isTTY || !output.isTTY || typeof input.setRawMode !== 'function') {
    throw new Error(`${prompt} requires a TTY for masked input; rerun in an interactive terminal or use --from-env`)
  }
  const suffix = options.defaultValue ? ' [leave blank to use generated/default value]' : ''
  while (true) {
    let answer = ''
    answer = await new Promise<string>((resolve, reject) => {
      let value = ''
      const onData = (chunk: Buffer) => {
        const text = chunk.toString('utf8')
        for (const char of text) {
          if (char === '\u0003') {
            cleanup()
            output.write('\n')
            reject(new Error('interrupted'))
            return
          }
          if (char === '\r' || char === '\n') {
            cleanup()
            output.write('\n')
            resolve(value)
            return
          }
          if (char === '\u007f' || char === '\b') {
            if (value.length > 0) {
              value = value.slice(0, -1)
              output.write('\b \b')
            }
            continue
          }
          value += char
          output.write('*')
        }
      }
      const cleanup = () => {
        input.off('data', onData)
        input.setRawMode(false)
        input.pause()
      }
      output.write(`${prompt}${suffix}: `)
      input.setRawMode(true)
      input.resume()
      input.on('data', onData)
    })
    const value = answer.trim() || options.defaultValue || ''
    if (value || !options.required) return value
    console.log('  Required. Paste a value or press Ctrl-C to stop.')
  }
}

function quotePart(part: string) {
  if (/^[A-Za-z0-9_./:=@+-]+$/.test(part)) return part
  return `'${part.replaceAll("'", "'\\''")}'`
}

function commandLine(parts: string[]) {
  return parts.map(quotePart).join(' ')
}

function repoRoot() {
  return resolve(dirname(fileURLToPath(import.meta.url)), '../../..')
}

export function resolveChartPath(path = 'contrib/chart') {
  if (path.startsWith('/')) return path
  if (existsSync(resolve(process.cwd(), path))) return path
  const fromRepoRoot = resolve(repoRoot(), path)
  return existsSync(fromRepoRoot) ? fromRepoRoot : path
}

function generatedSecret(bytes = 32) {
  return randomBytes(bytes).toString('base64url')
}

const DEFAULT_POSTGRES_PASSWORD = 'tempo_dev_change_me'

function defaultDatabaseUrl(postgresPassword: string) {
  return `postgres://tempo:${encodeURIComponent(postgresPassword)}@centaur-centaur-postgres:5432/ai_v2`
}

function readDotenvFile(path: string) {
  const env: NodeJS.ProcessEnv = {}
  try {
    const text = readFileSync(expandPath(path), 'utf8')
    for (const line of text.split(/\r?\n/)) {
      const trimmed = line.trim()
      if (!trimmed || trimmed.startsWith('#')) continue
      const equals = trimmed.indexOf('=')
      if (equals <= 0) continue
      const key = trimmed.slice(0, equals).trim()
      const rawValue = trimmed.slice(equals + 1).trim()
      try {
        env[key] = rawValue.startsWith('"') ? JSON.parse(rawValue) : rawValue
      } catch {
        env[key] = rawValue
      }
    }
  } catch {
    // Missing local files are reported by the env checks as missing keys.
  }
  return env
}

function commandExists(command: string) {
  return spawnSync('sh', ['-lc', `command -v ${command}`], { stdio: 'ignore' }).status === 0
}

function copyToClipboard(text: string) {
  const candidates: { command: string; args: string[] }[] = [
    { command: 'pbcopy', args: [] },
    { command: 'wl-copy', args: [] },
    { command: 'xclip', args: ['-selection', 'clipboard'] },
    { command: 'xsel', args: ['--clipboard', '--input'] },
    { command: 'clip.exe', args: [] },
  ]
  for (const candidate of candidates) {
    if (!commandExists(candidate.command)) continue
    const proc = spawnSync(candidate.command, candidate.args, { input: text, encoding: 'utf8' })
    if (proc.status === 0) return { ok: true, command: candidate.command }
  }
  return { ok: false, command: '' }
}

function readJson(path: string) {
  return JSON.parse(readFileSync(path, 'utf8')) as Record<string, unknown>
}

function nestedString(value: unknown, path: string[]) {
  let current = value
  for (const key of path) {
    if (!current || typeof current !== 'object' || !(key in current)) return ''
    current = (current as Record<string, unknown>)[key]
  }
  return typeof current === 'string' ? current : ''
}

function refreshTokenFromCredentialJson(value: unknown) {
  return (
    nestedString(value, ['claudeAiOauth', 'refreshToken']) ||
    nestedString(value, ['claudeAiOauth', 'refresh_token']) ||
    nestedString(value, ['refreshToken']) ||
    nestedString(value, ['refresh_token'])
  )
}

function readClaudeRefreshTokenFromKeychain() {
  if (process.platform !== 'darwin' || !commandExists('security')) return ''
  const proc = spawnSync(
    'security',
    ['find-generic-password', '-s', 'Claude Code-credentials', '-w'],
    {
      encoding: 'utf8',
      stdio: ['ignore', 'pipe', 'ignore'],
    },
  )
  if (proc.status !== 0) return ''
  try {
    return refreshTokenFromCredentialJson(JSON.parse(proc.stdout.trim()))
  } catch {
    return ''
  }
}

function readCodexSubscriptionAuth() {
  try {
    const auth = readJson(expandPath('~/.codex/auth.json'))
    return {
      refreshToken: nestedString(auth, ['tokens', 'refresh_token']),
      accountId: nestedString(auth, ['tokens', 'account_id']),
    }
  } catch {
    return { refreshToken: '', accountId: '' }
  }
}

function readClaudeSubscriptionAuth() {
  let refreshToken = ''
  try {
    const auth = readJson(expandPath('~/.claude/.credentials.json'))
    refreshToken = refreshTokenFromCredentialJson(auth)
  } catch {
    // Fall back to keychain below.
  }
  return refreshToken || readClaudeRefreshTokenFromKeychain()
}

export function kindDeploymentCommands(clusterName: string, namespace: string, release: string, values: string) {
  const chartPath = resolveChartPath()
  return [
    ['kind', 'create', 'cluster', '--name', clusterName],
    ['kubectl', 'cluster-info', '--context', `kind-${clusterName}`],
    ['kubectl', 'create', 'namespace', namespace, '--dry-run=client', '-o', 'yaml'],
    ['kubectl', 'apply', '-f', '-'],
    ['helm', 'dependency', 'update', chartPath],
    ['helm', 'upgrade', '--install', release, chartPath, '-n', namespace, '-f', values],
  ]
}

export function k3sDeploymentCommands(namespace: string, release: string, values: string) {
  const chartPath = resolveChartPath()
  return [
    ['kubectl', 'config', 'current-context'],
    ['kubectl', 'create', 'namespace', namespace, '--dry-run=client', '-o', 'yaml'],
    ['kubectl', 'apply', '-f', '-'],
    ['helm', 'dependency', 'update', chartPath],
    ['helm', 'upgrade', '--install', release, chartPath, '-n', namespace, '-f', values],
  ]
}

function deploymentCommandForInstallMode(installMode: string) {
  return installMode === 'local' || installMode === 'k3s' ? 'deploy k3s' : 'deploy k8s'
}

function runCommand(command: string[], inputBytes?: Buffer) {
  const proc = spawnSync(command[0]!, command.slice(1), {
    encoding: 'utf8',
    input: inputBytes,
    stdio: inputBytes ? ['pipe', 'pipe', 'pipe'] : ['ignore', 'pipe', 'pipe'],
  })
  if (proc.status !== 0) {
    throw new Error(`${commandLine(command)} failed: ${(proc.stderr || proc.stdout || '').trim()}`)
  }
  return proc.stdout
}

function runInteractive(command: string[]) {
  const proc = spawnSync(command[0]!, command.slice(1), { stdio: 'inherit' })
  return proc.status === 0
}

async function collectCodexSubscriptionSecrets(promptUser: boolean): Promise<SecretMap> {
  let refreshToken = ''
  let accountId = ''
  if (promptUser) {
    const existing = readCodexSubscriptionAuth()
    refreshToken = existing.refreshToken
    accountId = existing.accountId
    if ((!refreshToken || !accountId) && commandExists('codex')) {
      console.log('  Running codex login now. Complete the browser/device flow, then return here.')
      runInteractive(['codex', 'login'])
      const updated = readCodexSubscriptionAuth()
      refreshToken = updated.refreshToken
      accountId = updated.accountId
    } else if (!refreshToken || !accountId) {
      console.log('  codex is not installed or not on PATH; falling back to manual token prompts.')
    } else {
      console.log('  Found existing Codex ChatGPT login; using ~/.codex/auth.json.')
    }
  }
  refreshToken = refreshToken || (await askSecret('OPENAI_CODEX refresh token', { nonInteractive: !promptUser, required: true }))
  accountId = accountId || (await askSecret('OPENAI_CODEX account id', { nonInteractive: !promptUser, required: true }))
  return {
    OPENAI_CODEX_CLIENT_ID,
    OPENAI_CODEX_BLOB: JSON.stringify({ refresh_token: refreshToken }),
    OPENAI_CODEX_ACCOUNT_ID: accountId,
  }
}

async function collectClaudeSubscriptionSecrets(promptUser: boolean): Promise<SecretMap> {
  let refreshToken = ''
  if (promptUser) {
    refreshToken = readClaudeSubscriptionAuth()
    if (!refreshToken && commandExists('claude')) {
      console.log('  Running claude login now. Complete the browser/device flow, then return here.')
      runInteractive(['claude', 'login'])
      refreshToken = readClaudeSubscriptionAuth()
    } else if (!refreshToken) {
      console.log('  claude is not installed or not on PATH; falling back to manual token prompts.')
    } else {
      console.log('  Found existing Claude Code login; using local credentials.')
    }
  }
  refreshToken = refreshToken || (await askSecret('CLAUDE_CODE refresh token', { nonInteractive: !promptUser, required: true }))
  return {
    CLAUDE_CODE_CLIENT_ID,
    CLAUDE_CODE_BLOB: JSON.stringify({ refresh_token: refreshToken }),
  }
}

async function collectWizardSecrets(state: {
  installMode: string
  harness: Harness
  authMode: string
}, promptUser: boolean) {
  const secrets: SecretMap = {}
  secrets.SLACK_BOT_TOKEN = await askSecret('SLACK_BOT_TOKEN', {
    nonInteractive: !promptUser,
    required: true,
  })
  secrets.SLACK_SIGNING_SECRET = await askSecret('SLACK_SIGNING_SECRET', {
    nonInteractive: !promptUser,
    required: true,
  })
  if (state.installMode === 'local') {
    secrets.SLACK_APP_TOKEN = await askSecret('SLACK_APP_TOKEN', {
      nonInteractive: !promptUser,
      required: true,
    })
  }
  if (state.harness === 'codex' && state.authMode === 'api_key') {
    secrets.OPENAI_API_KEY = await askSecret('OPENAI_API_KEY', {
      nonInteractive: !promptUser,
      required: true,
    })
  } else if (state.harness === 'codex') {
    Object.assign(secrets, await collectCodexSubscriptionSecrets(promptUser))
  } else if (state.authMode === 'api_key') {
    secrets.ANTHROPIC_API_KEY = await askSecret('ANTHROPIC_API_KEY', {
      nonInteractive: !promptUser,
      required: true,
    })
  } else {
    Object.assign(secrets, await collectClaudeSubscriptionSecrets(promptUser))
  }
  secrets.POSTGRES_PASSWORD = await askSecret('POSTGRES_PASSWORD', {
    nonInteractive: !promptUser,
    defaultValue: DEFAULT_POSTGRES_PASSWORD,
    required: true,
  })
  secrets.DATABASE_URL = await askSecret('DATABASE_URL', {
    nonInteractive: !promptUser,
    defaultValue: defaultDatabaseUrl(secrets.POSTGRES_PASSWORD),
    required: true,
  })
  secrets.IRON_MANAGEMENT_API_KEY = await askSecret('IRON_MANAGEMENT_API_KEY', {
    nonInteractive: !promptUser,
    defaultValue: generatedSecret(),
    required: true,
  })
  secrets.SANDBOX_SIGNING_KEY = await askSecret('SANDBOX_SIGNING_KEY', {
    nonInteractive: !promptUser,
    defaultValue: generatedSecret(),
    required: true,
  })
  secrets.SLACKBOT_API_KEY = await askSecret('SLACKBOT_API_KEY', {
    nonInteractive: !promptUser,
    defaultValue: generatedSecret(),
    required: true,
  })
  return secrets
}

function requireEnv(name: string) {
  const value = process.env[name]
  if (!value) throw new Error(`${name} is required in the environment`)
  return value
}

function collectSecretsFromEnv(state: {
  installMode: string
  harness: Harness
  authMode: string
}) {
  const secrets: SecretMap = {
    SLACK_BOT_TOKEN: requireEnv('SLACK_BOT_TOKEN'),
    SLACK_SIGNING_SECRET: requireEnv('SLACK_SIGNING_SECRET'),
    POSTGRES_PASSWORD: process.env.POSTGRES_PASSWORD || DEFAULT_POSTGRES_PASSWORD,
    DATABASE_URL:
      process.env.DATABASE_URL ||
      defaultDatabaseUrl(process.env.POSTGRES_PASSWORD || DEFAULT_POSTGRES_PASSWORD),
    IRON_MANAGEMENT_API_KEY: process.env.IRON_MANAGEMENT_API_KEY || generatedSecret(),
    SANDBOX_SIGNING_KEY: process.env.SANDBOX_SIGNING_KEY || generatedSecret(),
    SLACKBOT_API_KEY: process.env.SLACKBOT_API_KEY || generatedSecret(),
  }
  if (state.installMode === 'local') secrets.SLACK_APP_TOKEN = requireEnv('SLACK_APP_TOKEN')
  if (state.harness === 'codex' && state.authMode === 'api_key') {
    secrets.OPENAI_API_KEY = requireEnv('OPENAI_API_KEY')
  } else if (state.harness === 'codex') {
    secrets.OPENAI_CODEX_CLIENT_ID = process.env.OPENAI_CODEX_CLIENT_ID || OPENAI_CODEX_CLIENT_ID
    secrets.OPENAI_CODEX_BLOB = requireEnv('OPENAI_CODEX_BLOB')
    secrets.OPENAI_CODEX_ACCOUNT_ID = requireEnv('OPENAI_CODEX_ACCOUNT_ID')
  } else if (state.authMode === 'api_key') {
    secrets.ANTHROPIC_API_KEY = requireEnv('ANTHROPIC_API_KEY')
  } else {
    secrets.CLAUDE_CODE_CLIENT_ID = process.env.CLAUDE_CODE_CLIENT_ID || CLAUDE_CODE_CLIENT_ID
    secrets.CLAUDE_CODE_BLOB = requireEnv('CLAUDE_CODE_BLOB')
  }
  return secrets
}

async function collectBackendOptions(
  backend: string,
  overlayPath: string,
  promptUser: boolean,
): Promise<SecretBackendOptions> {
  if (backend === 'local-env') {
    return {
      localEnvPath: await ask(
        'Local secrets file',
        defaultSecretTarget('local-env', overlayPath),
        !promptUser,
      ),
    }
  }
  if (backend === 'sops') {
    return {
      sopsPath: await ask('SOPS encrypted secrets file', defaultSecretTarget('sops', overlayPath), !promptUser),
    }
  }
  if (backend === 'kubernetes') {
    return {
      kubernetesNamespace: await ask('Kubernetes namespace for the Secret', 'centaur', !promptUser),
      kubernetesSecretName: await ask('Kubernetes Secret name', 'centaur-infra-env', !promptUser),
    }
  }
  if (backend === 'onepassword' || backend === 'onepassword-connect') {
    return {
      onePasswordVault: await ask('1Password vault name or id', process.env.OP_VAULT || 'centaur', !promptUser),
    }
  }
  if (backend === 'vault') {
    return {
      vaultPath: await ask('Vault KV path', defaultSecretTarget('vault', overlayPath), !promptUser),
    }
  }
  return {}
}

const overlay = Cli.create('overlay', {
  description: 'Create and validate Centaur overlays',
})
  .command('init', {
    description: 'Scaffold a Centaur overlay repo.',
    options: z.object({
      path: z.string().default('org').describe('Overlay directory'),
      org: z.string().default('acme').describe('Organization name'),
      assistantName: z.string().default('centaur').describe('Assistant name'),
      domain: z.string().default('centaur.example.com').describe('Deployment domain'),
      harness: harnessSchema.default('codex').describe('Default harness'),
      authMode: authModeSchema.default('api_key').describe('Auth mode for the selected harness'),
      socketMode: z.boolean().default(false).describe('Generate Slack manifest for Socket Mode'),
    }),
    run(c) {
      const written = writeOverlay({
        path: c.options.path,
        org: c.options.org,
        assistantName: c.options.assistantName,
        domain: c.options.domain,
        harness: c.options.harness,
        authMode: c.options.authMode,
      })
      const manifestPath = writeSlackManifest(
        join(expandPath(c.options.path), 'slack-app-manifest.json'),
        c.options.assistantName,
        c.options.domain,
        c.options.socketMode,
      )
      const auth = harnessAuthPlan(c.options.harness, c.options.authMode)
      return {
        overlayPath: expandPath(c.options.path),
        created: [...written, manifestPath],
        auth,
      }
    },
  })
  .command('validate', {
    description: 'Validate required overlay files.',
    options: z.object({
      path: z.string().default('org').describe('Overlay directory'),
    }),
    run(c) {
      const results = overlayChecks(c.options.path)
      const ok = allOk(results)
      setFailedExit(ok)
      return { ok, results }
    },
  })

const integrations = Cli.create('integrations', {
  description: 'Generate and verify integration setup',
})
  .command('slack-manifest', {
    description: 'Generate the Slack app manifest with scopes, events, commands, and interactivity.',
    options: z.object({
      domain: z.string().default('centaur.example.com').describe('Public Centaur domain'),
      appName: z.string().default('centaur').describe('Slack app name'),
      socketMode: z.boolean().default(false).describe('Use Socket Mode instead of public request URLs'),
      output: z.string().optional().describe('Write manifest to a file'),
      copy: z.boolean().default(false).describe('Copy manifest JSON to the system clipboard'),
      backend: secretBackendSchema.default('local-env').describe('Secret backend for the next secrets step'),
      installMode: installModeSchema.default('local').describe('Install mode for the next secrets step'),
      harness: harnessSchema.default('codex').describe('Selected default harness'),
      authMode: authModeSchema.default('api_key').describe('Auth mode for the selected harness'),
      overlayPath: z.string().default('org').describe('Overlay path for the next secrets step'),
    }),
    run(c) {
      const manifest = slackManifest(c.options.appName, c.options.domain, c.options.socketMode)
      const outputPath = c.options.output
        ? writeSlackManifest(c.options.output, c.options.appName, c.options.domain, c.options.socketMode)
        : undefined
      const manifestJson = `${JSON.stringify(manifest, null, 2)}\n`
      const clipboard = c.options.copy ? copyToClipboard(manifestJson) : undefined
      return c.ok(
        {
          manifest,
          outputPath,
          copied: clipboard?.ok ?? false,
          clipboardCommand: clipboard?.command || undefined,
          requiredBotScopes: [...SLACK_SCOPES],
          requiredSecrets: ['SLACK_BOT_TOKEN', 'SLACK_SIGNING_SECRET'],
          optionalSecrets: ['SLACK_APP_TOKEN'],
          userStep: clipboard?.ok
            ? 'Open https://api.slack.com/apps, create an app from manifest, alt-tab, and paste.'
            : 'Open https://api.slack.com/apps and paste the returned manifest JSON or the output file contents.',
          nextCommand: commandLine([
            'secrets',
            'collect',
            '--backend',
            c.options.backend,
            '--install-mode',
            c.options.installMode,
            '--harness',
            c.options.harness,
            '--auth-mode',
            c.options.authMode,
            '--overlay-path',
            c.options.overlayPath,
          ]),
        },
        {
          cta: {
            description: 'After installing the Slack app:',
            commands: [
              {
                command: commandLine([
                  'secrets',
                  'collect',
                  '--backend',
                  c.options.backend,
                  '--install-mode',
                  c.options.installMode,
                  '--harness',
                  c.options.harness,
                  '--auth-mode',
                  c.options.authMode,
                  '--overlay-path',
                  c.options.overlayPath,
                ]),
                description: 'prompt for Slack, harness, and infra secrets and write them to the backend',
              },
            ],
          },
        },
      )
    },
  })
  .command('harness-auth', {
    description: 'Show api_key vs subscription access_token setup for one selected harness.',
    options: z.object({
      harness: harnessSchema.default('codex').describe('Harness to configure'),
      authMode: authModeSchema.default('api_key').describe('Auth mode for the selected harness'),
    }),
    run(c) {
      return harnessAuthPlan(c.options.harness, c.options.authMode)
    },
  })
  .command('setup', {
    description: 'Return the agent-driven setup command chain for Slack, secrets, validation, and deploy.',
    options: z.object({
      org: z.string().default('acme').describe('Organization name'),
      assistantName: z.string().default('centaur').describe('Assistant display name'),
      domain: z.string().default('centaur.example.com').describe('Public deployment domain'),
      installMode: installModeSchema.default('local').describe('local, k3s, k8s, or ssh'),
      backend: secretBackendSchema.default('local-env').describe('Secret backend'),
      harness: harnessSchema.default('codex').describe('Selected default harness'),
      authMode: authModeSchema.default('api_key').describe('Auth mode for the selected harness'),
      overlayPath: z.string().default('org').describe('Overlay directory'),
    }),
    run(c) {
      const manifestPath = join(c.options.overlayPath, 'slack-app-manifest.json')
      const deployCommand = deploymentCommandForInstallMode(c.options.installMode)
      return {
        commands: [
          commandLine([
            'init',
            '--org',
            c.options.org,
            '--assistant-name',
            c.options.assistantName,
            '--domain',
            c.options.domain,
            '--install-mode',
            c.options.installMode,
            '--secret-backend',
            c.options.backend,
            '--harness',
            c.options.harness,
            '--auth-mode',
            c.options.authMode,
            '--overlay-path',
            c.options.overlayPath,
          ]),
          commandLine([
            'integrations',
            'slack-manifest',
            '--domain',
            c.options.domain,
            '--app-name',
            c.options.assistantName,
            '--output',
            manifestPath,
            '--copy',
            '--backend',
            c.options.backend,
            '--install-mode',
            c.options.installMode,
            '--harness',
            c.options.harness,
            '--auth-mode',
            c.options.authMode,
            '--overlay-path',
            c.options.overlayPath,
          ]),
          commandLine([
            'secrets',
            'collect',
            '--backend',
            c.options.backend,
            '--install-mode',
            c.options.installMode,
            '--harness',
            c.options.harness,
            '--auth-mode',
            c.options.authMode,
            '--overlay-path',
            c.options.overlayPath,
          ]),
          commandLine([
            'doctor',
            '--deep',
            '--overlay-path',
            c.options.overlayPath,
            '--harness',
            c.options.harness,
            '--auth-mode',
            c.options.authMode,
            '--secret-backend',
            c.options.backend,
            '--install-mode',
            c.options.installMode,
          ]),
          deployCommand,
        ],
        harness: c.options.harness,
        authMode: c.options.authMode,
        note: 'Use exactly one default harness for the deployment: codex or claude-code.',
      }
    },
  })

const secrets = Cli.create('secrets', {
  description: 'Populate and validate secret backend setup',
}).command('doctor', {
  description: 'Validate the selected secret backend enough to continue onboarding.',
  options: z.object({
    backend: secretBackendSchema.default('local-env').describe('Secret backend'),
    harness: harnessSchema.default('codex').describe('Selected default harness'),
    authMode: authModeSchema.default('api_key').describe('Auth mode for the selected harness'),
    overlayPath: z.string().default('org').describe('Overlay path for local generated files'),
    localEnvPath: z.string().optional().describe('local-env source file'),
  }),
  run(c) {
    let results: CheckResult[]
    if (c.options.backend === 'onepassword') {
      results = [commandCheck('1password:op', ['op', 'vault', 'list'], 'Set OP_SERVICE_ACCOUNT_TOKEN and run op vault list.')]
    } else if (c.options.backend === 'onepassword-connect') {
      results = [
        {
          name: 'env:OP_CONNECT_TOKEN',
          ok: Boolean(process.env.OP_CONNECT_TOKEN),
          detail: process.env.OP_CONNECT_TOKEN ? 'set' : 'missing',
          repair: 'Set OP_CONNECT_TOKEN for 1Password Connect.',
        },
        {
          name: 'env:OP_VAULT',
          ok: Boolean(process.env.OP_VAULT),
          detail: process.env.OP_VAULT ? 'set' : 'missing',
          repair: 'Set OP_VAULT to the vault name or id.',
        },
      ]
    } else if (c.options.backend === 'sops') {
      results = [
        commandCheck('sops:version', ['sops', '--version'], 'Install sops.'),
        commandCheck('age:version', ['age', '--version'], 'Install age and generate a key.'),
      ]
    } else if (c.options.backend === 'kubernetes') {
      results = [
        commandCheck('kubectl:secrets', ['kubectl', 'get', 'secret', '-A'], 'Create Kubernetes secrets or configure cluster access.'),
      ]
    } else {
      const env =
        c.options.backend === 'local-env'
          ? {
              ...process.env,
              ...readDotenvFile(c.options.localEnvPath || defaultSecretTarget('local-env', c.options.overlayPath)),
            }
          : process.env
      results = envChecks(env, { harness: c.options.harness, authMode: c.options.authMode })
    }
    const ok = allOk(results)
    setFailedExit(ok)
    return { ok, results }
  },
}).command('collect', {
  description: 'Collect required setup secrets and write them into the selected backend.',
  options: z.object({
    backend: secretBackendSchema.default('local-env').describe('Secret backend to populate'),
    installMode: installModeSchema.default('local').describe('local, k3s, k8s, or ssh'),
    harness: harnessSchema.default('codex').describe('Selected default harness'),
    authMode: authModeSchema.default('api_key').describe('Auth mode for the selected harness'),
    overlayPath: z.string().default('org').describe('Overlay path for local generated files'),
    fromEnv: z.boolean().default(false).describe('Read required secret values from environment variables'),
    localEnvPath: z.string().optional().describe('local-env target file'),
    kubernetesNamespace: z.string().optional().describe('Kubernetes namespace for secret writes'),
    kubernetesSecretName: z.string().optional().describe('Kubernetes Secret name'),
    onePasswordVault: z.string().optional().describe('1Password vault name or id'),
    sopsPath: z.string().optional().describe('SOPS encrypted dotenv target file'),
    vaultPath: z.string().optional().describe('Vault KV path'),
  }),
  async run(c) {
    const state = {
      installMode: c.options.installMode,
      harness: c.options.harness,
      authMode: c.options.authMode,
    }
    const promptUser = !c.options.fromEnv
    if (promptUser && (!input.isTTY || !output.isTTY || typeof input.setRawMode !== 'function')) {
      return c.error({
        code: 'TTY_REQUIRED',
        message: 'secrets collect needs an interactive terminal so secret prompts can be masked',
        retryable: true,
        cta: {
          description: 'Run one of these:',
          commands: [
            {
              command: commandLine([
                'secrets',
                'collect',
                '--backend',
                c.options.backend,
                '--install-mode',
                c.options.installMode,
                '--harness',
                c.options.harness,
                '--auth-mode',
                c.options.authMode,
                '--overlay-path',
                c.options.overlayPath,
              ]),
              description: 'run in a real terminal and type secrets into masked prompts',
            },
            {
              command: commandLine([
                'secrets',
                'collect',
                '--backend',
                c.options.backend,
                '--install-mode',
                c.options.installMode,
                '--harness',
                c.options.harness,
                '--auth-mode',
                c.options.authMode,
                '--overlay-path',
                c.options.overlayPath,
                '--from-env',
              ]),
              description: 'let an agent populate required environment variables first',
            },
          ],
        },
      })
    }
    const secrets = c.options.fromEnv ? collectSecretsFromEnv(state) : await collectWizardSecrets(state, promptUser)
    const promptedBackendOptions = c.options.fromEnv
      ? {}
      : await collectBackendOptions(c.options.backend, c.options.overlayPath, true)
    const backendOptions = {
      localEnvPath:
        c.options.localEnvPath ||
        promptedBackendOptions.localEnvPath ||
        defaultSecretTarget('local-env', c.options.overlayPath),
      kubernetesNamespace:
        c.options.kubernetesNamespace || promptedBackendOptions.kubernetesNamespace,
      kubernetesSecretName:
        c.options.kubernetesSecretName || promptedBackendOptions.kubernetesSecretName,
      onePasswordVault:
        c.options.onePasswordVault || promptedBackendOptions.onePasswordVault,
      sopsPath:
        c.options.sopsPath ||
        promptedBackendOptions.sopsPath ||
        defaultSecretTarget('sops', c.options.overlayPath),
      vaultPath: c.options.vaultPath || promptedBackendOptions.vaultPath,
    }
    const result = writeSecrets(c.options.backend, secrets, backendOptions)
    const nextDeployCommand = deploymentCommandForInstallMode(c.options.installMode)
    const doctorCommand = [
      'doctor',
      '--deep',
      '--overlay-path',
      c.options.overlayPath,
      '--harness',
      c.options.harness,
      '--auth-mode',
      c.options.authMode,
      '--secret-backend',
      c.options.backend,
      '--install-mode',
      c.options.installMode,
    ]
    if (c.options.backend === 'local-env') {
      doctorCommand.push('--local-env-path', backendOptions.localEnvPath!)
    }
    return c.ok(
      {
        backend: result.backend,
        target: result.target,
        writtenKeys: result.writtenKeys,
        command: result.command,
      },
      {
        cta: {
          description: 'Next validation commands:',
          commands: [
            {
              command: commandLine(doctorCommand),
              description: 'verify prerequisites and generated setup files',
            },
            {
              command: nextDeployCommand,
              description: 'print or run the deployment plan',
            },
          ],
        },
      },
    )
  },
})

const deploy = Cli.create('deploy', {
  description: 'Prepare Centaur deployments',
})
  .command('k8s', {
    description: 'Print the existing-cluster deployment command.',
    options: z.object({
      namespace: z.string().default('centaur'),
      release: z.string().default('centaur'),
      values: z.string().default('org/values.centaur.yaml'),
    }),
    run(c) {
      const chartPath = resolveChartPath()
      return {
        commands: [
          `kubectl create namespace ${quotePart(c.options.namespace)} --dry-run=client -o yaml | kubectl apply -f -`,
          commandLine(['helm', 'dependency', 'update', chartPath]),
          commandLine([
            'helm',
            'upgrade',
            '--install',
            c.options.release,
            chartPath,
            '-n',
            c.options.namespace,
            '-f',
            c.options.values,
          ]),
        ],
      }
    },
  })
  .command('k3s', {
    description: 'Deploy Centaur into the current local k3s-compatible Kubernetes context.',
    options: z.object({
      namespace: z.string().default('centaur'),
      release: z.string().default('centaur'),
      values: z.string().default('org/values.centaur.yaml').describe('Helm values file'),
      apply: z.boolean().default(false).describe('Run the commands instead of printing them'),
    }),
    run(c) {
      const commands = k3sDeploymentCommands(c.options.namespace, c.options.release, c.options.values)
      if (c.options.apply) {
        runCommand(commands[0]!)
        const namespaceYaml = Buffer.from(runCommand(commands[1]!))
        runCommand(commands[2]!, namespaceYaml)
        for (const command of commands.slice(3)) runCommand(command)
      }
      return {
        applied: c.options.apply,
        commands: [
          commandLine(commands[0]!),
          `${commandLine(commands[1]!)} | ${commandLine(commands[2]!)}`,
          commandLine(commands[3]!),
          commandLine(commands[4]!),
        ],
      }
    },
  })
  .command('kind', {
    description: 'Create a local kind cluster and deploy Centaur into it.',
    options: z.object({
      clusterName: z.string().default('centaur').describe('kind cluster name'),
      namespace: z.string().default('centaur'),
      release: z.string().default('centaur'),
      values: z.string().default('org/values.centaur.yaml').describe('Helm values file'),
      apply: z.boolean().default(false).describe('Run the commands instead of printing them'),
    }),
    run(c) {
      const commands = kindDeploymentCommands(c.options.clusterName, c.options.namespace, c.options.release, c.options.values)
      if (c.options.apply) {
        runCommand(commands[0]!)
        runCommand(commands[1]!)
        const namespaceYaml = Buffer.from(runCommand(commands[2]!))
        runCommand(commands[3]!, namespaceYaml)
        for (const command of commands.slice(4)) runCommand(command)
      }
      return {
        applied: c.options.apply,
        commands: [
          commandLine(commands[0]!),
          commandLine(commands[1]!),
          `${commandLine(commands[2]!)} | ${commandLine(commands[3]!)}`,
          commandLine(commands[4]!),
          commandLine(commands[5]!),
        ],
      }
    },
  })
  .command('ssh', {
    description: 'Print the SSH/k3s bootstrap plan for a new server.',
    args: z.object({
      host: z.string().describe('SSH host'),
    }),
    options: z.object({
      domain: z.string().describe('Public domain for this host'),
    }),
    run(c) {
      return {
        host: c.args.host,
        plan: [
          `ssh into ${c.args.host} and install k3s`,
          'copy kubeconfig locally',
          'install ingress-nginx, cert-manager, and ArgoCD',
          `point DNS for ${c.options.domain} at the host`,
          'run centaur deploy k8s once the kube context works',
        ],
      }
    },
  })

export const app = Cli.create('centaur', {
  description: 'Centaur onboarding, deployment, and agent operations CLI',
  version: VERSION,
  mcp: {},
})
  .command('run', {
    description: 'Run one Centaur agent turn and pipe API events.',
    args: z.object({
      prompt: z.string().describe('Prompt to send to the Centaur agent.'),
    }),
    options: z.object({
      thread: z.string().optional().describe('Thread key to reuse or create'),
      harness: z.string().optional().describe('Harness to run, for example codex, amp, or claude-code'),
      engine: z.string().optional().describe('Optional harness engine/model override'),
      persona: z.string().optional().describe('Optional Centaur persona id'),
      apiUrl: z
        .string()
        .optional()
        .describe('Centaur API URL. Defaults to CENTAUR_API_URL or http://127.0.0.1:8000.'),
      apiKey: z.string().optional().describe('Centaur API key. Defaults to CENTAUR_API_KEY.'),
      noStream: z.boolean().default(false).describe('Skip SSE streaming and poll final state only'),
      pollMs: z.number().int().positive().optional().describe('Server stream polling interval in milliseconds'),
    }),
    env: z.object({
      CENTAUR_API_URL: z.string().optional().describe('Default Centaur API URL'),
      CENTAUR_API_KEY: z.string().optional().describe('Centaur API key'),
    }),
    async *run(c) {
      const apiUrl = c.options.apiUrl || c.env.CENTAUR_API_URL || 'http://127.0.0.1:8000'
      const apiKey = c.options.apiKey || c.env.CENTAUR_API_KEY
      if (!apiKey) {
        return c.error({
          code: 'MISSING_API_KEY',
          message: 'Set CENTAUR_API_KEY or pass --api-key.',
          retryable: true,
          cta: {
            commands: [
              {
                command: 'export CENTAUR_API_KEY=<api-key>',
                description: 'set the API key for subsequent CLI runs',
              },
            ],
          },
        })
      }

      const stream = runAgent({
        apiUrl,
        apiKey,
        prompt: c.args.prompt,
        threadKey: c.options.thread,
        harness: c.options.harness,
        engine: c.options.engine,
        personaId: c.options.persona,
        stream: !c.options.noStream,
        pollMs: c.options.pollMs,
      })

      let next = await stream.next()
      while (!next.done) {
        yield next.value
        next = await stream.next()
      }

      return c.ok(next.value, {
        cta: {
          commands: [
            {
              command: commandLine(['run', c.args.prompt, '--thread', next.value.threadKey]),
              description: 'continue this same Centaur thread',
            },
          ],
        },
      })
    },
  })
  .command('init', {
    description: 'Start a Centaur setup run by scaffolding files and returning next-step CTAs.',
    options: z.object({
      org: z.string().default('acme').describe('Organization name'),
      assistantName: z.string().default('centaur').describe('Assistant display name'),
      domain: z.string().default('centaur.example.com').describe('Public deployment domain'),
      adminEmail: z.string().default('admin@example.com').describe('Admin email'),
      installMode: installModeSchema.default('local').describe('local, k3s, k8s, or ssh'),
      secretBackend: secretBackendSchema.default('local-env').describe('Secret backend'),
      harness: harnessSchema.default('codex').describe('Default harness: codex or claude-code'),
      authMode: authModeSchema.default('api_key').describe('Auth mode for the selected harness'),
      overlayPath: z.string().default('org').describe('Overlay directory to create or validate'),
      home: z.string().default(DEFAULT_HOME).describe('Centaur config directory'),
      resume: z.boolean().default(false).describe('Resume from existing onboarding state'),
      nonInteractive: z.boolean().default(false).describe('Use provided/default values without prompts'),
    }),
    run(c) {
      const prior = c.options.resume ? loadState(c.options.home) : emptyState()
      const state = { ...prior }
      state.org = c.options.org || prior.org || 'acme'
      state.assistantName = c.options.assistantName || prior.assistantName || 'centaur'
      state.domain = c.options.domain || prior.domain || 'centaur.example.com'
      state.adminEmail = c.options.adminEmail || prior.adminEmail || 'admin@example.com'
      state.installMode = c.options.installMode || prior.installMode || 'local'
      state.secretBackend = c.options.secretBackend || prior.secretBackend || 'local-env'
      state.harness = c.options.harness || prior.harness || 'codex'
      state.authMode = c.options.authMode || prior.authMode || 'api_key'
      state.overlayPath = c.options.overlayPath

      const manifestPath = join(expandPath(state.overlayPath), 'slack-app-manifest.json')
      const manifestExisted = existsSync(manifestPath)
      const auth = harnessAuthPlan(state.harness, state.authMode)
      const written = writeOverlay({
        path: state.overlayPath,
        org: state.org,
        assistantName: state.assistantName,
        domain: state.domain,
        harness: state.harness,
        authMode: state.authMode,
      })
      writeSlackManifest(manifestPath, state.assistantName, state.domain, state.installMode === 'local')
      for (const step of ['local-state', 'overlay', 'slack-manifest', 'secrets-plan', 'deployment-plan']) {
        markDone(state, step)
      }
      state.data = { ...state.data, auth }
      const saved = saveState(state, c.options.home)
      const nextDeployCommand = `centaur ${deploymentCommandForInstallMode(state.installMode)}`

      return c.ok(
        {
          statePath: saved.statePath,
          configPath: saved.configPath,
          overlayPath: expandPath(state.overlayPath),
          manifestPath,
          harness: state.harness,
          authMode: state.authMode,
          secretBackend: state.secretBackend,
          installMode: state.installMode,
          created: manifestExisted ? written : [...written, manifestPath],
          auth,
        },
        {
          cta: {
            description: 'Next setup commands:',
            commands: [
              {
                command: commandLine([
                  'integrations',
                  'slack-manifest',
                  '--domain',
                  state.domain,
                  '--app-name',
                  state.assistantName,
                  '--output',
                  manifestPath,
                  '--copy',
                  '--backend',
                  state.secretBackend,
                  '--install-mode',
                  state.installMode,
                  '--harness',
                  state.harness,
                  '--auth-mode',
                  state.authMode,
                  '--overlay-path',
                  state.overlayPath,
                ]),
                description: 'copy the Slack app manifest JSON to the clipboard',
              },
              {
                command: commandLine([
                  'secrets',
                  'collect',
                  '--backend',
                  state.secretBackend,
                  '--install-mode',
                  state.installMode,
                  '--harness',
                  state.harness,
                  '--auth-mode',
                  state.authMode,
                  '--overlay-path',
                  state.overlayPath,
                ]),
                description: 'prompt for needed secrets and populate the selected backend',
              },
              {
                command: commandLine([
                  'doctor',
                  '--deep',
                  '--overlay-path',
                  state.overlayPath,
                  '--harness',
                  state.harness,
                  '--auth-mode',
                  state.authMode,
                  '--secret-backend',
                  state.secretBackend,
                  '--install-mode',
                  state.installMode,
                ]),
                description: 'verify local tools and generated files after secrets are populated',
              },
              {
                command: nextDeployCommand,
                description: 'print or run the deployment plan',
              },
            ],
          },
        },
      )
    },
  })
  .command('doctor', {
    description: 'Check local prerequisites and generated Centaur setup files.',
    options: z.object({
      deep: z.boolean().default(false).describe('Include deploy and environment checks'),
      overlayPath: z.string().default('org').describe('Overlay path'),
      harness: harnessSchema.default('codex').describe('Selected default harness'),
      authMode: authModeSchema.default('api_key').describe('Auth mode for the selected harness'),
      secretBackend: secretBackendSchema.default('local-env').describe('Secret backend for repair CTAs'),
      installMode: installModeSchema.default('local').describe('Install mode for repair CTAs'),
      localEnvPath: z.string().optional().describe('local-env source file for deep checks'),
    }),
    run(c) {
      const results = [...binaryChecks({ includeDeploy: c.options.deep }), ...overlayChecks(c.options.overlayPath)]
      if (c.options.deep) {
        const env =
          c.options.secretBackend === 'local-env'
            ? {
                ...process.env,
                ...readDotenvFile(c.options.localEnvPath || defaultSecretTarget('local-env', c.options.overlayPath)),
              }
            : process.env
        results.push(
          ...envChecks(env, {
            harness: c.options.harness,
            authMode: c.options.authMode,
          }),
          dockerDaemonCheck(),
        )
        if (results.some(result => result.name === 'binary:kubectl' && result.ok)) {
          results.push(commandCheck('kubectl:cluster', ['kubectl', 'cluster-info'], 'Select a working Kubernetes context or use centaur deploy k3s.'))
        }
        if (results.some(result => result.name === 'binary:helm' && result.ok)) {
          results.push(commandCheck('helm:version', ['helm', 'version', '--short'], 'Install Helm before deploying to Kubernetes.'))
        }
      }
      const ok = allOk(results)
      setFailedExit(ok)
      return c.ok(
        { ok, results },
        {
          cta: {
            commands: [
              {
                command: commandLine([
                  'secrets',
                  'collect',
                  '--backend',
                  c.options.secretBackend,
                  '--install-mode',
                  c.options.installMode,
                  '--harness',
                  c.options.harness,
                  '--auth-mode',
                  c.options.authMode,
                  '--overlay-path',
                  c.options.overlayPath,
                ]),
                description: 'populate missing Slack, harness, and infra secrets',
              },
              {
                command: 'deploy k3s',
                description: 'for local development, print the k3s deployment plan',
              },
              {
                command: 'deploy k8s',
                description: 'for an existing cluster, print the Helm deployment plan',
              },
            ],
          },
        },
      )
    },
  })
  .command('status', {
    description: 'Show resumable onboarding state.',
    options: z.object({
      home: z.string().default(DEFAULT_HOME).describe('Centaur config directory'),
    }),
    run(c) {
      return loadState(c.options.home)
    },
  })
  .command('smoke-test', {
    description: 'Print the exact commands for an end-to-end Centaur smoke test.',
    options: z.object({
      namespace: z.string().default('centaur'),
      release: z.string().default('centaur'),
    }),
    run(c) {
      return {
        command: `just namespace=${quotePart(c.options.namespace)} release=${quotePart(c.options.release)} smoke`,
      }
    },
  })
  .command(overlay)
  .command(integrations)
  .command(secrets)
  .command(deploy)
  .command('logs', {
    description: 'Print the kubectl log command for a Centaur component.',
    options: z.object({
      component: z.string().default('api'),
      namespace: z.string().default('centaur'),
      release: z.string().default('centaur'),
    }),
    run(c) {
      return {
        command: commandLine([
          'kubectl',
          'logs',
          '-n',
          c.options.namespace,
          `deploy/${c.options.release}-centaur-${c.options.component}`,
          '--tail=200',
          '-f',
        ]),
      }
    },
  })
  .command('repair', {
    description: 'Print focused repair instructions for one onboarding area.',
    args: z.object({
      step: z.string().describe('Repair area: slack, github, secrets, deploy, codex, or claude'),
    }),
    run(c) {
      const repairs: Record<string, string> = {
        slack:
          'Regenerate the manifest, update Slack request URLs, reinstall the app, then send a test mention.',
        github:
          'Check GitHub App permissions, installation id, private key formatting, webhook delivery status, or GITHUB_TOKEN scope.',
        secrets:
          'Run centaur secrets doctor --backend <backend> and sync missing keys into the selected backend.',
        deploy:
          'Run centaur doctor --deep, fix cluster/helm failures, then rerun centaur deploy k8s.',
        codex:
          'For CODEX_AUTH_MODE=access_token, run centaur secrets collect --harness codex --auth-mode access_token with a dedicated ChatGPT account.',
        claude:
          'For CLAUDE_CODE_AUTH_MODE=access_token, run centaur secrets collect --harness claude-code --auth-mode access_token with a dedicated Claude.ai Pro or Max account.',
      }
      return {
        step: c.args.step,
        repair: repairs[c.args.step] ?? 'Known repair steps: slack, github, secrets, deploy, codex, claude.',
      }
    },
  })
