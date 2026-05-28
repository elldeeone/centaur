import { existsSync, mkdirSync, writeFileSync } from 'node:fs'
import { dirname, join } from 'node:path'

import {
  CLAUDE_ACCESS_TOKEN_SECRETS,
  CLAUDE_CODE_CLIENT_ID,
  CODEX_ACCESS_TOKEN_SECRETS,
  OPENAI_CODEX_CLIENT_ID,
  type AuthMode,
  type Harness,
} from './constants.js'
import { expandPath } from './state.js'

export const SLACK_SCOPES = [
  'app_mentions:read',
  'channels:history',
  'channels:read',
  'chat:write',
  'files:read',
  'files:write',
  'groups:history',
  'groups:read',
  'im:history',
  'im:read',
  'users:read',
] as const

export type OverlayOptions = {
  path: string
  org: string
  assistantName: string
  domain: string
  harness: Harness
  authMode: AuthMode
}

function publicBaseUrl(domain: string) {
  const trimmed = (domain || 'centaur.example.com').trim().replace(/\/+$/, '')
  if (trimmed.startsWith('http://') || trimmed.startsWith('https://')) return trimmed
  return `https://${trimmed}`
}

function hostName(domain: string) {
  return publicBaseUrl(domain).replace(/^https?:\/\//, '')
}

export function slackManifest(appName: string, domain: string, socketMode: boolean) {
  const baseUrl = publicBaseUrl(domain)
  const manifest: {
    display_information: { name: string }
    features: {
      bot_user: { display_name: string; always_online: boolean }
      slash_commands: { command: string; description: string; url: string; should_escape: boolean }[]
    }
    oauth_config: { scopes: { bot: string[] } }
    settings: {
      interactivity: { is_enabled: boolean; request_url?: string }
      event_subscriptions: { request_url?: string; bot_events: string[] }
      org_deploy_enabled: boolean
      socket_mode_enabled: boolean
      token_rotation_enabled: boolean
    }
  } = {
    display_information: { name: appName },
    features: {
      bot_user: { display_name: appName, always_online: true },
      slash_commands: [
        {
          command: '/centaur',
          description: 'Send a command to Centaur',
          url: `${baseUrl}/api/slack/commands`,
          should_escape: false,
        },
      ],
    },
    oauth_config: { scopes: { bot: [...SLACK_SCOPES] } },
    settings: {
      interactivity: {
        is_enabled: true,
        request_url: `${baseUrl}/api/slack/actions`,
      },
      event_subscriptions: {
        request_url: `${baseUrl}/api/webhooks/slack`,
        bot_events: ['app_mention', 'message.channels', 'message.groups', 'message.im', 'file_shared'],
      },
      org_deploy_enabled: false,
      socket_mode_enabled: socketMode,
      token_rotation_enabled: false,
    },
  }
  if (socketMode) {
    delete manifest.settings.event_subscriptions.request_url
    delete manifest.settings.interactivity.request_url
  }
  return manifest
}

export function harnessLabel(harness: Harness) {
  return harness === 'codex' ? 'Codex' : 'Claude Code'
}

export function harnessAuthPlan(harness: Harness, authMode: AuthMode) {
  if (harness === 'codex') {
    return {
      harness,
      label: harnessLabel(harness),
      env: 'CODEX_AUTH_MODE',
      mode: authMode,
      upstream: authMode === 'access_token' ? 'chatgpt.com' : 'api.openai.com',
      requiredSecrets:
        authMode === 'access_token' ? [...CODEX_ACCESS_TOKEN_SECRETS] : ['OPENAI_API_KEY'],
      values: {
        api: { defaultHarness: harness, extraEnv: { CODEX_AUTH_MODE: authMode } },
        sandbox: { extraEnv: { CODEX_AUTH_MODE: authMode } },
      },
      warning:
        authMode === 'access_token'
          ? [
              'Use a dedicated ChatGPT account for Centaur Codex subscription auth.',
              'Do not keep using that same local codex login after storing the refresh token in the broker-backed secret store.',
            ]
          : [],
      bootstrap:
        authMode === 'access_token'
          ? [
              'centaur secrets collect --harness codex --auth-mode access_token runs codex login with a dedicated ChatGPT account.',
              'The CLI reads ~/.codex/auth.json and stores OPENAI_CODEX_BLOB plus OPENAI_CODEX_ACCOUNT_ID in the selected backend.',
              `The CLI stores OPENAI_CODEX_CLIENT_ID as ${OPENAI_CODEX_CLIENT_ID}.`,
            ]
          : ['Store OPENAI_API_KEY in the configured secret backend.'],
    }
  }
  return {
    harness,
    label: harnessLabel(harness),
    env: 'CLAUDE_CODE_AUTH_MODE',
    mode: authMode,
    upstream: 'api.anthropic.com',
    requiredSecrets:
      authMode === 'access_token' ? [...CLAUDE_ACCESS_TOKEN_SECRETS] : ['ANTHROPIC_API_KEY'],
    values: {
      api: { defaultHarness: harness, extraEnv: { CLAUDE_CODE_AUTH_MODE: authMode } },
      sandbox: { extraEnv: { CLAUDE_CODE_AUTH_MODE: authMode } },
    },
    warning:
      authMode === 'access_token'
        ? [
            'Use a dedicated Claude.ai Pro or Max account for Centaur Claude Code subscription auth.',
            'Do not keep using that same local claude login after storing the refresh token in the broker-backed secret store.',
          ]
        : [],
    bootstrap:
      authMode === 'access_token'
        ? [
            'centaur secrets collect --harness claude-code --auth-mode access_token runs claude login with a dedicated Claude.ai Pro or Max account.',
            'The CLI reads Claude Code credentials and stores CLAUDE_CODE_BLOB in the selected backend.',
            `The CLI stores CLAUDE_CODE_CLIENT_ID as ${CLAUDE_CODE_CLIENT_ID}.`,
          ]
        : ['Store ANTHROPIC_API_KEY in the configured secret backend.'],
  }
}

function selectedHarnessSecrets(harness: Harness, authMode: AuthMode) {
  if (harness === 'codex') {
    if (authMode === 'access_token') {
      return `# Codex access_token mode: ChatGPT subscription through iron-token-broker
OPENAI_CODEX_CLIENT_ID=${OPENAI_CODEX_CLIENT_ID}
OPENAI_CODEX_BLOB={"refresh_token":"..."}
OPENAI_CODEX_ACCOUNT_ID=00000000-0000-0000-0000-000000000000
`
    }
    return `# Codex api_key mode
OPENAI_API_KEY=...
`
  }
  if (authMode === 'access_token') {
    return `# Claude Code access_token mode: Claude.ai Pro or Max subscription through iron-token-broker
CLAUDE_CODE_CLIENT_ID=${CLAUDE_CODE_CLIENT_ID}
CLAUDE_CODE_BLOB={"refresh_token":"..."}
`
  }
  return `# Claude Code api_key mode
ANTHROPIC_API_KEY=...
`
}

function selectedSandboxExtraEnv(harness: Harness, authMode: AuthMode) {
  return harness === 'codex'
    ? `    CODEX_AUTH_MODE: ${authMode}`
    : `    CLAUDE_CODE_AUTH_MODE: ${authMode}`
}

function selectedApiExtraEnv(harness: Harness, authMode: AuthMode) {
  return harness === 'codex'
    ? `  extraEnv:
    CODEX_AUTH_MODE: ${authMode}`
    : `  extraEnv:
    CLAUDE_CODE_AUTH_MODE: ${authMode}`
}

function overlayFiles(options: OverlayOptions) {
  const domain = hostName(options.domain)
  return {
    'AGENTS.md': `# ${options.assistantName}

You are ${options.assistantName}, the AI assistant for ${options.org}.

## Operating Rules

- Be direct and concrete.
- Verify external writes before claiming success.
- Ask before taking destructive actions.
- Use the configured Centaur tools before ad hoc external calls.

## Deployment

- Domain: ${domain}
- Overlay owner: ${options.org}
`,
    'secrets.example.env': `# Infra secret: centaur-infra-env
POSTGRES_PASSWORD=...
DATABASE_URL=postgres://tempo:...@centaur-centaur-postgres:5432/ai_v2
IRON_MANAGEMENT_API_KEY=...
SANDBOX_SIGNING_KEY=...
SLACKBOT_API_KEY=...

# Slack app
SLACK_BOT_TOKEN=xoxb-...
SLACK_SIGNING_SECRET=...
SLACK_APP_TOKEN=xapp-...
SLACK_CLIENT_ID=...
SLACK_CLIENT_SECRET=...

${selectedHarnessSecrets(options.harness, options.authMode)}

# Optional harnesses and routers
AMP_API_KEY=...
OPENROUTER_API_KEY=...

# GitHub
GITHUB_APP_ID=...
GITHUB_APP_PRIVATE_KEY=...
GITHUB_INSTALLATION_ID=...
GITHUB_TOKEN=...
`,
    'values.centaur.yaml': `secretManager:
  existingSecretName: centaur-infra-env
  envPrefix: ""

ironProxy:
  secretSource: onepassword
  secretTtl: 10m

api:
  defaultHarness: ${options.harness}
  executionWorkerEnabled: true
  warmPoolEnabled: true
${selectedApiExtraEnv(options.harness, options.authMode)}

sandbox:
  extraEnv:
${selectedSandboxExtraEnv(options.harness, options.authMode)}

slackbot:
  enabled: true

ingress:
  enabled: true
  host: ${domain}

overlay:
  mountPath: /app/overlay/org
  systemPrompt: |-
    You are ${options.assistantName}, the AI assistant for ${options.org}.
`,
    'personas/base.md': `You are ${options.assistantName}. Follow the overlay instructions in AGENTS.md.
`,
    '.agents/skills/README.md': '# Skills\n\nAdd organization-specific Centaur skills here.\n',
    'tools/README.md': '# Tools\n\nAdd organization-specific tool wrappers here.\n',
    'workflows/README.md': '# Workflows\n\nAdd durable workflow definitions here.\n',
  }
}

export function writeOverlay(options: OverlayOptions) {
  const root = expandPath(options.path)
  mkdirSync(root, { recursive: true })
  const written: string[] = []
  for (const [relativePath, content] of Object.entries(overlayFiles(options))) {
    const target = join(root, relativePath)
    mkdirSync(dirname(target), { recursive: true })
    if (!existsSync(target)) {
      writeFileSync(target, content)
      written.push(target)
    }
  }
  return written
}

export function writeSlackManifest(path: string, appName: string, domain: string, socketMode: boolean) {
  const target = expandPath(path)
  mkdirSync(dirname(target), { recursive: true })
  writeFileSync(target, `${JSON.stringify(slackManifest(appName, domain, socketMode), null, 2)}\n`)
  return target
}
