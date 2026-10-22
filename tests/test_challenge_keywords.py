"""验证问题 (算术题等) 不应被误判为签到成功.

背景: MICU 点签到后要答算术题, EmbyYing_2 要把答案输入框发送. 机器人的提示往往是
"签到成功, 请回答: 5+5=?" 这种混合文本, 旧实现命中 "成功" 就直接收工, 用户到聊天里
一看根本没签到. 现在命中验证关键词后交给智能回答, 且不终止签到流程.
"""

import asyncio

import pytest
from loguru import logger

from embykeeper.runinfo import RunContext, RunStatus
from embykeeper.telegram.checkiner._base import BotCheckin, default_keywords
from embykeeper.telegram.checkiner._templ_a import TemplateACheckinConfig


def make_checkin():
    """绕过 __init__ 的依赖, 只装配 on_text 需要的最小状态."""
    inst = object.__new__(BotCheckin)
    inst.ctx = RunContext.prepare("test")
    inst.finished = asyncio.Event()
    inst.current_retries = 0
    inst.log = logger.bind(name="test")
    inst.gpt_calls = []

    async def fake_gpt(message, unexpected=True):
        inst.gpt_calls.append(unexpected)
        return True

    inst.gpt_handle_message = fake_gpt
    return inst


@pytest.fixture(autouse=True)
def no_cache_write(monkeypatch):
    # finish() 会把结果写进 cache, 单测里没有初始化全局 config
    monkeypatch.setattr(RunContext, "save", lambda self: None)


def test_default_challenge_keywords_cover_common_phrasing():
    words = default_keywords["challenge"]
    for phrase in ("请回答", "请计算", "请输入答案", "请选择正确答案"):
        assert phrase in words


def test_challenge_message_is_answered_not_counted_as_success():
    inst = make_checkin()

    async def scenario():
        await inst.on_text(None, "签到成功, 请回答: 5+5=?")

    asyncio.run(scenario())
    # 交给智能回答, 且用非 unexpected 模式 (回答问题后继续等待真实成功消息)
    assert inst.gpt_calls == [False]
    assert inst.ctx.status != RunStatus.SUCCESS
    assert not inst.finished.is_set()


def test_challenge_with_numbers_is_not_misparsed_as_points():
    """旧实现会把 "当前 5 分, 请回答 5+3=?" 里的两个 5 当成 "+5 分 -> 5 分"."""
    inst = make_checkin()

    async def scenario():
        await inst.on_text(None, "签到成功, 当前 5 分, 请回答 5+3=?")

    asyncio.run(scenario())
    assert inst.gpt_calls == [False]
    assert inst.ctx.status != RunStatus.SUCCESS


def test_plain_success_still_finishes():
    inst = make_checkin()

    async def scenario():
        await inst.on_text(None, "签到成功, +5 分 -> 15 分")

    asyncio.run(scenario())
    assert inst.gpt_calls == []
    assert inst.ctx.status == RunStatus.SUCCESS
    assert inst.finished.is_set()


def test_custom_challenge_keywords_override_builtin():
    inst = make_checkin()
    inst.bot_challenge_keywords = ["算一下"]

    async def scenario():
        await inst.on_text(None, "签到成功, 算一下 3+4")

    asyncio.run(scenario())
    assert inst.gpt_calls == [False]


def test_templ_a_config_accepts_challenge_keywords():
    cfg = TemplateACheckinConfig.model_validate({"bot_challenge_keywords": ["请回答"]})
    assert cfg.bot_challenge_keywords == ["请回答"]
