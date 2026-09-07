from __future__ import annotations

import asyncio
import importlib.util
import shutil
import sqlite3
import sys
import threading
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI

from agent.plugin_composition import DashboardContext
from agent.plugins.composable import ComposablePlugin
from agent.plugins.manager import PluginManager
from agent.plugins.static_manifest import load_static_plugin_manifest
from bus.event_bus import EventBus
from session.log import MessageLog, SessionAttributes
from session.message import (
    CallRef,
    ContentPart,
    ContentReferences,
    Input,
    Output,
    ToolCall,
    ToolResult,
)


def _load_plugin_module():
    path = Path(__file__).parents[1] / "plugin.py"
    spec = importlib.util.spec_from_file_location(
        "test_observe_plugin", path, submodule_search_locations=[str(path.parent)]
    )
    if spec is None or spec.loader is None:
        raise ImportError(str(path))
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


module = _load_plugin_module()
GlobalErrorCollector = module.GlobalErrorCollector


class _Emitter:
    def __init__(self) -> None:
        self.events: list[Any] = []

    def emit(self, event: object) -> None:
        self.events.append(event)


async def _wait_rows(db_path: Path, table: str, count: int) -> None:
    for _ in range(200):
        if db_path.exists():
            with sqlite3.connect(db_path) as connection:
                actual = connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[
                    0
                ]
            if actual >= count:
                return
        await asyncio.sleep(0.02)
    raise AssertionError(f"{table} rows did not reach {count}")


def _write_owner_plugin(root: Path) -> None:
    owner = root / "owners"
    owner.mkdir(parents=True)
    (owner / "plugin.py").write_text(
        """
from datetime import datetime, timezone
from agent.plugin_composition import RUNTIME_STARTED
from agent.plugin_composition.messages import OWNER_STATE
from plugins.akasha.message_plugin import AKASHA_RECORDS_VIEW
from plugins.akasha.recalls import Hit, ProgramSource, Recall, RecallRecords, RecallRecordsRead
from plugins.markdown_memory.store import MEMORY_WRITES
from plugins.models.projection import MODEL_CALL_HISTORY, MODEL_CALLS
from plugins.tools.plugin import TOOL_DISPLAY_NAME
from plugins.turn_projection.plugin import TURN_PROJECTION, TurnProjection
api_version = 3
name = "owners"
version = "1.0.0"
inject = ()
CALLS = {
 "call-1": {"id":"call-1","state":"success","binding":{"model":"test-model"},"request_digest":"a","started_at":"2026-09-08 00:00:00","finished_at":"2026-09-08 00:00:01","first_token_ms":10.0,"duration_ms":20.0,"failure":None,"usage":{"input_tokens":100,"cache_write_input_tokens":0,"cached_input_tokens":80,"output_tokens":20,"reasoning_output_tokens":0,"request_count":1,"covered_request_count":1,"coverage":"exact"}},
 "call-2": {"id":"call-2","state":"success","binding":{"model":"test-model"},"request_digest":"b","started_at":"2026-09-08 00:00:01","finished_at":"2026-09-08 00:00:02","first_token_ms":11.0,"duration_ms":21.0,"failure":None,"usage":{"input_tokens":120,"cache_write_input_tokens":0,"cached_input_tokens":90,"output_tokens":30,"reasoning_output_tokens":0,"request_count":1,"covered_request_count":1,"coverage":"exact"}},
 "call-failed": {"id":"call-failed","state":"unknown","binding":{"model":"test-model"},"request_digest":"c","started_at":"2026-09-08 00:00:02","finished_at":"2026-09-08 00:00:03","first_token_ms":None,"duration_ms":5.0,"failure":"provider timeout","usage":None},
}
WRITES = (
 {"source_ref":"summary-1","kind":"markdown_projection_order_v1","payload":{"session_key":"s","generation":1},"done_at":"2026-09-08 00:00:04"},
 {"source_ref":"summary-1","kind":"markdown_memory_applied_v1","payload":{"digest":"abc"},"done_at":"2026-09-08 00:00:05"},
)
RECALL = Recall(
   learning_binding="learning-1", graph_version=1,
   source=ProgramSource(key="manual", query="天气"),
   timestamp=datetime(2026, 9, 8, tzinfo=timezone.utc), limit=5,
   hits=(Hit(node_id=0, session_id="s", message_ids=("input-1",), score=0.8, lane="dense", sources=("direct_dense",)),),
   presented_message_ids=("input-1",), active_basin_count=0, pushes=0, residual_l1=0.0,
  )
async def apply(ctx, config):
 def read_records():
  return RecallRecordsRead(ctx.require(OWNER_STATE).open(ctx))
 async def start(_event):
  async with ctx.runtime_scope():
   records = RecallRecords(ctx.require(OWNER_STATE).open(ctx))
   if records.read("recall-1") is None:
    records.save("recall-1", RECALL)
 await ctx.on(RUNTIME_STARTED, start)
 await ctx.provide(TURN_PROJECTION, TurnProjection())
 await ctx.provide(MODEL_CALLS, lambda identity: CALLS[identity])
 await ctx.provide(MODEL_CALL_HISTORY, lambda after, limit: tuple(CALLS[key] for key in sorted(CALLS) if key > after)[:limit])
 await ctx.provide(MEMORY_WRITES, lambda after, limit: tuple(row for row in WRITES if after is None or (row["source_ref"], row["kind"]) > after)[:limit])
 await ctx.provide(AKASHA_RECORDS_VIEW, read_records)
 await ctx.provide(TOOL_DISPLAY_NAME, lambda binding_id: {"tools.weather.v1": "weather"}[binding_id])
""",
        encoding="utf-8",
    )


def _checks():
    return {
        name: (lambda _part: ContentReferences())
        for name in ("text", "model.facts", "artifact_ref")
    }


def _append_real_messages(log: MessageLog, *, complete: bool) -> None:
    log.ensure_session("s", SessionAttributes("listed", "eligible"))
    log.save_binding("tools.weather.v1", {"kind": "tool", "name": "weather"})
    iw = log.writer(
        "s",
        author="user",
        source="conversation",
        body_types=(Input,),
        content=_checks(),
    )
    ow = log.writer(
        "s",
        author="akashic",
        source="conversation",
        body_types=(Output,),
        content=_checks(),
        check_call=lambda _call: None,
        message_metadata_keys=frozenset({"meme"}),
    )
    iw.append("input-1", Input((ContentPart("text", "你好"),)))
    ow.append(
        "output-1",
        Output(
            (
                ContentPart("text", "先查一下"),
                ToolCall("tools.weather.v1", {"city": "杭州"}),
                ContentPart(
                    "model.facts",
                    {
                        "call_record_id": "call-1",
                        "tool_ids": {"1": "provider-call"},
                        "thinking": None,
                        "continuation": None,
                    },
                ),
            ),
            "continue",
        ),
    )
    rw = log.writer(
        "s",
        author="tool",
        source="conversation",
        body_types=(ToolResult,),
        content=_checks(),
        call_ref=CallRef("output-1", 1),
    )
    rw.append(
        "result-1",
        ToolResult(CallRef("output-1", 1), "success", (ContentPart("text", "晴"),)),
    )
    if complete:
        _append_final(log, ow)


def _append_final(log: MessageLog, output_writer=None) -> None:
    ow = output_writer or log.writer(
        "s",
        author="akashic",
        source="conversation",
        body_types=(Output,),
        content=_checks(),
        message_metadata_keys=frozenset({"meme"}),
    )
    ow.append(
        "output-2",
        Output(
            (
                ContentPart("text", "今天晴"),
                ContentPart("artifact_ref", "artifact-1"),
                ContentPart(
                    "model.facts",
                    {
                        "call_record_id": "call-2",
                        "tool_ids": {},
                        "thinking": None,
                        "continuation": None,
                    },
                ),
            ),
            "complete",
        ),
        metadata={"meme": {"version": 1, "category": "happy", "status": "selected"}},
    )


def _manager(root: Path, log: MessageLog, workspace: Path) -> PluginManager:
    plugins = root / "plugins"
    _write_owner_plugin(plugins)
    shutil.copytree(
        Path(module.__file__ or "").resolve().parent,
        plugins / "observe",
        ignore=shutil.ignore_patterns(".git", ".pytest_cache", "__pycache__"),
    )
    return PluginManager(
        plugin_dirs=[plugins],
        event_bus=EventBus(),
        tool_registry=None,
        workspace=workspace,
        installed_cache_root=root / "cache",
        message_log=log,
    )


@pytest.mark.asyncio
async def test_real_manager_projects_histories_and_restart_is_idempotent(
    tmp_path: Path,
) -> None:
    log = MessageLog(tmp_path / "sessions.db")
    _append_real_messages(log, complete=False)
    workspace = tmp_path / "workspace"
    manager = _manager(tmp_path / "first", log, workspace)
    db_path = workspace / "observe" / "observe.db"
    try:
        await manager.load_all()
        await manager.start_runtime()
        await _wait_rows(db_path, "model_calls", 3)
        await _wait_rows(db_path, "memory_writes", 2)
        await _wait_rows(db_path, "rag_queries", 1)
        with sqlite3.connect(db_path) as connection:
            assert connection.execute("SELECT COUNT(*) FROM turns").fetchone()[0] == 0
        _append_final(log)
        await _wait_rows(db_path, "turns", 1)
        with sqlite3.connect(db_path) as connection:
            row = connection.execute(
                "SELECT assistant_message_id,user_msg,llm_output,meme_tag,meme_media_count,react_iteration_count,model_output_tokens,react_cache_prompt_tokens,react_cache_hit_tokens FROM turns"
            ).fetchone()
            assert row == ("output-2", "你好", "今天晴", "happy", 1, 2, 50, 220, 170)
            assert (
                '"name": "weather"'
                in connection.execute("SELECT tool_chain_json FROM turns").fetchone()[0]
            )
            assert connection.execute(
                "SELECT state,failure FROM model_calls WHERE call_id='call-failed'"
            ).fetchone() == ("unknown", "provider timeout")
            assert (
                connection.execute(
                    "SELECT through_seq FROM projection_cursors"
                ).fetchone()[0]
                == 3
            )
        await manager.terminate_all()
        manager = _manager(tmp_path / "second", log, workspace)
        await manager.load_all()
        await manager.start_runtime()
        await asyncio.sleep(2.2)
        with sqlite3.connect(db_path) as connection:
            assert connection.execute("SELECT COUNT(*) FROM turns").fetchone()[0] == 1
            assert (
                connection.execute("SELECT COUNT(*) FROM memory_writes").fetchone()[0]
                == 2
            )
            assert (
                connection.execute("SELECT COUNT(*) FROM model_calls").fetchone()[0]
                == 3
            )
            assert (
                connection.execute("SELECT COUNT(*) FROM rag_queries").fetchone()[0]
                == 1
            )
    finally:
        if manager.current_snapshot is not None:
            await manager.terminate_all()
        log.close()


def test_static_manifest_and_module_exports_match() -> None:
    plugin_dir = Path(module.__file__ or "").resolve().parent
    manifest = load_static_plugin_manifest(plugin_dir)
    composable = ComposablePlugin.from_module(module)
    assert manifest.name == composable.name == "observe"
    assert manifest.version == composable.version == "2.0.0"
    assert manifest.api_version == composable.api_version == 3
    assert composable.dashboard_module == "dashboard.py"
    assert composable.workspace_roots == ("observe",)


def test_dashboard_uses_declared_generation_root(tmp_path: Path) -> None:
    spec = importlib.util.spec_from_file_location(
        f"{module.__name__}.dashboard",
        Path(module.__file__ or "").resolve().parent / "dashboard.py",
    )
    assert spec is not None and spec.loader is not None
    dashboard = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = dashboard
    spec.loader.exec_module(dashboard)
    app = FastAPI()
    dashboard.register(
        app,
        DashboardContext(
            plugin_id="observe",
            plugin_dir=Path(module.__file__ or "").resolve().parent,
            data_root=tmp_path / "plugin-data",
            validation=True,
            _workspace_roots=(("observe", tmp_path / "workspace" / "observe"),),
        ),
    )
    assert any(
        getattr(route, "path", None) == "/api/dashboard/observe/overview"
        for route in app.routes
    )


def test_kvcache_bootstrap_fails_loudly_on_projection_drift(tmp_path: Path) -> None:
    db_module = sys.modules[f"{module.__name__}.db"]
    db_path = tmp_path / "observe" / "observe.db"
    connection = db_module.open_db(db_path)
    try:
        connection.execute(
            "INSERT INTO turns(ts,source,session_key,llm_output) VALUES (?,?,?,?)",
            ("2026-09-08T00:00:00+00:00", "agent", "s", "ok"),
        )
        connection.commit()
    finally:
        connection.close()
    with pytest.raises(RuntimeError, match="投影水位不一致"):
        module._mobile_ui_query(
            tmp_path / "observe", "kvcache.bootstrap", {}, session_id=None, turn_id=None
        )


def test_existing_turn_gets_projection_receipt_without_duplicate(
    tmp_path: Path,
) -> None:
    db_module = sys.modules[f"{module.__name__}.db"]
    writer_module = sys.modules[f"{module.__name__}.writer"]
    events_module = sys.modules[f"{module.__name__}.events"]
    connection = db_module.open_db(tmp_path / "observe.db")
    try:
        writer_module._write_turn(
            connection,
            events_module.TurnTrace(
                source="agent",
                session_key="s",
                user_msg="旧输入",
                llm_output="旧输出",
                assistant_message_id="answer-1",
            ),
            "2026-09-01T00:00:00+00:00",
        )
        writer_module._write_turn(
            connection,
            events_module.TurnTrace(
                source="agent",
                session_key="s",
                user_msg="新投影输入",
                llm_output="新投影输出",
                assistant_message_id="answer-1",
                projection_key="turn:s:conversation:3:answer-1",
                projection_source="conversation",
                through_seq=3,
            ),
            "2026-09-08T00:00:00+00:00",
        )
        assert connection.execute("SELECT COUNT(*) FROM turns").fetchone()[0] == 1
        assert (
            connection.execute("SELECT projection_key FROM turns").fetchone()[0]
            == "turn:s:conversation:3:answer-1"
        )
        assert connection.execute(
            "SELECT scope,through_seq FROM projection_cursors"
        ).fetchone() == ("s\nconversation", 3)
    finally:
        connection.close()


def _model_trace(identity: str):
    events = sys.modules[f"{module.__name__}.events"]
    return events.ModelCallTrace(
        call_id=identity,
        state="success",
        model="test-model",
        started_at="2026-09-08 00:00:00",
        finished_at="2026-09-08 00:00:01",
        first_token_ms=1.0,
        duration_ms=2.0,
        usage=None,
        failure=None,
    )


@pytest.mark.asyncio
async def test_durable_submit_does_not_drop_more_than_queue_capacity(
    tmp_path: Path,
) -> None:
    writer_type = sys.modules[f"{module.__name__}.writer"].TraceWriter
    writer = writer_type(tmp_path / "observe.db")
    task = asyncio.create_task(writer.run())
    try:
        await asyncio.gather(
            *(writer.submit(_model_trace(f"call-{index:04d}")) for index in range(600))
        )
        with sqlite3.connect(tmp_path / "observe.db") as connection:
            assert (
                connection.execute("SELECT COUNT(*) FROM model_calls").fetchone()[0]
                == 600
            )
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
async def test_durable_submit_reports_one_write_failure_then_recovers(
    tmp_path: Path,
) -> None:
    writer_type = sys.modules[f"{module.__name__}.writer"].TraceWriter
    writer = writer_type(tmp_path / "observe.db")
    write = writer._write_one
    attempts = 0

    def fail_once(connection, event):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise sqlite3.OperationalError("injected write failure")
        write(connection, event)

    writer._write_one = fail_once
    task = asyncio.create_task(writer.run())
    try:
        with pytest.raises(sqlite3.OperationalError, match="injected"):
            await writer.submit(_model_trace("call-retry"))
        await writer.submit(_model_trace("call-retry"))
        assert not task.done()
        with sqlite3.connect(tmp_path / "observe.db") as connection:
            assert (
                connection.execute("SELECT COUNT(*) FROM model_calls").fetchone()[0]
                == 1
            )
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
async def test_rag_retention_keeps_receipt_and_does_not_resurrect(
    tmp_path: Path,
) -> None:
    events = sys.modules[f"{module.__name__}.events"]
    writer_type = sys.modules[f"{module.__name__}.writer"].TraceWriter
    retention = sys.modules[f"{module.__name__}.retention"]
    db_path = tmp_path / "observe.db"
    writer = writer_type(db_path)
    task = asyncio.create_task(writer.run())
    event = events.RagQueryLog(
        caller="explicit",
        session_key="s",
        query="old",
        orig_query=None,
        aux_queries=[],
        hits=[],
        injected_count=0,
        projection_key="akasha:old",
        recorded_at="2020-01-01T00:00:00+00:00",
    )
    try:
        await writer.submit(event)
        with sqlite3.connect(db_path) as connection:
            connection.execute("DELETE FROM projection_receipts")
            connection.commit()
        reopened = sys.modules[f"{module.__name__}.db"].open_db(db_path)
        reopened.close()
        await asyncio.to_thread(retention._run_cleanup, db_path)
        await writer.submit(event)
        with sqlite3.connect(db_path) as connection:
            assert (
                connection.execute("SELECT COUNT(*) FROM rag_queries").fetchone()[0]
                == 0
            )
            assert (
                connection.execute(
                    "SELECT COUNT(*) FROM projection_receipts WHERE domain='akasha'"
                ).fetchone()[0]
                == 1
            )
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
async def test_global_error_collector_restores_process_hooks() -> None:
    emitter = _Emitter()
    collector = GlobalErrorCollector(emitter)
    original_sys, original_thread = sys.excepthook, threading.excepthook
    original_loop = asyncio.get_running_loop().get_exception_handler()
    await collector.install()
    collector.capture(
        source="log",
        logger_name="test",
        error_type="ValueError",
        message="bad 123",
        traceback_text="trace",
        level="ERROR",
        top_frame="test.py:1",
        session_key="s",
    )
    await collector.uninstall()
    assert len(emitter.events) == 1
    assert sys.excepthook == original_sys
    assert threading.excepthook == original_thread
    assert asyncio.get_running_loop().get_exception_handler() == original_loop


def test_error_fingerprint_normalizes_runtime_numbers() -> None:
    collector = sys.modules[f"{module.__name__}.collector"]
    assert collector._fingerprint(
        "ValueError", "bad 123 at 0xabc", "x.py"
    ) == collector._fingerprint("ValueError", "bad 999 at 0xdef", "x.py")
