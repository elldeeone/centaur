import { mkdtempSync, readFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'

import { describe, expect, it } from 'vitest'

import { app, k3sDeploymentCommands } from '../src/app.js'
import { envChecks } from '../src/checks.js'
import { CentaurClient, parseSse } from '../src/client.js'
import { CLAUDE_CODE_CLIENT_ID, OPENAI_CODEX_CLIENT_ID } from '../src/constants.js'
import { runAgent } from '../src/run.js'
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

function response(data: unknown, init?: ResponseInit) {
  return new Response(JSON.stringify(data), {
    status: 200,
    headers: { 'content-type': 'application/json' },
    ...init,
  })
}

function sse(body: string) {
  return new Response(
    new ReadableStream({
      start(controller) {
        controller.enqueue(new TextEncoder().encode(body))
        controller.close()
      },
    }),
    { headers: { 'content-type': 'text/event-stream' } },
  )
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

describe('agent run client', () => {
  it('posts spawn, message, and execute payloads', async () => {
    const calls: Array<{ url: string; init?: RequestInit }> = []
    const fetchImpl = (async (url: string | URL | Request, init?: RequestInit) => {
      calls.push({ url: String(url), init })
      if (String(url).endsWith('/agent/spawn')) {
        return response({ thread_key: 'cli:test', assignment_generation: 7 })
      }
      if (String(url).endsWith('/agent/message')) return response({ ok: true, message_id: 'msg-1' })
      if (String(url).endsWith('/agent/execute')) return response({ execution_id: 'exe-1', status: 'queued' })
      throw new Error(`unexpected url ${String(url)}`)
    }) as typeof fetch

    const client = new CentaurClient({
      apiUrl: 'http://api.test/',
      apiKey: 'key',
      fetchImpl,
    })

    const spawn = await client.spawn({ threadKey: 'cli:test', harness: 'codex' })
    await client.message({
      threadKey: 'cli:test',
      assignmentGeneration: spawn.assignment_generation,
      parts: [{ type: 'text', text: 'hello' }],
    })
    await client.execute({
      threadKey: 'cli:test',
      assignmentGeneration: spawn.assignment_generation,
      harness: 'codex',
    })

    expect(calls.map(call => [call.url, call.init?.body && JSON.parse(String(call.init.body))])).toEqual([
      [
        'http://api.test/agent/spawn',
        {
          thread_key: 'cli:test',
          harness: 'codex',
        },
      ],
      [
        'http://api.test/agent/message',
        {
          thread_key: 'cli:test',
          assignment_generation: 7,
          role: 'user',
          parts: [{ type: 'text', text: 'hello' }],
          metadata: { platform: 'cli' },
        },
      ],
      [
        'http://api.test/agent/execute',
        {
          thread_key: 'cli:test',
          assignment_generation: 7,
          harness: 'codex',
          delivery: { platform: 'cli' },
          metadata: { platform: 'cli' },
        },
      ],
    ])
    expect(calls[0]?.init?.headers).toMatchObject({
      Authorization: 'Bearer key',
      'X-Api-Key': 'key',
    })
  })

  it('parses SSE frames with ids, events, multiline data, and invalid json fallback', async () => {
    const frames = []
    const stream = sse(
      [
        'id: 1',
        'event: assistant',
        'data: {"result":"hello"}',
        '',
        'id: 2',
        'event: message',
        'data: first',
        'data: second',
        '',
      ].join('\n'),
    ).body!

    for await (const frame of parseSse(stream)) frames.push(frame)

    expect(frames).toEqual([
      { id: '1', event: 'assistant', data: '{"result":"hello"}' },
      { id: '2', event: 'message', data: 'first\nsecond' },
    ])
  })

  it('runs the durable agent flow and preserves every streamed API event', async () => {
    const calls: string[] = []
    const fetchImpl = (async (url: string | URL | Request) => {
      const target = String(url)
      calls.push(target)
      if (target.endsWith('/agent/spawn')) {
        return response({ thread_key: 'cli:test', assignment_generation: 7 })
      }
      if (target.endsWith('/agent/message')) return response({ ok: true, message_id: 'msg-1' })
      if (target.endsWith('/agent/execute')) return response({ execution_id: 'exe-1', status: 'queued' })
      if (target.includes('/agent/threads/cli%3Atest/events')) {
        return sse(
          [
            'id: 1',
            'event: harness_raw_event',
            'data: {"item":{"type":"userMessage","content":[{"type":"text","text":"hello"}]},"type":"item.completed"}',
            '',
            'id: 2',
            'event: chat_stream_chunk',
            'data: {"type":"markdown_text","text":"P"}',
            '',
            'id: 3',
            'event: chat_stream_chunk',
            'data: {"type":"markdown_text","text":"P"}',
            '',
            'id: 4',
            'event: execution_state',
            'data: {"status":"completed","result_text":"P"}',
            '',
          ].join('\n'),
        )
      }
      if (target.endsWith('/agent/executions/exe-1')) {
        return response({ status: 'completed', result_text: 'P' })
      }
      throw new Error(`unexpected url ${target}`)
    }) as typeof fetch

    const stream = runAgent({
      apiUrl: 'http://api.test',
      apiKey: 'key',
      prompt: 'hello',
      threadKey: 'cli:test',
      harness: 'codex',
      fetchImpl,
    })

    const yielded = []
    let next = await stream.next()
    while (!next.done) {
      yielded.push(next.value)
      next = await stream.next()
    }

    expect(yielded).toEqual([
      {
        phase: 'spawned',
        threadKey: 'cli:test',
        assignmentGeneration: 7,
      },
      {
        phase: 'message_persisted',
        messageId: 'msg-1',
      },
      {
        phase: 'execution_queued',
        executionId: 'exe-1',
        status: 'queued',
      },
      {
        phase: 'api_event',
        eventId: 1,
        eventKind: 'harness_raw_event',
        data: {
          item: {
            type: 'userMessage',
            content: [{ type: 'text', text: 'hello' }],
          },
          type: 'item.completed',
        },
      },
      {
        phase: 'api_event',
        eventId: 2,
        eventKind: 'chat_stream_chunk',
        data: { type: 'markdown_text', text: 'P' },
      },
      {
        phase: 'api_event',
        eventId: 3,
        eventKind: 'chat_stream_chunk',
        data: { type: 'markdown_text', text: 'P' },
      },
      {
        phase: 'api_event',
        eventId: 4,
        eventKind: 'execution_state',
        data: { status: 'completed', result_text: 'P' },
      },
      {
        phase: 'final_state',
        executionId: 'exe-1',
        state: { status: 'completed', result_text: 'P' },
      },
    ])
    expect(next.value).toEqual({
      threadKey: 'cli:test',
      assignmentGeneration: 7,
      executionId: 'exe-1',
      status: 'completed',
      resultText: 'P',
    })
    expect(calls.at(-1)).toBe('http://api.test/agent/executions/exe-1')
  })
})
