import { mkdtempSync, readFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'

import { describe, expect, it } from 'vitest'

import { app, k3sDeploymentCommands } from '../src/app.js'
import { envChecks } from '../src/checks.js'
import { CLAUDE_CODE_CLIENT_ID, OPENAI_CODEX_CLIENT_ID } from '../src/constants.js'
import { kubernetesEnvFile, writeSecrets } from '../src/secrets.js'
import { harnessAuthPlan, slackManifest, writeOverlay, writeSlackManifest } from '../src/templates.js'

async function runCli(args: string[]) {
  let stdout = ''
  await app.serve(args, {
    stdout: chunk => {
      stdout += chunk
    },
    exit: code => {
      if (code !== 0) throw new Error(`unexpected exit ${code}: ${stdout}`)
    },
  })
  return stdout
}

describe('slack manifest', () => {
  it('uses the Slackbot production routes', () => {
    const manifest = slackManifest('centaur', 'centaur.example.com', false)

    expect(manifest.settings.event_subscriptions.request_url).toBe(
      'https://centaur.example.com/api/webhooks/slack',
    )
    expect(manifest.settings.interactivity.request_url).toBe(
      'https://centaur.example.com/api/slack/actions',
    )
    expect(manifest.features.slash_commands[0]?.url).toBe(
      'https://centaur.example.com/api/slack/commands',
    )
    expect(manifest.oauth_config.scopes.bot).toContain('chat:write')
  })

  it('removes request URLs for socket mode', () => {
    const manifest = slackManifest('centaur', 'centaur.example.com', true)

    expect(manifest.settings.socket_mode_enabled).toBe(true)
    expect('request_url' in manifest.settings.event_subscriptions).toBe(false)
    expect('request_url' in manifest.settings.interactivity).toBe(false)
  })
})

describe('harness auth', () => {
  it('describes Codex subscription OAuth secrets for the selected harness', () => {
    const plan = harnessAuthPlan('codex', 'access_token')

    expect(plan.values.api.extraEnv).toEqual({ CODEX_AUTH_MODE: 'access_token' })
    expect(plan.values.sandbox.extraEnv).toEqual({ CODEX_AUTH_MODE: 'access_token' })
    expect(plan.requiredSecrets).toEqual([
      'OPENAI_CODEX_CLIENT_ID',
      'OPENAI_CODEX_BLOB',
      'OPENAI_CODEX_ACCOUNT_ID',
    ])
    expect(plan.bootstrap.join('\n')).toContain(OPENAI_CODEX_CLIENT_ID)
  })

  it('describes Claude Code subscription OAuth secrets for the selected harness', () => {
    const plan = harnessAuthPlan('claude-code', 'access_token')

    expect(plan.values.api.extraEnv).toEqual({ CLAUDE_CODE_AUTH_MODE: 'access_token' })
    expect(plan.values.sandbox.extraEnv).toEqual({ CLAUDE_CODE_AUTH_MODE: 'access_token' })
    expect(plan.requiredSecrets).toEqual(['CLAUDE_CODE_CLIENT_ID', 'CLAUDE_CODE_BLOB'])
    expect(plan.bootstrap.join('\n')).toContain(CLAUDE_CODE_CLIENT_ID)
  })
})

describe('overlay scaffolding', () => {
  it('writes access_token auth modes and OAuth secret placeholders', () => {
    const root = mkdtempSync(join(tmpdir(), 'centaur-cli-overlay-'))
    const overlayPath = join(root, 'org')

    const written = writeOverlay({
      path: overlayPath,
      org: 'acme',
      assistantName: 'centaur',
      domain: 'centaur.acme.com',
      harness: 'codex',
      authMode: 'access_token',
    })
    writeSlackManifest(join(overlayPath, 'slack-app-manifest.json'), 'centaur', 'centaur.acme.com', false)

    expect(written.length).toBeGreaterThan(0)
    expect(readFileSync(join(overlayPath, 'values.centaur.yaml'), 'utf8')).toContain(
      'CODEX_AUTH_MODE: access_token',
    )
    const secrets = readFileSync(join(overlayPath, 'secrets.example.env'), 'utf8')
    expect(secrets).toContain(`OPENAI_CODEX_CLIENT_ID=${OPENAI_CODEX_CLIENT_ID}`)
    expect(secrets).not.toContain(`CLAUDE_CODE_CLIENT_ID=${CLAUDE_CODE_CLIENT_ID}`)
    expect(readFileSync(join(overlayPath, 'slack-app-manifest.json'), 'utf8')).toContain(
      '/api/webhooks/slack',
    )
  })

  it('init creates state and returns contextual next-command CTAs', async () => {
    const root = mkdtempSync(join(tmpdir(), 'centaur-cli-init-'))
    const overlayPath = join(root, 'org')
    const home = join(root, 'home')

    const stdout = await runCli([
      'init',
      '--org',
      'acme',
      '--assistant-name',
      'centaur',
      '--domain',
      'centaur.acme.com',
      '--overlay-path',
      overlayPath,
      '--home',
      home,
      '--harness',
      'codex',
      '--auth-mode',
      'access_token',
      '--json',
    ])

    const output = JSON.parse(stdout)
    const ctaCommands = output.cta.commands.map((command: { command: string }) => command.command)
    expect(ctaCommands[0]).toContain('centaur integrations slack-manifest')
    expect(ctaCommands[0]).toContain('--copy')
    expect(ctaCommands[0]).toContain('--harness codex')
    expect(ctaCommands[1]).toContain('centaur secrets collect')
    expect(ctaCommands[1]).toContain('--auth-mode access_token')
    expect(ctaCommands[2]).toContain('centaur doctor --deep')
    expect(ctaCommands[2]).toContain('--harness codex')
    expect(ctaCommands[2]).toContain('--auth-mode access_token')

    const state = JSON.parse(readFileSync(join(home, 'onboarding-state.json'), 'utf8'))
    expect(state.org).toBe('acme')
    expect(state.harness).toBe('codex')
    expect(state.authMode).toBe('access_token')
    expect(state.completedSteps).toContain('slack-manifest')
    expect(readFileSync(join(overlayPath, 'values.centaur.yaml'), 'utf8')).toContain(
      'CODEX_AUTH_MODE: access_token',
    )
  })

  it('slack-manifest returns the exact next secrets collection command', async () => {
    const root = mkdtempSync(join(tmpdir(), 'centaur-cli-slack-'))
    const overlayPath = join(root, 'org')
    const outputPath = join(overlayPath, 'slack-app-manifest.json')

    const stdout = await runCli([
      'integrations',
      'slack-manifest',
      '--domain',
      'centaur.acme.com',
      '--app-name',
      'centaur',
      '--output',
      outputPath,
      '--backend',
      'kubernetes',
      '--install-mode',
      'k8s',
      '--harness',
      'claude-code',
      '--auth-mode',
      'access_token',
      '--overlay-path',
      overlayPath,
      '--json',
    ])

    const output = JSON.parse(stdout)
    expect(output.copied).toBe(false)
    expect(output.nextCommand).toContain('secrets collect --backend kubernetes')
    expect(output.nextCommand).toContain('--harness claude-code')
    expect(output.nextCommand).toContain('--auth-mode access_token')
    expect(output.cta.commands[0].command).toContain('centaur secrets collect --backend kubernetes')
  })
})

describe('environment checks', () => {
  it('validates only the selected default harness', () => {
    const results = envChecks(
      {
        SLACK_BOT_TOKEN: 'xoxb-test',
        SLACK_SIGNING_SECRET: 'signing-test',
        ANTHROPIC_API_KEY: 'sk-ant-test',
      },
      { harness: 'claude-code', authMode: 'api_key' },
    )

    expect(results.some(result => result.name === 'env:codex-auth')).toBe(false)
    expect(results.find(result => result.name === 'env:claude-code-auth')?.ok).toBe(true)
  })
})

describe('deploy plans', () => {
  it('prints local k3s cluster commands', () => {
    const commands = k3sDeploymentCommands('centaur', 'centaur', 'org/values.centaur.yaml')

    expect(commands[0]).toEqual(['kubectl', 'config', 'current-context'])
    expect(commands.at(-1)?.slice(0, 4)).toEqual(['helm', 'upgrade', '--install', 'centaur'])
    expect(commands.at(-1)?.[4]).toMatch(/contrib\/chart$/)
    expect(commands.at(-1)?.slice(5)).toEqual(['-n', 'centaur', '-f', 'org/values.centaur.yaml'])
  })
})

describe('secret backends', () => {
  it('preserves JSON values for kubectl env-file secrets', () => {
    const text = kubernetesEnvFile({
      OPENAI_CODEX_BLOB: '{"refresh_token":"secret"}',
    })

    expect(text).toBe('OPENAI_CODEX_BLOB={"refresh_token":"secret"}\n')
  })

  it('writes local-env secrets without printing values in the command summary', () => {
    const root = mkdtempSync(join(tmpdir(), 'centaur-cli-secrets-'))
    const target = join(root, 'secrets.local.env')

    const result = writeSecrets(
      'local-env',
      { SLACK_BOT_TOKEN: 'xoxb-secret', OPENAI_API_KEY: 'sk-secret' },
      { localEnvPath: target },
    )

    const text = readFileSync(target, 'utf8')
    expect(text).toContain('SLACK_BOT_TOKEN=xoxb-secret')
    expect(text).toContain('OPENAI_API_KEY=sk-secret')
    expect(result.command).toBe(`write ${target}`)
    expect(result.command).not.toContain('xoxb-secret')
  })
})
