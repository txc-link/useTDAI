import type { Plugin } from "@opencode-ai/plugin"
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
        "--canonical-hook", "--platform", "opencode",
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

function normalizeMessages(response: any) {
  const rows = Array.isArray(response) ? response : response?.data
  if (!Array.isArray(rows)) return []
  return rows.flatMap((row: any) => {
    const role = row?.info?.role
    if (role !== "user" && role !== "assistant") return []
    const content = (row?.parts || [])
      .filter((part: any) => part?.type === "text" && typeof part.text === "string")
      .map((part: any) => part.text)
      .join("\n")
    return content ? [{ role, content }] : []
  })
}

export const TDAIMemoryPlugin: Plugin = async ({ client }) => {
  const logFailure = async (error: unknown) => {
    await client.app.log({
      body: {
        service: "tdai-memory",
        level: "warn",
        message: error instanceof Error ? error.message : String(error),
      },
    }).catch(() => undefined)
  }

  const sessionMessages = async (sessionID: string) => {
    const api: any = client.session
    try {
      return await api.messages({ sessionID })
    } catch {
      return await api.messages({ path: { id: sessionID } })
    }
  }

  return {
    event: async ({ event }) => {
      const data: any = event
      const sessionID = data?.properties?.sessionID || data?.data?.sessionID
      if (typeof sessionID !== "string") return

      if (event.type === "session.idle") {
        try {
          const response = await sessionMessages(sessionID)
          void runBridge({
            hook_event_name: "session.idle",
            session_id: sessionID,
            messages: normalizeMessages(response),
          }).catch(logFailure)
        } catch (error) {
          await logFailure(error)
        }
      } else if (event.type === "session.compacted" || event.type === "session.deleted") {
        void runBridge({
          hook_event_name: event.type,
          session_id: sessionID,
          messages: [],
          force: true,
        }).catch(logFailure)
      }
    },
  }
}
