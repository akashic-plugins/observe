"""Observe 使用的公开能力结构合同。

这些声明只描述外部 owner 已发布的窄读口；Observe 不导入任何业务插件实现。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from typing import Any, Literal, Protocol

from agent.plugin_composition import ServiceKey
from agent.plugin_contracts import CallRef, Message


class Turn(Protocol):
    source: str
    through_seq: int
    ending_message_id: str | None
    status: Literal["open", "complete", "quiet", "abandoned"]
    message_ids: tuple[str, ...]
    observations: tuple[tuple[CallRef, str], ...]


class TurnProjection(Protocol):
    def project(self, messages: Sequence[Message], source: str) -> tuple[Turn, ...]: ...


TURN_PROJECTION = ServiceKey[TurnProjection]("turn.projection.v1")


class RecallHit(Protocol):
    session_id: str
    message_ids: tuple[str, ...]
    score: float


class RecallContextSource(Protocol):
    kind: Literal["context"]
    session_id: str


class RecallProgramSource(Protocol):
    kind: Literal["program"]
    query: str


class RecallToolSource(Protocol):
    kind: Literal["tool"]
    session_id: str


class Recall(Protocol):
    source: RecallContextSource | RecallProgramSource | RecallToolSource
    timestamp: datetime
    hits: tuple[RecallHit, ...]
    presented_message_ids: tuple[str, ...]


class RecallRecordsRead(Protocol):
    def list(self) -> tuple[tuple[str, Recall], ...]: ...


AKASHA_RECORDS_VIEW = ServiceKey[Callable[[], RecallRecordsRead]](
    "akasha.recall-records.v1"
)
MODEL_CALLS = ServiceKey[Callable[[str], Mapping[str, Any]]]("models.calls.v1")
MODEL_CALL_HISTORY = ServiceKey[
    Callable[[str, int], tuple[Mapping[str, Any], ...]]
]("models.call-history.v1")
MEMORY_WRITES = ServiceKey[
    Callable[[tuple[str, str] | None, int], tuple[dict[str, object], ...]]
]("markdown-memory.writes.v1")
TOOL_DISPLAY_NAME = ServiceKey[Callable[[str], str]]("tools.display-name.v1")


__all__ = [
    "AKASHA_RECORDS_VIEW",
    "MEMORY_WRITES",
    "MODEL_CALL_HISTORY",
    "MODEL_CALLS",
    "Recall",
    "RecallContextSource",
    "RecallHit",
    "RecallProgramSource",
    "RecallRecordsRead",
    "RecallToolSource",
    "TOOL_DISPLAY_NAME",
    "TURN_PROJECTION",
    "Turn",
    "TurnProjection",
]
