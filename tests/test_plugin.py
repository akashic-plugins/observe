from __future__ import annotations

import asyncio
import importlib.util
import sqlite3
import sys
import threading
from pathlib import Path

import pytest

from agent.plugins.context import PluginContext, PluginKVStore
from agent.plugins.scope import PluginScope, ScopedEventBus
from bus.event_bus import EventBus


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
    def emit(self, _event: object) -> None:
        return None


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
