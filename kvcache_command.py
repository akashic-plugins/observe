from __future__ import annotations

import logging
import re
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Protocol, cast, final
from zoneinfo import ZoneInfo

from agent.lifecycle.types import BeforeTurnCtx, TurnState
from agent.prompting import is_context_frame

logger = logging.getLogger("plugin.observe.kvcache_command")

_SESSION_SLOT = "session:session"
_CTX_SLOT = "session:ctx"
_TS_PATTERN = re.compile(r"(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})")
_BEIJING_TZ = ZoneInfo("Asia/Shanghai")


class _BeforeTurnFrame(Protocol):
    input: TurnState
    slots: dict[str, object]


@final
class KVCacheCommandModule:
    """把 Observe 自有的 KV Cache 数据渲染成短路命令回复。"""

    slot = "observe.kvcache_command"
    requires = ("before_turn.acquire_session", _SESSION_SLOT)
    produces = (_CTX_SLOT,)

    def __init__(self, db_path: Path | None) -> None:
        self._db_path = db_path

    async def run(self, frame: object) -> object:
        typed_frame = cast(_BeforeTurnFrame, frame)
        if _CTX_SLOT in typed_frame.slots:
            return frame
        state = typed_frame.input
        if _normalize_command(state.msg.content) not in {"/kvcache", "/cache_status"}:
            return frame
        typed_frame.slots[_CTX_SLOT] = _abort_ctx(state, self._build_reply(state))
        return frame

    def _build_reply(self, state: TurnState) -> str:
        """读取当前会话快照并保持既有命令结果。"""

        # 1. 将缺少 Observe 状态表达为有效的空诊断结果
        db_path = self._db_path
        if db_path is None or not db_path.exists():
            return "暂无 KVCache 数据（observe 数据库不存在）。"
        limit = _command_limit(state.msg.content)

        # 2. 只查询当前 Session 的 Observe 自有数据
        try:
            with sqlite3.connect(str(db_path)) as connection:
                raw_rows = connection.execute(
                    """
                    SELECT llm_output, ts,
                           react_cache_prompt_tokens, react_cache_hit_tokens
                    FROM turns
                    WHERE session_key=? AND react_cache_prompt_tokens IS NOT NULL
                    ORDER BY id DESC LIMIT ?
                    """,
                    (state.session_key, limit),
                ).fetchall()
                rows = cast(
                    list[tuple[object, object, object, object]],
                    raw_rows,
                )
        except sqlite3.Error:
            logger.exception("KVCache 查询失败")
            return "KVCache 查询失败。"

        # 3. 保持既有用户结果，不向 Core 泄露 schema
        if not rows:
            return "暂无 KVCache 数据。"
        return _format_reply(rows)


def _normalize_command(content: str) -> str:
    parts = (content or "").strip().split(maxsplit=1)
    if not parts:
        return ""
    return parts[0].lower().split("@", 1)[0]


def _command_limit(content: str) -> int:
    args = (content or "").strip().split()
    if len(args) <= 1:
        return 5
    try:
        return max(1, min(30, int(args[1])))
    except ValueError:
        return 5


def _format_reply(
    rows: list[tuple[object, object, object, object]],
) -> str:
    overall_prompt = sum(_db_int(row[2]) for row in rows)
    overall_hit = sum(_db_int(row[3]) for row in rows)
    overall_pct = (overall_hit / overall_prompt * 100) if overall_prompt > 0 else 0.0
    lines = [
        f"⚡ KVCache · 最近 {len(rows)} 轮",
        "",
        f"命中率  {overall_pct:.1f}%  {_pct_bar(overall_pct)}",
        f"Token  {overall_hit:,} / {overall_prompt:,}",
    ]
    for llm_output, ts, prompt_tokens, hit_tokens in rows:
        content = _content_to_text(_db_text(llm_output))
        if is_context_frame(content):
            content = ""
        preview = _preview_text(content, limit=72)
        hit = _db_int(hit_tokens)
        prompt = _db_int(prompt_tokens)
        pct = (hit / prompt * 100) if prompt > 0 else 0.0
        lines.extend(
            [
                "",
                "",
                f"{_format_ts(_db_text(ts))}   {_pct_emoji(pct)} {pct:.1f}%  {_pct_bar(pct)}",
                f"    {hit:,} / {prompt:,} tokens",
            ]
        )
        if preview:
            lines.append(f"    {preview}")
    return "\n".join(lines)


def _db_int(value: object) -> int:
    if value is None:
        return 0
    if not isinstance(value, int) or isinstance(value, bool):
        raise RuntimeError("observe.db KV Cache token 字段不是整数")
    return value


def _db_text(value: object) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise RuntimeError("observe.db KV Cache 文本字段不是字符串")
    return value


def _abort_ctx(state: TurnState, reply: str) -> BeforeTurnCtx:
    return BeforeTurnCtx(
        session_key=state.session_key,
        channel=state.msg.channel,
        chat_id=state.msg.chat_id,
        content=state.msg.content,
        timestamp=state.msg.timestamp,
        skill_names=[],
        retrieved_memory_block="",
        retrieval_trace_raw=None,
        history_messages=(),
        abort=True,
        abort_reply=reply,
    )


def _format_ts(ts: str) -> str:
    try:
        parsed = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone(_BEIJING_TZ)
        return f"{parsed.month}-{parsed.day} {parsed.hour:02d}:{parsed.minute:02d}"
    except ValueError:
        match = _TS_PATTERN.search(ts)
        if match:
            return f"{int(match.group(2))}-{int(match.group(3))} {match.group(4)}:{match.group(5)}"
        return ts


def _content_to_text(content: object) -> str:
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        raw_items = cast(list[object], content)
        parts: list[str] = []
        for item in raw_items:
            if not isinstance(item, dict):
                continue
            mapping = cast(dict[object, object], item)
            if mapping.get("type") == "text":
                parts.append(str(mapping.get("text", "")).strip())
        return "\n".join(part for part in parts if part).strip()
    return str(content).strip()


def _preview_text(text: str, limit: int = 80) -> str:
    normalized = " ".join(text.split())
    if len(normalized) <= limit:
        return normalized
    return normalized[: limit - 1] + "…"


def _pct_bar(pct: float, width: int = 10) -> str:
    filled = max(0, min(width, round(pct / 100 * width)))
    return "█" * filled + "░" * (width - filled)


def _pct_emoji(pct: float) -> str:
    if pct >= 80:
        return "🟢"
    if pct >= 40:
        return "🟡"
    return "🔴"
