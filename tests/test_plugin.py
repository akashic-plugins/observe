from __future__ import annotations

import asyncio
import importlib.util
import sqlite3
import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent.plugins.context import PluginContext, PluginKVStore
from agent.plugins.scope import PluginScope, ScopedEventBus
from bus.event_bus import EventBus
from bus.events_lifecycle import TurnCommitted


def _load_plugin_module():
    path = Path(__file__).parents[1] / "plugin.py"
    spec = importlib.util.spec_from_file_location(
        "test_observe_plugin",
        path,
        submodule_search_locations=[str(path.parent)],
    )
    if spec is None or spec.loader is None:
        raise ImportError(str(path))
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


module = _load_plugin_module()
ObservePlugin = module.ObservePlugin
GlobalErrorCollector = module.GlobalErrorCollector


class _Emitter:
    def __init__(self) -> None:
        self.events: list[object] = []

    def emit(self, event: object) -> None:
        self.events.append(event)


@pytest.mark.asyncio
async def test_observe_plugin_initialize_and_terminate(tmp_path: Path) -> None:
    plugin = ObservePlugin()
    scope = PluginScope("observe")
    plugin.context = PluginContext(
        event_bus=ScopedEventBus(EventBus(), scope),
        tool_registry=None,
        plugin_id="observe",
        plugin_dir=tmp_path,
        data_dir=tmp_path,
        kv_store=PluginKVStore(tmp_path / ".kv.json"),
        workspace=tmp_path,
        scope=scope,
    )
    await plugin.initialize()
    await asyncio.sleep(0.05)
    await plugin.terminate()
    assert await scope.aclose() == []
    db_path = tmp_path / "observe" / "observe.db"
    assert db_path.exists()
    conn = sqlite3.connect(db_path)
    try:
        tables = {row[0] for row in conn.execute("select name from sqlite_master where type='table'")}
    finally:
        conn.close()
    assert "turns" in tables


@pytest.mark.asyncio
async def test_global_hooks_survive_overlapping_generations() -> None:
    loop = asyncio.get_running_loop()
    original_sys = sys.excepthook
    original_thread = threading.excepthook
    original_loop = loop.get_exception_handler()
    first = GlobalErrorCollector(_Emitter())
    second = GlobalErrorCollector(_Emitter())
    try:
        first.install()
        second.install()
        await first.uninstall()

        assert sys.excepthook == second._on_sys_except
        assert threading.excepthook == second._on_thread_except
        assert loop.get_exception_handler() == second._on_loop_except

        await second.uninstall()
        assert sys.excepthook == original_sys
        assert threading.excepthook == original_thread
        assert loop.get_exception_handler() == original_loop
    finally:
        await second.uninstall()
        await first.uninstall()


def test_turn_trace_keeps_message_identity_and_output_tokens(tmp_path: Path) -> None:
    emitter = _Emitter()
    module._emit_turn_trace(
        emitter,
        TurnCommitted(
            session_key="mobile:demo",
            channel="mobile",
            chat_id="demo",
            input_message="hi",
            persisted_user_message="hi",
            assistant_response="hello",
            tools_used=[],
            turn_id="turn-1",
            assistant_message_id="mobile:demo:2",
            model_usage={"output_tokens": 321, "coverage": "exact"},
        ),
    )

    trace = emitter.events[0]
    assert trace.turn_id == "turn-1"
    assert trace.assistant_message_id == "mobile:demo:2"
    assert trace.model_output_tokens == 321
    db_module = sys.modules[f"{module.__name__}.db"]
    conn = db_module.open_db(tmp_path / "observe.db")
    try:
        module.TraceWriter(tmp_path / "observe.db")._write_one(conn, trace)
        row = conn.execute(
            "SELECT turn_id, assistant_message_id, model_output_tokens FROM turns"
        ).fetchone()
    finally:
        conn.close()
    assert row == ("turn-1", "mobile:demo:2", 321)


def test_partial_usage_and_empty_turn_ids_do_not_claim_complete_output(
    tmp_path: Path,
) -> None:
    emitter = _Emitter()
    for index in range(2):
        module._emit_turn_trace(
            emitter,
            TurnCommitted(
                session_key="mobile:demo",
                channel="mobile",
                chat_id="demo",
                input_message="hi",
                persisted_user_message="hi",
                assistant_response="hello",
                tools_used=[],
                assistant_message_id=f"mobile:demo:{index + 2}",
                model_usage={"output_tokens": 100, "coverage": "partial"},
            ),
        )

    assert all(event.turn_id is None for event in emitter.events)
    assert all(event.model_output_tokens is None for event in emitter.events)
    db_module = sys.modules[f"{module.__name__}.db"]
    conn = db_module.open_db(tmp_path / "observe.db")
    try:
        writer = module.TraceWriter(tmp_path / "observe.db")
        for event in emitter.events:
            writer._write_one(conn, event)
        count = conn.execute("SELECT COUNT(*) FROM turns").fetchone()[0]
    finally:
        conn.close()
    assert count == 2


@pytest.mark.asyncio
async def test_mobile_message_usage_returns_true_output_tokens(tmp_path: Path) -> None:
    plugin = ObservePlugin()
    plugin.context = SimpleNamespace(workspace=tmp_path)
    db_module = sys.modules[f"{module.__name__}.db"]
    conn = db_module.open_db(tmp_path / "observe" / "observe.db")
    try:
        conn.execute(
            """
            INSERT INTO turns(
                ts, source, session_key, turn_id, assistant_message_id,
                user_msg, llm_output, model_output_tokens
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "2026-07-17T00:00:00+00:00",
                "agent",
                "mobile:demo",
                "turn-1",
                "mobile:demo:2",
                "hi",
                "hello",
                321,
            ),
        )
        conn.commit()
    finally:
        conn.close()

    result = await plugin.mobile_ui_call(
        "kvcache.message_usage",
        {"message_id": "mobile:demo:2"},
        session_id="mobile:demo",
        turn_id=None,
    )

    assert result == {"usage": {"output_tokens": 321}}
    assert ObservePlugin.mobile_ui_module() == "mobile_panel.js"
    assert ObservePlugin.mobile_ui_stylesheet() == "mobile_panel.css"


@pytest.mark.asyncio
async def test_mobile_health_reuses_global_error_projection(tmp_path: Path) -> None:
    plugin = ObservePlugin()
    plugin.context = SimpleNamespace(workspace=tmp_path)
    db_module = sys.modules[f"{module.__name__}.db"]
    conn = db_module.open_db(tmp_path / "observe" / "observe.db")
    now = datetime.now(timezone.utc)
    traceback_text = "Traceback\n" + ("failure detail\n" * 500)
    try:
        for offset, count in ((2, 1), (1, 1), (0, 5)):
            moment = now - timedelta(hours=offset)
            conn.execute(
                """
                INSERT INTO global_errors(
                    fingerprint, bucket, source, logger_name, error_type,
                    message, traceback_text, level, first_ts, last_ts,
                    count, session_keys, status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "fp-mobile-health",
                    moment.isoformat()[:13],
                    "asyncio",
                    "agent.worker",
                    "RuntimeError",
                    "background task failed",
                    traceback_text,
                    "ERROR",
                    (now - timedelta(hours=2)).isoformat(),
                    moment.isoformat(),
                    count,
                    '["mobile:demo"]',
                    "active",
                ),
            )
        conn.commit()
    finally:
        conn.close()

    overview = await plugin.mobile_ui_call(
        "health.overview",
        {"range": "24h"},
        session_id=None,
        turn_id=None,
    )
    errors = await plugin.mobile_ui_call(
        "health.errors",
        {"range": "24h"},
        session_id=None,
        turn_id=None,
    )
    detail = await plugin.mobile_ui_call(
        "health.error_detail",
        {"range": "24h", "fingerprint": "fp-mobile-health"},
        session_id=None,
        turn_id=None,
    )

    assert overview["total"] == 7
    assert overview["types"] == 1
    assert overview["spiking_types"] == 1
    assert errors["types"] == 1
    item = errors["items"][0]
    assert item["error_type"] == "RuntimeError"
    assert "traceback" not in item
    assert detail["error"]["traceback"].startswith("Traceback")
    assert len(detail["error"]["traceback"]) == 4000

    with pytest.raises(ValueError, match="range 只支持"):
        await plugin.mobile_ui_call(
            "health.overview",
            {"range": "all"},
            session_id=None,
            turn_id=None,
        )


def test_open_db_removes_legacy_unique_turn_id_index(tmp_path: Path) -> None:
    db_path = tmp_path / "observe.db"
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(
            """
            CREATE TABLE turns(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL,
                source TEXT NOT NULL,
                session_key TEXT NOT NULL,
                turn_id TEXT
            );
            CREATE UNIQUE INDEX ux_turns_turn_id
            ON turns (turn_id) WHERE turn_id IS NOT NULL;
            """
        )
        conn.commit()
    finally:
        conn.close()

    db_module = sys.modules[f"{module.__name__}.db"]
    migrated = db_module.open_db(db_path)
    try:
        legacy_index = migrated.execute(
            "SELECT 1 FROM sqlite_master WHERE type='index' AND name='ux_turns_turn_id'"
        ).fetchone()
    finally:
        migrated.close()
    assert legacy_index is None
