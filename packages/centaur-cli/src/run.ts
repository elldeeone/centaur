import { randomUUID } from 'node:crypto'

import { CentaurClient, type StreamEvent } from './client.js'

export interface RunAgentOptions {
  apiUrl: string
  apiKey: string
  prompt: string
  threadKey?: string
  harness?: string
  engine?: string
  personaId?: string
  stream?: boolean
  pollMs?: number
  fetchImpl?: typeof fetch
}

export interface RunAgentSummary {
  threadKey: string
  assignmentGeneration: number
  executionId: string
  status: string
  resultText: string
}

export type RunAgentEvent =
  | {
      phase: 'spawned'
      threadKey: string
      assignmentGeneration: number
      runtimeId?: string
    }
  | {
      phase: 'message_persisted'
      messageId: string
    }
  | {
      phase: 'execution_queued'
      executionId: string
      status: string
    }
  | ({
      phase: 'api_event'
    } & StreamEvent)
  | {
      phase: 'final_state'
      executionId: string
      state: Record<string, unknown>
    }

export async function* runAgent(
  options: RunAgentOptions,
): AsyncGenerator<RunAgentEvent, RunAgentSummary> {
  const threadKey = options.threadKey ?? `cli:${Date.now()}:${randomUUID().slice(0, 8)}`
  const client = new CentaurClient({
    apiUrl: options.apiUrl,
    apiKey: options.apiKey,
    fetchImpl: options.fetchImpl,
  })

  const spawn = await client.spawn({
    threadKey,
    harness: options.harness,
    engine: options.engine,
    personaId: options.personaId,
  })
  const spawned: RunAgentEvent = {
    phase: 'spawned',
    threadKey,
    assignmentGeneration: spawn.assignment_generation,
  }
  if (spawn.runtime_id) spawned.runtimeId = spawn.runtime_id
  yield spawned

  const message = await client.message({
    threadKey,
    assignmentGeneration: spawn.assignment_generation,
    parts: [{ type: 'text', text: options.prompt }],
  })
  yield { phase: 'message_persisted', messageId: message.message_id }

  const execute = await client.execute({
    threadKey,
    assignmentGeneration: spawn.assignment_generation,
    harness: options.harness,
  })
  yield {
    phase: 'execution_queued',
    executionId: execute.execution_id,
    status: execute.status,
  }

  let resultText = ''
  let status = execute.status

  if (options.stream !== false) {
    for await (const event of client.streamEvents({
      threadKey,
      executionId: execute.execution_id,
      afterEventId: 0,
      pollMs: options.pollMs,
    })) {
      yield { phase: 'api_event', ...event }
      const eventStatus = statusFromEvent(event)
      if (eventStatus) status = eventStatus
    }
  }

  const finalState = await client.getExecution(execute.execution_id)
  status = String(finalState.status ?? status)
  resultText = String(finalState.result_text ?? resultText)
  yield {
    phase: 'final_state',
    executionId: execute.execution_id,
    state: finalState,
  }

  const summary = {
    threadKey,
    assignmentGeneration: spawn.assignment_generation,
    executionId: execute.execution_id,
    status,
    resultText,
  }
  return summary
}

function statusFromEvent(event: StreamEvent): string {
  if (event.eventKind !== 'execution_state') return ''
  return typeof event.data.status === 'string' ? event.data.status : ''
}
