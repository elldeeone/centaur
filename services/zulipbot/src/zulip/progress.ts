import type { AppConfig } from '../config'
import { logWarn } from '../logging'
import type { ZulipClient, ZulipSendMessage, ZulipTypingStatus } from './client'
import type { NormalizedZulipEvent } from './types'

type ProgressClient = Pick<ZulipClient, 'sendMessage' | 'updateMessage' | 'setTypingStatus'>

type ProgressState = {
  threadKey: string
  target: ZulipTarget
  startedAt: number
  messageId?: number
  heartbeat?: Timer
  maxTimer?: Timer
}

type ZulipTarget =
  | {
      type: 'stream'
      streamId: number
      topic: string
    }
  | {
      type: 'private'
      recipientIds: number[]
      recipientEmails: string[]
    }

export class ZulipProgressTracker {
  readonly config: AppConfig
  readonly client: ProgressClient
  readonly byExecutionId = new Map<string, ProgressState>()
  readonly pendingByThreadKey = new Map<string, ProgressState>()

  constructor(config: AppConfig, client: ProgressClient) {
    this.config = config
    this.client = client
  }

  async start(event: NormalizedZulipEvent): Promise<void> {
    const target = targetFromEvent(event)
    if (!target) return
    const state: ProgressState = {
      threadKey: event.thread_key,
      target,
      startedAt: Date.now()
    }
    this.pendingByThreadKey.set(event.thread_key, state)
    await this.sendTyping(state, 'start')
    if (this.config.ZULIP_PROGRESS_PLACEHOLDER) {
      try {
        const response = await this.client.sendMessage(
          sendMessageFromTarget(target, this.config.ZULIP_PROGRESS_TEXT)
        )
        if (response.id !== undefined) state.messageId = response.id
      } catch (error) {
        logWarn('zulip_progress_placeholder_failed', {
          thread_key: event.thread_key,
          error: errorMessage(error)
        })
      }
    }
    this.startHeartbeat(state)
  }

  attachExecution(threadKey: string, body: unknown): string | undefined {
    const executionId = executionIdFromBody(body)
    if (!executionId) return undefined
    const state = this.pendingByThreadKey.get(threadKey)
    if (!state) return executionId
    this.pendingByThreadKey.delete(threadKey)
    this.byExecutionId.set(executionId, state)
    return executionId
  }

  async completeDelivery(executionId: string, threadKey: string, content: string): Promise<boolean> {
    const state = this.byExecutionId.get(executionId) ?? this.pendingByThreadKey.get(threadKey)
    if (!state) return false
    this.cleanupExecution(executionId, state)
    await this.sendTyping(state, 'stop')
    if (state.messageId === undefined) return false
    try {
      await this.client.updateMessage(state.messageId, content)
      return true
    } catch (error) {
      logWarn('zulip_progress_update_failed', {
        execution_id: executionId,
        thread_key: state.threadKey,
        error: errorMessage(error)
      })
      return false
    }
  }

  async failThread(threadKey: string, content: string): Promise<void> {
    const state = this.pendingByThreadKey.get(threadKey)
    if (!state) return
    this.pendingByThreadKey.delete(threadKey)
    this.stopTimers(state)
    await this.sendTyping(state, 'stop')
    if (state.messageId === undefined) return
    try {
      await this.client.updateMessage(state.messageId, content)
    } catch (error) {
      logWarn('zulip_progress_fail_update_failed', {
        thread_key: threadKey,
        error: errorMessage(error)
      })
    }
  }

  async stopExecution(executionId: string): Promise<void> {
    const state = this.byExecutionId.get(executionId)
    if (!state) return
    this.cleanupExecution(executionId, state)
    await this.sendTyping(state, 'stop')
  }

  private startHeartbeat(state: ProgressState): void {
    state.heartbeat = setInterval(() => {
      void this.heartbeat(state)
    }, this.config.ZULIP_PROGRESS_UPDATE_MS)
    state.heartbeat.unref?.()
    state.maxTimer = setTimeout(() => {
      this.stopTimers(state)
      void this.sendTyping(state, 'stop')
    }, this.config.ZULIP_PROGRESS_MAX_MS)
    state.maxTimer.unref?.()
  }

  private async heartbeat(state: ProgressState): Promise<void> {
    await this.sendTyping(state, 'start')
    if (state.messageId === undefined) return
    const elapsedSeconds = Math.max(Math.round((Date.now() - state.startedAt) / 1000), 1)
    try {
      await this.client.updateMessage(
        state.messageId,
        `${this.config.ZULIP_PROGRESS_TEXT}\n\nStill working (${elapsedSeconds}s).`
      )
    } catch (error) {
      logWarn('zulip_progress_heartbeat_update_failed', {
        thread_key: state.threadKey,
        error: errorMessage(error)
      })
    }
  }

  private async sendTyping(state: ProgressState, op: 'start' | 'stop'): Promise<void> {
    const status = typingStatusFromTarget(state.target, op)
    if (!status) return
    try {
      await this.client.setTypingStatus(status)
    } catch (error) {
      logWarn('zulip_typing_status_failed', {
        thread_key: state.threadKey,
        op,
        error: errorMessage(error)
      })
    }
  }

  private cleanupExecution(executionId: string, state: ProgressState): void {
    this.byExecutionId.delete(executionId)
    this.pendingByThreadKey.delete(state.threadKey)
    this.stopTimers(state)
  }

  private stopTimers(state: ProgressState): void {
    if (state.heartbeat) clearInterval(state.heartbeat)
    if (state.maxTimer) clearTimeout(state.maxTimer)
    state.heartbeat = undefined
    state.maxTimer = undefined
  }
}

function targetFromEvent(event: NormalizedZulipEvent): ZulipTarget | undefined {
  const delivery = event.delivery
  if (delivery.message_type === 'stream') {
    if (delivery.stream_id === undefined || delivery.topic === undefined) return undefined
    return {
      type: 'stream',
      streamId: delivery.stream_id,
      topic: delivery.topic
    }
  }
  if (!delivery.recipient_ids?.length && !delivery.recipient_emails?.length) return undefined
  return {
    type: 'private',
    recipientIds: delivery.recipient_ids ?? [],
    recipientEmails: delivery.recipient_emails ?? []
  }
}

function sendMessageFromTarget(target: ZulipTarget, content: string): ZulipSendMessage {
  if (target.type === 'stream') {
    return {
      type: 'stream',
      to: target.streamId,
      topic: target.topic,
      content
    }
  }
  return {
    type: 'private',
    to: target.recipientIds.length ? target.recipientIds : target.recipientEmails,
    content
  }
}

function typingStatusFromTarget(
  target: ZulipTarget,
  op: 'start' | 'stop'
): ZulipTypingStatus | undefined {
  if (target.type === 'stream') {
    return {
      type: 'stream',
      op,
      stream_id: target.streamId,
      topic: target.topic
    }
  }
  if (!target.recipientIds.length) return undefined
  return {
    type: 'direct',
    op,
    to: target.recipientIds
  }
}

function executionIdFromBody(body: unknown): string | undefined {
  if (!body || typeof body !== 'object') return undefined
  const value = (body as { execution_id?: unknown }).execution_id
  return typeof value === 'string' && value ? value : undefined
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error)
}
