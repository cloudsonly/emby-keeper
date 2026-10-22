"""验证问题 (算术题等) 不应被误判为签到成功.

背景: MICU 点签到后要答算术题, EmbyYing_2 要把答案输入框发送. 机器人的提示往往是
"签到成功, 请回答: 5+5=?" 这种混合文本, 旧实现命中 "成功" 就直接收工, 用户到聊天里
一看根本没签到. 现在命中验证关键词后交给智能回答, 且不终止签到流程.
"""

import asyncio

import pytest
from loguru import logger

from embykeeper.runinfo import RunContext, RunStatus
from embykeeper.telegram.checkiner import _base
from embykeeper.telegram.checkiner._base import BotCheckin, MessageType, default_keywords
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


MICU_PROMPT = """【🎯 每日签到
请完成下面这道题后签到：
17 - 15 = ?
答错会重新生成一题。】"""


def test_micu_prompt_is_not_treated_as_success():
    """MICU 题面含 "完成" (成功关键词) 且措辞不含 "请回答", 必须靠算式模式识别."""
    inst = make_checkin()

    async def scenario():
        await inst.on_text(None, MICU_PROMPT)

    asyncio.run(scenario())
    assert inst.gpt_calls == [False]
    assert inst.ctx.status != RunStatus.SUCCESS


def test_math_expression_detected_without_keywords():
    inst = make_checkin()
    inst.bot_challenge_keywords = ["绝不出现的词"]
    assert inst.is_challenge_text("7*8=?") is True
    assert inst.is_challenge_text("12 ÷ 3 = ?") is True
    assert inst.is_challenge_text("签到成功, +5 分 -> 15 分") is False


def test_plain_success_with_numbers_is_not_challenge():
    inst = make_checkin()
    assert inst.is_challenge_text("签到成功: 获得奖励 5, 当前余额 347.") is False


NIUBI_PROMPT = """🌸 防機器签到验证 🌸

请依次点击下方按钮补全诗句：

忆对中秋丹桂＿。 花＿杯中。 月在杯中。

出自：辛弃疾《一剪梅·中秋元月》"""


def test_niubi_poem_prompt_is_a_challenge():
    inst = make_checkin()
    assert inst.is_challenge_text(NIUBI_PROMPT) is True


def test_niubi_poem_prompt_is_not_counted_as_success():
    """旧实现里这条消息不含任何成功关键词, 会走 "异常返回" 分支并直接判失败."""
    inst = make_checkin()

    async def scenario():
        await inst.on_text(None, NIUBI_PROMPT)

    asyncio.run(scenario())
    assert inst.gpt_calls == [False]
    assert inst.ctx.status != RunStatus.SUCCESS


class FakePhotoMessage:
    def __init__(self, caption):
        self.photo = object()
        self.caption = caption
        self.text = None


def test_photo_with_challenge_caption_is_not_ocrd():
    """题目常以 "配图 + 文字按钮" 的形式发来, 图片只是装饰, 当验证码 OCR 只会白跑重试."""
    inst = make_checkin()
    inst.bot_use_captcha = True
    inst.bot_checkin_caption_pat = None
    assert inst.message_type(FakePhotoMessage(NIUBI_PROMPT)) == MessageType.CAPTION
    # 普通验证码图片仍然走 OCR
    assert inst.message_type(FakePhotoMessage("1234")) == MessageType.CAPTCHA


def test_instruction_with_completion_word_is_a_challenge():
    """ "请完成诗句填空进行验证" 只含 "完成" (成功关键词), 必须靠指令模式识别."""
    inst = make_checkin()
    for text in (
        "请完成诗句填空进行验证",
        "请进行人机验证",
        "请完成下列填空",
        "需要完成校验后才能签到",
    ):
        assert inst.is_challenge_text(text) is True, text


def test_instruction_pattern_does_not_swallow_normal_replies():
    inst = make_checkin()
    for text in (
        "请先完成签到",
        "签到成功, 请点击查看详情",
        "验证码错误, 请重试",
        "签到成功: 获得奖励 5, 当前余额 347.",
    ):
        assert inst.is_challenge_text(text) is False, text


def test_poem_fill_pattern_matches_both_phrasings():
    for text in ("请依次点击下方按钮补全诗句：", "请完成诗句填空进行验证"):
        assert _base.POEM_FILL_PATTERN.search(text), text
    assert _base.POEM_FILL_PATTERN.search("签到成功, 当前 347 分") is None


def test_success_ignored_while_challenge_pending():
    """题面提示先到时, 不能让它把正在进行的作答取消掉."""
    inst = make_checkin()
    inst._challenge_pending = True

    async def scenario():
        await inst.on_text(None, "签到成功, 已完成验证")

    asyncio.run(scenario())
    assert inst.ctx.status != RunStatus.SUCCESS
    assert not inst.finished.is_set()


def test_duplicate_challenge_ignored_while_pending():
    inst = make_checkin()
    inst._challenge_pending = True

    async def scenario():
        await inst.on_text(None, NIUBI_PROMPT)

    asyncio.run(scenario())
    assert inst.gpt_calls == []
