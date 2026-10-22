"""签到结果判定的回归测试.

背景: 智能回答在"未预期消息"上点击按钮后会调用 fail() 停止流程, 但机器人随后仍可能
返回"签到成功". 旧实现会因 current_retries 被置为 inf 而在收尾阶段用 FAIL 覆盖 SUCCESS,
导致日志显示成功、汇总却算失败.
"""

import asyncio

from embykeeper.runinfo import RunContext, RunStatus
from embykeeper.telegram.checkiner._base import BotCheckin


def make_checkin():
    """绕过 __init__ 的依赖, 只装配 finish() 需要的最小状态."""
    inst = object.__new__(BotCheckin)
    inst.ctx = RunContext.prepare("test")
    inst.finished = asyncio.Event()
    inst.current_retries = 0
    return inst


def test_success_is_not_overridden_by_later_fail():
    async def scenario():
        inst = make_checkin()

        await inst.finish(RunStatus.SUCCESS, "签到成功")
        assert inst.ctx.status == RunStatus.SUCCESS

        # 智能回答路径在成功消息之后仍可能触发 fail(), 不应回退状态
        await inst.fail()
        assert inst.ctx.status == RunStatus.SUCCESS

    asyncio.run(scenario())


def test_fail_still_works_before_success():
    async def scenario():
        inst = make_checkin()

        await inst.fail()
        assert inst.ctx.status == RunStatus.FAIL
        assert inst.current_retries == float("inf")

    asyncio.run(scenario())


def test_noneed_is_also_terminal():
    async def scenario():
        inst = make_checkin()

        await inst.finish(RunStatus.NONEED, "今日已签到")
        await inst.fail()
        assert inst.ctx.status == RunStatus.NONEED

    asyncio.run(scenario())


def test_timeout_handler_precedes_oserror():
    """超时必须在 OSError 之前捕获.

    Python 3.11 起 asyncio.TimeoutError 成为 OSError 的子类 (3.10 及以前是独立类),
    顺序写反会让超时被当作"初始化错误"处理, 并打印无信息的空 traceback.
    """
    import inspect
    import re

    src = inspect.getsource(BotCheckin.start)
    timeout_match = re.search(r"except asyncio\.TimeoutError:", src)
    oserror_match = re.search(r"except OSError as e:", src)

    assert timeout_match is not None, "start() 缺少 TimeoutError 处理"
    assert oserror_match is not None, "start() 缺少 OSError 处理"
    assert timeout_match.start() < oserror_match.start(), "except 顺序错误: TimeoutError 必须在 OSError 之前"
