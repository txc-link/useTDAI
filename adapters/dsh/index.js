import Schema from '@deepseek-ai/schemastery'
import { defineTool } from '@deepseek-ai/dsh-tools'
import { spawn } from 'node:child_process'

export const name = 'tdai-memory-sidecar'
export const inject = ['tools']
export const Config = Schema.object({
  command: Schema.string().default('uv'),
  script: Schema.string().required(),
  endpoint: Schema.string().required(),
  serviceId: Schema.string().default('default'),
  teamId: Schema.string().required(),
  agentId: Schema.string().required(),
  userId: Schema.string().required(),
  taskId: Schema.string().default(''),
  userKeyEnv: Schema.string().default('TDAI_USER_KEY'),
  localDataDir: Schema.string().default(''),
  captureEnabled: Schema.boolean().default(true),
  toolsEnabled: Schema.boolean().default(true),
  batchTurns: Schema.number().min(1).default(5),
  idleSeconds: Schema.number().min(10).default(180),
  toolTimeoutSeconds: Schema.number().min(5).default(60),
})

function scopeArgs(config) {
  const args = [
    '--endpoint', config.endpoint,
    '--service-id', config.serviceId,
    '--team-id', config.teamId,
    '--agent-id', config.agentId,
    '--user-id', config.userId,
  ]
  if (config.taskId) args.push('--task-id', config.taskId)
  if (config.localDataDir) args.push('--local-data-dir', config.localDataDir)
  return args
}

function childEnv(config) {
  const env = { ...process.env }
  const key = process.env[config.userKeyEnv]
  if (key) env.TDAI_USER_KEY = key
  return env
}

function runProcess(config, args, payload, captureStdout = false) {
  return new Promise((resolve, reject) => {
    const child = spawn(
      config.command,
      ['run', '--script', config.script, ...args, ...scopeArgs(config)],
      {
        stdio: ['pipe', captureStdout ? 'pipe' : 'ignore', 'pipe'],
        windowsHide: true,
        env: childEnv(config),
      },
    )
    let error = ''
    let output = ''
    if (captureStdout) {
      child.stdout.setEncoding('utf8')
      child.stdout.on('data', (chunk) => { output += chunk })
    }
    child.stderr.setEncoding('utf8')
    child.stderr.on('data', (chunk) => { error += chunk })
    child.on('error', reject)
    child.on('close', (code) => {
      if (code) reject(new Error(error.slice(-1000) || `bridge exited ${code}`))
      else resolve(output.trim())
    })
    child.stdin.end(JSON.stringify(payload))
  })
}

function runBridge(config, payload) {
  void runProcess(
    config,
    [
      '--canonical-hook', '--platform', 'dsh',
      '--batch-turns', String(config.batchTurns),
      '--idle-seconds', String(config.idleSeconds),
    ],
    payload,
  ).catch((cause) => console.warn('[tdai-memory] bridge failed', cause.message))
}

async function runTool(config, name, args) {
  let timer
  try {
    const output = await Promise.race([
      runProcess(config, ['--json-tool', name], args, true),
      new Promise((_, reject) => {
        timer = setTimeout(
          () => reject(new Error(`TDAI tool timed out after ${config.toolTimeoutSeconds}s`)),
          config.toolTimeoutSeconds * 1000,
        )
      }),
    ])
    return output || '{}'
  } finally {
    if (timer) clearTimeout(timer)
  }
}

function registerTool(ctx, config, definition) {
  const { operation, ...tool } = definition
  ctx.tools.register(defineTool({
    ...tool,
    output: {
      schema: { type: 'string' },
      render: (_args, value) => [{ type: 'text', text: value }],
    },
    execute: (args) => runTool(config, operation, args),
  }))
}

function messages(session) {
  return session.deriveMessages().flatMap((message) => {
    if (message?.role !== 'user' && message?.role !== 'assistant') return []
    return [{ role: message.role, content: message.content }]
  })
}

export function apply(ctx, config) {
  if (config.captureEnabled) {
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

  if (!config.toolsEnabled) return
  registerTool(ctx, config, {
    operation: 'memory_search',
    name: 'tdai_memory_search',
    description: 'Search durable TDAI facts, preferences, constraints, and decisions.',
    parameters: {
      query: { type: 'string', required: true },
      limit: { type: 'number' },
      memory_type: { type: 'string' },
      task_id: { type: 'string' },
    },
  })
  registerTool(ctx, config, {
    operation: 'conversation_search',
    name: 'tdai_conversation_search',
    description: 'Search raw TDAI conversation history for exact wording or context.',
    parameters: {
      query: { type: 'string', required: true },
      limit: { type: 'number' },
      session_id: { type: 'string' },
      task_id: { type: 'string' },
    },
  })
  registerTool(ctx, config, {
    operation: 'core_memory_read',
    name: 'tdai_core_memory_read',
    description: 'Read compact TDAI core/persona memory for broad context restoration.',
    parameters: { task_id: { type: 'string' } },
  })
  registerTool(ctx, config, {
    operation: 'memory_status',
    name: 'tdai_memory_status',
    description: 'Check authenticated TDAI L0-L3 counts and latest timestamps.',
    parameters: { task_id: { type: 'string' } },
  })
  registerTool(ctx, config, {
    operation: 'remember',
    name: 'tdai_remember',
    description: 'Store one durable, non-secret note in the configured remote TDAI memory.',
    parameters: {
      note: { type: 'string', required: true },
      session_id: { type: 'string' },
      task_id: { type: 'string' },
    },
  })
}
