import { homedir } from 'node:os'
import { dirname, isAbsolute, join, resolve } from 'node:path'
import { existsSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs'

import type { AuthMode, Harness, InstallMode, SecretBackend } from './constants.js'

export const DEFAULT_HOME = join(homedir(), '.centaur')

export type OnboardingState = {
  org: string
  assistantName: string
  domain: string
  adminEmail: string
  installMode: InstallMode
  secretBackend: SecretBackend
  overlayPath: string
  harness: Harness
  authMode: AuthMode
  completedSteps: string[]
  data: Record<string, unknown>
}

export function emptyState(): OnboardingState {
  return {
    org: '',
    assistantName: 'centaur',
    domain: '',
    adminEmail: '',
    installMode: 'local',
    secretBackend: 'local-env',
    overlayPath: '',
    harness: 'codex',
    authMode: 'api_key',
    completedSteps: [],
    data: {},
  }
}

function defaultBaseDir() {
  if (process.env.INIT_CWD) return process.env.INIT_CWD

  let dir = process.cwd()
  while (true) {
    if (existsSync(join(dir, 'contrib', 'chart')) && existsSync(join(dir, 'Justfile'))) return dir
    const parent = dirname(dir)
    if (parent === dir) return process.cwd()
    dir = parent
  }
}

export function expandPath(path: string): string {
  if (path === '~') return homedir()
  if (path.startsWith('~/')) return join(homedir(), path.slice(2))
  return isAbsolute(path) ? path : resolve(defaultBaseDir(), path)
}

export function statePath(home = DEFAULT_HOME): string {
  return join(expandPath(home), 'onboarding-state.json')
}

export function configPath(home = DEFAULT_HOME): string {
  return join(expandPath(home), 'config.json')
}

export function loadState(home = DEFAULT_HOME): OnboardingState {
  try {
    return { ...emptyState(), ...JSON.parse(readFileSync(statePath(home), 'utf8')) }
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code === 'ENOENT') return emptyState()
    throw error
  }
}

export function saveState(state: OnboardingState, home = DEFAULT_HOME) {
  const resolvedHome = expandPath(home)
  mkdirSync(resolvedHome, { recursive: true })
  mkdirSync(join(resolvedHome, 'logs'), { recursive: true })
  const text = `${JSON.stringify(state, null, 2)}\n`
  const stateFile = statePath(resolvedHome)
  const configFile = configPath(resolvedHome)
  mkdirSync(dirname(stateFile), { recursive: true })
  writeFileSync(stateFile, text)
  writeFileSync(configFile, text)
  return { statePath: stateFile, configPath: configFile }
}

export function markDone(state: OnboardingState, step: string) {
  if (!state.completedSteps.includes(step)) state.completedSteps.push(step)
}
