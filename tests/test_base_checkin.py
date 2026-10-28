import asyncio
from types import SimpleNamespace
from unittest.mock import patch

from embykeeper.telegram.checkiner._base import BotCheckin


def make_checkin() -> BotCheckin:
    context = SimpleNamespace(bind_logger=lambda log: log)
    client = SimpleNamespace(me=SimpleNamespace(full_name="tester"))
    return BotCheckin(client=client, context=context, config={})


def test_on_photo_submits_recognized_captcha():
    checkin = make_checkin()
    checkin.bot_captcha_len = 4
    submitted = []
    retried = []

    async def fake_on_captcha(message, captcha):
        submitted.append(captcha)

    async def fake_retry():
        retried.append(True)

    async def fake_download(message, in_memory=False):
        return b"img"

    class FakeOCR:
        async def run(self, data, expected_len=None):
            assert expected_len == 4
            return "AB12"

    checkin.on_captcha = fake_on_captcha
    checkin.retry = fake_retry
    checkin.client = SimpleNamespace(
        download_media=fake_download,
        me=SimpleNamespace(full_name="tester"),
    )

    with patch("embykeeper.telegram.checkiner._base.OCRService.get", return_value=FakeOCR()):
        with patch("embykeeper.telegram.checkiner._base.random.uniform", return_value=0):
            asyncio.run(checkin.on_photo(SimpleNamespace()))

    assert submitted == ["AB12"]
    assert retried == []


def test_on_photo_retries_when_ocr_returns_empty():
    checkin = make_checkin()
    checkin.bot_captcha_len = 4
    submitted = []
    retried = []

    async def fake_on_captcha(message, captcha):
        submitted.append(captcha)

    async def fake_retry():
        retried.append(True)

    async def fake_download(message, in_memory=False):
        return b"img"

    class FakeOCR:
        async def run(self, data, expected_len=None):
            return ""

    checkin.on_captcha = fake_on_captcha
    checkin.retry = fake_retry
    checkin.client = SimpleNamespace(
        download_media=fake_download,
        me=SimpleNamespace(full_name="tester"),
    )

    with patch("embykeeper.telegram.checkiner._base.OCRService.get", return_value=FakeOCR()):
        asyncio.run(checkin.on_photo(SimpleNamespace()))

    assert submitted == []
    assert retried == [True]


def test_on_text_captcha_error_hook_short_circuits():
    checkin = make_checkin()
    checkin.bot_captcha_error_keywords = ["验证码错误"]
    handled = []
    retried = []

    async def fake_on_captcha_error(message, text):
        handled.append(text)
        return True

    async def fake_retry():
        retried.append(True)

    checkin.on_captcha_error = fake_on_captcha_error
    checkin.retry = fake_retry

    asyncio.run(checkin.on_text(SimpleNamespace(), "验证码错误!"))

    assert handled == ["验证码错误!"]
    assert retried == []


def test_on_text_captcha_error_hook_can_fall_through_to_retry():
    checkin = make_checkin()
    checkin.bot_captcha_error_keywords = ["验证码错误"]
    retried = []

    async def fake_on_captcha_error(message, text):
        return False

    async def fake_retry():
        retried.append(True)

    checkin.on_captcha_error = fake_on_captcha_error
    checkin.retry = fake_retry

    asyncio.run(checkin.on_text(SimpleNamespace(), "验证码错误!"))

    assert retried == [True]


# ------------------------------------------------ 图片验证题的题面不能被当成签到结果

# 终点站(@EmbyPublicBot) 的真实题面: 同时含 "完成" 和 "失败", 两个方向都会误判.
CAPTCHA_CAPTION = "请在 30 秒内点击图中事物的按钮以完成签到\n\n" "每天只有一次机会，失败或者过期当天不可重试"


def photo_message(caption):
    return SimpleNamespace(photo=object(), caption=caption, text=None)


def test_captcha_instruction_caption_is_classified_as_captcha():
    """带 "点击图中…以完成签到" 的图片必须走验证码流程, 否则视觉一次都不会调用."""
    from embykeeper.telegram.checkiner._base import MessageType

    checkin = make_checkin()
    assert checkin.message_type(photo_message(CAPTCHA_CAPTION)) == MessageType.CAPTCHA


def test_result_card_caption_is_still_caption():
    """真正的结果卡片不能被当成验证码去做视觉/OCR."""
    from embykeeper.telegram.checkiner._base import MessageType

    checkin = make_checkin()
    for caption in ("🎉 签到成功，获得了 3 积分\n总分: 603", "✅ 验证通过"):
        assert checkin.message_type(photo_message(caption)) == MessageType.CAPTION, caption


def test_captcha_instruction_text_does_not_trigger_retry():
    """题面单独发成文字时, 也不能因为含 "失败" 就立刻 retry."""
    checkin = make_checkin()
    retried = []
    finished = []

    async def fake_retry():
        retried.append(True)

    async def fake_finish(status, message=None, fail=False):
        finished.append(status)

    checkin.retry = fake_retry
    checkin.finish = fake_finish

    asyncio.run(checkin.on_text(SimpleNamespace(), CAPTCHA_CAPTION))

    assert retried == []
    assert finished == []


def test_captcha_instruction_pattern_ignores_normal_messages():
    checkin = make_checkin()
    for text in ("🎉 签到成功 | 1 牛币", "签到中...", "你今天已签到或签到机会已用完", ""):
        assert checkin.is_captcha_instruction(text) is False, text


def test_poem_prompt_is_not_mistaken_for_captcha_instruction():
    """诗句题面里的 "请依次点击下方按钮补全诗句" 也含 "点击下方".

    一旦把它当成图片验证题面, 图片会被判成 CAPTCHA 去做 OCR, 而这条消息根本没有图,
    诗句补全就会永远等不到图片. 两个模式必须能分开.
    """
    from embykeeper.telegram.checkiner._base import MessageType

    poem_prompt = (
        "🌸 防機器签到验证 🌸\n\n"
        "请依次点击下方按钮补全诗句：\n\n"
        "忆对中秋丹桂＿。 花＿杯中。 月在杯中。\n\n"
        "出自：辛弃疾《一剪梅·中秋元月》"
    )
    checkin = make_checkin()
    assert checkin.is_captcha_instruction(poem_prompt) is False
    assert checkin.is_challenge_text(poem_prompt) is True
    assert checkin.message_type(photo_message(poem_prompt)) == MessageType.CAPTION


def test_terminus_visual_answer_tolerates_wrapping():
    """模型回 "答案是鸟" 时也要能点到 "鸟", 不能抛 ValueError 把签到炸掉."""
    from embykeeper.telegram.checkiner import terminus

    checkin = object.__new__(terminus.TerminusCheckin)
    from loguru import logger

    checkin.log = logger.bind(name="test")
    checkin.client = SimpleNamespace(me=SimpleNamespace(full_name="tester"))
    clicked = []
    failed = []

    class FakeMarkup:
        inline_keyboard = [
            [
                SimpleNamespace(text="鸟", callback_data="a"),
                SimpleNamespace(text="电话机", callback_data="b"),
            ]
        ]

    class FakeMessage:
        reply_markup = FakeMarkup()
        photo = SimpleNamespace(file_id="fid")

        async def click(self, text):
            clicked.append(text)

    class FakeLink:
        def __init__(self, client):
            pass

        async def visual(self, photo, options, question=None):
            return "答案是鸟", "fake"

    async def fake_fail(*a, **kw):
        failed.append(True)

    checkin.fail = fake_fail
    with patch.object(terminus, "Link", FakeLink):
        with patch.object(terminus, "random") as fake_random:
            fake_random.uniform.return_value = 0
            asyncio.run(checkin.on_photo(FakeMessage()))

    assert clicked == ["鸟"]
    assert failed == []
