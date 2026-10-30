"""账号关闭阶段 "Cannot operate on a closed database" 的回归测试.

背景: 10/30 的日志在签到汇总打印完之后还挂着一段

    Task exception was never retrieved
    future: <Task finished ... coro=<Session.restart() ...>>
    sqlite3.ProgrammingError: Cannot operate on a closed database.

原因是 pyrogram 的 Session.start() 在遇到 OSError 时不抛出, 而是
`self.client.loop.create_task(self.restart())` 起一个没人 await、也没有句柄的游离任务.
restart() 里会 stop() + start(), 而 start() 第一件事就是读 storage.api_id();
此时 clean() 已经 await client.stop() (disconnect 会 storage.close()), 于是撞上已关闭的库.

这些用例锁住 _freeze_session 的行为: 冻结之后, 无论重启任务是在冻结前还是冻结后创建的,
都不能再碰 storage.
"""

import asyncio
from types import SimpleNamespace

from embykeeper.telegram.session import _freeze_session


class FakeStorage:
    """关闭后读取 api_id 会抛异常, 模拟 sqlite3.ProgrammingError."""

    def __init__(self):
        self.closed = False

    async def api_id(self):
        if self.closed:
            raise RuntimeError("Cannot operate on a closed database")
        return 123456


class FakeSession:
    def __init__(self):
        self.storage = FakeStorage()
        self.start_calls = 0
        self.stop_calls = 0

    async def stop(self):
        self.stop_calls += 1

    async def start(self):
        self.start_calls += 1
        return await self.storage.api_id()

    async def restart(self):
        await self.stop()
        await self.start()


def make_client(*sessions):
    main = sessions[0]
    return SimpleNamespace(session=main, sessions={i: s for i, s in enumerate(sessions)})


def test_restart_after_freeze_does_not_touch_storage():
    session = FakeSession()
    client = make_client(session)

    _freeze_session(client)
    session.storage.closed = True

    asyncio.run(session.restart())

    assert session.start_calls == 0


def test_start_after_freeze_is_noop():
    session = FakeSession()
    _freeze_session(make_client(session))
    session.storage.closed = True

    asyncio.run(session.start())

    assert session.start_calls == 0


def test_task_created_before_freeze_is_still_neutralized():
    """冻结之前抓到的是原始 bound method, 但它内部调用的 self.start 会被换成空实现."""
    session = FakeSession()
    client = make_client(session)

    original_restart = session.restart  # 模拟 create_task(session.restart()) 的抓取时机
    _freeze_session(client)
    session.storage.closed = True

    asyncio.run(original_restart())

    assert session.stop_calls == 1
    assert session.start_calls == 0


def test_all_sessions_in_the_client_are_frozen():
    main = FakeSession()
    other = FakeSession()
    client = make_client(main, other)

    _freeze_session(client)
    main.storage.closed = True
    other.storage.closed = True

    async def run():
        await main.restart()
        await other.restart()

    asyncio.run(run())

    assert main.start_calls == 0
    assert other.start_calls == 0


def test_unfrozen_session_still_restarts():
    """对照组: 没冻结的会话照常重启, 证明冻结不是全局副作用."""
    session = FakeSession()

    asyncio.run(session.restart())

    assert session.start_calls == 1


def test_freeze_tolerates_client_without_session():
    """client.start() 超时的情况下 session 可能还是 None, 不能因此抛异常."""
    _freeze_session(SimpleNamespace(session=None, sessions={}))
