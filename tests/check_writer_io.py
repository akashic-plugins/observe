"""用真实 SQLite 验证 Observe 的调度、取消、事务确认与物理关闭。"""
from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import time

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--core', type=Path, required=True)
parser.add_argument('--source', type=Path, default=Path(__file__).resolve().parents[1])
parser.add_argument('--baseline', action='store_true')
args = parser.parse_args()
sys.path[:0] = [str(args.core), str(args.core / 'sdk/python/src')]
spec = importlib.util.spec_from_file_location('observe_io_fixture', args.source / 'plugin.py',
                                            submodule_search_locations=[str(args.source)])
assert spec is not None and spec.loader is not None
plugin = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = plugin
spec.loader.exec_module(plugin)
writer_module = sys.modules[spec.name + '.writer']
events = sys.modules[spec.name + '.events']


class LockedWriter(plugin.TraceWriter):
    """目标 INSERT 使用真实写锁，独立线程等待 loop 心跳才释放。"""

    def __init__(self, path):
        super().__init__(path)
        self.loop = asyncio.get_running_loop()
        self.observation = None

    def _write_one(self, conn, event):
        if self.observation is not None:
            return super()._write_one(conn, event)
        blocker = sqlite3.connect(self._db_path, check_same_thread=False)
        blocker.execute('BEGIN IMMEDIATE')
        heartbeat = threading.Event()
        started = time.perf_counter()
        self.observation = {}

        def tick():
            self.observation['loop_lag_ms'] = (time.perf_counter() - started) * 1000
            heartbeat.set()

        def release():
            self.loop.call_soon_threadsafe(tick)
            self.observation['heartbeat_before_release'] = heartbeat.wait(1)
            blocker.rollback()
            blocker.close()

        releaser = threading.Thread(target=release)
        releaser.start()
        try:
            return super()._write_one(conn, event)
        finally:
            releaser.join(2)
            assert not releaser.is_alive()
            self.observation['operation_ms'] = (time.perf_counter() - started) * 1000


async def check(directory):
    """真实 submit 回执后读取同一 trace/cursor，再等待 writer 停机。"""
    path = directory / 'observe.db'
    writer = LockedWriter(path)
    task = asyncio.create_task(writer.run())
    try:
        await writer.submit(events.TurnTrace('agent', 'fixture', 'input', 'output',
                                             projection_key='fixture:0',
                                             projection_source='agent', through_seq=0))
        await writer.drain()
        assert writer.observation['heartbeat_before_release'] is (not args.baseline)
        with sqlite3.connect(path) as conn:
            assert conn.execute('SELECT COUNT(*) FROM turns').fetchone()[0] == 1
            assert conn.execute('SELECT through_seq FROM projection_cursors').fetchone()[0] == 0
            assert conn.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
        return writer.observation
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


async def startup_failure(directory):
    """真实坏 SQLite 同时拒绝初始化与提交，并关闭已经打开的连接。"""
    path = directory / 'bad.db'
    path.write_bytes(b'fixture invalid sqlite file')
    opened = []
    real_connect = sqlite3.connect

    class TrackedConnection(sqlite3.Connection):
        closed = False

        def close(self):
            super().close()
            self.closed = True

    def connect(*values, **options):
        connection = real_connect(*values, **options, factory=TrackedConnection)
        opened.append(connection)
        return connection

    sqlite3.connect = connect
    writer = plugin.TraceWriter(path)
    caller = asyncio.create_task(writer.submit(events.TurnTrace('agent', 'bad', None, 'fixture')))
    ready = asyncio.create_task(writer.wait_ready())
    runner = asyncio.create_task(writer.run())
    try:
        results = await asyncio.gather(caller, ready, runner, return_exceptions=True)
        assert all(isinstance(error, sqlite3.DatabaseError) for error in results)
        await writer.drain()
        assert len(opened) == 1 and opened[0].closed, '初始化失败不能把活连接留在错误回执中'
        return {'case': 'bad-sqlite-startup', 'failed_waiters': 3, 'closed_connections': 1}
    finally:
        sqlite3.connect = real_connect
        await asyncio.gather(caller, ready, runner, return_exceptions=True)
        for connection in opened:
            if not connection.closed:
                connection.close()


async def loop_marker():
    """用一次明确的 loop 排程让已创建任务走到下一等待点。"""
    done = asyncio.get_running_loop().create_future()
    asyncio.get_running_loop().call_soon(done.set_result, None)
    await done


class HeldWriter(plugin.TraceWriter):
    """在实际 SQL 前暂停首项，让取消与队列接纳顺序可控制。"""

    def __init__(self, path):
        super().__init__(path)
        self.loop = asyncio.get_running_loop()
        self.entered = self.loop.create_future()
        self.release = threading.Event()
        self.calls = 0
        self.threads = set()

    def _write_one(self, connection, event):
        self.threads.add(threading.get_ident())
        self.calls += 1
        if self.calls == 1:
            self.loop.call_soon_threadsafe(self.entered.set_result, None)
            if not self.release.wait(5):
                raise TimeoutError('fixture write gate was not released')
        return super()._write_one(connection, event)

    def _read_pending_recalls(self, connection, keys):
        self.threads.add(threading.get_ident())
        return super()._read_pending_recalls(connection, keys)


def trace(index, scope):
    return events.TurnTrace('agent', scope, 'input', 'output',
                            projection_key=f'{scope}:{index}',
                            projection_source=scope, through_seq=index)


async def cancel_full_queue(directory):
    """重复取消后停止新接纳，但 501 项已接纳事务仍逐项确认并关闭。"""
    path = directory / 'cancel-full.db'
    writer = HeldWriter(path)
    runner = asyncio.create_task(writer.run())
    await writer.wait_ready()
    first = asyncio.create_task(writer.submit(trace(0, 'full')))
    callers = [first]
    try:
        await writer.entered
        callers += [asyncio.create_task(writer.submit(trace(i, 'full'))) for i in range(1, 501)]
        await loop_marker()
        assert writer._queue.qsize() == 500
        overflow = asyncio.create_task(writer.submit(trace(501, 'full')))
        await loop_marker()
        assert not overflow.done()
        runner.cancel()
        await loop_marker()
        runner.cancel()
        await loop_marker()
        assert not runner.done() and writer._connection is not None
        rejected = await asyncio.gather(overflow, return_exceptions=True)
        assert isinstance(rejected[0], asyncio.QueueShutDown)
        writer.emit(trace(502, 'full'))
        assert writer._dropped == 1
        writer.release.set()
        outcomes = await asyncio.gather(*callers, runner, return_exceptions=True)
        assert all(value is None for value in outcomes[:-1])
        assert isinstance(outcomes[-1], asyncio.CancelledError)
        await writer.drain()
        assert writer._connection is None and writer._executor is None
        assert len(writer.threads) == 1 and threading.get_ident() not in writer.threads
        with sqlite3.connect(path) as connection:
            assert connection.execute('SELECT COUNT(*) FROM turns').fetchone()[0] == 501
            assert connection.execute('SELECT through_seq FROM projection_cursors').fetchone()[0] == 500
            assert connection.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
        return {'accepted_and_committed': 501, 'rejected_blocked_submit': 1,
                'rejected_emit': 1, 'physical_threads': 1, 'closed_after_drain': True}
    finally:
        writer.release.set()
        runner.cancel()
        await asyncio.gather(*callers, runner, return_exceptions=True)


def leaves(error):
    if isinstance(error, BaseExceptionGroup):
        return [leaf for child in error.exceptions for leaf in leaves(child)]
    return [error]


async def cancel_sql_failure(directory):
    """取消同时遭遇真实 SQL 拒绝，当前项和排队项都收到完整失败。"""
    path = directory / 'cancel-failure.db'
    writer = HeldWriter(path)
    runner = asyncio.create_task(writer.run())
    await writer.wait_ready()
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TRIGGER fixture_refusal BEFORE INSERT ON turns "
                           "BEGIN SELECT RAISE(ABORT, 'fixture-refusal'); END")
    callers = [asyncio.create_task(writer.submit(trace(0, 'fail')))]
    try:
        await writer.entered
        callers += [asyncio.create_task(writer.submit(trace(i, 'fail'))) for i in (1, 2)]
        await loop_marker()
        runner.cancel()
        await loop_marker()
        runner.cancel()
        await loop_marker()
        writer.release.set()
        outcomes = await asyncio.gather(*callers, runner, return_exceptions=True)
        for error in outcomes:
            assert isinstance(error, BaseExceptionGroup), error
            failures = leaves(error)
            assert any(isinstance(leaf, asyncio.CancelledError) for leaf in failures)
            assert any(isinstance(leaf, sqlite3.IntegrityError) and
                       'fixture-refusal' in str(leaf) for leaf in failures)
        await writer.drain()
        assert writer.calls == 1 and writer._connection is None and writer._executor is None
        with sqlite3.connect(path) as connection:
            assert connection.execute('SELECT COUNT(*) FROM turns').fetchone()[0] == 0
            assert connection.execute('SELECT COUNT(*) FROM projection_cursors').fetchone()[0] == 0
            assert connection.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
        return {'failed_current_and_queued_and_runner': 4, 'original_sql_error': True,
                'cancel_cause': True, 'partial_commits': 0, 'closed_connection': True}
    finally:
        writer.release.set()
        runner.cancel()
        await asyncio.gather(*callers, runner, return_exceptions=True)


async def cancelled_caller(directory):
    """提交者取消等待不撤销已经接纳的耐久事务，后续查询仍在同一线程。"""
    path = directory / 'cancel-caller.db'
    writer = HeldWriter(path)
    runner = asyncio.create_task(writer.run())
    await writer.wait_ready()
    caller = asyncio.create_task(writer.submit(trace(0, 'caller')))
    try:
        await writer.entered
        caller.cancel()
        outcome = await asyncio.gather(caller, return_exceptions=True)
        assert isinstance(outcome[0], asyncio.CancelledError) and not runner.done()
        writer.release.set()
        await writer.drain()
        assert await writer.pending_recalls(['missing', 'missing']) == {'missing'}
        assert len(writer.threads) == 1 and threading.get_ident() not in writer.threads
        with sqlite3.connect(path) as connection:
            assert connection.execute('SELECT COUNT(*) FROM turns').fetchone()[0] == 1
            assert connection.execute('SELECT through_seq FROM projection_cursors').fetchone()[0] == 0
        return {'caller_cancelled': True, 'accepted_transaction_committed': True,
                'read_and_write_same_thread': True}
    finally:
        writer.release.set()
        runner.cancel()
        await asyncio.gather(caller, runner, return_exceptions=True)


async def startup_cancel(directory, bad=False):
    """取消初始化仍等待真实 open 结算，并在物理线程关闭连接。"""
    path = directory / ('cancel-bad-start.db' if bad else 'cancel-start.db')
    if bad:
        path.write_bytes(b'fixture invalid sqlite file')
    loop = asyncio.get_running_loop()
    entered = loop.create_future()
    release = threading.Event()
    real_open = writer_module.open_db
    real_connect = sqlite3.connect
    connections = []

    class TrackedConnection(sqlite3.Connection):
        closed = False

        def close(self):
            super().close()
            self.closed = True
            self.closed_thread = threading.get_ident()

    def connect(*values, **options):
        connection = real_connect(*values, **options, factory=TrackedConnection)
        connection.opened_thread = threading.get_ident()
        connections.append(connection)
        return connection

    def held_open(path):
        loop.call_soon_threadsafe(entered.set_result, None)
        if not release.wait(5):
            raise TimeoutError('fixture startup gate was not released')
        return real_open(path)

    sqlite3.connect = connect
    writer_module.open_db = held_open
    writer = plugin.TraceWriter(path)
    caller = asyncio.create_task(writer.submit(trace(0, 'startup')))
    ready = asyncio.create_task(writer.wait_ready())
    runner = asyncio.create_task(writer.run())
    try:
        await entered
        runner.cancel()
        await loop_marker()
        runner.cancel()
        await loop_marker()
        assert not runner.done() and not ready.done() and not caller.done()
        release.set()
        outcomes = await asyncio.gather(caller, ready, runner, return_exceptions=True)
        for error in outcomes:
            assert any(isinstance(leaf, asyncio.CancelledError) for leaf in leaves(error))
            if bad:
                assert any(isinstance(leaf, sqlite3.DatabaseError) for leaf in leaves(error))
        await writer.drain()
        assert len(connections) == 1 and connections[0].closed
        assert connections[0].opened_thread == connections[0].closed_thread != threading.get_ident()
        assert writer._connection is None and writer._executor is None
        return {'cancelled_waiters': 3, 'closed_connections': 1,
                'original_open_error': bad, 'open_close_same_thread': True}
    finally:
        release.set()
        await asyncio.gather(caller, ready, runner, return_exceptions=True)
        writer_module.open_db = real_open
        sqlite3.connect = real_connect


async def close_cancel_preserves_error(directory):
    """关闭阶段再次取消不能把已发生的 SQL 拒绝变成普通停止。"""
    path = directory / 'cancel-close.db'
    loop = asyncio.get_running_loop()
    closing = loop.create_future()
    release_close = threading.Event()
    real_open = writer_module.open_db
    real_connect = sqlite3.connect

    class HeldClose(sqlite3.Connection):
        closed = False

        def close(self):
            loop.call_soon_threadsafe(closing.set_result, None)
            if not release_close.wait(5):
                raise TimeoutError('fixture close gate was not released')
            super().close()
            self.closed = True

    def tracked_open(path):
        def connect(*values, **options):
            return real_connect(*values, **options, factory=HeldClose)
        sqlite3.connect = connect
        try:
            return real_open(path)
        finally:
            sqlite3.connect = real_connect

    writer_module.open_db = tracked_open
    writer = HeldWriter(path)
    runner = asyncio.create_task(writer.run())
    callers = []
    try:
        await writer.wait_ready()
        connection = writer._connection
        with real_connect(path) as setup:
            setup.execute("CREATE TRIGGER fixture_refusal BEFORE INSERT ON turns "
                          "BEGIN SELECT RAISE(ABORT, 'fixture-close-refusal'); END")
        callers.append(asyncio.create_task(writer.submit(trace(0, 'close'))))
        await writer.entered
        runner.cancel()
        await loop_marker()
        writer.release.set()
        await closing
        runner.cancel()
        await loop_marker()
        assert not runner.done() and not connection.closed
        release_close.set()
        outcomes = await asyncio.gather(*callers, runner, return_exceptions=True)
        for error in outcomes:
            assert any(isinstance(leaf, sqlite3.IntegrityError) for leaf in leaves(error)), error
            assert any(isinstance(leaf, asyncio.CancelledError) for leaf in leaves(error)), error
        assert connection.closed and writer._connection is None and writer._executor is None
        await writer.drain()
        return {'sql_error_reaches_runner': True, 'close_cancel_preserved': True,
                'closed_after_physical_close': True}
    finally:
        writer.release.set()
        release_close.set()
        await asyncio.gather(*callers, runner, return_exceptions=True)
        writer_module.open_db = real_open
        sqlite3.connect = real_connect


async def run_checks(directory):
    """八个边界场景使用独立数据库，不写正式 workspace。"""
    result = {'write_lock': await check(directory)}
    if not args.baseline:
        result['startup_failure'] = await startup_failure(directory)
        result['cancel_full_queue'] = await cancel_full_queue(directory)
        result['cancel_sql_failure'] = await cancel_sql_failure(directory)
        result['cancelled_caller'] = await cancelled_caller(directory)
        result['startup_cancel'] = await startup_cancel(directory)
        result['startup_cancel_sql_failure'] = await startup_cancel(directory, bad=True)
        result['close_cancel_preserves_error'] = await close_cancel_preserves_error(directory)
    return result


if __name__ == '__main__':
    with tempfile.TemporaryDirectory(prefix='observe-writer-io-') as temporary:
        result = asyncio.run(asyncio.wait_for(run_checks(Path(temporary)), 20))
    print(json.dumps({'source': str(args.source), 'baseline': args.baseline,
                      'check': result, 'formal_workspace': 'not touched'}, indent=2))
