"""模板 A 签到按钮「按位置 / 按正则」定位的回归测试.

背景: Ask Ash (@ashUniversal_Bot) 的签到按钮文案每天换 (如 "今朝已至"),
文本匹配必然失效. 新增 bot_checkin_button_index / bot_checkin_button_pat 两条定位路径.
"""

import asyncio

import pytest
from loguru import logger
from pyrogram.raw.types.messages import BotCallbackAnswer

from embykeeper.telegram.checkiner import _templ_a
from embykeeper.telegram.checkiner._base import BotCheckin
from embykeeper.telegram.checkiner._templ_a import TemplateACheckin, TemplateACheckinConfig

# 截图里的真实面板, 2 列网格, 签到按钮是第 8 个 (第 4 行第 2 列).
PANEL = [
    ["兑换商店", "删除账号"],
    ["显示/隐藏", "重置密码"],
    ["我的收藏", "我的设备"],
    ["唤醒口令", "到此一游"],
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
        # 与 pyrogram 一致: x 是列, y 是行, 内部取 keyboard[y][x].
        # 故意照抄这个反直觉的顺序, 传反参数时测试才会像线上一样报错.
        keyboard = self.reply_markup.inline_keyboard
        try:
            if y is None:
                button = [b for row in keyboard for b in row][x]
            else:
                button = keyboard[y][x]
        except IndexError:
            raise ValueError(f"The button at position ({x}, {y}) doesn't exist")
        self.clicked.append((x, y))
        return button.text


class BrokenClickMessage(FakeMessage):
    """模拟面板在定位之后被机器人改版: 位置算得出, 点下去却不存在."""

    async def click(self, x, y=None):
        raise ValueError(f"The button at position ({x}, {y}) doesn't exist")


def answer(text):
    return BotCallbackAnswer(cache_time=0, message=text)


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
    # pyrogram 的参数是 (列, 行): 第 4 行第 2 列 -> click(1, 3)
    assert message.clicked == [(1, 3)]
    assert inst.answers == ["到此一游"]
    assert inst.failed == []


def test_handler_fails_when_unmatched(fast_sleep):
    inst = make_checkin()
    message = FakeMessage(PANEL)
    run(inst.message_handler(None, message))
    assert message.clicked == []
    assert inst.failed == [True]


def test_stale_position_fails_gracefully(fast_sleep):
    """定位算得出但点击时按钮已不存在, 应记失败而不是把整个签到器炸掉."""
    inst = make_checkin(bot_checkin_button_index=8)
    message = BrokenClickMessage(PANEL)
    run(inst.message_handler(None, message))
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
    assert "8.到此一游" in text
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


# ------------------------------------------------- 导航按钮的回调提示过滤


def make_answer_handler(**kw):
    """构造只测 on_button_answer 的实例."""
    inst = make_checkin(**kw)
    inst.use_button_answer = True
    inst.bot_text_ignore_answer = ["Done"]
    inst.texts = []

    async def on_text(message, text=None):
        inst.texts.append(text if text is not None else message.text)

    inst.on_text = on_text
    # 真实方法, 不是测试里替换过的桩
    inst.on_button_answer = TemplateACheckin.on_button_answer.__get__(inst, TemplateACheckin)
    inst.is_challenge_text = lambda text: "请回答" in text
    inst.is_result_text = lambda text: any(k in text for k in ("成功", "失败", "已签到", "只能", "签过"))
    return inst


def test_navigation_answer_is_not_sent_to_gpt():
    """点「用户功能」得到的 "✅ 用户界面" 既不是结果也不是题目, 不能交给智能回答."""
    inst = make_answer_handler()
    run(inst.on_button_answer(answer("✅ 用户界面"), from_path=True))
    assert inst.texts == []


def test_navigation_answer_with_result_is_kept():
    """路径最后一级本身就是签到按钮时, 提示里的成功信息仍要处理."""
    inst = make_answer_handler()
    run(inst.on_button_answer(answer("签到成功, 获得 5 币"), from_path=True))
    assert inst.texts == ["签到成功, 获得 5 币"]


def test_navigation_answer_with_challenge_is_kept():
    inst = make_answer_handler()
    run(inst.on_button_answer(answer("请回答: 3+4=?"), from_path=True))
    assert inst.texts == ["请回答: 3+4=?"]


def test_panel_click_answer_is_always_processed():
    """真正的签到点击不受过滤影响, 哪怕提示看起来像导航语."""
    inst = make_answer_handler()
    run(inst.on_button_answer(answer("✅ 用户界面"), from_path=False))
    assert inst.texts == ["✅ 用户界面"]


def test_ignored_answer_still_honours_ignore_list():
    inst = make_answer_handler()
    inst.bot_text_ignore_answer = ["用户界面"]
    run(inst.on_button_answer(answer("✅ 用户界面"), from_path=False))
    assert inst.texts == []
