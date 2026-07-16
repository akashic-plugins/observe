from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Mapping
from contextlib import suppress
from typing import Literal, Protocol, cast, runtime_checkable

from agent.plugins import Plugin
from bus.events_lifecycle import ProactiveFinished, TurnCommitted
from core.memory.events import MemoryWritten, RetrievalCompleted

from .collector import GlobalErrorCollector
from .mobile_kvcache import KVCacheDashboardReader
from .retention import run_retention_if_needed
from .writer import TraceWriter

logger = logging.getLogger("plugin.observe")


@runtime_checkable
class _ObserveWriter(Protocol):
    def emit(self, event: object) -> None: ...


class ObservePlugin(Plugin):
    @classmethod
    def dashboard_module(cls) -> str | None:
        return "dashboard.py"

    @classmethod
    def mobile_ui_module(cls) -> str | None:
        return "mobile_panel.js"

    @classmethod
    def mobile_ui_stylesheet(cls) -> str | None:
        return "mobile_panel.css"

    name = "observe"
    version = "1.0.0"

    async def initialize(self) -> None:
        workspace = self.context.workspace
        if workspace is None:
            logger.warning("observe 插件缺少 workspace，跳过加载")
            return

        self._writer = TraceWriter(workspace / "observe" / "observe.db")
        self._writer_task = self.context.create_task(
            self._writer.run(),
            name="observe_writer",
        )
        self._retention_task = self.context.create_task(
            run_retention_if_needed(workspace / "observe" / "observe.db"),
            name="observe_retention",
        )
        self._collector = GlobalErrorCollector(
            self._writer,
            create_task=self.context.create_task,
        )
        self._collector.install()
        self.context.event_bus.on(TurnCommitted, self._observe_turn_committed)
        self.context.event_bus.on(ProactiveFinished, self._observe_proactive_finished)
        self.context.event_bus.on(RetrievalCompleted, self._observe_retrieval)
        self.context.event_bus.on(MemoryWritten, self._observe_memory_written)

    async def terminate(self) -> None:
        collector = getattr(self, "_collector", None)
        if collector is not None:
            await collector.uninstall()
        for task in (
            getattr(self, "_retention_task", None),
            getattr(self, "_writer_task", None),
        ):
            if task is None:
                continue
            _ = task.cancel()
            with suppress(asyncio.CancelledError):
                await task

    async def mobile_ui_call(
        self,
        method: str,
        payload: dict[str, object],
        *,
        session_id: str | None,
        turn_id: str | None,
    ) -> dict[str, object]:
        """返回 observe 自有的 KV Cache 移动投影。"""

        # 1. 在插件 RPC 边界校验方法与查询参数
        if method not in {
            "kvcache.overview",
            "kvcache.turns",
            "kvcache.message_usage",
        }:
            raise ValueError(f"未知 observe 移动方法: {method}")
        workspace = self.context.workspace
        if workspace is None:
            raise RuntimeError("observe 移动看板缺少 workspace")
        reader = KVCacheDashboardReader(workspace)
        if method == "kvcache.overview":
            return cast("dict[str, object]", await asyncio.to_thread(reader.get_summary))
        if method == "kvcache.message_usage":
            message_id = _required_mobile_string(payload, "message_id")
            if session_id is None:
                raise ValueError("kvcache.message_usage 缺少 session_id")
            usage = await asyncio.to_thread(
                reader.get_message_usage,
                message_id=message_id,
                session_key=session_id,
            )
            return {"usage": usage}

        # 2. 列表查询复用 observe 的真实 Turn 数据
        page = _mobile_page_value(payload, "page", default=1, maximum=10_000)
        page_size = _mobile_page_value(payload, "page_size", default=25, maximum=50)
        source = _mobile_source_value(payload)
        items, total = await asyncio.to_thread(
            reader.list_turns,
            page=page,
            page_size=page_size,
            source=source,
        )
        return {
            "items": cast("list[object]", items),
            "total": total,
            "page": page,
            "page_size": page_size,
        }

    def _observe_turn_committed(self, event: TurnCommitted) -> None:
        writer = getattr(self, "_writer", None)
        if not isinstance(writer, _ObserveWriter):
            return
        _emit_turn_trace(writer, event)

    def _observe_retrieval(self, event: RetrievalCompleted) -> None:
        writer = getattr(self, "_writer", None)
        if not isinstance(writer, _ObserveWriter):
            return
        writer.emit(_to_rag_query_log(event))

    def _observe_proactive_finished(self, event: ProactiveFinished) -> None:
        writer = getattr(self, "_writer", None)
        if not isinstance(writer, _ObserveWriter):
            return
        writer.emit(_to_proactive_turn_trace(event))

    def _observe_memory_written(self, event: MemoryWritten) -> None:
        writer = getattr(self, "_writer", None)
        if not isinstance(writer, _ObserveWriter):
            return
        writer.emit(_to_memory_write_trace(event))


def _emit_turn_trace(writer: _ObserveWriter, event: TurnCommitted) -> None:
    from .events import TurnTrace as TurnTraceEvent

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
            source="agent",
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


def _mobile_page_value(
    payload: dict[str, object],
    name: str,
    *,
    default: int,
    maximum: int,
) -> int:
    value = payload.get(name, default)
    if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= maximum:
        raise ValueError(f"{name} 必须是 1 到 {maximum} 的整数")
    return value


def _mobile_source_value(payload: dict[str, object]) -> Literal["agent"] | None:
    value = payload.get("source")
    if value is None:
        return None
    if value != "agent":
        raise ValueError("source 只支持 agent")
    return "agent"


def _required_mobile_string(payload: dict[str, object], name: str) -> str:
    value = payload.get(name)
    if not isinstance(value, str) or not value or len(value) > 512:
        raise ValueError(f"{name} 必须是 1 到 512 字符的字符串")
    return value


def _model_usage_int(model_usage: Mapping[str, object], name: str) -> int | None:
    if model_usage.get("coverage") != "exact":
        return None
    value = model_usage.get(name)
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _to_proactive_turn_trace(event: ProactiveFinished):
    from .events import TurnTrace as TurnTraceEvent

    summary = event.final_message or event.skip_reason or event.gate_exit or ""
    return TurnTraceEvent(
        source=event.mode,
        session_key=event.session_key,
        user_msg=None,
        llm_output=summary,
        raw_llm_output=None,
        react_iteration_count=event.llm_call_count,
        react_input_sum_tokens=None,
        react_input_peak_tokens=None,
        react_final_input_tokens=None,
        react_cache_prompt_tokens=event.cache_prompt_tokens,
        react_cache_hit_tokens=event.cache_hit_tokens,
    )


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
