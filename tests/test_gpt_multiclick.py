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
    prompts = []

    def __init__(self, client):
        self.client = client

    async def gpt(self, prompt):
        FakeLink.prompts.append(prompt)
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
    FakeLink.prompts = []


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


def test_poem_fill_accepts_plain_chars():
    """模型有时直接给字 ("丛在") 而不写 [CLICK]^丛^, 诗句题要能兜住."""
    FakeLink.answer = "答案是：丛在"
    inst = make_checkin()
    message = FakeMessage(POEM, BUTTONS)
    assert asyncio.run(inst.gpt_handle_message(message, unexpected=False)) is True
    assert message.clicked == ["丛", "在"]


def test_plain_chars_ignored_outside_poem_fill():
    """非诗句题不启用字符兜底, 避免把解释文字当成答案乱点."""
    FakeLink.answer = "丛在"
    inst = make_checkin()
    message = FakeMessage("请选择正确答案", BUTTONS)
    asyncio.run(inst.gpt_handle_message(message, unexpected=False))
    assert message.clicked == []


def test_click_pattern_extracts_all_targets():
    assert _base.CLICK_PATTERN.findall("[CLICK]^丛^[CLICK]^在^") == ["丛", "在"]
    assert _base.CLICK_PATTERN.findall("[CLICK]^丛^") == ["丛"]
    assert _base.CLICK_PATTERN.findall("[NO_RESP]") == []


# ------------------------------------------------- 空位计数与 "没补全" 重答

# 只有一个占位符, 但机器人实际要求补两个字 (用户线上遇到的原始题面).
POEM_SINGLE = """🌸 防机器签到验证 🌸

请继续点击补全诗句：

乃者兵░是凶器，圣人不得已而用之..."""

BUTTONS_SINGLE = [["天", "孤", "共", "齐"], ["者", "落", "知", "飞"]]

COMPLAINT = "空位连在一起了没有补全，至少要按顺序点击两个答案按钮"


def test_count_poem_blanks():
    assert _base.count_poem_blanks("忆对中秋丹桂＿。 花＿杯中。") == 2
    assert _base.count_poem_blanks("乃者兵░是凶器") == 1
    assert _base.count_poem_blanks("乃者兵░░是凶器") == 2
    # 单个下划线属于用户名 (如 @some_bot), 不能算成空位
    assert _base.count_poem_blanks("@some_bot 签到") == 0
    assert _base.count_poem_blanks("") == 0


def test_prompt_requires_exact_number_of_clicks():
    """多个空位时, 要明确告诉模型必须输出同样数量的 [CLICK]."""
    FakeLink.answer = "[CLICK]^丛^[CLICK]^在^"
    inst = make_checkin()
    asyncio.run(inst.gpt_handle_message(FakeMessage(POEM, BUTTONS), unexpected=False))
    assert "一共挖空了 2 个字" in FakeLink.prompts[0]


def test_single_placeholder_still_warns_about_multiple_chars():
    """只看到一个占位符也不能就答一个字: 占位符宽度不代表字数."""
    FakeLink.answer = "[CLICK]^者^"
    inst = make_checkin()
    asyncio.run(inst.gpt_handle_message(FakeMessage(POEM_SINGLE, BUTTONS_SINGLE), unexpected=False))
    assert "占位符的宽度不代表空缺的字数" in FakeLink.prompts[0]


def test_is_challenge_incomplete_requires_a_known_quiz():
    inst = make_checkin()
    # 没记下题目时不能接管, 否则会把正常消息吞掉
    assert inst.is_challenge_incomplete(COMPLAINT) is False
    inst._challenge_message = object()
    assert inst.is_challenge_incomplete(COMPLAINT) is True
    assert inst.is_challenge_incomplete("🎉 签到成功") is False


def test_incomplete_complaint_triggers_retry():
    """机器人说 "没补全" 时, 带着它的原话和上次点过的字重新作答."""
    FakeLink.answer = "[CLICK]^者^[CLICK]^落^"
    inst = make_checkin()
    quiz = FakeMessage(POEM_SINGLE, BUTTONS_SINGLE)
    inst._challenge_message = quiz
    inst._challenge_clicked = ["者"]

    assert asyncio.run(inst.on_challenge_incomplete(None, COMPLAINT)) is True
    assert quiz.clicked == ["者", "落"]
    assert inst._challenge_retries == 1
    prompt = FakeLink.prompts[-1]
    assert COMPLAINT in prompt
    assert "上次已经点击过: 者" in prompt


def test_retry_is_capped_at_two():
    FakeLink.answer = "[CLICK]^者^"
    inst = make_checkin()
    inst._challenge_message = FakeMessage(POEM_SINGLE, BUTTONS_SINGLE)
    for _ in range(5):
        assert asyncio.run(inst.on_challenge_incomplete(None, COMPLAINT)) is True
    assert inst._challenge_retries == 2
    # 超限后不再调用模型, 只是把消息吞掉等超时重试
    assert len(FakeLink.prompts) == 2


def test_challenge_branch_records_quiz_message():
    """出题时要把题面消息和点过的字记下来, 否则机器人抱怨时无从重答."""
    FakeLink.answer = "[CLICK]^者^"
    inst = make_checkin()
    quiz = FakeMessage(POEM_SINGLE, BUTTONS_SINGLE)
    asyncio.run(inst.on_text(quiz, POEM_SINGLE))
    assert inst._challenge_message is quiz
    assert inst._challenge_clicked == ["者"]
    assert quiz.clicked == ["者"]
    assert inst.failed == []


def test_on_text_routes_complaint_to_retry():
    """on_text 必须把 "没补全" 的抱怨接到重答分支, 而不是当成异常返回信息."""
    FakeLink.answer = "[CLICK]^者^[CLICK]^落^"
    inst = make_checkin()
    quiz = FakeMessage(POEM_SINGLE, BUTTONS_SINGLE)
    inst._challenge_message = quiz

    asyncio.run(inst.on_text(None, COMPLAINT))
    assert quiz.clicked == ["者", "落"]
    assert inst.failed == []


def test_complaint_without_quiz_is_not_swallowed():
    """没有正在作答的题目时, 这句抱怨不能被悄悄吃掉, 仍走通用处理."""
    inst = make_checkin()
    assert asyncio.run(inst.on_challenge_incomplete(None, COMPLAINT)) is False
