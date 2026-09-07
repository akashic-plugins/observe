"""异步 TraceWriter：把 TurnTrace / RagQueryLog 写入 SQLite。

非阻塞：调用方用 emit() put_nowait，后台 task 消费队列写 DB。
Queue 满时 drop + 计数，不崩溃主循环。
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from .db import open_db
from .events import (
    GlobalErrorTrace,
    MemoryWriteTrace,
    ModelCallTrace,
    RagQueryLog,
    TurnTrace,
)

logger = logging.getLogger("observe.writer")

_QUEUE_MAX = 500
_ARG_MAX = 300
_RESULT_MAX = 500

type TraceEvent = (
    TurnTrace | RagQueryLog | MemoryWriteTrace | ModelCallTrace | GlobalErrorTrace
)
type QueueItem = tuple[TraceEvent, asyncio.Future[None] | None]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _serialize_tool_calls(tool_calls: list[dict]) -> str | None:
    if not tool_calls:
        return None
    slim = [
        {
            "name": c.get("name", ""),
            "args": str(c.get("args", c.get("arguments", "")))[:_ARG_MAX],
            "result": str(c.get("result", ""))[:_RESULT_MAX],
        }
        for c in tool_calls
    ]
    return json.dumps(slim, ensure_ascii=False)


class TraceWriter:
    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._queue: asyncio.Queue[QueueItem] = asyncio.Queue(maxsize=_QUEUE_MAX)
        self._dropped = 0

    # ── 公共接口 ─────────────────────────────────

    def emit(
        self,
        event: (
            TurnTrace
            | RagQueryLog
            | MemoryWriteTrace
            | ModelCallTrace
            | GlobalErrorTrace
        ),
    ) -> None:
        """非阻塞 emit。Queue 满时 drop 并记录计数。"""
        try:
            self._queue.put_nowait((event, None))
        except asyncio.QueueFull:
            self._dropped += 1
            if self._dropped % 100 == 1:
                logger.warning("observe queue full, total_dropped=%d", self._dropped)

    async def submit(self, event: TraceEvent) -> None:
        """为耐久投影提供背压和事务完成确认。"""
        done = asyncio.get_running_loop().create_future()
        await self._queue.put((event, done))
        await done

    async def drain(self) -> None:
        """等待已入队事件写入完成。"""
        await self._queue.join()

    async def run(self) -> None:
        """后台循环，持续消费队列写 DB。作为 asyncio task 运行。"""
        conn = open_db(self._db_path)
        logger.info("observe writer started: %s", self._db_path)
        try:
            while True:
                event, done = await self._queue.get()
                try:
                    self._write_one(conn, event)
                except Exception as error:
                    logger.exception(
                        "observe write failed for %s", type(event).__name__
                    )
                    if done is not None and not done.done():
                        done.set_exception(error)
                else:
                    if done is not None and not done.done():
                        done.set_result(None)
                finally:
                    self._queue.task_done()
        finally:
            # flush remaining on shutdown
            while not self._queue.empty():
                try:
                    e, done = self._queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
                try:
                    self._write_one(conn, e)
                except Exception as error:
                    logger.exception(
                        "observe shutdown flush failed for %s", type(e).__name__
                    )
                    if done is not None and not done.done():
                        done.set_exception(error)
                else:
                    if done is not None and not done.done():
                        done.set_result(None)
                finally:
                    self._queue.task_done()
            conn.close()
            logger.info("observe writer stopped")

    # ── 内部写入 ─────────────────────────────────

    def _write_one(
        self,
        conn,
        event: (
            TurnTrace
            | RagQueryLog
            | MemoryWriteTrace
            | ModelCallTrace
            | GlobalErrorTrace
        ),
    ) -> None:
        ts = _now_iso()
        if isinstance(event, TurnTrace):
            _write_turn(conn, event, ts)
        elif isinstance(event, RagQueryLog):
            _write_rag(conn, event, ts)
        elif isinstance(event, MemoryWriteTrace):
            _write_memory_write(conn, event, ts)
        elif isinstance(event, ModelCallTrace):
            _write_model_call(conn, event)
        elif isinstance(event, GlobalErrorTrace):
            _write_global_error(conn, event)


# ── DB 写入函数 ───────────────────────────────────────────────────────────────


def _write_turn(conn, e: TurnTrace, ts: str) -> None:
    ts = e.recorded_at or ts
    with conn:
        if e.projection_key is not None and e.assistant_message_id is not None:
            existing = conn.execute(
                "SELECT projection_key FROM turns WHERE assistant_message_id=?",
                (e.assistant_message_id,),
            ).fetchone()
            if existing is not None:
                if existing[0] not in (None, e.projection_key):
                    raise RuntimeError("既有 Observe Turn 对应另一投影身份")
                conn.execute(
                    "UPDATE turns SET projection_key=? "
                    "WHERE assistant_message_id=? AND projection_key IS NULL",
                    (e.projection_key, e.assistant_message_id),
                )
                _advance_turn_cursor(conn, e)
                return
        cursor = conn.execute(
            """
            INSERT INTO turns (
                ts, source, session_key, turn_id, assistant_message_id,
                user_msg, llm_output,
                raw_llm_output, meme_tag, meme_media_count,
                tool_calls, tool_chain_json,
                history_window, history_messages, history_chars,
                history_tokens, prompt_tokens, next_turn_baseline_tokens,
                react_iteration_count, react_input_sum_tokens,
                react_input_peak_tokens, react_final_input_tokens,
                model_output_tokens,
                react_cache_prompt_tokens, react_cache_hit_tokens,
                error, projection_key
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(projection_key) WHERE projection_key IS NOT NULL DO NOTHING
            """,
            (
                ts,
                e.source,
                e.session_key,
                e.turn_id,
                e.assistant_message_id,
                e.user_msg,
                e.llm_output,
                e.raw_llm_output,
                e.meme_tag,
                e.meme_media_count,
                _serialize_tool_calls(e.tool_calls),
                e.tool_chain_json,
                e.history_window,
                e.history_messages,
                e.history_chars,
                e.history_tokens,
                e.prompt_tokens,
                e.next_turn_baseline_tokens,
                e.react_iteration_count,
                e.react_input_sum_tokens,
                e.react_input_peak_tokens,
                e.react_final_input_tokens,
                e.model_output_tokens,
                e.react_cache_prompt_tokens,
                e.react_cache_hit_tokens,
                e.error,
                e.projection_key,
            ),
        )
        if cursor.rowcount == 0:
            return
        turn_id = int(cursor.lastrowid)
        tracked = e.react_cache_prompt_tokens is not None
        prompt_tokens = int(e.react_cache_prompt_tokens or 0)
        hit_tokens = int(e.react_cache_hit_tokens or 0)
        passive = tracked and e.source == "agent"
        proactive = tracked and e.source in {"proactive", "drift"}
        totals_update = conn.execute(
            """
            UPDATE kv_cache_totals SET
                turn_count = turn_count + 1,
                tracked_turn_count = tracked_turn_count + ?,
                prompt_tokens = prompt_tokens + ?,
                hit_tokens = hit_tokens + ?,
                passive_prompt_tokens = passive_prompt_tokens + ?,
                passive_hit_tokens = passive_hit_tokens + ?,
                passive_tracked_turn_count = passive_tracked_turn_count + ?,
                proactive_prompt_tokens = proactive_prompt_tokens + ?,
                proactive_hit_tokens = proactive_hit_tokens + ?,
                proactive_tracked_turn_count = proactive_tracked_turn_count + ?,
                last_tracked_at = CASE WHEN ? THEN ? ELSE last_tracked_at END
            WHERE id = 1
            """,
            (
                int(tracked),
                prompt_tokens,
                hit_tokens,
                prompt_tokens if passive else 0,
                hit_tokens if passive else 0,
                int(passive),
                prompt_tokens if proactive else 0,
                hit_tokens if proactive else 0,
                int(proactive),
                int(tracked),
                ts,
            ),
        )
        if totals_update.rowcount != 1:
            raise RuntimeError("KV Cache 聚合投影缺少 singleton 行")
        state_update = conn.execute(
            "UPDATE kv_cache_projection_state SET last_turn_id = ? WHERE id = 1",
            (turn_id,),
        )
        if state_update.rowcount != 1:
            raise RuntimeError("KV Cache 投影水位缺少 singleton 行")
        _advance_turn_cursor(conn, e)


def _advance_turn_cursor(conn, event: TurnTrace) -> None:
    """与 Turn receipt 同事务推进已闭合前缀，open Turn 从不调用。"""
    if event.projection_key is None:
        return
    if event.through_seq is None:
        raise ValueError("Message Turn 投影缺少 through_seq")
    conn.execute(
        """
        INSERT INTO projection_cursors(domain, scope, through_seq, ending_id)
        VALUES ('turn', ?, ?, ?)
        ON CONFLICT(domain, scope) DO UPDATE SET
            through_seq=MAX(through_seq, excluded.through_seq),
            ending_id=CASE
                WHEN excluded.through_seq >= through_seq THEN excluded.ending_id
                ELSE ending_id
            END
        """,
        (
            f"{event.session_key}\n{event.projection_source or event.source}",
            event.through_seq,
            event.assistant_message_id,
        ),
    )


def _write_rag(conn, e: RagQueryLog, ts: str) -> None:
    ts = e.recorded_at or ts
    hits_json = (
        json.dumps(
            [
                {
                    "id": h.item_id,
                    "type": h.memory_type,
                    "score": round(h.score, 4),
                    "summary": h.summary,
                    "injected": h.injected,
                }
                for h in e.hits
            ],
            ensure_ascii=False,
        )
        if e.hits
        else None
    )
    with conn:
        if e.projection_key is not None and _has_receipt(
            conn, "akasha", e.projection_key
        ):
            return
        conn.execute(
            """
            INSERT INTO rag_queries (
                ts, caller, session_key, query, orig_query,
                aux_queries, hits_json, injected_count, route_decision, error,
                projection_key
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(projection_key) WHERE projection_key IS NOT NULL DO NOTHING
            """,
            (
                ts,
                e.caller,
                e.session_key,
                e.query,
                e.orig_query,
                (
                    json.dumps(e.aux_queries, ensure_ascii=False)
                    if e.aux_queries
                    else None
                ),
                hits_json,
                e.injected_count,
                e.route_decision,
                e.error,
                e.projection_key,
            ),
        )
        _record_receipt(conn, "akasha", e.projection_key, ts)


_SESSION_KEYS_CAP = 20


# 按 (fingerprint, bucket) UPSERT：已存在则累加 count、推进 last_ts、合并 session_keys，
# 沿用首次插入的代表样本（error_type / message / traceback_text 等）。
def _write_global_error(conn, e: GlobalErrorTrace) -> None:
    with conn:
        existing = conn.execute(
            "SELECT count, last_ts, session_keys FROM global_errors WHERE fingerprint = ? AND bucket = ?",
            (e.fingerprint, e.bucket),
        ).fetchone()
        if existing is None:
            conn.execute(
                """
                INSERT INTO global_errors (
                    fingerprint, bucket, source, logger_name, error_type, message,
                    traceback_text, level, first_ts, last_ts, count, session_keys, status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active')
                """,
                (
                    e.fingerprint,
                    e.bucket,
                    e.source,
                    e.logger_name,
                    e.error_type,
                    e.message,
                    e.traceback_text,
                    e.level,
                    e.first_ts,
                    e.last_ts,
                    e.count,
                    _merge_session_keys([], e.session_keys),
                ),
            )
            return
        prev_count = int(existing[0] or 0)
        prev_last_ts = str(existing[1] or e.last_ts)
        prev_keys = _parse_session_keys(existing[2])
        conn.execute(
            """
            UPDATE global_errors
            SET count = ?, last_ts = ?, session_keys = ?
            WHERE fingerprint = ? AND bucket = ?
            """,
            (
                prev_count + e.count,
                max(prev_last_ts, e.last_ts),
                _merge_session_keys(prev_keys, e.session_keys),
                e.fingerprint,
                e.bucket,
            ),
        )


def _parse_session_keys(raw: object) -> list[str]:
    if not raw:
        return []
    try:
        data = json.loads(str(raw))
    except (ValueError, TypeError):
        return []
    return [str(x) for x in data] if isinstance(data, list) else []


def _merge_session_keys(prev: list[str], new: list[str]) -> str | None:
    merged: list[str] = list(prev)
    for key in new:
        if key and key not in merged:
            merged.append(key)
        if len(merged) >= _SESSION_KEYS_CAP:
            break
    return (
        json.dumps(merged[:_SESSION_KEYS_CAP], ensure_ascii=False) if merged else None
    )


def _write_memory_write(conn, e: MemoryWriteTrace, ts: str) -> None:
    import json as _json

    ts = e.recorded_at or ts
    with conn:
        if e.projection_key is not None and _has_receipt(
            conn, "markdown", e.projection_key
        ):
            return
        conn.execute(
            """
            INSERT INTO memory_writes (
                ts, session_key, source_ref, action, memory_type, item_id,
                summary, superseded_ids, error, projection_key
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(projection_key) WHERE projection_key IS NOT NULL DO NOTHING
            """,
            (
                ts,
                e.session_key,
                e.source_ref,
                e.action,
                e.memory_type,
                e.item_id,
                e.summary,
                (
                    _json.dumps(e.superseded_ids, ensure_ascii=False)
                    if e.superseded_ids
                    else None
                ),
                e.error,
                e.projection_key,
            ),
        )
        _record_receipt(conn, "markdown", e.projection_key, ts)


def _has_receipt(conn, domain: str, identity: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM projection_receipts WHERE domain=? AND identity=?",
            (domain, identity),
        ).fetchone()
        is not None
    )


def _record_receipt(conn, domain: str, identity: str | None, recorded_at: str) -> None:
    if identity is None:
        return
    conn.execute(
        "INSERT INTO projection_receipts(domain, identity, recorded_at) "
        "VALUES (?, ?, ?)",
        (domain, identity, recorded_at),
    )


def _write_model_call(conn, event: ModelCallTrace) -> None:
    """调用记录按 owner ID 更新 started 状态，终态重扫保持幂等。"""
    usage = (
        json.dumps(event.usage, ensure_ascii=False, sort_keys=True)
        if event.usage is not None
        else None
    )
    with conn:
        conn.execute(
            """
            INSERT INTO model_calls (
                call_id, state, model, started_at, finished_at,
                first_token_ms, duration_ms, usage_json, failure
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(call_id) DO UPDATE SET
                state=excluded.state,
                model=excluded.model,
                finished_at=excluded.finished_at,
                first_token_ms=excluded.first_token_ms,
                duration_ms=excluded.duration_ms,
                usage_json=excluded.usage_json,
                failure=excluded.failure
            """,
            (
                event.call_id,
                event.state,
                event.model,
                event.started_at,
                event.finished_at,
                event.first_token_ms,
                event.duration_ms,
                usage,
                event.failure,
            ),
        )
