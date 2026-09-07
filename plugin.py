from __future__ import annotations

import logging
import asyncio
from pathlib import Path
from typing import Literal, cast

from agent.plugin_composition import (
    Context,
    MobileUiDefinition,
    MobileUiNavigation,
    MobileUiRpcInvalidRequest,
    RUNTIME_STARTED,
    RUNTIME_STOPPING,
    UI_SLOTS,
)
from agent.plugin_composition.messages import MESSAGE_CATALOG
from plugins.akasha.message_plugin import AKASHA_RECORDS_VIEW
from plugins.markdown_memory.store import MEMORY_WRITES
from plugins.models.projection import MODEL_CALL_HISTORY, MODEL_CALLS
from plugins.turn_projection.plugin import TURN_PROJECTION

from .collector import GlobalErrorCollector
from .dashboard import ObserveDashboardReader
from .mobile_kvcache import KVCacheDashboardReader
from .retention import run_retention_if_needed
from .projection import run_projection
from .writer import TraceWriter

logger = logging.getLogger("plugin.observe")

api_version = 3
name = "observe"
version = "2.0.0"
inject = (
    UI_SLOTS,
    MESSAGE_CATALOG,
    TURN_PROJECTION,
    MODEL_CALLS,
    MODEL_CALL_HISTORY,
    AKASHA_RECORDS_VIEW,
    MEMORY_WRITES,
)
workspace_roots = ("observe",)
dashboard_module = "dashboard.py"
web_module = "web_module.js"
web_requires = ("workbench.panels.v2",)
web_provides = ()
web_contract_digests = {
    "workbench.panels.v2": "fb6417c9bf532c1fdb344767d06065d5d3293da85deb64eff1e8088889a33bcb",
}


async def apply(ctx: Context, config: object) -> None:
    """启动 owner 历史投影、错误采集、Dashboard 与移动端查询。"""

    # 1. 所有运行时文件都落在 Core 分配的声明式 Observe workspace root。
    del config
    observe_root = ctx.workspace_root("observe")
    db_path = observe_root / "observe.db"
    writer = TraceWriter(db_path)
    _ = await ctx.spawn(writer.run(), name="observe_writer")
    _ = await ctx.spawn(
        run_retention_if_needed(db_path),
        name="observe_retention",
    )

    # 2. 全局错误出口由一个可逆 Effect 持有，flush task 也归当前 Fiber。
    collector = GlobalErrorCollector(writer)

    async def setup_collector() -> object:
        try:
            await collector.install(spawn_task=ctx.spawn)
        except BaseException:
            await collector.uninstall()
            raise
        return collector.uninstall

    _ = await ctx.effect(setup_collector, label="observe_global_errors")

    # 3. 正式启动后只读各 owner 的耐久事实；candidate 不读取正式状态。
    projection_task: asyncio.Task[None] | None = None

    async def start_projection(_event: object) -> None:
        nonlocal projection_task
        projection_task = await ctx.spawn(
            run_projection(
                catalog=ctx.require(MESSAGE_CATALOG),
                turns=ctx.require(TURN_PROJECTION),
                read_call=ctx.require(MODEL_CALLS),
                model_history=ctx.require(MODEL_CALL_HISTORY),
                memory_history=ctx.require(MEMORY_WRITES),
                akasha_records=ctx.require(AKASHA_RECORDS_VIEW),
                writer=writer,
                db_path=db_path,
            ),
            name="observe_projection",
        )

    async def stop_projection(_event: object) -> None:
        if projection_task is None:
            return
        projection_task.cancel()
        try:
            await projection_task
        except asyncio.CancelledError:
            pass

    _ = await ctx.on(RUNTIME_STARTED, start_projection)
    _ = await ctx.on(RUNTIME_STOPPING, stop_projection)

    # 4. Mobile 只读查询和静态资源绑定同一 generation Effect。
    def mobile_query(
        method: str,
        payload: dict[str, object],
        *,
        session_id: str | None,
        turn_id: str | None,
    ) -> dict[str, object]:
        return _mobile_ui_query(
            observe_root,
            method,
            payload,
            session_id=session_id,
            turn_id=turn_id,
        )

    await ctx.require(UI_SLOTS).register_mobile(
        ctx,
        MobileUiDefinition(
            module="mobile_panel.js",
            stylesheet="mobile_panel.css",
            navigation=MobileUiNavigation(
                label="Observe",
                description="缓存效率与运行健康",
            ),
            slots=("turn.after_answer",),
        ),
        query=mobile_query,
    )


def _mobile_ui_query(
    observe_root: Path,
    method: str,
    payload: dict[str, object],
    *,
    session_id: str | None,
    turn_id: str | None,
) -> dict[str, object]:
    """返回 Observe 自有的移动端只读投影。"""

    # 1. 在插件 RPC 边界校验方法与查询参数。
    _ = turn_id
    if method not in {
        "kvcache.bootstrap",
        "kvcache.message_usage",
        "health.snapshot",
        "health.error_detail",
    }:
        raise MobileUiRpcInvalidRequest(f"未知 observe 移动方法: {method}")
    if method.startswith("health."):
        return _mobile_health_query(method, payload, observe_root)
    reader = KVCacheDashboardReader(observe_root)
    if method == "kvcache.bootstrap":
        return cast("dict[str, object]", reader.get_bootstrap())
    if method == "kvcache.message_usage":
        message_id = _required_mobile_string(payload, "message_id")
        if session_id is None:
            raise MobileUiRpcInvalidRequest("kvcache.message_usage 缺少 session_id")
        usage = reader.get_message_usage(
            message_id=message_id,
            session_key=session_id,
        )
        return {"usage": usage}
    raise AssertionError(f"未处理的 observe 移动方法: {method}")


def _mobile_health_query(
    method: str,
    payload: dict[str, object],
    observe_root: Path,
) -> dict[str, object]:
    """把 Observe 错误聚合裁成手机排障所需的只读投影。"""

    # 1. 复用桌面聚合 owner，只在 RPC 边界限制时间范围和载荷体积。
    range_token = _mobile_range_value(payload)
    reader = ObserveDashboardReader(observe_root)
    if method == "health.snapshot":
        result = reader.get_mobile_global_health(range_token, limit=50)
        raw_items = cast("list[dict[str, object]]", result["items"])
        return {
            "range": range_token,
            "items": cast(
                "list[object]",
                [_mobile_error_summary(item) for item in raw_items],
            ),
            "types": int(result["types"]),
            "total": int(result["total"]),
            "new_types": int(result["new_types"]),
            "spiking_types": int(result["spiking_types"]),
        }

    # 2. 详情按用户展开时再读取，列表不搬运 traceback 和 occurrence。
    fingerprint = _required_mobile_string(payload, "fingerprint")
    detail = reader.get_mobile_global_detail(fingerprint, range_token)
    if not detail:
        return {"error": None}
    return {"error": _mobile_error_detail(detail)}


def _mobile_range_value(payload: dict[str, object]) -> Literal["24h", "7d"]:
    value = payload.get("range", "24h")
    if not isinstance(value, str) or value not in {"24h", "7d"}:
        raise MobileUiRpcInvalidRequest("range 只支持 24h 或 7d")
    return cast("Literal['24h', '7d']", value)


def _mobile_error_summary(item: dict[str, object]) -> dict[str, object]:
    return {
        "fingerprint": str(item["fingerprint"]),
        "error_type": str(item["error_type"]),
        "message": str(item["message"]),
        "source": str(item["source"]),
        "logger_name": str(item["logger_name"]),
        "status": str(item["status"]),
        "count": int(cast("int", item["count"])),
        "last_ts": str(item["last_ts"]),
        "sessions": int(cast("int", item["sessions"])),
        "is_new": bool(item["is_new"]),
        "is_spiking": bool(item["is_spiking"]),
    }


def _mobile_error_detail(item: dict[str, object]) -> dict[str, object]:
    result = _mobile_error_summary(item)
    traceback_text = str(item.get("traceback_text") or "")
    result.update(
        {
            "first_ts": str(item["first_ts"]),
            "traceback": traceback_text[:4000],
        }
    )
    return result


def _required_mobile_string(payload: dict[str, object], name: str) -> str:
    value = payload.get(name)
    if not isinstance(value, str) or not value or len(value) > 512:
        raise MobileUiRpcInvalidRequest(f"{name} 必须是 1 到 512 字符的字符串")
    return value
