# observe

Akashic 可观测性插件（Plugin API v3），负责投影已提交 Message、模型调用、记忆记录和全局错误，并提供 Dashboard 与移动端只读投影。

插件只读取各领域 owner 的公开能力：

- `core.message_catalog` 与 `turn.projection.v1`：从完整 Message 前缀得到闭合 Turn；
- `models.calls.v1` 与 `models.call-history.v1`：保存成功、失败和未确定调用；
- `akasha.recall-records.v1`：投影真实召回记录与已呈现的 Message；
- `markdown-memory.writes.v1`：投影 Markdown draft/applied receipt；
- `core.ui_slots`：发布移动端静态资源和只读 query；
- `workspace_roots = ("observe",)`：数据库与 retention marker 由 Core 分配。

Observe DB 保留原有 `turns`、`rag_queries`、`memory_writes` 和 `global_errors` 历史。投影 receipt 使用 owner 的不可变 ID 防止重启重复写；未闭合 Turn 不推进 cursor，后来提交 Output 后仍会被投影。一次 Turn 引用的全部 `model.facts` 都计入用量，未产生 Message 的失败调用也保存在 `model_calls`。

Message source `wake` 继续显示为 `proactive`，`drift` 显示为 `drift`，其余来源显示为 `agent`。原始模型输出和旧 context 临时统计不从 Message 反推；对应列保留，已有历史不改写。

Dashboard 使用 `register(app, DashboardContext)`，只读取当前 generation 的声明式 workspace root。candidate 验证只写 Core 分配的临时 workspace，formal publish 后才继续写正式 `observe` workspace。

## 移动端

移动端入口保留两个视图：

- `缓存效率`：展示近期 KV Cache 命中率、被动/主动链路差异和 Turn 明细；
- `运行健康`：展示错误次数、新类型、增长项和按需读取的错误现场。

回答尾部继续显示本轮模型输出 token。颜色只表达稳定、新问题和增长问题。
