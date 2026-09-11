# 多 Agent 平台适配

本项目用一个 `server.py` 承担过滤、脱敏、去重、缓冲和 TDAI 写入；各平台适配器只把自己的生命周期事件转换成统一消息格式。正常模型请求不经过 TDAI。

## 统一配置

每个运行平台的进程都需要下列环境变量：

```text
TDAI_USER_KEY
TDAI_MEMORY_ENDPOINT
TDAI_SERVICE_ID
TDAI_TEAM_ID
TDAI_AGENT_ID
TDAI_USER_ID
TDAI_MEMORY_SCRIPT=/absolute/path/to/useTDAI/scripts/server.py
TDAI_UV=/absolute/path/to/uv
```

`TDAI_USER_KEY` 只放用户环境或密钥管理器。建议 Codex、Claude Code、Hermes、OpenCode、Pi 和 DeepSeek Harness 各自使用独立 TDAI Agent ID，然后在工作台把需共享的 Chat Memory 显式绑定给其他 Agent。这样既能跨 Agent 检索，又能保留来源、权限和可撤销性。

`TDAI_LOCAL_DATA_DIR` 可选，用来改变本地 checkpoint/pending 目录。默认仍是 `~/.codex/tdai-memory`，但 checkpoint 已按平台和 session 双重隔离。

## 事件映射

| 平台 | 常规轮次 | 压缩/会话边界 | 适配方式 |
| --- | --- | --- | --- |
| Codex | `Stop` | `PreCompact` | Codex Hook |
| Claude Code | `Stop` | `PreCompact`, `SessionEnd` | Claude Code Hook |
| Hermes | `post_llm_call` | `on_session_finalize` | Hermes shell hook |
| OpenCode | `session.idle` | `session.compacted`, `session.deleted` | TypeScript plugin |
| Pi | `agent_settled` | `session_before_compact`, `session_shutdown` | TypeScript extension |
| DeepSeek Harness | completed `turn/end` | `session/disposed` | Cordis plugin |

常规轮次只写本地队列；累计五个 assistant 轮次或空闲三分钟后异步上传。压缩和会话边界会强制刷新。

## Claude Code

把 `adapters/claude-code/settings.example.json` 中的 Hook 合并到用户或项目的 Claude Code settings，替换 `uv` 和脚本绝对路径。不要覆盖已有 `hooks`，而是合并同名数组。

按需召回使用标准 stdio MCP，命令为：

```text
uv run --script /absolute/path/to/useTDAI/scripts/server.py
```

## Hermes

合并 `adapters/hermes/config.example.yaml` 到 `~/.hermes/config.yaml`，替换绝对路径。首次启用 shell hook 时按 Hermes 提示审核，然后用 `hermes hooks doctor` 检查。该示例同时注册 `tdai_memory` MCP，因此写入和按需召回都可用。

## OpenCode

把 `adapters/opencode/tdai-memory.ts` 复制到全局 `~/.config/opencode/plugins/` 或项目 `.opencode/plugins/`。设好统一环境变量并重启 OpenCode。适配器在 session idle 时取得消息快照，不会拦截模型请求。

召回可在 `opencode.json` 中把同一条 `uv run --script .../server.py` 配成本地 MCP server。

## Pi

把 `adapters/pi/tdai-memory.ts` 复制到 `~/.pi/agent/extensions/`，设好统一环境变量后重启 Pi。扩展先在 `agent_end` 保留消息，等 `agent_settled` 确认没有后续重试/跟进时才入队。

Pi 的自动写入不需要 MCP。如果当前 Pi 发行版未内置 MCP client，按需召回需配合 Pi MCP 扩展来启动本项目的 stdio server；不应为此把模型 provider 指向 TDAI Proxy。

## DeepSeek Harness

1. 先把 `adapters/dsh/cordis.patch.yml` 中的 `script` 改成真实绝对路径。
2. 在 DSH 所在机器执行 `dsh plugin --profile <profile> add /absolute/path/to/useTDAI/adapters/dsh`。
3. 检查该 profile 生成的 Cordis 配置，并确保 DSH 进程已继承统一环境变量。
4. 重启对应 CLI/Web profile，用一轮完成对话检查 pending/checkpoint。

按需召回使用 DSH 的 `@deepseek-ai/dsh-mcp-client`，将本项目 `server.py` 配为 stdio MCP server。写入插件和 MCP client 是两条独立旁路：任何一条故障都不应影响 DSH 正常问答。

## 验证和故障隔离

在每台机器上先执行：

```bash
uv run --script scripts/server.py --parser-test
uv run --script scripts/server.py --buffer-test
uv run --script scripts/server.py --self-test
```

再从目标平台完成一轮有意义的对话，观察 `~/.codex/tdai-memory/pending/`；等满五轮/三分钟，或触发一次压缩/会话结束，再检查 `checkpoints.json` 和 TDAI 工作台 L0。故障记录在 `hook-errors.log`，待处理队列会保留以便后续重试。

