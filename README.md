# observe

Akashic 可观测性插件，负责采集 Turn、检索、记忆写入和全局错误遥测。

## 移动端

插件自带移动端 KV Cache 看板，并通过 `turn.after_answer` 在助手回答尾部显示真实的本轮模型输出 token。移动端核心只负责注册插件资源与转发带会话上下文的 RPC；未启用 `observe` 时不会出现 KV Cache 入口或 Turn 统计。
