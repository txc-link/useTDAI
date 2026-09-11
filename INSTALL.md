# 安装与迁移

## 前提

- Codex CLI 或 Codex Desktop；
- Python 3.11 或更高版本；
- 可执行的 [`uv`](https://docs.astral.sh/uv/)；
- 可通过 HTTPS 访问、兼容 `/v3` API 的 TDAI MemoryCore；
- TDAI 用户密钥，以及服务/团队/Agent/用户的隔离标识。

## 1. 安装目录

从 GitHub 克隆时，将目录放到 Codex 可发现的 Skill 路径：

### Windows PowerShell

```powershell
git clone https://github.com/txc-link/useTDAI.git "$env:USERPROFILE\.codex\skills\tdai-memory"
```

### Linux/macOS

```bash
git clone https://github.com/txc-link/useTDAI.git "${CODEX_HOME:-$HOME/.codex}/skills/tdai-memory"
```

如果目标目录已经存在，请使用正常的 Git 更新流程，不要嵌套克隆。

## 2. 保存用户密钥

密钥不要写入 `config.toml`、`.env`、Skill 文件或 Git。

### Windows PowerShell

```powershell
[Environment]::SetEnvironmentVariable(
  "TDAI_USER_KEY",
  "<YOUR_USER_KEY>",
  "User"
)
```

设置后完全退出并重新打开 Codex。桥接器在 Windows 上也会读取当前用户注册表中的用户级环境变量，以处理父进程环境尚未刷新的情况。

### Linux

用操作系统的密钥管理器、服务管理器或受保护的登录环境提供：

```bash
export TDAI_USER_KEY='<YOUR_USER_KEY>'
```

不要将真实值提交到 shell 配置仓库。

### macOS Codex Desktop

macOS 的 Hook 机制可以使用，但从 Finder/Dock 启动的 Codex Desktop 通常不会读取
`.zshrc` 中的 `export`。应在启动 Codex 之前，把密钥放进当前登录会话的
`launchd` 环境：

```bash
launchctl setenv TDAI_USER_KEY '<YOUR_USER_KEY>'
```

完全退出并重新打开 Codex 后生效。`launchctl setenv` 不会把密钥写入本仓库，
但它也不是加密存储；重启或重新登录后如果变量消失，需要重新注入。长期方案应由
Keychain/密码管理器在登录时注入，避免把真实密钥明文写入可同步的 shell 配置或
LaunchAgent 文件。

## 3. 配置 MCP 和 Hook

参考 [config.example.toml](config.example.toml)，把 `tdai_memory` MCP 块和 `PreCompact` Hook 合并到 `~/.codex/config.toml`。需要 Wiki、CodeGraph 或 Skill 时，再配置独立的 `tdai_knowledge` MCP；它默认关闭。

必须替换：

- `command`：本机 `uv` 的路径或命令名；
- `args` 中的 `server.py` 绝对路径；
- `TDAI_MEMORY_ENDPOINT`；
- TDAI 的 Service、Team、Agent 和 User 标识。

先在当前机器执行 `command -v uv`（Windows 用 `Get-Command uv`）取得真实绝对
路径。默认安装位置通常是 Windows 的 `%USERPROFILE%\.local\bin\uv.exe`、
macOS 的 `$HOME/.local/bin/uv`，Homebrew 安装则可能是
`/opt/homebrew/bin/uv`（Apple Silicon）或 `/usr/local/bin/uv`（Intel）；以命令
实际输出为准。Skill 路径在 macOS 通常是
`/Users/<用户名>/.codex/skills/tdai-memory/scripts/server.py`，Linux 才通常是
`/home/<用户名>/...`。

`PreCompact` 是独立命令进程，不会继承
`[mcp_servers.tdai_memory.env]`。因此 Hook 命令必须使用可执行文件的绝对路径，
并通过 `--endpoint`、`--service-id`、`--team-id`、`--agent-id` 和
`--user-id` 显式传入非敏感隔离参数。用户密钥仍只从 `TDAI_USER_KEY`
读取，不能写进 Hook 命令或配置文件。

Hook 处理器跨平台都使用 `command` 字段；Windows 也把 `uv.exe` 和脚本的
Windows 绝对路径直接写进 `command`。不要添加 `command_windows`：当前 Codex
Hook 配置并不接受该字段。

`TDAI_TASK_ID` 是可选项。只有当这个 Codex 配置长期专用于同一个 TDAI Task 时才写入 MCP 环境；日常多任务使用应留空，在 `remember`、`memory_search` 或 `conversation_search` 调用时按需传入。

保持主配置的 `model_provider = "openai"`，不要为了使用本 Skill 把官方模型流量切换到 TDAI Proxy。

`TDAI_KNOWLEDGE_ENDPOINT` 必须指向 MemoryKnowledge 服务根地址。不同部署的内部默认端口可能不同，应从实际 Compose/服务配置确认，不要猜端口。该服务只应固定访问你配置的地址；桥接器不会跟随 API 返回的其他服务 URL。

如果 `[features]` 已存在，只添加或合并 `hooks = true`，不要创建重复表。

同一份 Hook 逻辑支持 Windows、macOS 和 Linux，但配置与信任是逐机的：

- 每台机器填写自己的 `uv` 与 Skill 绝对路径；
- 建议每个 Codex 实例使用独立的 TDAI Agent ID，再通过工作台资产绑定共享记忆；
- Hook 信任绑定完整定义的哈希，换路径、Agent ID 或命令后必须在该机器重新打开
  `/hooks` 审核并信任；
- 本地去重检查点也逐机保存在 `~/.codex/tdai-memory/checkpoints.json`，不会随 Git
  同步。

## 4. 验证

在 Skill 目录执行：

```bash
uv run --script scripts/server.py --self-test
uv run --script scripts/server.py --parser-test
uv run --script scripts/smoke_test.py
uv run --script scripts/knowledge_smoke_test.py
codex mcp list
```

预期结果：

- `self-test` 显示鉴权以及 L0–L3 只读接口通过；没有 L2/L3 内容时返回空结果也属于通过；
- `parser-test` 显示系统/环境内容被排除、凭据被脱敏；
- `smoke_test.py` 显示十一个 Memory MCP 工具并完成当前记忆、状态和共享绑定只读检查；
- `knowledge_smoke_test.py` 使用临时本地模拟服务验证五个 Knowledge MCP 工具；
- `codex mcp list` 中 `tdai_memory` 为 `enabled`。

这些测试不会调用 `remember` 或 `capture_transcript`，因此不会产生测试记忆。真实 Knowledge 服务配置完成后，可额外运行 `uv run --script scripts/knowledge_server.py --self-test`，它也只读。

## 5. 启用

完全重启 Codex。在该机器首次 Hook 信任提示中检查并启用 `PreCompact`。之后：

- 明确要求记忆时，Skill 可调用 `remember` 立即保存；
- 自动或手动压缩上下文前，Hook 调用 `capture_transcript`；
- 搜索只在跨任务上下文有价值时按需执行。

## 更新

进入 Skill 仓库后正常拉取新版本，然后重新执行四项测试并重启 Codex。更新前建议备份 `~/.codex/config.toml`。

## 卸载

1. 从 `~/.codex/config.toml` 删除 `[mcp_servers.tdai_memory]`、其 `.env` 子表以及对应的 `[[hooks.PreCompact]]`；如已配置，也删除 `[mcp_servers.tdai_knowledge]` 及其 `.env` 子表；
2. 删除 Skill 目录；
3. 如不再使用，删除用户级 `TDAI_USER_KEY`；
4. 可选删除本地检查点 `~/.codex/tdai-memory/checkpoints.json`。

卸载本地 Skill 不会自动删除 TDAI 服务端已经保存的记忆。

## 常见问题

### HTTP 401

检查 `TDAI_USER_KEY` 是否属于当前用户，并确认反向代理接受 `Authorization: Bearer <key>`。Windows 上修改用户环境变量后应重启 Codex。

### MCP 启动失败

检查 `uv` 和 `server.py` 是否使用绝对路径，再单独运行 `--self-test` 查看错误。
macOS Codex Desktop 还应执行 `launchctl getenv TDAI_USER_KEY`，只确认结果非空，
不要把输出粘贴到日志或问题报告中。

### 检索为空

确认 MCP 配置中的 Service、Team、Agent、User 标识与写入时一致。空结果不等于连接失败。

先调用 `memory_status` 区分“连接/隔离范围错误”和“当前层确实没有记录”。L0/L1 最近时间用于判断数据是否继续推进；异步提炼失败原因仍应在工作台生成日志中查看。

### 工作台绑定了另一个 Agent 的 Chat Memory，但搜不到

先调用 `shared_memory_list`，确认目标记忆块出现在当前 Agent 的固定绑定中。然后用返回的 `asset_id` 调用 `shared_memory_search` 搜 L1；如果目标只有原始对话、尚未生成 L1，则改用 `shared_conversation_search` 搜 L0。共享工具只接受列表中已绑定的资产，不接受任意 `agent_id`。

### Knowledge MCP 启动失败

先保持 `enabled = false`，确认实际 MemoryKnowledge 根地址后运行 `knowledge_server.py --self-test`。Wiki/CodeGraph API 使用服务隔离头，通常不具备与 MemoryCore 用户 Key 等价的公网鉴权能力，因此不要仅凭 HTTPS 就认定它适合直接暴露。

### 自动写入没有发生

`PreCompact` 只会在自动压缩或手动 `/compact` 前触发。普通消息、切换任务或关闭窗口不会触发该 Hook。

Hook 在任务启动时加载。新增或修改配置、重新审核信任后，应完全重启 Codex，
再创建或重新打开任务进行验证；配置生效前已经完成的压缩不会被事后补传。

若 `~/.codex/tdai-memory/checkpoints.json` 从未生成，说明 Hook 尚未成功完成；
优先检查 Hook 是否使用绝对 `uv` 路径，以及命令是否显式提供完整隔离参数。
空 transcript 或过滤后没有可上传消息时不会创建 checkpoint，因此应使用至少
包含一条普通用户消息和一条最终回答的非敏感测试任务验证。
Codex 的内部审批/审查子任务可能自行压缩，但不会运行用户任务的 Hook，也不应写入长期记忆。

### `remember` 成功但 `memory_search` 暂时搜不到

`remember` 首先写入 L0；L1/L2/L3 由服务端异步管线按阈值和提取提示词生成。先用 `conversation_search` 或工作台确认 L0，再观察 `/health` 的 pipeline worker 和生成日志。不要通过重复写入同一事实来催促管线。

### 记忆出现在错误的 Agent 或默认桶

确认 `TDAI_TEAM_ID`、`TDAI_AGENT_ID` 和 `TDAI_USER_ID` 都已配置。MemoryCore v3 的隔离字段必须进入数据请求体；本桥接器会自动添加，缺少时直接拒绝启动，避免静默写入 default 桶。

### 公网部署

不要把 MemoryCore、MemoryKnowledge 或 MemoryProxy 的管理端点直接暴露到公网。HTTPS 只保护传输，不替代鉴权和网络隔离；优先使用 VPN/零信任网络或反向代理 IP 白名单，并将 Panel 与数据面分开限制。Agent 使用普通业务用户 Key，不使用系统管理员 Key。更完整的风险与检查项见 [运行实践手册](references/operating-playbook.md)。
