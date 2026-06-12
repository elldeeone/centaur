import { errorMessage } from '../logging'
import type { NormalizedZulipEvent, ZulipbotLogger } from '../types'
import type { ZulipClient, ZulipSendMessage, ZulipTypingStatus } from './client'

type ProgressClient = Pick<ZulipClient, 'sendMessage' | 'updateMessage' | 'setTypingStatus'>

type ProgressConfig = {
  maxMs: number
  placeholder: boolean
  progressText: string
  updateMs: number
}

type ProgressState = {
  answerStarted: boolean
  coldStart: boolean
  coldStartNoticeSent: boolean
  heartbeat?: ReturnType<typeof setInterval>
  maxTimer?: ReturnType<typeof setTimeout>
  messageId?: number
  startedAt: number
  target: ZulipTarget
  threadKey: string
}

type ZulipTarget =
  | {
      type: 'stream'
      streamId: number
      topic: string
    }
  | {
      type: 'direct'
      recipientIds: number[]
      recipientEmails: string[]
    }

export class ZulipProgressTracker {
  readonly client: ProgressClient
  readonly config: ProgressConfig
  readonly logger: ZulipbotLogger
  private readonly seenThreadKeys = new Set<string>()

  constructor(config: ProgressConfig, client: ProgressClient, logger: ZulipbotLogger) {
    this.config = config
    this.client = client
    this.logger = logger
  }

  async start(event: NormalizedZulipEvent): Promise<ZulipProgressHandle | null> {
    const target = targetFromEvent(event)
    if (!target) return null
    const coldStart = !this.seenThreadKeys.has(event.thread_key)
    this.seenThreadKeys.add(event.thread_key)
    const state: ProgressState = {
      answerStarted: false,
      coldStart,
      coldStartNoticeSent: false,
      startedAt: Date.now(),
      target,
      threadKey: event.thread_key
    }
    const handle = new ZulipProgressHandle(this.config, this.client, this.logger, state)
    await handle.start()
    return handle
  }

  markWarmThread(threadKey: string, handle?: ZulipProgressHandle | null): void {
    this.seenThreadKeys.add(threadKey)
    handle?.markWarm()
  }
}

export class ZulipProgressHandle {
  readonly client: ProgressClient
  readonly config: ProgressConfig
  readonly logger: ZulipbotLogger
  private readonly state: ProgressState

  constructor(
    config: ProgressConfig,
    client: ProgressClient,
    logger: ZulipbotLogger,
    state: ProgressState
  ) {
    this.config = config
    this.client = client
    this.logger = logger
    this.state = state
  }

  async start(): Promise<void> {
    await this.sendTyping('start')
    if (this.config.placeholder) {
      try {
        const response = await this.client.sendMessage(
          sendMessageFromTarget(this.state.target, this.config.progressText)
        )
        if (response.id !== undefined) this.state.messageId = response.id
      } catch (error) {
        this.logger.warn('zulip_progress_placeholder_failed', {
          thread_key: this.state.threadKey,
          error: errorMessage(error)
        })
      }
    }
    this.startHeartbeat()
  }

  markWarm(): void {
    this.state.coldStart = false
  }

  async update(content: string): Promise<boolean> {
    this.state.answerStarted = true
    if (this.state.messageId === undefined) return false
    try {
      await this.client.updateMessage(this.state.messageId, content)
      return true
    } catch (error) {
      this.logger.warn('zulip_progress_update_failed', {
        thread_key: this.state.threadKey,
        error: errorMessage(error)
      })
      return false
    }
  }

  async complete(content: string): Promise<boolean> {
    this.stopTimers()
    await this.sendTyping('stop')
    return this.update(content)
  }

  async fail(content: string): Promise<void> {
    this.stopTimers()
    await this.sendTyping('stop')
    if (this.state.messageId === undefined) return
    try {
      await this.client.updateMessage(this.state.messageId, content)
    } catch (error) {
      this.logger.warn('zulip_progress_fail_update_failed', {
        thread_key: this.state.threadKey,
        error: errorMessage(error)
      })
    }
  }

  async stop(): Promise<void> {
    this.stopTimers()
    await this.sendTyping('stop')
  }

  private startHeartbeat(): void {
    this.state.heartbeat = setInterval(() => {
      void this.heartbeat()
    }, this.config.updateMs)
    this.state.heartbeat.unref?.()
    this.state.maxTimer = setTimeout(() => {
      this.stopTimers()
      void this.sendTyping('stop')
    }, this.config.maxMs)
    this.state.maxTimer.unref?.()
  }

  private async heartbeat(): Promise<void> {
    await this.sendTyping('start')
    if (this.state.answerStarted || this.state.messageId === undefined) return
    const elapsedSeconds = Math.max(Math.round((Date.now() - this.state.startedAt) / 1000), 1)
    try {
      await this.client.updateMessage(
        this.state.messageId,
        progressText(this.state, this.config, elapsedSeconds)
      )
      this.state.coldStartNoticeSent = true
    } catch (error) {
      this.logger.warn('zulip_progress_heartbeat_update_failed', {
        thread_key: this.state.threadKey,
        error: errorMessage(error)
      })
    }
  }

  private async sendTyping(op: 'start' | 'stop'): Promise<void> {
    const status = typingStatusFromTarget(this.state.target, op)
    if (!status) return
    try {
      await this.client.setTypingStatus(status)
    } catch (error) {
      this.logger.warn('zulip_typing_status_failed', {
        thread_key: this.state.threadKey,
        op,
        error: errorMessage(error)
      })
    }
  }

  private stopTimers(): void {
    if (this.state.heartbeat) clearInterval(this.state.heartbeat)
    if (this.state.maxTimer) clearTimeout(this.state.maxTimer)
    this.state.heartbeat = undefined
    this.state.maxTimer = undefined
  }
}

export function progressText(
  state: Pick<ProgressState, 'coldStart' | 'coldStartNoticeSent'>,
  config: Pick<ProgressConfig, 'progressText'>,
  elapsedSeconds: number
): string {
  if (state.coldStart && !state.coldStartNoticeSent) {
    return 'Starting Centaur runtime.\n\nFirst reply can take a moment.'
  }
  return `${config.progressText}\n\nStill working (${elapsedSeconds}s).`
}

export function targetFromEvent(event: NormalizedZulipEvent): ZulipTarget | undefined {
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
    type: 'direct',
    recipientIds: delivery.recipient_ids ?? [],
    recipientEmails: delivery.recipient_emails ?? []
  }
}

export function sendMessageFromTarget(target: ZulipTarget, content: string): ZulipSendMessage {
  if (target.type === 'stream') {
    return {
      type: 'stream',
      to: target.streamId,
      topic: target.topic,
      content
    }
  }
  return {
    type: 'direct',
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
