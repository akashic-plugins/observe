from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Any, Generator, Literal
import sqlite3
import threading


class KVCacheDashboardReader:
    """从 observe 数据库读取桌面与移动端共用的 KV Cache 投影。"""

    def __init__(self, workspace: Path) -> None:
        self.db_path = workspace / "observe" / "observe.db"
        self._lock = threading.RLock()

    def get_summary(self) -> dict[str, Any]:
        if not self.db_path.exists():
            return _summary_from_row(None)
        with self._lock:
            with _connect(self.db_path) as db:
                db.execute("BEGIN")
                row, _, _ = _checked_projection(db)
        return _summary_from_row(row)

    def get_bootstrap(self) -> dict[str, Any]:
        """在一个 SQLite 快照中返回移动首屏的聚合与两组最近记录。"""

        if not self.db_path.exists():
            empty = _summary_from_row(None)
            return {
                "overview": empty,
                "recent": {"items": [], "total": 0},
                "recent_agent": {"items": [], "total": 0},
                "snapshot_turn_id": 0,
                "projection_through_turn_id": 0,
            }

        # 1. 固定同一个读事务，聚合与列表共享快照水位
        with self._lock:
            with _connect(self.db_path) as db:
                db.execute("BEGIN")
                row, snapshot_turn_id, projection_turn_id = _checked_projection(db)
                overview = _summary_from_row(row)

                # 2. 列表总数直接来自 O(1) 投影，只读取首屏需要的行
                recent = _list_turns_in_snapshot(
                    db,
                    page_size=50,
                    source=None,
                    total=int(row["tracked_turn_count"]),
                )
                recent_agent = _list_turns_in_snapshot(
                    db,
                    page_size=10,
                    source="agent",
                    total=int(row["passive_tracked_turn_count"]),
                )
        return {
            "overview": overview,
            "recent": recent,
            "recent_agent": recent_agent,
            "snapshot_turn_id": snapshot_turn_id,
            "projection_through_turn_id": projection_turn_id,
        }

    def list_turns(
        self,
        *,
        page: int = 1,
        page_size: int = 25,
        source: Literal["agent"] | None = None,
    ) -> tuple[list[dict[str, Any]], int]:
        if not self.db_path.exists():
            return [], 0
        safe_page = max(1, page)
        safe_size = max(1, min(page_size, 100))
        offset = (safe_page - 1) * safe_size
        source_filter = "" if source is None else " AND source = 'agent'"
        with self._lock:
            with _connect(self.db_path) as db:
                total_row = db.execute(f"""
                    SELECT COUNT(*) AS total
                    FROM turns
                    WHERE react_cache_prompt_tokens IS NOT NULL
                    {source_filter}
                    """).fetchone()
                rows = db.execute(
                    f"""
                    SELECT
                        id,
                        ts,
                        source,
                        session_key,
                        user_msg,
                        react_cache_prompt_tokens AS prompt_tokens,
                        react_cache_hit_tokens AS hit_tokens
                    FROM turns
                    WHERE react_cache_prompt_tokens IS NOT NULL
                    {source_filter}
                    ORDER BY ts DESC, id DESC
                    LIMIT ? OFFSET ?
                    """,
                    (safe_size, offset),
                ).fetchall()
        total = int(total_row["total"] or 0) if total_row is not None else 0
        return [_row_to_cache_turn(row) for row in rows], total

    def get_turn(self, turn_id: int) -> dict[str, Any] | None:
        if not self.db_path.exists():
            return None
        with self._lock:
            with _connect(self.db_path) as db:
                row = db.execute(
                    """
                    SELECT
                        id,
                        ts,
                        source,
                        session_key,
                        user_msg,
                        react_cache_prompt_tokens AS prompt_tokens,
                        react_cache_hit_tokens AS hit_tokens
                    FROM turns
                    WHERE id = ? AND react_cache_prompt_tokens IS NOT NULL
                    """,
                    (turn_id,),
                ).fetchone()
        return _row_to_cache_turn(row) if row is not None else None

    def get_message_usage(
        self,
        *,
        message_id: str,
        session_key: str,
    ) -> dict[str, Any] | None:
        """返回消息对应 Turn 的真实模型输出 token。"""

        if not self.db_path.exists():
            return None
        with self._lock:
            with _connect(self.db_path) as db:
                row = db.execute(
                    """
                    SELECT
                        model_output_tokens AS output_tokens
                    FROM turns
                    WHERE assistant_message_id = ?
                      AND session_key = ?
                      AND source = 'agent'
                      AND model_output_tokens IS NOT NULL
                    """,
                    (message_id, session_key),
                ).fetchone()
                if row is None:
                    return None
        return {"output_tokens": int(row["output_tokens"])}


def _summary_from_row(row: sqlite3.Row | None) -> dict[str, Any]:
    prompt_tokens = int(row["prompt_tokens"] or 0) if row is not None else 0
    hit_tokens = int(row["hit_tokens"] or 0) if row is not None else 0
    miss_tokens = max(0, prompt_tokens - hit_tokens)
    passive = _source_summary_from_row(row, "passive")
    proactive = _source_summary_from_row(row, "proactive")
    return {
        "turn_count": int(row["turn_count"] or 0) if row is not None else 0,
        "tracked_turn_count": int(row["tracked_turn_count"] or 0) if row is not None else 0,
        "prompt_tokens": prompt_tokens,
        "hit_tokens": hit_tokens,
        "miss_tokens": miss_tokens,
        "hit_rate": (hit_tokens / prompt_tokens) if prompt_tokens > 0 else None,
        "last_tracked_at": row["last_tracked_at"] if row is not None else None,
        "passive": passive,
        "proactive": proactive,
    }


def _checked_projection(
    db: sqlite3.Connection,
) -> tuple[sqlite3.Row, int, int]:
    row = db.execute("SELECT * FROM kv_cache_totals WHERE id = 1").fetchone()
    state = db.execute(
        "SELECT schema_version, last_turn_id FROM kv_cache_projection_state WHERE id = 1"
    ).fetchone()
    snapshot_turn_id = int(
        db.execute("SELECT COALESCE(MAX(id), 0) FROM turns").fetchone()[0]
    )
    if row is None or state is None or int(state["schema_version"]) != 1:
        raise RuntimeError("KV Cache 投影尚未初始化")
    projection_turn_id = int(state["last_turn_id"])
    if projection_turn_id != snapshot_turn_id:
        raise RuntimeError(
            "KV Cache 投影水位不一致: "
            f"projection={projection_turn_id}, turns={snapshot_turn_id}"
        )
    return row, snapshot_turn_id, projection_turn_id


def _list_turns_in_snapshot(
    db: sqlite3.Connection,
    *,
    page_size: int,
    source: Literal["agent"] | None,
    total: int,
) -> dict[str, Any]:
    source_filter = "" if source is None else " AND source = 'agent'"
    rows = db.execute(
        f"""
        SELECT
            id, ts, source, session_key, user_msg,
            react_cache_prompt_tokens AS prompt_tokens,
            react_cache_hit_tokens AS hit_tokens
        FROM turns
        WHERE react_cache_prompt_tokens IS NOT NULL
        {source_filter}
        ORDER BY ts DESC, id DESC
        LIMIT ?
        """,
        (page_size,),
    ).fetchall()
    return {"items": [_row_to_cache_turn(row) for row in rows], "total": total}


def _source_summary_from_row(row: sqlite3.Row | None, prefix: str) -> dict[str, Any]:
    prompt_tokens = int(row[f"{prefix}_prompt_tokens"] or 0) if row is not None else 0
    hit_tokens = int(row[f"{prefix}_hit_tokens"] or 0) if row is not None else 0
    miss_tokens = max(0, prompt_tokens - hit_tokens)
    return {
        "tracked_turn_count": int(row[f"{prefix}_tracked_turn_count"] or 0) if row is not None else 0,
        "prompt_tokens": prompt_tokens,
        "hit_tokens": hit_tokens,
        "miss_tokens": miss_tokens,
        "hit_rate": (hit_tokens / prompt_tokens) if prompt_tokens > 0 else None,
    }


@contextmanager
def _connect(db_path: Path) -> Generator[sqlite3.Connection, None, None]:
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def _row_to_cache_turn(row: sqlite3.Row) -> dict[str, Any]:
    prompt_tokens = int(row["prompt_tokens"] or 0)
    hit_tokens = int(row["hit_tokens"] or 0)
    miss_tokens = max(0, prompt_tokens - hit_tokens)
    return {
        "id": int(row["id"]),
        "ts": row["ts"],
        "source": row["source"],
        "session_key": row["session_key"],
        "user_preview": _preview_text(row["user_msg"], 90),
        "prompt_tokens": prompt_tokens,
        "hit_tokens": hit_tokens,
        "miss_tokens": miss_tokens,
        "hit_rate": (hit_tokens / prompt_tokens) if prompt_tokens > 0 else None,
    }


def _preview_text(value: Any, limit: int) -> str:
    text = str(value or "").replace("\n", " ").strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "..."
