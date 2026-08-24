from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from pathlib import Path
from typing import Literal, Protocol, cast

from agent.plugin_composition import (
    Context,
    MobileUiDefinition,
    MobileUiNavigation,
    MobileUiRpcInvalidRequest,
    UI_SLOTS,
)
from agent.turn_events.after_turn import AFTER_TURN_COMMITTED
from agent.turn_events.observe import (
    MEMORY_WRITTEN,
    RETRIEVAL_COMPLETED,
)
from bus.events_lifecycle import TurnCommitted
from core.memory.events import MemoryWritten, RetrievalCompleted

from .collector import GlobalErrorCollector
from .dashboard import ObserveDashboardReader
from .mobile_kvcache import KVCacheDashboardReader
from .retention import run_retention_if_needed
from .events import TurnTrace as TurnTraceEvent
from .writer import TraceWriter

logger = logging.getLogger("plugin.observe")

api_version = 3
name = "observe"
version = "1.4.0"
inject = (UI_SLOTS,)
workspace_roots = ("observe",)
dashboard_module = "dashboard.py"


class _ObserveWriter(Protocol):
    def emit(self, event: TurnTraceEvent) -> None: ...


async def apply(ctx: Context, config: object) -> None:
    """登记 Observe 的 committed 事件、写入任务和只读投影。"""

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

    # 3. TurnCommitted 使用 request-bound composition Root 的 typed emit。
    def observe_turn_committed(event: TurnCommitted) -> None:
        _emit_turn_trace(writer, event)

    _ = await ctx.on(AFTER_TURN_COMMITTED, observe_turn_committed)

    def observe_retrieval(event: RetrievalCompleted) -> None:
        writer.emit(_to_rag_query_log(event))

    def observe_memory_written(event: MemoryWritten) -> None:
        writer.emit(_to_memory_write_trace(event))

    _ = await ctx.on(RETRIEVAL_COMPLETED, observe_retrieval)
    _ = await ctx.on(MEMORY_WRITTEN, observe_memory_written)

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


def _emit_turn_trace(writer: _ObserveWriter, event: TurnCommitted) -> None:
    post_reply_budget = event.post_reply_budget
    react_stats = event.react_stats
    tool_chain = event.tool_chain_raw
    tool_chain_json = (
        json.dumps(_slim_tool_chain(tool_chain), ensure_ascii=False)
        if tool_chain
        else None
    )
    tool_calls = _slim_tool_calls(tool_chain)
    writer.emit(
        TurnTraceEvent(
            source=_turn_source(event),
            session_key=event.session_key,
            turn_id=event.turn_id or None,
            assistant_message_id=event.assistant_message_id,
            user_msg=event.persisted_user_message,
            llm_output=event.assistant_response,
            raw_llm_output=event.raw_reply,
            meme_tag=event.meme_tag,
            meme_media_count=event.meme_media_count,
            tool_calls=tool_calls,
            tool_chain_json=tool_chain_json,
            history_window=post_reply_budget.get("history_window"),
            history_messages=post_reply_budget.get("history_messages"),
            history_chars=post_reply_budget.get("history_chars"),
            history_tokens=post_reply_budget.get("history_tokens"),
            prompt_tokens=post_reply_budget.get("prompt_tokens"),
            next_turn_baseline_tokens=post_reply_budget.get(
                "next_turn_baseline_tokens"
            ),
            react_iteration_count=react_stats.get("iteration_count"),
            react_input_sum_tokens=react_stats.get("turn_input_sum_tokens"),
            react_input_peak_tokens=react_stats.get("turn_input_peak_tokens"),
            react_final_input_tokens=react_stats.get("final_call_input_tokens"),
            model_output_tokens=_model_usage_int(event.model_usage, "output_tokens"),
            react_cache_prompt_tokens=react_stats.get("cache_prompt_tokens"),
            react_cache_hit_tokens=react_stats.get("cache_hit_tokens"),
        )
    )
    logger.info(
        "[observe] turn_trace 已入队 session=%s tool_calls=%d",
        event.session_key,
        len(tool_calls),
    )


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


def _model_usage_int(model_usage: Mapping[str, object], name: str) -> int | None:
    if model_usage.get("coverage") != "exact":
        return None
    value = model_usage.get(name)
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _turn_source(event: TurnCommitted) -> Literal["agent", "proactive", "drift"]:
    if event.channel == "wake":
        return "proactive"
    if event.channel == "drift":
        return "drift"
    return "agent"


def _to_rag_query_log(event: RetrievalCompleted):
    from .events import RagHitLog, RagQueryLog

    return RagQueryLog(
        caller="passive",
        session_key=event.session_key,
        query=event.query,
        orig_query=event.orig_query,
        aux_queries=list(event.aux_queries),
        hits=[
            RagHitLog(
                item_id=hit.item_id,
                memory_type=hit.memory_type,
                score=hit.score,
                summary=hit.summary[:120],
                injected=hit.injected,
                confidence_label=hit.confidence_label,
                forced=hit.forced,
            )
            for hit in event.hits
        ],
        injected_count=event.injected_count,
        route_decision=event.route_decision,
        error=event.error,
    )


def _to_memory_write_trace(event: MemoryWritten):
    from .events import MemoryWriteTrace

    return MemoryWriteTrace(
        session_key=event.session_key,
        source_ref=event.source_ref,
        action=event.action,
        memory_type=event.memory_type,
        item_id=event.item_id,
        summary=event.summary,
        superseded_ids=list(event.superseded_ids),
        error=event.error,
    )


def _slim_tool_calls(tool_chain: list[dict[str, object]]) -> list[dict[str, str]]:
    return [
        {
            "name": str(call.get("name", "")),
            "args": str(call.get("arguments", ""))[:300],
            "result": str(call.get("result", ""))[:500],
        }
        for group in tool_chain
        for call in _group_calls(group)
    ]


def _slim_tool_chain(tool_chain: list[dict[str, object]]) -> list[dict[str, object]]:
    return [
        {
            "text": str(group.get("text") or ""),
            "calls": [
                {
                    "name": str(call.get("name", "")),
                    "args": str(call.get("arguments", ""))[:800],
                    "result": str(call.get("result", ""))[:1200],
                }
                for call in _group_calls(group)
            ],
        }
        for group in tool_chain
    ]


def _group_calls(group: dict[str, object]) -> list[dict[str, object]]:
    calls = group.get("calls")
    if not isinstance(calls, list):
        return []
    raw_calls = cast(list[object], calls)
    out: list[dict[str, object]] = []
    for call in raw_calls:
        if isinstance(call, Mapping):
            mapping = cast(Mapping[object, object], call)
            out.append(
                {
                    str(key): value
                    for key, value in mapping.items()
                    if isinstance(key, str)
                }
            )
    return out
