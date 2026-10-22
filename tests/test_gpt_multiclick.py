"""智能回答的多按钮依次点击.

背景: @niubi2233_bot 的签到验证是 "请依次点击下方按钮补全诗句", 题面给出一句挖空的
诗 (如 "忆对中秋丹桂＿。 花＿杯中。"), 下面是 8 个候选字按钮, 必须按顺序点中 "丛"
和 "在" 才算完成. 旧实现只解析回答里的第一个 [CLICK]^XXX^, 点一下就收工, 诗句没补
完, 机器人不会给成功消息.
"""

import asyncio

import pytest
from loguru import logger
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from embykeeper.telegram.checkiner import _base
from embykeeper.telegram.checkiner._base import BotCheckin


def make_markup(rows):
    """必须用真的 InlineKeyboardMarkup, gpt_handle_message 会 isinstance 检查它."""
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(text, callback_data=f"cb_{text}") for text in row] for row in rows]
    )


class FakeMessage:
    def __init__(self, text, rows):
        self.text = text
        self.caption = None
        self.reply_markup = make_markup(rows)
        self.clicked = []

    async def click(self, button_text):
        self.clicked.append(button_text)
        return None

    async def reply(self, text):
        return None


class FakeLink:
    answer = ""

    def __init__(self, client):
        self.client = client

    async def gpt(self, prompt):
        return FakeLink.answer, "fake"


class FakeAsyncIO:
    """把 asyncio.sleep 换成空实现, 避免多级点击的间隔拖慢测试."""

    @staticmethod
    async def sleep(_):
        return None


@pytest.fixture(autouse=True)
def patch_environment(monkeypatch):
    monkeypatch.setattr(_base, "Link", FakeLink)
    monkeypatch.setattr(_base, "asyncio", FakeAsyncIO)


def make_checkin():
    """绕过 __init__ 的依赖, 只装配 gpt_handle_message 需要的最小状态."""
    inst = object.__new__(BotCheckin)
    inst.log = logger.bind(name="test")
    inst.client = None
    inst.failed = []

    async def fail(status=None, message=None):
        inst.failed.append(message)
        return False

    inst.fail = fail
    return inst


POEM = """🌸 防機器签到验证 🌸

请依次点击下方按钮补全诗句：

忆对中秋丹桂＿。 花＿杯中。 月在杯中。

出自：辛弃疾《一剪梅·中秋元月》"""

BUTTONS = [["丛", "柳", "疑", "无"], ["在", "山", "水", "路"]]


def test_multiple_clicks_in_order():
    FakeLink.answer = "[CLICK]^丛^[CLICK]^在^"
    inst = make_checkin()
    message = FakeMessage(POEM, BUTTONS)
    assert asyncio.run(inst.gpt_handle_message(message, unexpected=False)) is True
    assert message.clicked == ["丛", "在"]
    assert inst.failed == []


def test_single_click_unchanged():
    FakeLink.answer = "[CLICK]^在^"
    inst = make_checkin()
    message = FakeMessage(POEM, BUTTONS)
    assert asyncio.run(inst.gpt_handle_message(message, unexpected=False)) is True
    assert message.clicked == ["在"]


def test_unmatched_button_aborts_all_clicks():
    """有一个目标匹配不上就整体放弃, 不能只点对一半把题目做废."""
    FakeLink.answer = "[CLICK]^丛^[CLICK]^甲乙丙丁^"
    inst = make_checkin()
    message = FakeMessage(POEM, BUTTONS)
    asyncio.run(inst.gpt_handle_message(message, unexpected=False))
    assert message.clicked == []


def test_multiclick_in_unexpected_mode_still_fails():
    """异常返回时的旧行为要保留: 点完按钮仍判失败, 避免脚本盲点下去."""
    FakeLink.answer = "[CLICK]^丛^[CLICK]^在^"
    inst = make_checkin()
    message = FakeMessage(POEM, BUTTONS)
    assert asyncio.run(inst.gpt_handle_message(message, unexpected=True)) is False
    assert message.clicked == ["丛", "在"]
    assert len(inst.failed) == 1


def test_click_pattern_extracts_all_targets():
    assert _base.CLICK_PATTERN.findall("[CLICK]^丛^[CLICK]^在^") == ["丛", "在"]
    assert _base.CLICK_PATTERN.findall("[CLICK]^丛^") == ["丛"]
    assert _base.CLICK_PATTERN.findall("[NO_RESP]") == []
