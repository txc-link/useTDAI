# TDAI 运行实践手册

本手册回答“别人实际怎么用，以及哪些做法值得复用”。资料核对时间为 2026-09-10，官方 `feat/server_team` 分支提交为 `906b5823b5106eed8f842b62f16d23228838149a`。TDAI 仍在快速迭代，下面明确区分官方能力、社区个案和本 Skill 的架构建议。

主要依据：

- [腾讯官方仓库与 Team Memory 设计](https://github.com/TencentCloud/TencentDB-Agent-Memory)
- [官方安装和多 Agent 接入说明](https://github.com/TencentCloud/TencentDB-Agent-Memory/blob/feat/server_team/INSTALL.md)
- [官方 MemoryCore V3 API](https://github.com/TencentCloud/TencentDB-Agent-Memory/blob/feat/server_team/MemoryCore/v3-api-memorycore-doc.md)
- [社区案例：一套 TDAI 服务供 Hermes、Claude Code、Codex 共用](https://guancyxx.cn/en/blog/tdai-agent-memory-knowledge-stack)
- [公开 Issues：抽取可靠性、运维静默失效和备份缺口](https://github.com/TencentCloud/TencentDB-Agent-Memory/issues)

## 先选接入形态

| 形态 | 适合什么 | 收益 | 代价 |
| --- | --- | --- | --- |
| 当前 Codex 旁路 MCP | 官方 Codex 性能和兼容性优先 | 模型流量不经过 TDAI；记忆故障不阻断正常问答 | 召回/写入按需触发；资产通过独立只读 Knowledge MCP 按需获取 |
| 官方 Memory Proxy | 希望自动 Team/Agent/Task 绑定、L0 捕获和资产注入 | 一条主链路自动完成注入与归档 | Proxy 成为延迟和兼容性依赖；Codex 首次选择还有运行模式限制 |
| 框架原生插件 | Hermes/OpenClaw 等已有成熟生命周期 Hook | 可在每轮或阈值处自动召回、提炼 | 更新后插件可能脱离，必须做端到端监控 |

本机 Codex 延续旁路 MCP。不要为了追求“全自动”静默切换官方模型 provider。

## 真实部署反复出现的四分法

不要把所有内容都当 Chat Memory：

| 信息 | 放哪里 | 例子 |
| --- | --- | --- |
| 事实、偏好、约束、决策 | Chat Memory L0–L3 | “生产库迁移必须保留回滚窗口” |
| 可重复执行的流程 | Skill | 发布检查、故障处理、代码审查流程 |
| 人类维护的文档知识 | Wiki | 设计文档、会议纪要、研究笔记 |
| 代码结构和影响关系 | CodeGraph | 符号、调用链、变更影响面 |

官方设计和社区案例都采用这种分层。把流程文档混进事实记忆会造成召回污染；把一次决策做成 Skill 又会让流程缺乏适用边界。

## 单一真源与一向同步

社区多 Agent 案例采用的有效模式是：Obsidian/Git 仍是唯一真源，TDAI 是可搜索、可重建的副本；通过内容哈希做增量同步，不做双向回写。

本环境建议：

1. Obsidian 保存人类可编辑的项目知识与本体说明。
2. Git 保存 Skill 和可版本化的流程。
3. TDAI Wiki/Skill 只作为检索副本；删除后能够从真源重建。
4. Chat Memory 保存对话中形成、尚未进入正式文档的事实和决策。
5. 当记忆变成正式规范时，把它提升到 Obsidian/Git，并在原记忆中保留来源或替代关系。

不要让 TDAI 与 Obsidian 双向自动覆盖。冲突时必须由人确定哪一边是权威。

## Agent 的正确粒度

Agent 是“身份 + 规则 + 记忆/资产装配”，不是某个客户端名称。Codex、OpenCode、Hermes、DSH 可以共享同一个业务 Agent，只要它们服务同一项目和同一角色。

推荐：

- 跨工具个人大脑：只放稳定偏好、身份和跨项目规则。
- 每个长期项目一个 Project Agent：隔离 L2 场景和项目 Persona。
- Reviewer/Researcher：只有行为规则或资产装配确实不同才拆分。
- 短任务：创建 Task，不创建新 Agent。

不要建立“Codex Agent”“Hermes Agent”这种按客户端拆分的镜像身份，否则跨工具共享会被人为切断。

## Agent Loadout 要少而准

官方示例给 Scout、Builder、Reviewer 配不同资产。实务上应遵循最小装配：

- Builder：产品 Wiki + 当前项目 CodeGraph + 交付 Skill。
- Reviewer：事故记忆 + 当前项目 CodeGraph + 发布检查 Skill。
- Researcher：研究 Wiki + 来源核查 Skill；通常不需要代码资产。
- 个人大脑：Chat Memory 为主，不默认绑定全部 Wiki/CodeGraph。

“可见”不等于“每轮全部注入”。当前旁路 Codex 默认只启用 Chat Memory MCP；启用独立 Knowledge MCP 后，也必须先列资产/工具，再只读检索一个相关资产，不能把全部内容塞进每轮上下文。

## 一次工作中的召回和写入节奏

### 开始长期任务

1. 只有历史上下文明显有价值时才召回。
2. `core_memory_read` 读取 L3 方向性信息。
3. `scenario_list` 先看 L2 目录；只对相关项调用 `scenario_read`。
4. 用 `memory_search` 查精确事实、约束和决策。
5. 必须核对原话或时间时才用 `conversation_search`。

任何一层已经足够，就停止向下展开。这样比每轮塞完整历史更接近官方“少拿但拿对”的设计。

### 工作进行中

- 用户明确说“记住”时调用 `remember`。
- 架构决策、稳定偏好、已验证流程或清晰交接状态可以写。
- 临时报错、命令日志、未验证猜测、凭据和系统提示不写。
- 用 Task ID 关联持续工作；不要把一次 Task ID 固定给所有日常会话。

### 里程碑和压缩

- 里程碑完成时写一条短、独立、带范围的结论，不倾倒整个 transcript。
- PreCompact 捕获用户消息和最终答案，是防止长会话丢失的兜底，不是每轮归档器。
- `remember`/PreCompact 首先产生 L0；L1/L2/L3 是异步提炼，可能因阈值或提示词判断而暂不生成。

建议记忆格式：

```text
[项目/范围] 已确认结论；适用条件；验证依据；确认日期；若有则注明替代了哪条旧结论。
```

## Task 怎么用才有价值

Task 适合有明确目标、持续多轮并会产生多个决策的工作。它提供记忆组织维度，不负责执行。

- 同一交付过程共用一个 Task ID。
- 新目标或不同验收标准建立新 Task。
- 只是一次答疑可以不建 Task。
- Task 结束时更新状态，并把最终结论提升为正式文档或稳定记忆。

官方文档指出，不绑定 Task 仍可工作，但 L2/L3 会少掉 Task 维度。旁路 MCP 不经过 Proxy，因此工作台中的“实际参与 Agent/User”也不会自动出现。

## 批量导入的现实做法

社区案例的经验可以作为保守起点，不应当视为产品保证：

- 先导入主文档或 `SKILL.md`，发现召回缺口后再补 references，避免一次性摄入全部文件。
- 不同语义的资料建不同 Wiki，例如个人笔记与 Skill 库分开，减少 BM25 同主题竞争。
- 小批量试跑并回读抽查；API 返回成功不等于内容完整、可检索。
- 结构化输出能力差的抽取模型可能产生 malformed JSON，导致 L1 静默少抽或不抽；先用代表性中文、代码和 Markdown 样本验证。
- 超大文件先分块。社区案例报告过 1 MiB 请求体和约 50,000 字符正文限制；具体阈值要以当前服务端代码和日志为准。
- 同名 Skill 重新导入可能产生重复资产；更新前先检查版本/现有条目，不要假定覆盖。

## 不能只看 `/health`

真实故障中出现过“Agent 正常回答，但记忆已停止写入”。至少同时观察：

1. `/health` 是否可达。
2. 最近一次 L0 时间是否随真实对话前进。
3. L1 数量或最近更新时间是否继续变化。
4. L2/L3 pipeline 的 consumed/completed/failed 指标和生成日志。
5. 从另一个会话召回一条带唯一措辞的非敏感测试事实。

本 Skill 的 `memory_status` 可快速查看当前 Service/Team/Agent/User/Task 隔离范围、L0–L3 数量及最近的 L0/L1/L3 时间。它证明数据面可读，但不证明异步抽取模型健康；自动切换到备用在线模型时，应在服务端生成日志中记录实际 provider、模型、触发原因和重试结果，避免静默降级。

升级 Hermes/OpenClaw 后还要验证插件仍被发现；只看主 Agent 服务存活不足以证明记忆链路正常。批量导入时记录输入数、成功数、失败数，并回读抽样。

## 备份与恢复边界

截至本手册核对的版本，官方仓库仍有开放 Issue 请求完整的资产备份/恢复流程。不要假定只复制一个 SQLite 文件就能保留治理元数据、向量索引、Wiki 和 CodeGraph。

- 保留 Obsidian/Git 真源，确保 Wiki/Skill 可重建。
- 备份前记录 TDAI 版本、存储后端、容器编排配置和卷清单。
- 对 Docker 卷或数据库做一致性快照；恢复必须先在隔离环境演练。
- 恢复后验证身份/ACL、L0–L3、Skill、Wiki、CodeGraph 和搜索结果。
- 在官方导出/恢复流程成熟前，不把 TDAI 作为唯一不可替代的数据副本。

## 公网安全底线

TDAI 面向自托管内网的组件不应直接裸露到公网。公开安全 Issue [#672](https://github.com/TencentCloud/TencentDB-Agent-Memory/issues/672) 报告了 MemoryProxy 管理端点鉴权绕过以及 MemoryKnowledge 的 SSRF/参数注入风险；对应修复 PR 在核对时仍未合并。

因此：

- MemoryCore、MemoryKnowledge、Proxy 管理接口只监听内网/回环或受 VPN、零信任网络、IP ACL 保护的入口。
- HTTPS 是必要条件，但不能代替访问控制。
- Panel 登录入口与数据/API 入口分开限制，管理接口不经公网转发。
- Agent 使用普通业务用户 Key；系统管理员 Key 只用于管理，不给 Codex/Hermes/DSH。
- 为不同客户端或用途创建可撤销、可过期的 Key，定期轮换并检查 `last_used_at`。
- 在安全修复版本确认前，不允许不受信任用户提交 Git/Wiki 抓取 URL。

## 适合当前环境的落地顺序

1. 保持 Codex 直连 OpenAI，TDAI 仅做旁路 Chat Memory。
2. 用同一业务 Agent ID 让 Codex/OpenCode/Hermes/DSH 共享项目记忆，而不是按客户端拆 Agent。
3. 用一个测试 Task 验证 L0 写入、L1 提炼、L2/L3 生成和跨客户端召回。
4. 将 Obsidian 项目资料导入独立 Wiki，但继续以本地 Vault 为真源。
5. Skill 库单建 Wiki/资产域，小批量导入并回读验证。
6. Wiki/CodeGraph 要给 Codex 使用时，启用独立只读 `tdai_knowledge` MCP，并先跑离线模拟与真实只读自检；不要把模型主流量切到 Proxy 作为默认方案。
7. 公网入口收紧后，再扩展到更多客户端和自动化同步。
