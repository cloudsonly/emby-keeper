"""模板 A 多级按钮路径的回归测试.

背景: Ask Ash (@ashUniversal_Bot) 需要先点一级菜单 "用户功能", 才会出现 "数据存档"
按钮. 旧实现只在同一条消息里找签到按钮, 找不到就判 "账户错误", 因此永远签到失败.
"""

import pytest
from loguru import logger

from embykeeper.telegram.checkiner import _templ_a
from embykeeper.telegram.checkiner._base import BotCheckin
from embykeeper.telegram.checkiner._templ_a import TemplateACheckin, TemplateACheckinConfig


class FakeButton:
    def __init__(self, text):
        self.text = text


class FakeMarkup:
    def __init__(self, rows):
        self.inline_keyboard = [[FakeButton(text) for text in row] for row in rows]


class FakeKeyboardMarkup:
    """底部回复键盘: 没有 inline_keyboard, 按钮文字要当消息发出去."""

    def __init__(self, rows):
        self.keyboard = [[FakeButton(text) for text in row] for row in rows]


class FakeMessage:
    def __init__(self, rows, clicked, markup=None):
        self.reply_markup = markup if markup is not None else FakeMarkup(rows)
        self.clicked = clicked
        self.replied = []
        self.text = ""
        self.caption = None
        self.edit_date = None

    async def click(self, button_text):
        self.clicked.append(button_text)
        return "callback-answer"

    async def reply(self, text):
        self.replied.append(text)
        return None


class FakeAsyncIO:
    """把 asyncio.sleep 换成空实现, 避免测试等真实延迟."""

    @staticmethod
    async def sleep(_):
        return None


@pytest.fixture
def fast_sleep(monkeypatch):
    monkeypatch.setattr(_templ_a, "asyncio", FakeAsyncIO)


def make_checkin(path):
    inst = object.__new__(TemplateACheckin)
    inst.bot_checkin_path = path
    inst._path_index = 0
    inst.log = logger.bind(name="test")
    inst.answers = []

    async def on_button_answer(answer, from_path=False):
        inst.answers.append((answer, from_path))

    inst.on_button_answer = on_button_answer
    return inst


def test_config_accepts_checkin_path():
    cfg = TemplateACheckinConfig.model_validate({"bot_checkin_path": ["用户功能", "数据存档"]})
    assert cfg.bot_checkin_path == ["用户功能", "数据存档"]
    # 字符串形式也应被接受 (to_iterable 会包成单元素列表)
    cfg = TemplateACheckinConfig.model_validate({"bot_checkin_path": "用户功能"})
    assert cfg.bot_checkin_path == "用户功能"


def test_path_advances_step_by_step(fast_sleep):
    inst = make_checkin(["用户功能", "数据存档"])

    clicked = []
    first = FakeMessage([["用户功能", "我的信息"]], clicked)
    assert asyncio_run(inst._advance_button_path(first)) is True
    assert clicked == ["用户功能"]
    assert inst._path_index == 1
    # 中间步骤不应触发结果解析
    assert inst.answers == []

    second = FakeMessage([["数据存档", "返回"]], clicked)
    assert asyncio_run(inst._advance_button_path(second)) is True
    assert clicked == ["用户功能", "数据存档"]
    assert inst._path_index == 2
    # 最后一级点击后交给 on_button_answer 解析, 并标记为路径点击
    assert inst.answers == [("callback-answer", True)]


def test_path_returns_false_when_button_absent(fast_sleep):
    inst = make_checkin(["用户功能", "数据存档"])
    clicked = []
    message = FakeMessage([["我的信息", "返回"]], clicked)
    assert asyncio_run(inst._advance_button_path(message)) is False
    assert clicked == []
    assert inst._path_index == 0


def test_path_is_inert_after_completion(fast_sleep):
    inst = make_checkin(["用户功能"])
    clicked = []
    message = FakeMessage([["用户功能"]], clicked)
    assert asyncio_run(inst._advance_button_path(message)) is True
    # 路径已走完, 同一条面板再次出现时不应重复点击
    again = FakeMessage([["用户功能"]], clicked)
    assert asyncio_run(inst._advance_button_path(again)) is False
    assert clicked == ["用户功能"]


def test_no_path_configured_is_noop(fast_sleep):
    inst = make_checkin(None)
    clicked = []
    message = FakeMessage([["用户功能"]], clicked)
    assert asyncio_run(inst._advance_button_path(message)) is False
    assert clicked == []


def test_path_supports_reply_keyboard(fast_sleep):
    """底部回复键盘没有回调, 应把按钮文字当消息发出."""
    inst = make_checkin(["用户功能", "数据存档"])
    clicked = []
    message = FakeMessage(None, clicked, markup=FakeKeyboardMarkup([["用户功能", "我的信息"]]))
    assert asyncio_run(inst._advance_button_path(message)) is True
    assert message.replied == ["用户功能"]
    assert clicked == []
    assert inst._path_index == 1


def test_send_checkin_resets_path_index(fast_sleep, monkeypatch):
    inst = make_checkin(["用户功能", "数据存档"])
    inst._path_index = 2

    async def fake_send_checkin(self, **kw):
        return None

    monkeypatch.setattr(BotCheckin, "send_checkin", fake_send_checkin)
    asyncio_run(inst.send_checkin())
    assert inst._path_index == 0


def asyncio_run(coro):
    import asyncio

    return asyncio.run(coro)
