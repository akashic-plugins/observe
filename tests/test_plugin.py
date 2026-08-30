from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import json
import shutil
import sqlite3
import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, cast

import pytest
from fastapi import FastAPI

from agent.plugin_composition import (
    CompositionRoot,
    DashboardContext,
    PluginRuntime,
    PluginUiSlots,
    UI_SLOTS,
)
from agent.plugins.composable import ComposablePlugin
from agent.plugins.dashboard_host import DashboardBinding, PluginDashboardHost
from agent.plugins.manager import PluginManager
from agent.plugins.mobile_ui import PluginMobileUiProvider
from agent.plugins.static_manifest import load_static_plugin_manifest
from agent.turn_events.after_turn import AFTER_TURN_COMMITTED
from agent.turn_events.observe import (
    MEMORY_WRITTEN,
    RETRIEVAL_COMPLETED,
)
from bus.event_bus import EventBus
from bus.events_lifecycle import TurnCommitted
from core.memory.events import (
    MemoryWritten,
    RetrievalCompleted,
    RetrievalHitSummary,
)


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
GlobalErrorCollector = module.GlobalErrorCollector


class _Emitter:
    def __init__(self) -> None:
        self.events: list[Any] = []

    def emit(self, event: object) -> None:
        self.events.append(event)


def _turn_event(
    *,
    assistant_message_id: str | None = "mobile:demo:2",
    turn_id: str = "turn-1",
    channel: str = "mobile",
    model_usage: dict[str, object] | None = None,
) -> TurnCommitted:
    return TurnCommitted(
        session_key="mobile:demo",
        channel=channel,
        chat_id="demo",
        input_message="hi",
        persisted_user_message="hi",
        assistant_response="hello",
        tools_used=[],
        turn_id=turn_id,
        assistant_message_id=assistant_message_id,
        model_usage=model_usage
        if model_usage is not None
        else {"coverage": "exact", "output_tokens": 321},
        react_stats={
            "cache_prompt_tokens": 100,
            "cache_hit_tokens": 80,
        },
    )


async def _mount_observe(tmp_path: Path) -> tuple[CompositionRoot, Path]:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    root = CompositionRoot("observe-test")
    ui_slots = PluginUiSlots()
    _ = await root.context.provide(UI_SLOTS, ui_slots)
    plugin_dir = Path(module.__file__ or "").resolve().parent
    composable = ComposablePlugin.from_module(module)
    await root.mount(
        composable.apply,
        name="observe",
        inject=module.inject,
        runtime=PluginRuntime(
            plugin_id="observe",
            generation_id=root.generation_id,
            plugin_dir=plugin_dir,
            data_dir=tmp_path / "plugin-data",
            workspace=workspace,
            config={},
            workspace_roots=module.workspace_roots,
        ),
    )
    return root, workspace


def _manager_for_observe(tmp_path: Path) -> tuple[PluginManager, Path]:
    workspace = tmp_path / "workspace"
    plugin_root = tmp_path / "plugins" / "observe"
    shutil.copytree(
        Path(module.__file__ or "").resolve().parent,
        plugin_root,
        ignore=shutil.ignore_patterns(
            ".git", ".akashic-core", ".pytest_cache", "__pycache__"
        ),
    )
    manager = PluginManager(
        plugin_dirs=[plugin_root.parent],
        event_bus=EventBus(),
        tool_registry=None,
        workspace=workspace,
        installed_cache_root=tmp_path / "cache",
    )
    return manager, workspace


async def _wait_for_turn_row(db_path: Path) -> None:
    for _ in range(50):
        if db_path.exists():
            conn = sqlite3.connect(db_path)
            try:
                if conn.execute("SELECT COUNT(*) FROM turns").fetchone()[0] > 0:
                    return
            finally:
                conn.close()
        await asyncio.sleep(0.01)
    raise AssertionError(f"Observe 没有写入 Turn row: {db_path}")


async def _wait_for_table_rows(
    db_path: Path,
    table: str,
    expected: int,
) -> None:
    for _ in range(50):
        if db_path.exists():
            conn = sqlite3.connect(db_path)
            try:
                count = conn.execute(
                    f"SELECT COUNT(*) FROM {table}"
                ).fetchone()[0]
            finally:
                conn.close()
            if count >= expected:
                return
        await asyncio.sleep(0.01)
    raise AssertionError(
        f"Observe 没有写入 {table} rows={expected}: {db_path}"
    )


def _retrieval_completed_event() -> RetrievalCompleted:
    return RetrievalCompleted(
        session_key="mobile:demo",
        channel="mobile",
        chat_id="demo",
        query="rewritten query",
        orig_query="original query",
        hits=[
            RetrievalHitSummary(
                item_id="memory-1",
                memory_type="event",
                score=0.91,
                summary="retrieved summary",
                injected=True,
                confidence_label="certain",
                forced=True,
                metadata={"forced": True},
            )
        ],
        injected_count=1,
        route_decision="RETRIEVE",
        aux_queries=["hypothesis"],
    )


def _memory_written_event() -> MemoryWritten:
    return MemoryWritten(
        session_key="mobile:demo",
        channel="mobile",
        chat_id="demo",
        action="supersede",
        source_ref="mobile:demo@post_response",
        superseded_ids=["memory-old"],
    )


@pytest.mark.asyncio
async def test_v3_apply_emits_turn_trace_and_disposes_all_effects(tmp_path: Path) -> None:
    root, workspace = await _mount_observe(tmp_path)
    try:
        root.context.emit(AFTER_TURN_COMMITTED, _turn_event())
        db_path = workspace / "observe" / "observe.db"
        await _wait_for_turn_row(db_path)
        result = module._mobile_ui_query(
            workspace / "observe",
            "kvcache.message_usage",
            {"message_id": "mobile:demo:2"},
            session_id="mobile:demo",
            turn_id=None,
        )
        assert result == {"usage": {"output_tokens": 321}}
        assert root.receipt().ready
        assert root.topology_view().listeners
    finally:
        await root.dispose()
    assert root.receipt().effects == ()
    assert root.topology_view().listeners == ()


@pytest.mark.asyncio
async def test_v3_observes_memory_domain_events_without_duplicate_rows(
    tmp_path: Path,
) -> None:
    root, workspace = await _mount_observe(tmp_path)
    retrieval = _retrieval_completed_event()
    memory = _memory_written_event()
    db_path = workspace / "observe" / "observe.db"
    try:
        rag_projection = module._to_rag_query_log(retrieval)
        assert rag_projection.caller == "passive"
        assert rag_projection.hits[0].confidence_label == "certain"
        assert rag_projection.hits[0].forced is True
        assert module._to_memory_write_trace(memory).superseded_ids == [
            "memory-old"
        ]
        await root.context.observe(RETRIEVAL_COMPLETED, retrieval)
        await root.context.observe(MEMORY_WRITTEN, memory)
        await _wait_for_table_rows(db_path, "rag_queries", 1)
        await _wait_for_table_rows(db_path, "memory_writes", 1)

        conn = sqlite3.connect(db_path)
        try:
            retrieval_row = conn.execute(
                """
                SELECT caller, session_key, query, orig_query, aux_queries,
                       hits_json, injected_count, route_decision, error
                FROM rag_queries
                """
            ).fetchone()
            memory_row = conn.execute(
                """
                SELECT session_key, source_ref, action, memory_type, item_id,
                       summary, superseded_ids, error
                FROM memory_writes
                """
            ).fetchone()
            assert retrieval_row[0:5] == (
                "passive",
                "mobile:demo",
                "rewritten query",
                "original query",
                '["hypothesis"]',
            )
            assert json.loads(retrieval_row[5]) == [
                {
                    "id": "memory-1",
                    "type": "event",
                    "score": 0.91,
                    "summary": "retrieved summary",
                    "injected": 1,
                }
            ]
            assert retrieval_row[6:] == (1, "RETRIEVE", None)
            assert memory_row == (
                "mobile:demo",
                "mobile:demo@post_response",
                "supersede",
                None,
                None,
                None,
                '["memory-old"]',
                None,
            )
            assert conn.execute("SELECT COUNT(*) FROM turns").fetchone()[0] == 0
            assert conn.execute("SELECT COUNT(*) FROM rag_queries").fetchone()[0] == 1
            assert conn.execute("SELECT COUNT(*) FROM memory_writes").fetchone()[0] == 1
        finally:
            conn.close()
    finally:
        await root.dispose()


@pytest.mark.asyncio
async def test_committed_channel_classification_uses_one_trace_path(
    tmp_path: Path,
) -> None:
    root, workspace = await _mount_observe(tmp_path)
    db_path = workspace / "observe" / "observe.db"
    try:
        listeners = root.topology_view().listeners
        assert sum("turn.after_turn.committed" in item for item in listeners) == 1
        assert all("proactive.finished" not in item for item in listeners)

        root.context.emit(AFTER_TURN_COMMITTED, _turn_event())
        root.context.emit(
            AFTER_TURN_COMMITTED,
            _turn_event(
                channel="wake",
                turn_id="turn-wake",
                assistant_message_id="wake:default:2",
            ),
        )
        root.context.emit(
            AFTER_TURN_COMMITTED,
            _turn_event(
                channel="drift",
                turn_id="turn-drift",
                assistant_message_id="drift:default:2",
            ),
        )
        await _wait_for_table_rows(db_path, "turns", 3)

        with sqlite3.connect(db_path) as conn:
            rows = conn.execute(
                "SELECT turn_id, source FROM turns ORDER BY id"
            ).fetchall()
        assert rows == [
            ("turn-1", "agent"),
            ("turn-wake", "proactive"),
            # 显式 drift channel 只验证普通 Turn 分类，不模拟 Wake 内的 Drift duty。
            ("turn-drift", "drift"),
        ]
    finally:
        await root.dispose()


@pytest.mark.asyncio
async def test_global_hooks_survive_overlapping_generations() -> None:
    loop = asyncio.get_running_loop()
    original_sys = sys.excepthook
    original_thread = threading.excepthook
    original_loop = loop.get_exception_handler()
    first = GlobalErrorCollector(_Emitter())
    second = GlobalErrorCollector(_Emitter())
    try:
        await first.install()
        await second.install()
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


@pytest.mark.asyncio
async def test_global_hooks_roll_back_when_flush_task_cannot_spawn() -> None:
    original_sys = sys.excepthook
    original_thread = threading.excepthook
    loop = asyncio.get_running_loop()
    original_loop = loop.get_exception_handler()
    collector = GlobalErrorCollector(_Emitter())

    async def reject_spawn(coroutine: Any, *, name: str) -> asyncio.Task[Any]:
        del name
        coroutine.close()
        raise RuntimeError("flush task rejected")

    with pytest.raises(RuntimeError, match="flush task rejected"):
        await collector.install(spawn_task=reject_spawn)
    assert sys.excepthook == original_sys
    assert threading.excepthook == original_thread
    assert loop.get_exception_handler() == original_loop
    await collector.uninstall()


def test_turn_trace_keeps_message_identity_and_output_tokens(tmp_path: Path) -> None:
    emitter = _Emitter()
    module._emit_turn_trace(emitter, _turn_event())
    trace = emitter.events[0]
    assert trace.turn_id == "turn-1"
    assert trace.assistant_message_id == "mobile:demo:2"
    assert trace.model_output_tokens == 321
    db_module = sys.modules[f"{module.__name__}.db"]
    db_path = tmp_path / "observe.db"
    conn = db_module.open_db(db_path)
    try:
        module.TraceWriter(db_path)._write_one(conn, trace)
        row = conn.execute(
            "SELECT turn_id, assistant_message_id, model_output_tokens FROM turns"
        ).fetchone()
    finally:
        conn.close()
    assert row == ("turn-1", "mobile:demo:2", 321)


def test_partial_usage_and_empty_turn_ids_do_not_claim_complete_output() -> None:
    emitter = _Emitter()
    for index in range(2):
        event = _turn_event(
            assistant_message_id=f"mobile:demo:{index + 2}",
            turn_id="",
            model_usage={"coverage": "partial", "output_tokens": 100},
        )
        module._emit_turn_trace(emitter, event)
    assert all(event.turn_id is None for event in emitter.events)
    assert all(event.model_output_tokens is None for event in emitter.events)


def test_mobile_message_usage_returns_true_output_tokens(tmp_path: Path) -> None:
    db_module = sys.modules[f"{module.__name__}.db"]
    db_path = tmp_path / "observe" / "observe.db"
    conn = db_module.open_db(db_path)
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
    result = module._mobile_ui_query(
        tmp_path / "observe",
        "kvcache.message_usage",
        {"message_id": "mobile:demo:2"},
        session_id="mobile:demo",
        turn_id=None,
    )
    assert result == {"usage": {"output_tokens": 321}}


def test_mobile_health_reuses_global_error_projection(tmp_path: Path) -> None:
    db_module = sys.modules[f"{module.__name__}.db"]
    db_path = tmp_path / "observe" / "observe.db"
    conn = db_module.open_db(db_path)
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
        conn.execute(
            """
            INSERT INTO global_errors(
                fingerprint, bucket, source, logger_name, error_type,
                message, traceback_text, level, first_ts, last_ts,
                count, session_keys, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "fp-high-count",
                now.isoformat()[:13],
                "log",
                "agent.runtime",
                "LogError",
                "frequent but stable",
                "not returned to the mobile list",
                "ERROR",
                now.isoformat(),
                now.isoformat(),
                20,
                "[]",
                "active",
            ),
        )
        conn.commit()
    finally:
        conn.close()

    snapshot = module._mobile_ui_query(
        tmp_path / "observe",
        "health.snapshot",
        {"range": "24h"},
        session_id=None,
        turn_id=None,
    )
    detail = module._mobile_ui_query(
        tmp_path / "observe",
        "health.error_detail",
        {"range": "24h", "fingerprint": "fp-mobile-health"},
        session_id=None,
        turn_id=None,
    )
    assert snapshot["total"] == 27
    assert snapshot["types"] == 2
    assert snapshot["spiking_types"] == 1
    item = snapshot["items"][0]
    assert item["fingerprint"] == "fp-mobile-health"
    assert item["error_type"] == "RuntimeError"
    assert "traceback" not in item
    assert detail["error"]["traceback"].startswith("Traceback")
    assert len(detail["error"]["traceback"]) == 4000
    assert "occurrences" not in detail["error"]

    conn = db_module.open_db(db_path)
    try:
        conn.execute("UPDATE global_errors SET status = 'ignored'")
        conn.commit()
    finally:
        conn.close()
    ignored = module._mobile_ui_query(
        tmp_path / "observe",
        "health.snapshot",
        {"range": "24h"},
        session_id=None,
        turn_id=None,
    )
    assert ignored == {
        "range": "24h",
        "items": [],
        "types": 0,
        "total": 0,
        "new_types": 0,
        "spiking_types": 0,
    }
    for invalid_range in ("all", [], {}, True, None):
        with pytest.raises(ValueError, match="range 只支持"):
            module._mobile_ui_query(
                tmp_path / "observe",
                "health.snapshot",
                {"range": invalid_range},
                session_id=None,
                turn_id=None,
            )


def test_static_manifest_and_module_exports_match() -> None:
    plugin_dir = Path(module.__file__ or "").resolve().parent
    manifest = load_static_plugin_manifest(plugin_dir)
    composable = ComposablePlugin.from_module(module)
    assert manifest.name == composable.name == "observe"
    assert manifest.version == composable.version == "1.4.1"
    assert manifest.api_version == composable.api_version == 3
    assert manifest.entrypoint == "plugin.py"
    assert composable.dashboard_module == "dashboard.py"
    assert composable.workspace_roots == ("observe",)


def test_dashboard_uses_declared_generation_root(tmp_path: Path) -> None:
    dashboard = importlib.util.spec_from_file_location(
        f"{module.__name__}.dashboard",
        Path(module.__file__ or "").resolve().parent / "dashboard.py",
    )
    assert dashboard is not None and dashboard.loader is not None
    dashboard_module = importlib.util.module_from_spec(dashboard)
    sys.modules[dashboard.name] = dashboard_module
    dashboard.loader.exec_module(dashboard_module)
    app = FastAPI()
    declared = tmp_path / "workspace" / "observe"
    dashboard_module.register(
        app,
        DashboardContext(
            plugin_id="observe",
            plugin_dir=Path(module.__file__ or "").resolve().parent,
            data_root=tmp_path / "plugin-data",
            validation=True,
            _workspace_roots=(("observe", declared),),
        ),
    )
    assert any(
        getattr(route, "path", None) == "/api/dashboard/observe/overview"
        for route in app.routes
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


def test_kvcache_bootstrap_fails_loudly_on_projection_drift(tmp_path: Path) -> None:
    db_module = sys.modules[f"{module.__name__}.db"]
    db_path = tmp_path / "observe" / "observe.db"
    conn = db_module.open_db(db_path)
    try:
        conn.execute(
            "INSERT INTO turns(ts, source, session_key, llm_output) VALUES (?, ?, ?, ?)",
            ("2026-07-17T00:00:00+00:00", "agent", "mobile:demo", "ok"),
        )
        conn.commit()
    finally:
        conn.close()
    with pytest.raises(RuntimeError, match="投影水位不一致"):
        module._mobile_ui_query(
            tmp_path / "observe",
            "kvcache.bootstrap",
            {},
            session_id=None,
            turn_id=None,
        )


@pytest.mark.asyncio
async def test_real_manager_publishes_v3_observe_mobile_query_and_candidate(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    plugin_root = tmp_path / "plugins" / "observe"
    shutil.copytree(
        Path(module.__file__ or "").resolve().parent,
        plugin_root,
        ignore=shutil.ignore_patterns(
            ".git", ".akashic-core", ".pytest_cache", "__pycache__"
        ),
    )
    manager = PluginManager(
        plugin_dirs=[plugin_root.parent],
        event_bus=EventBus(),
        tool_registry=None,
        workspace=workspace,
        installed_cache_root=tmp_path / "cache",
    )
    try:
        await manager.load_all()
        snapshot = manager.current_snapshot
        assert snapshot is not None
        assert snapshot.mobile_ui_registry is not None
        dashboard_host = PluginDashboardHost(core_routes=())
        dashboard_host.prepare_initial_snapshot(snapshot)
        manager.bind_dashboard_preparer(
            dashboard_host.prepare_snapshot,
            validation_releaser=dashboard_host.release_validation,
        )
        assert snapshot.dashboard_bindings
        assert isinstance(snapshot.dashboard_bindings[0], DashboardBinding)
        provider = PluginMobileUiProvider(manager)
        catalog = cast(list[dict[str, object]], provider.catalog()["items"])
        item = next(value for value in catalog if value["id"] == "observe")
        assert (await provider.query(
            "observe",
            cast(str, item["revision"]),
            "health.snapshot",
            {},
            session_id=None,
            turn_id=None,
        ))["total"] == 0
        stable_root = snapshot.composition_root
        assert stable_root is not None
        stable_db = workspace / "observe" / "observe.db"
        before = (
            hashlib.sha256(stable_db.read_bytes()).hexdigest()
            if stable_db.exists()
            else None
        )

        candidate = await manager.prepare_candidate("observe")
        assert candidate is not None
        assert manager.current_snapshot is snapshot
        candidate_snapshot = candidate.runtime_snapshot
        assert candidate_snapshot is not None
        candidate_root = candidate_snapshot.composition_root
        assert candidate_root is not None
        dashboard_host.prepare_snapshot(candidate_snapshot)
        assert len(candidate_snapshot.dashboard_bindings) == 1
        candidate_dashboard = candidate_snapshot.dashboard_bindings[0]
        assert isinstance(candidate_dashboard, DashboardBinding)
        assert candidate_dashboard.validation is True
        candidate_mobile = candidate_snapshot.mobile_ui_registry.binding(
            "observe"
        )
        assert candidate_mobile is not None
        assert candidate_mobile.is_live()
        await manager.discard_prepared("observe")
        assert candidate_root.receipt().effects == ()
        assert candidate_root.topology_view().listeners == ()
        assert candidate_snapshot.dashboard_bindings == ()
        assert not candidate_mobile.is_live()
        assert all(
            not binding.validation for binding in dashboard_host._bindings.values()
        )
        assert provider.catalog()["items"]
        after = (
            hashlib.sha256(stable_db.read_bytes()).hexdigest()
            if stable_db.exists()
            else None
        )
        assert after == before
        await manager.terminate_all()
        assert stable_root.receipt().effects == ()
        assert stable_root.topology_view().listeners == ()
        assert not dashboard_host._bindings
        assert provider.catalog()["items"] == []
    finally:
        if manager.current_snapshot is not None:
            await manager.terminate_all()


@pytest.mark.asyncio
async def test_manager_candidate_domain_observe_isolated_and_cleaned(
    tmp_path: Path,
) -> None:
    manager, workspace = _manager_for_observe(tmp_path)
    original_sys = sys.excepthook
    original_thread = threading.excepthook
    original_loop = asyncio.get_running_loop().get_exception_handler()
    candidate_root: CompositionRoot | None = None
    candidate_workspace: Path | None = None
    try:
        await manager.load_all()
        stable_db = workspace / "observe" / "observe.db"
        stable_hook = sys.excepthook
        candidate = await manager.prepare_candidate("observe")
        assert candidate is not None
        candidate_snapshot = candidate.runtime_snapshot
        assert candidate_snapshot is not None
        candidate_root = candidate_snapshot.composition_root
        assert candidate_root is not None
        runtime = candidate_root.root_fiber.children[0].runtime
        assert runtime is not None
        candidate_workspace = runtime.workspace
        candidate_db = candidate_workspace / "observe" / "observe.db"

        assert sys.excepthook != stable_hook
        await candidate_root.context.observe(
            MEMORY_WRITTEN,
            _memory_written_event(),
        )
        await _wait_for_table_rows(candidate_db, "memory_writes", 1)
        if stable_db.exists():
            with sqlite3.connect(stable_db) as conn:
                assert conn.execute(
                    "SELECT COUNT(*) FROM memory_writes"
                ).fetchone()[0] == 0

        await manager.discard_prepared("observe")
        assert candidate_root.receipt().effects == ()
        assert candidate_root.topology_view().listeners == ()
        assert candidate_workspace is not None
        assert not candidate_workspace.parent.exists()
        assert sys.excepthook == stable_hook
    finally:
        if manager.current_snapshot is not None:
            await manager.terminate_all()
    assert sys.excepthook == original_sys
    assert threading.excepthook == original_thread
    assert asyncio.get_running_loop().get_exception_handler() == original_loop


@pytest.mark.asyncio
async def test_manager_formal_publish_drops_candidate_rows_and_keeps_observe_live(
    tmp_path: Path,
) -> None:
    manager, workspace = _manager_for_observe(tmp_path)
    original_sys = sys.excepthook
    original_thread = threading.excepthook
    original_loop = asyncio.get_running_loop().get_exception_handler()
    stable_root: CompositionRoot | None = None
    formal_root: CompositionRoot | None = None
    candidate_workspace: Path | None = None
    try:
        await manager.load_all()
        stable_snapshot = manager.current_snapshot
        assert stable_snapshot is not None
        stable_root = stable_snapshot.composition_root
        assert stable_root is not None
        candidate = await manager.prepare_candidate("observe")
        assert candidate is not None
        candidate_snapshot = candidate.runtime_snapshot
        assert candidate_snapshot is not None
        candidate_root = candidate_snapshot.composition_root
        assert candidate_root is not None
        runtime = candidate_root.root_fiber.children[0].runtime
        assert runtime is not None
        candidate_workspace = runtime.workspace
        candidate_db = candidate_workspace / "observe" / "observe.db"

        await candidate_root.context.observe(
            RETRIEVAL_COMPLETED,
            _retrieval_completed_event(),
        )
        await _wait_for_table_rows(candidate_db, "rag_queries", 1)
        stable_db = workspace / "observe" / "observe.db"
        if stable_db.exists():
            with sqlite3.connect(stable_db) as conn:
                assert conn.execute(
                    "SELECT COUNT(*) FROM rag_queries"
                ).fetchone()[0] == 0

        publication = await manager.publish_prepared("observe")
        assert publication["publication_state"] == "committed"
        formal_snapshot = manager.current_snapshot
        assert formal_snapshot is not None
        formal_root = formal_snapshot.composition_root
        assert formal_root is not None
        assert formal_root is not candidate_root
        assert candidate_workspace is not None
        assert not candidate_workspace.parent.exists()

        formal_db = workspace / "observe" / "observe.db"
        await _wait_for_table_rows(formal_db, "rag_queries", 0)
        with sqlite3.connect(formal_db) as conn:
            assert conn.execute(
                "SELECT COUNT(*) FROM rag_queries"
            ).fetchone()[0] == 0

        await formal_root.context.observe(
            MEMORY_WRITTEN,
            _memory_written_event(),
        )
        await _wait_for_table_rows(formal_db, "memory_writes", 1)
        with sqlite3.connect(formal_db) as conn:
            assert conn.execute(
                "SELECT COUNT(*) FROM memory_writes"
            ).fetchone()[0] == 1
    finally:
        if manager.current_snapshot is not None:
            await manager.terminate_all()
    assert stable_root is not None
    assert stable_root.receipt().effects == ()
    assert stable_root.topology_view().listeners == ()
    assert formal_root is not None
    assert formal_root.receipt().effects == ()
    assert formal_root.topology_view().listeners == ()
    assert sys.excepthook == original_sys
    assert threading.excepthook == original_thread
    assert asyncio.get_running_loop().get_exception_handler() == original_loop
