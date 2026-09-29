# observe

Akashic 可观测性插件（Plugin API v3），负责投影已提交 Message、模型调用、记忆记录和全局错误，并提供 Dashboard 与Web 插件界面只读投影。

插件只读取各领域 owner 的公开能力：

- `core.message_catalog` 与 `turn.projection.v1`：按闭合消费边界读取 Message 尾部，得到 Turn 引用；
- `models.calls.v1` 与 `models.call-history.v1`：保存成功、失败和未确定调用；
- `akasha.recall-records.v1`：投影真实召回记录与已呈现的 Message；
- `markdown-memory.writes.v1`：投影 Markdown draft/applied receipt；
- `core.ui_slots`：发布Web 插件界面静态资源和只读 query；
- `workspace_roots = ("observe",)`：数据库与 retention marker 由 Core 分配。

Observe DB 保留原有 `turns`、`rag_queries`、`memory_writes` 和 `global_errors` 历史。投影 receipt 使用 owner 的不可变 ID 防止重启重复写；未闭合 Turn 不推进 cursor，后来提交 Output 后仍会被投影。一次 Turn 引用的全部 `model.facts` 都计入用量，未产生 Message 的失败调用也保存在 `model_calls`。

消息投影只读取 head 变化的会话，并按每个来源的持久 cursor 从上次闭合位置继续。
固定上界内的扫描在受限 I/O worker 中逐页进行；Turn 分段只保存引用。开放尾段仍从
原消息恢复，不缓存完整会话。一次只读取一个新闭合 Turn 的正文，生成诊断后立即释放；
TraceWriter 提交成功才推进原 cursor。崩溃或取消可安全重读，未增加第二套进度或数据库。
完整重建使用同一投影算法的起点读取；单个 Turn 的诊断正文仍决定该条写入的内存大小。
该版本要求 Core 的 MessageReader.scan 和 TurnProjection.after_seq 合同，旧 Core 不提供降级路径。

Akasha 投影在读取 Message 正文前，先由 TraceWriter 的同一连接查询既有 receipt，只处理尚未提交的不可变 recall。读取或写入失败不留下完成回执，下一轮仍会重试；不另建进度账本。`rag_queries` 继续保留 90 天，独立 receipt 不随 trace 清理，因此重启或全量枚举不会复活过期记录。`memory_writes` 与既有合同一致，不参与自动 retention。

Message source `wake` 继续显示为 `proactive`，`drift` 显示为 `drift`，其余来源显示为 `agent`。原始模型输出和旧 context 临时统计不从 Message 反推；对应列保留，已有历史不改写。

Dashboard 使用 `register(app, DashboardContext)`，只读取当前 generation 的声明式 workspace root。candidate 验证只写 Core 分配的临时 workspace，formal publish 后才继续写正式 `observe` workspace。

## Web 插件界面

Web 插件界面入口保留两个视图：

- `缓存效率`：展示近期 KV Cache 命中率、被动/主动链路差异和 Turn 明细；
- `运行健康`：展示错误次数、新类型、增长项和按需读取的错误现场。

回答尾部继续显示本轮模型输出 token。颜色只表达稳定、新问题和增长问题。
