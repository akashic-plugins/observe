from __future__ import annotations

import asyncio
import importlib.util
import sqlite3
import sys
from pathlib import Path

import pytest

from agent.plugins.context import PluginContext, PluginKVStore
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


@pytest.mark.asyncio
async def test_observe_plugin_initialize_and_terminate(tmp_path: Path) -> None:
    plugin = ObservePlugin()
    plugin.context = PluginContext(
        event_bus=EventBus(),
        tool_registry=None,
        plugin_id="observe",
        plugin_dir=tmp_path,
        data_dir=tmp_path,
        kv_store=PluginKVStore(tmp_path / ".kv.json"),
        workspace=tmp_path,
    )
    await plugin.initialize()
    await asyncio.sleep(0.05)
    await plugin.terminate()
    db_path = tmp_path / "observe" / "observe.db"
    assert db_path.exists()
    conn = sqlite3.connect(db_path)
    try:
        tables = {row[0] for row in conn.execute("select name from sqlite_master where type='table'")}
    finally:
        conn.close()
    assert "turns" in tables
