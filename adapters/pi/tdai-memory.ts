import type { ExtensionAPI } from "@earendil-works/pi-coding-agent"
import { spawn } from "node:child_process"

const bridgeScript = process.env.TDAI_MEMORY_SCRIPT
const uv = process.env.TDAI_UV || "uv"

function runBridge(payload: unknown) {
  if (!bridgeScript) return Promise.reject(new Error("TDAI_MEMORY_SCRIPT is not set"))
  return new Promise<void>((resolve, reject) => {
    const child = spawn(
      uv,
      [
        "run", "--script", bridgeScript,
        "--canonical-hook", "--platform", "pi",
        "--batch-turns", "5", "--idle-seconds", "180",
      ],
      { stdio: ["pipe", "ignore", "pipe"], windowsHide: true },
    )
    let error = ""
    child.stderr.setEncoding("utf8")
    child.stderr.on("data", (chunk) => { error += chunk })
    child.on("error", reject)
    child.on("close", (code) => code === 0 ? resolve() : reject(new Error(error.slice(-1000))))
    child.stdin.end(JSON.stringify(payload))
  })
}

function sessionID(ctx: any) {
  return ctx.sessionManager.getSessionFile() || `ephemeral:${ctx.cwd}`
}

function normalizeMessages(messages: any[]) {
  return (messages || []).flatMap((message: any) => {
    if (message?.role !== "user" && message?.role !== "assistant") return []
    return [{ role: message.role, content: message.content }]
  })
}

export default function tdaiMemory(pi: ExtensionAPI) {
  const latest = new Map<string, any[]>()

  pi.on("agent_end", async (event, ctx) => {
    latest.set(sessionID(ctx), normalizeMessages(event.messages as any[]))
  })

  pi.on("agent_settled", async (_event, ctx) => {
    const id = sessionID(ctx)
    const messages = latest.get(id) || []
    latest.delete(id)
    void runBridge({
      hook_event_name: "agent_settled",
      session_id: id,
      messages,
    }).catch(() => undefined)
  })

  pi.on("session_before_compact", async (_event, ctx) => {
    await runBridge({
      hook_event_name: "PreCompact",
      session_id: sessionID(ctx),
      messages: [],
      force: true,
    }).catch(() => undefined)
  })

  pi.on("session_shutdown", async (_event, ctx) => {
    await runBridge({
      hook_event_name: "session_shutdown",
      session_id: sessionID(ctx),
      messages: [],
      force: true,
    }).catch(() => undefined)
  })
}
