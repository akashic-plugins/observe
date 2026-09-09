"""从公开 owner 读口把运行事实投影到 Observe 自有数据库。"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from collections.abc import Callable, Mapping
from contextlib import AbstractAsyncContextManager
from pathlib import Path
from typing import Any, cast

from plugins.akasha.recalls import ContextSource, ProgramSource, RecallRecordsRead
from plugins.turn_projection.plugin import Turn, TurnProjection
from session.log import MessageCatalog
from session.message_codec import json_value
from session.message import (
    CallRef,
    ContentPart,
    Input,
    Message,
    Output,
    ToolCall,
    ToolResult,
)

from .events import MemoryWriteTrace, ModelCallTrace, RagHitLog, RagQueryLog, TurnTrace
from .writer import TraceWriter

logger = logging.getLogger("observe.projection")


def _texts(message: Message) -> str:
    body = message.body
    if not isinstance(body, (Input, Output, ToolResult)):
        return ""
    return "\n".join(
        cast(str, part.value)
        for part in body.parts
        if isinstance(part, ContentPart)
        and part.kind == "text"
        and isinstance(part.value, str)
    )


def _cursor(db_path: Path, session_id: str, source: str) -> int:
    if not db_path.exists():
        return -1
    with sqlite3.connect(str(db_path)) as connection:
        row = connection.execute(
            "SELECT through_seq FROM projection_cursors "
            "WHERE domain='turn' AND scope=?",
            (f"{session_id}\n{source}",),
        ).fetchone()
    return -1 if row is None else int(row[0])


def _call_ids(messages: Mapping[str, Message], turn: Turn) -> tuple[str, ...]:
    call_ids: list[str] = []
    for identity in turn.message_ids:
        message = messages[identity]
        if not isinstance(message.body, Output):
            continue
        for part in message.body.parts:
            if not isinstance(part, ContentPart) or part.kind != "model.facts":
                continue
            if not isinstance(part.value, Mapping):
                raise ValueError("model.facts 不是对象")
            call_id = part.value.get("call_record_id")
            if not isinstance(call_id, str) or not call_id:
                raise ValueError("model.facts 缺少调用记录")
            call_ids.append(call_id)
    return tuple(call_ids)


def _usage(
    call_ids: tuple[str, ...],
    read_call: Callable[[str], Mapping[str, Any]],
) -> tuple[int | None, int | None, int | None]:
    """聚合一个 Turn 引用的全部成功调用，不把未知用量当零。"""
    if not call_ids:
        return None, None, None
    usages: list[Mapping[str, object]] = []
    for identity in call_ids:
        record = read_call(identity)
        if record.get("state") != "success":
            raise ValueError(f"已提交 Output 引用未成功的模型调用: {identity}")
        value = record.get("usage")
        if not isinstance(value, Mapping) or value.get("coverage") != "exact":
            return None, None, None
        usages.append(cast(Mapping[str, object], value))

    def total(name: str) -> int | None:
        values = [usage.get(name) for usage in usages]
        if any(type(value) is not int for value in values):
            return None
        return sum(cast(list[int], values))

    return total("output_tokens"), total("input_tokens"), total("cached_input_tokens")


def _tool_chain(
    messages: Mapping[str, Message],
    turn: Turn,
    tool_name: Callable[[str], str],
) -> list[dict[str, object]]:
    results = {
        call_ref: messages[result_id] for call_ref, result_id in turn.observations
    }
    groups: list[dict[str, object]] = []
    for identity in turn.message_ids:
        message = messages[identity]
        if not isinstance(message.body, Output):
            continue
        calls: list[dict[str, object]] = []
        for index, part in enumerate(message.body.parts):
            if not isinstance(part, ToolCall):
                continue
            result = results.get(CallRef(identity, index))
            calls.append(
                {
                    "name": tool_name(part.binding_id),
                    "arguments": json_value(part.arguments),
                    "result": None if result is None else _texts(result),
                    "outcome": (
                        None
                        if result is None or not isinstance(result.body, ToolResult)
                        else result.body.outcome
                    ),
                }
            )
        if calls or _texts(message):
            groups.append({"text": _texts(message), "calls": calls})
    return groups


def _turn_trace(
    session_id: str,
    turn: Turn,
    by_id: Mapping[str, Message],
    read_call: Callable[[str], Mapping[str, Any]],
    tool_name: Callable[[str], str],
) -> TurnTrace:
    members = [by_id[identity] for identity in turn.message_ids]
    inputs = [message for message in members if isinstance(message.body, Input)]
    outputs = [message for message in members if isinstance(message.body, Output)]
    final = outputs[-1] if outputs else None
    final_body = None if final is None else cast(Output, final.body)
    calls = _call_ids(by_id, turn)
    output_tokens, prompt_tokens, cache_hits = _usage(calls, read_call)
    chain = _tool_chain(by_id, turn, tool_name)
    meme = {} if final is None else final.metadata.get("meme", {})
    meme = meme if isinstance(meme, Mapping) else {}
    media = 0
    if final_body is not None:
        media = sum(
            isinstance(part, ContentPart) and part.kind == "artifact_ref"
            for part in final_body.parts
        )
    return TurnTrace(
        source=(
            "proactive"
            if turn.source == "wake"
            else "drift" if turn.source == "drift" else "agent"
        ),
        session_key=session_id,
        user_msg="\n".join(filter(None, (_texts(message) for message in inputs)))
        or None,
        llm_output="" if final is None else _texts(final),
        turn_id=turn.ending_message_id,
        assistant_message_id=None if final is None else final.message_id,
        meme_tag=cast(str | None, meme.get("category")),
        meme_media_count=media,
        tool_calls=[
            call
            for group in chain
            for call in cast(list[dict[str, object]], group["calls"])
        ],
        tool_chain_json=json.dumps(chain, ensure_ascii=False) if chain else None,
        react_iteration_count=len(calls) or None,
        react_input_sum_tokens=prompt_tokens,
        model_output_tokens=output_tokens,
        react_cache_prompt_tokens=prompt_tokens,
        react_cache_hit_tokens=cache_hits,
        error="abandoned" if turn.status == "abandoned" else None,
        projection_key=f"turn:{session_id}:{turn.source}:{turn.through_seq}:{turn.ending_message_id}",
        projection_source=turn.source,
        through_seq=turn.through_seq,
        recorded_at=(
            (final or members[-1]).recorded_at.isoformat() if members else None
        ),
    )


async def project_messages(
    catalog: MessageCatalog,
    projection: TurnProjection,
    read_call: Callable[[str], Mapping[str, Any]],
    tool_name: Callable[[str], str],
    writer: TraceWriter,
    db_path: Path,
    *,
    heads: Mapping[str, int] | None = None,
) -> None:
    """重读完整 Session 前缀，只提交 cursor 后新闭合的 Turn。"""
    for session_id, head in (
        catalog.snapshot_heads() if heads is None else heads
    ).items():
        messages = catalog.reader(session_id).snapshot(through_seq=head)
        by_id = {message.message_id: message for message in messages}
        for source in sorted({message.source for message in messages}):
            after = _cursor(db_path, session_id, source)
            for turn in projection.project(messages, source):
                if turn.status == "open" or turn.through_seq <= after:
                    continue
                await writer.submit(
                    _turn_trace(session_id, turn, by_id, read_call, tool_name)
                )


async def project_model_calls(
    read_page: Callable[[str, int], tuple[Mapping[str, Any], ...]],
    writer: TraceWriter,
) -> None:
    after = ""
    while True:
        page = read_page(after, 250)
        for record in page:
            binding = record.get("binding")
            model = binding.get("model") if isinstance(binding, Mapping) else None
            if not isinstance(model, str) or not model:
                raise ValueError("模型调用缺少 model")
            await writer.submit(
                ModelCallTrace(
                    call_id=cast(str, record["id"]),
                    state=cast(str, record["state"]),
                    model=model,
                    started_at=cast(str, record["started_at"]),
                    finished_at=cast(str | None, record["finished_at"]),
                    first_token_ms=cast(float | None, record["first_token_ms"]),
                    duration_ms=cast(float | None, record["duration_ms"]),
                    usage=(
                        dict(cast(Mapping[str, object], record["usage"]))
                        if isinstance(record.get("usage"), Mapping)
                        else None
                    ),
                    failure=cast(str | None, record["failure"]),
                )
            )
        if len(page) < 250:
            break
        after = cast(str, page[-1]["id"])


async def project_memory_writes(
    read_page: Callable[[tuple[str, str] | None, int], tuple[dict[str, object], ...]],
    writer: TraceWriter,
) -> None:
    after: tuple[str, str] | None = None
    rows: list[dict[str, object]] = []
    while True:
        page = read_page(after, 1000)
        rows.extend(page)
        if len(page) < 1000:
            break
        after = (cast(str, page[-1]["source_ref"]), cast(str, page[-1]["kind"]))
    sessions = {
        cast(str, row["source_ref"]): cast(
            str, cast(Mapping[str, object], row["payload"])["session_key"]
        )
        for row in rows
        if row["kind"] == "markdown_projection_order_v1"
        and isinstance(row.get("payload"), Mapping)
        and isinstance(
            cast(Mapping[str, object], row["payload"]).get("session_key"), str
        )
    }
    for row in rows:
        source_ref, kind = cast(str, row["source_ref"]), cast(str, row["kind"])
        payload = row.get("payload")
        await writer.submit(
            MemoryWriteTrace(
                session_key=sessions.get(source_ref, ""),
                source_ref=source_ref,
                action=kind,
                item_id=source_ref,
                summary=json.dumps(payload, ensure_ascii=False, sort_keys=True)[:1000],
                projection_key=f"markdown:{source_ref}:{kind}",
                recorded_at=cast(str, row["done_at"]),
            )
        )


async def project_akasha(
    records: RecallRecordsRead,
    catalog: MessageCatalog,
    writer: TraceWriter,
) -> None:
    for identity, recall in records.list():
        hits: list[RagHitLog] = []
        for hit in recall.hits:
            for message_id in hit.message_ids:
                message = catalog.reader(hit.session_id).get(message_id)
                hits.append(
                    RagHitLog(
                        item_id=message_id,
                        memory_type="akasha",
                        score=float(hit.score),
                        summary="" if message is None else _texts(message)[:120],
                        injected=message_id in recall.presented_message_ids,
                    )
                )
        source = recall.source
        query = (
            source.query
            if isinstance(source, ProgramSource)
            else f"{source.kind}:{identity}"
        )
        await writer.submit(
            RagQueryLog(
                caller=("passive" if isinstance(source, ContextSource) else "explicit"),
                session_key=(
                    "" if isinstance(source, ProgramSource) else source.session_id
                ),
                query=query,
                orig_query=None,
                aux_queries=[],
                hits=hits,
                injected_count=len(recall.presented_message_ids),
                route_decision="RETRIEVE" if recall.hits else "NO_RETRIEVE",
                projection_key=f"akasha:{identity}",
                recorded_at=recall.timestamp.isoformat(),
            )
        )


async def run_projection(
    *,
    runtime_scope: Callable[[], AbstractAsyncContextManager[None]],
    catalog: MessageCatalog,
    turns: TurnProjection,
    read_call: Callable[[str], Mapping[str, Any]],
    tool_name: Callable[[str], str],
    model_history: Callable[[str, int], tuple[Mapping[str, Any], ...]],
    memory_history: Callable[
        [tuple[str, str] | None, int], tuple[dict[str, object], ...]
    ],
    akasha_records: Callable[[], RecallRecordsRead],
    writer: TraceWriter,
    db_path: Path,
) -> None:
    """周期重扫 owner 历史；诊断失败保持可见并在下一轮重试。"""
    previous_heads: Mapping[str, int] | None = None
    while True:
        try:
            # 每轮单独取得正式 scope；后台 task 不能借用启动回调的授权。
            async with runtime_scope():
                heads = catalog.snapshot_heads()
                if heads != previous_heads:
                    await project_messages(
                        catalog,
                        turns,
                        read_call,
                        tool_name,
                        writer,
                        db_path,
                        heads=heads,
                    )
                    previous_heads = dict(heads)
                await project_model_calls(model_history, writer)
                await project_memory_writes(memory_history, writer)
                await project_akasha(akasha_records(), catalog, writer)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Observe owner projection failed; will retry")
        await asyncio.sleep(2)
