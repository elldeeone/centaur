import { existsSync } from 'node:fs'
import { join } from 'node:path'
import { spawnSync } from 'node:child_process'

import {
  CLAUDE_ACCESS_TOKEN_SECRETS,
  CODEX_ACCESS_TOKEN_SECRETS,
  type AuthMode,
  type Harness,
} from './constants.js'
import { expandPath } from './state.js'

export type CheckResult = {
  name: string
  ok: boolean
  detail: string
  repair?: string
}

const REQUIRED_BINARIES = ['git', 'jq', 'openssl']
const DEPLOY_BINARIES = ['kubectl', 'helm']
const OPTIONAL_BINARIES = ['gh', 'docker', 'kind', 'ssh', 'op', 'sops', 'age', 'argocd']

function commandPath(name: string) {
  const proc = spawnSync('sh', ['-lc', `command -v ${name}`], { encoding: 'utf8' })
  return proc.status === 0 ? proc.stdout.trim() : ''
}

export function binaryChecks(options: { includeDeploy?: boolean; includeSsh?: boolean } = {}) {
  const names = Array.from(
    new Set([...REQUIRED_BINARIES, ...OPTIONAL_BINARIES, ...(options.includeDeploy ? DEPLOY_BINARIES : [])]),
  )
  return names.map((name): CheckResult => {
    const path = commandPath(name)
    const required =
      REQUIRED_BINARIES.includes(name) ||
      (options.includeDeploy === true && DEPLOY_BINARIES.includes(name)) ||
      (options.includeSsh === true && name === 'ssh')
    return {
      name: `binary:${name}`,
      ok: Boolean(path) || !required,
      detail: path || (required ? 'not installed' : 'missing optional'),
      repair: !path && required ? `Install ${name} and rerun centaur doctor.` : undefined,
    }
  })
}

export function commandCheck(name: string, command: string[], repair: string): CheckResult {
  const proc = spawnSync(command[0]!, command.slice(1), {
    encoding: 'utf8',
    timeout: 15_000,
  })
  const output = `${proc.stdout || proc.stderr || ''}`.trim().split('\n')[0]
  return {
    name,
    ok: proc.status === 0,
    detail: output || `exit ${proc.status ?? 'unknown'}`,
    repair: proc.status === 0 ? undefined : repair,
  }
}

export function dockerDaemonCheck(): CheckResult {
  if (!commandPath('docker')) {
    return {
      name: 'docker:daemon',
      ok: false,
      detail: 'docker not installed',
      repair: 'Install Docker or use an existing Kubernetes cluster.',
    }
  }
  return commandCheck('docker:daemon', ['docker', 'info', '--format', '{{.ServerVersion}}'], 'Start Docker Desktop or the Docker daemon.')
}

function has(env: NodeJS.ProcessEnv, name: string) {
  return Boolean(env[name]?.trim())
}

function hasAll(env: NodeJS.ProcessEnv, names: readonly string[]) {
  return names.every(name => has(env, name))
}

function selectedHarnessSecrets(harness: Harness, authMode: AuthMode) {
  if (harness === 'codex') {
    return authMode === 'access_token' ? CODEX_ACCESS_TOKEN_SECRETS : ['OPENAI_API_KEY']
  }
  return authMode === 'access_token' ? CLAUDE_ACCESS_TOKEN_SECRETS : ['ANTHROPIC_API_KEY']
}

export function envChecks(
  env: NodeJS.ProcessEnv = process.env,
  options: { harness?: Harness; authMode?: AuthMode } = {},
) {
  const harness = options.harness || 'codex'
  const authMode = options.authMode || 'api_key'
  const harnessSecrets = selectedHarnessSecrets(harness, authMode)
  const missingHarnessSecrets = harnessSecrets.filter(name => !has(env, name))
  const results: CheckResult[] = [
    {
      name: 'env:slack',
      ok: has(env, 'SLACK_BOT_TOKEN') && has(env, 'SLACK_SIGNING_SECRET'),
      detail:
        has(env, 'SLACK_BOT_TOKEN') && has(env, 'SLACK_SIGNING_SECRET')
          ? 'SLACK_BOT_TOKEN and SLACK_SIGNING_SECRET set'
          : 'missing Slack bot token or signing secret',
      repair: 'Create the Slack app, install it, and store SLACK_BOT_TOKEN plus SLACK_SIGNING_SECRET.',
    },
    {
      name: `env:${harness}-auth`,
      ok: hasAll(env, harnessSecrets),
      detail:
        missingHarnessSecrets.length === 0
          ? `${authMode} secrets set for ${harness}`
          : `missing ${missingHarnessSecrets.join(', ')}`,
      repair: `Run centaur secrets collect --harness ${harness} --auth-mode ${authMode}, or set ${harnessSecrets.join(', ')} in the selected secret backend.`,
    },
    {
      name: 'env:github',
      ok: has(env, 'GITHUB_APP_ID') || has(env, 'GITHUB_TOKEN'),
      detail: has(env, 'GITHUB_APP_ID') ? 'GitHub App configured' : has(env, 'GITHUB_TOKEN') ? 'token configured' : 'missing',
      repair: 'Configure a GitHub App or scoped GITHUB_TOKEN.',
    },
  ]
  return results.map(result => (result.ok ? { ...result, repair: undefined } : result))
}

export function overlayChecks(path: string) {
  const root = expandPath(path)
  const required = ['AGENTS.md', 'secrets.example.env', 'values.centaur.yaml', 'slack-app-manifest.json']
  return required.map((relativePath): CheckResult => {
    const target = join(root, relativePath)
    return {
      name: `overlay:${relativePath}`,
      ok: existsSync(target),
      detail: target,
      repair: existsSync(target) ? undefined : `Run centaur overlay init --path ${root}.`,
    }
  })
}
