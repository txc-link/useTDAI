import Schema from '@deepseek-ai/schemastery'
import { spawn } from 'node:child_process'

export const name = 'tdai-memory-sidecar'
export const Config = Schema.object({
  command: Schema.string().default('uv'),
  script: Schema.string().required(),
  batchTurns: Schema.number().min(1).default(5),
  idleSeconds: Schema.number().min(10).default(180),
})

function runBridge(config, payload) {
  const child = spawn(
    config.command,
    [
      'run', '--script', config.script,
      '--canonical-hook', '--platform', 'dsh',
      '--batch-turns', String(config.batchTurns),
      '--idle-seconds', String(config.idleSeconds),
    ],
    { stdio: ['pipe', 'ignore', 'pipe'], windowsHide: true },
  )
  let error = ''
  child.stderr.setEncoding('utf8')
  child.stderr.on('data', (chunk) => { error += chunk })
  child.on('error', (cause) => console.warn('[tdai-memory] bridge error', cause.message))
  child.on('close', (code) => {
    if (code) console.warn('[tdai-memory] bridge failed', error.slice(-1000))
  })
  child.stdin.end(JSON.stringify(payload))
}

function messages(session) {
  return session.deriveMessages().flatMap((message) => {
    if (message?.role !== 'user' && message?.role !== 'assistant') return []
    return [{ role: message.role, content: message.content }]
  })
}

export function apply(ctx, config) {
  ctx.on('session/event', (session, event) => {
    if (event.type !== 'turn/end' || event.data?.reason?.kind !== 'completed') return
    runBridge(config, {
      hook_event_name: 'turn/end',
      session_id: String(session.id),
      messages: messages(session),
    })
  })

  ctx.on('session/disposed', (session) => {
    runBridge(config, {
      hook_event_name: 'session/disposed',
      session_id: String(session.id),
      messages: [],
      force: true,
    })
  })
}
