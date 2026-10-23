"""模板 A 签到按钮「按位置 / 按正则」定位的回归测试.

背景: Ask Ash (@ashUniversal_Bot) 的签到按钮文案每天换 (如 "今朝已至"),
文本匹配必然失效. 新增 bot_checkin_button_index / bot_checkin_button_pat 两条定位路径.
"""

import asyncio

import pytest
from loguru import logger

from embykeeper.telegram.checkiner import _templ_a
from embykeeper.telegram.checkiner._base import BotCheckin
from embykeeper.telegram.checkiner._templ_a import TemplateACheckin, TemplateACheckinConfig

# 截图里的真实面板, 2 列网格, "今朝已至" 是第 8 个按钮.
PANEL = [
    ["兑换商店", "删除账号"],
    ["显示/隐藏", "重置密码"],
    ["我的收藏", "我的设备"],
    ["唤醒口令", "今朝已至"],
    ["渔务托管", "德州托管"],
    ["徽章系统", "补充计划"],
    ["问币银行"],
    ["主界面"],
]


class FakeButton:
    def __init__(self, text, callback_data=None):
        self.text = text
        self.callback_data = callback_data or f"cb_{text}"


class FakeMarkup:
    def __init__(self, rows):
        self.inline_keyboard = [[FakeButton(text) for text in row] for row in rows]


class FakeMessage:
    def __init__(self, rows, text="欢迎进入用户面板！San"):
        self.reply_markup = FakeMarkup(rows)
        self.text = text
        self.caption = None
        self.edit_date = None
        self.clicked = []

    async def click(self, x, y=None):
        self.clicked.append((x, y))
        return "callback-answer"


class FakeAsyncIO:
    @staticmethod
    async def sleep(_):
        return None


@pytest.fixture
def fast_sleep(monkeypatch):
    monkeypatch.setattr(_templ_a, "asyncio", FakeAsyncIO)


def make_checkin(**kw):
    """造一个只带 message_handler 所需属性的实例, 避开 __init__ 里的网络依赖."""
    inst = object.__new__(TemplateACheckin)
    inst.log = logger.bind(name="test")
    inst.bot_checkin_path = None
    inst._path_index = 0
    inst.templ_panel_keywords = ["用户面板"]
    inst.bot_checkin_button = ["签到", "簽到"]
    inst.bot_checkin_button_pat = None
    inst.bot_checkin_button_index = None
    inst.failed = []
    inst.answers = []

    async def on_button_answer(answer):
        inst.answers.append(answer)

    async def fail():
        inst.failed.append(True)

    inst.on_button_answer = on_button_answer
    inst.fail = fail
    inst.is_challenge_text = lambda text: False
    for key, value in kw.items():
        setattr(inst, key, value)
    return inst


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------- 位置解析


def test_flat_index_counts_left_to_right_top_to_bottom():
    inst = make_checkin()
    inline = FakeMarkup(PANEL).inline_keyboard
    # 第 8 个按钮 = 第 4 行第 2 列 (0 起)
    assert inst._resolve_button_index(inline, 8) == (3, 1)
    assert inst._resolve_button_index(inline, 1) == (0, 0)
    assert inst._resolve_button_index(inline, 13) == (6, 0)


def test_row_col_pair_is_one_based():
    inst = make_checkin()
    inline = FakeMarkup(PANEL).inline_keyboard
    assert inst._resolve_button_index(inline, [4, 2]) == (3, 1)
    # 单元素列表按扁平序号处理
    assert inst._resolve_button_index(inline, [8]) == (3, 1)


def test_out_of_range_index_returns_none():
    inst = make_checkin()
    inline = FakeMarkup(PANEL).inline_keyboard
    assert inst._resolve_button_index(inline, 99) is None
    assert inst._resolve_button_index(inline, 0) is None
    assert inst._resolve_button_index(inline, -1) is None
    assert inst._resolve_button_index(inline, [4, 3]) is None  # 第 4 行只有 2 列
    assert inst._resolve_button_index(inline, [9, 1]) is None
    assert inst._resolve_button_index(inline, [1, 2, 3]) is None


# ---------------------------------------------------------------- 按钮定位


def test_index_locates_button_whose_text_changed():
    """文案换成完全不相干的词, 只要位置不变就能点中."""
    inst = make_checkin(bot_checkin_button_index=8)
    changed = [row[:] for row in PANEL]
    changed[3][1] = "春风又绿"
    inline = FakeMarkup(changed).inline_keyboard
    assert inst._find_checkin_button(inline) == (3, 1)


def test_index_wins_over_text_match():
    inst = make_checkin(bot_checkin_button_index=8)
    inline = FakeMarkup(PANEL).inline_keyboard
    # 面板里没有 "签到" 字样, 但即便有也应以位置为准
    assert inst._find_checkin_button(inline) == (3, 1)


def test_text_match_still_works_without_index():
    inst = make_checkin()
    inline = FakeMarkup([["签到", "返回"]]).inline_keyboard
    assert inst._find_checkin_button(inline) == (0, 0)


def test_regex_matches_changing_text():
    inst = make_checkin(bot_checkin_button_pat=r"^今朝")
    changed = [row[:] for row in PANEL]
    changed[3][1] = "今朝未至"
    inline = FakeMarkup(changed).inline_keyboard
    assert inst._find_checkin_button(inline) == (3, 1)


def test_no_match_returns_none():
    inst = make_checkin()
    inline = FakeMarkup(PANEL).inline_keyboard
    assert inst._find_checkin_button(inline) is None


def test_bad_regex_is_rejected_at_init():
    cfg = TemplateACheckinConfig.model_validate({"bot_checkin_button_pat": "([unclosed"})
    assert cfg.bot_checkin_button_pat == "([unclosed"


# ---------------------------------------------------------------- 完整流程


def test_handler_clicks_by_position(fast_sleep):
    inst = make_checkin(bot_checkin_button_index=8)
    message = FakeMessage(PANEL)
    run(inst.message_handler(None, message))
    assert message.clicked == [(3, 1)]
    assert inst.answers == ["callback-answer"]
    assert inst.failed == []


def test_handler_fails_when_unmatched(fast_sleep):
    inst = make_checkin()
    message = FakeMessage(PANEL)
    run(inst.message_handler(None, message))
    assert message.clicked == []
    assert inst.failed == [True]


def test_log_dumps_numbered_buttons():
    """失败日志里要带序号, 方便直接抄进 bot_checkin_button_index."""
    inst = make_checkin()
    records = []
    handler_id = logger.add(lambda m: records.append(str(m)), level="WARNING")
    try:
        inst._log_panel_buttons(FakeMarkup(PANEL).inline_keyboard)
    finally:
        logger.remove(handler_id)
    text = "".join(records)
    assert "8.今朝已至" in text
    assert "1.兑换商店" in text


def test_log_handles_message_without_buttons():
    inst = make_checkin()
    records = []
    handler_id = logger.add(lambda m: records.append(str(m)), level="WARNING")
    try:
        inst._log_panel_buttons([])
    finally:
        logger.remove(handler_id)
    assert "没有任何内联按钮" in "".join(records)


def test_handler_ignores_messages_without_panel_keyword(fast_sleep, monkeypatch):
    """面板关键词不匹配的消息应交给基类, 不能在这里抢答."""
    handled = []

    async def fake_base_handler(self, client, message, type=None):
        handled.append(message.text)

    monkeypatch.setattr(BotCheckin, "message_handler", fake_base_handler)
    inst = make_checkin(bot_checkin_button_index=8)
    message = FakeMessage(PANEL, text="完全无关的聊天内容")
    run(inst.message_handler(None, message))
    assert message.clicked == []
    assert handled == ["完全无关的聊天内容"]


def test_config_fields_are_typed():
    cfg = TemplateACheckinConfig.model_validate({"bot_checkin_button_index": 8})
    assert cfg.bot_checkin_button_index == 8
    cfg = TemplateACheckinConfig.model_validate({"bot_checkin_button_index": [4, 2]})
    assert cfg.bot_checkin_button_index == [4, 2]
