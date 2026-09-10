# TDAI 工作台使用指南

本指南针对 TencentDB Agent Memory 的 Team Memory / Memory Panel 工作台。界面名称和限制核对自官方 `feat/server_team` 分支 `906b582`（2026-09-10）。不同版本的文字或入口可能略有变化，但实体关系和 API 语义应以服务端版本为准。

官方资料：

- <https://github.com/TencentCloud/TencentDB-Agent-Memory>
- <https://github.com/TencentCloud/TencentDB-Agent-Memory/blob/feat/server_team/MemoryPanel/panel-api-doc.md>
- <https://github.com/TencentCloud/TencentDB-Agent-Memory/blob/feat/server_team/MemoryCore/v3-api-memorycore-doc.md>

## 先理解六个对象

| 对象 | 用途 | 关键点 |
| --- | --- | --- |
| User | 人的身份和权限 | userKey 属于 User，不属于 Agent |
| Team | 共享、权限和资产的边界 | 先选 Team，再管理 Agent/Task/资产 |
| Agent | 一个长期角色和独立记忆范围 | 创建后自动拥有自己的 Chat Memory |
| Task | 工作事项及参与关系 | 是记忆治理元数据，不是任务执行器或调度器 |
| Asset | Skill、Wiki、CodeGraph、Chat Memory | 可设 private/team/restricted 并分配给 Agent |
| Memory | L0 原始对话 → L1 原子 → L2 场景 → L3 Persona | 从证据逐层提炼，不等同于普通向量库 |

推荐顺序：`User/API Key → Team → Agent → 资产绑定 → Task → 外部客户端带身份工作 → 工作台复核记忆`。

如何把这些对象组织成长期可维护的系统，而不只是“把面板填满”，见 [运行实践手册](operating-playbook.md)。

## 创建 Agent

1. 在页面左上角 Team 切换器中选定目标 Team。
2. 打开“组织与权限 → Agents 管理”。
3. 点击“创建 Agent”。Agent 严格属于当前 Team，Owner 固定为当前登录用户，创建时不能转交。
4. 填写以下字段：

   - “名字”：唯一必填项，例如“跨工具项目大脑”或“PR Reviewer”。
   - “一句话描述”：用于人类识别，不要堆放执行规则。
   - “角色定位 prompt”：说明它是谁、服务什么目标、负责什么边界。
   - “规则固定 prompt”：每次对话都要遵守的硬约束，使用编号列表。

5. 按需展开原子能力并勾选 Wiki、CodeGraph、Skill、Chat Memory。
6. 创建后复制并保存生成的 `agent_id`，外部客户端必须使用同一个 ID 才会进入同一 Agent 记忆范围。

资源绑定不是同一种语义：

- Wiki/CodeGraph 是引用分配，多个 Agent 可读取同一团队资产。
- Chat Memory 是固定记忆绑定；Agent 自己的 Chat Memory 不需要再次绑定。
- Skill 在创建 Agent 时会 fork 成该 Agent 拥有的独立副本，之后不会自动跟随源 Skill 更新。

谨慎删除 Agent：当前 Panel 的级联删除会先删除它拥有的 active Skill，再归档 Agent，并清理该 Agent 的 Chat Memory。需要保留历史时优先归档或先导出资产。

## Agent 模板

创建 Agent 弹窗中的“保存为模板”只保存描述、角色 prompt 和规则 prompt，保存在当前浏览器本地；清理浏览器缓存会丢失，自定义模板也不会自动同步到另一台电脑。

Team 管理里的“默认 Agent 模板”是另一种能力：仅系统管理员可配置，用于新成员加入时自动生成专属 Agent，只对之后加入的成员生效，不会更新现有 Agent，并且只能预挂载 visibility=team 的公共资产。

## Agent prompt 与 Memory prompt 不要混淆

| 类型 | 控制什么 | 建议 |
| --- | --- | --- |
| Agent 角色/规则 prompt | Agent 的身份、职责和行为约束 | 日常在创建/编辑 Agent 时维护 |
| L1/L2/L3 Memory Prompt | 记忆抽取、场景聚合、Persona 生成规则 | 属于记忆管线管理；除非要调整抽取语义，否则使用内置默认值 |

Memory Prompt 支持按实例、Team 或指定 Agent 生效。覆盖错误会直接改变后续记忆生成质量，应先在测试 Agent 验证，再逐级推广。

当前 Codex 旁路集成不会把工作台的 Agent prompt 注入官方 Codex 模型请求；它只使用 `agent_id` 做记忆隔离。这保证 TDAI 不会覆盖 Codex 的系统提示。Codex 的实际记忆行为由本 Skill 和 MCP 工具控制。

同理，工作台中绑定的 Skill、Wiki 和 CodeGraph 也不会自动进入当前 Codex：本旁路只接入 Chat Memory。需要这些资产时应另接官方 Knowledge/Skill MCP，或明确选择 Memory Proxy；不要仅凭“已绑定”判断客户端正在使用资产。

提示词写法及完整示例见 [prompt-examples.md](prompt-examples.md)。

## 创建 Task

1. 左上角先选择目标 Team。
2. 打开“工作台 → 任务看板”。没有 Team 时需先到团队管理创建。
3. 点击“新建 Task”。
4. 填写必填的“标题”和“描述”，然后创建。

当前官方前端的创建弹窗只暴露标题和描述；`source_type` 默认为 `manual`，`source_url` 为空，`linked_agents` 默认为空。服务端 API 同时支持风险等级、来源链接和关联 Agent，但具体 Panel 版本可能尚未提供这些编辑控件。

Task 创建后可切换“进行中/已完成”、编辑标题与描述，并查看创建者、实际参与 User 和实际参与 Agent。

注意三种概念：

- 创建 Task：只登记工作事项，不会自动启动 Agent。
- linked Agent：声明“计划由谁处理”，由 `task-agent/link` 关系维护。
- 实际参与 Agent/User：外部客户端通过 Proxy 启动带 `task_id` 的会话后，由 participation log 观测产生。

Task 虽是可选项，但官方安装文档明确指出：跳过 Task 仍能使用记忆，L2/L3 会失去 Task 这一组织维度。持续数天、会产生多项决策的工作应建 Task；一次性短问答不必建。

当前 Codex 旁路模式不经过 TDAI Proxy，因此创建 Task 后不会自动出现执行记录。需要让记忆与 Task 关联时：

- 在单次 `remember`/搜索工具调用中传 `task_id`；或
- 对长期专用 Codex 配置设置 `TDAI_TASK_ID` 后重启 Codex。

不要为日常多任务配置固定的全局 `TDAI_TASK_ID`，否则不同工作可能被错误归到同一个 Task。

推荐的 Task 描述结构见 [prompt-examples.md](prompt-examples.md)。

## 跨 Codex、OpenCode、Hermes、DSH 共用

如果目标是四个客户端看到同一套长期记忆：

1. 使用同一个 `service_id + team_id + user_id + agent_id`。
2. 每个客户端保留自己的会话 ID，避免 L0 消息互相覆盖。
3. 统一使用工作台创建的 Agent ID，不要让各客户端落入 default Agent 桶。
4. 需要按项目隔离时使用不同 Agent；需要按一次工作隔离时使用 Task ID。
5. 通用 Wiki、CodeGraph、审核 Skill 放在 Team 资产池，再分配给各专用 Agent。

一个实用分层是：

- “个人/组织大脑”Agent：承载跨工具稳定偏好和长期背景；
- “项目 Agent”：每个长期项目一个，隔离项目 Persona 和场景；
- “角色 Agent”：Reviewer、Researcher 等，需要不同规则或资产装配时再创建。

不要为了“共享”让所有角色共用一个 Agent：这样会把不相干项目的 L2 场景和 L3 Persona 混在一起。

## 查看和维护记忆

打开“资产管理 → Chat_Memory”，选择 Agent 对应的记忆块：

- L0：原始对话证据，适合核对原话；
- L1：原子事实、偏好和决策，是日常召回首选；
- L2：按场景组织的长期上下文；
- L3：核心 Persona/稳定画像。

当前 MCP 的推荐读取顺序是：先用 `core_memory_read` 看 L3；需要项目场景时用 `scenario_list` 找目录并用 `scenario_read` 展开；再用 `memory_search` 查 L1 细节；只有要核对原话时才查 L0。不要每轮把四层全部读入上下文。

维护建议：

- 先检查 L1 是否准确，再考虑修改 L2/L3。
- 发现错误记忆时优先修正或删除具体条目，不要直接清空整个记忆块。
- visibility=private 仅 Owner 使用；team 对 Team 成员可见；restricted 依赖 ACL 精确授权。
- 团队共享只改变访问权，不会自动把所有资产注入所有 Agent；仍需完成 Agent 绑定。

## 推荐的首次配置

1. 建一个 Team，例如“AI 协作团队”。
2. 建一个“个人跨工具大脑”Agent，先不全选资产。
3. 给它绑定确实长期有效的项目 Wiki/CodeGraph，而不是所有资料。
4. 使用 [prompt-examples.md](prompt-examples.md) 中的“跨工具项目大脑”模板。
5. 在各客户端写入三到五条非敏感测试偏好，确认 L0/L1 和跨会话召回。
6. 再按项目或角色增加 Agent，避免一开始就把记忆空间做得过碎。
