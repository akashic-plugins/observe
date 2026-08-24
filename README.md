# observe

Akashic 可观测性插件（Plugin API v3），负责采集已提交 Turn 和全局错误遥测，
并提供 Dashboard 与移动端只读投影。

插件通过模块级 `api_version = 3` / `apply(ctx, config)` 接入 Core：

- `turn.after_turn.committed`：在 Core 提交 `TurnCommitted` 后写入 Observe 数据库；
- `core.ui_slots`：发布移动端静态资源和只读 query；
- `workspace_roots = ("observe",)`：数据库、retention marker 和候选副本都由 Core 分配。

Observe 不再导出 v2 `Plugin` class、`activate()`、`terminate()`、`mobile_ui()` 或
`mobile_ui_query()` ABI。Dashboard 使用 `register(app, DashboardContext)`，只读取当前
generation 的声明式 workspace root。

历史 `rag_queries` 与 `memory_writes` 表保留供兼容读取；
`memory.retrieval.completed` 和 `memory.written` typed Observe seam 直接转换为
既有 Observe 表结构，不复制或伪造领域 DTO。

所有普通 Turn 都只从同一个 `TurnCommitted` 事实写入 trace，并根据 `channel`
投影为既有 source：Wake 是 `proactive`，显式 `drift` channel 是 `drift`，其余是
`agent`。当前新 Drift duty 由 Wake 选中并在同一个 `channel=wake` Turn 中执行，
因此仍归入 `proactive`；显式 `channel=drift` 只是一条普通 channel 分类合同，不代表
当前 Drift duty 拥有独立 Turn。未来若需要把它单列，必须另立 provenance 合同。

quiet Wake 没有进入 after-turn，也不会产生 `TurnCommitted`，因此 Observe 不伪造
delivered trace。旧 `proactive.finished` 不再注册，也没有双写兼容路径。

插件测试与 CI 固定使用 workflow 声明的 Core commit；candidate 验证只写 Core
分配的临时 workspace，formal publish 后才继续写正式 `observe` workspace。

## 移动端

插件自带一个移动端 Observe 入口，并在同一看板内提供两个任务视图：

- `缓存效率`：展示近期 KV Cache 命中率、被动/主动链路差异和 Turn 明细。
- `运行健康`：先回答当前是否需要关注，再展示最近 24 小时的错误次数、新类型和增长项；错误现场只在用户展开时读取。

插件还会通过 `turn.after_answer` 在助手回答尾部显示真实的本轮模型输出 token。移动端核心只负责注册插件资源与转发带会话上下文的 RPC；未启用 `observe` 时不会出现 Observe 入口、运行健康数据或 Turn 统计。

移动看板不照搬桌面排障台：手机只保留状态判断、三项关键指标和可展开的问题列表。颜色只表达稳定、新问题和增长问题，列表层级依靠留白与分隔线，不把每条错误包装成卡片。
