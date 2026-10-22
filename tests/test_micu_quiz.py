"""MICU 算术题签到的回归测试.

题面 "请完成下面这道题后签到：8 - 5 = ?" 含成功关键词 "完成", 通用逻辑会误判成功.
MICU 单独实现: 解析算式 -> 点中等值按钮.
"""

import asyncio

import pytest
from loguru import logger

from embykeeper.telegram.checkiner import micu
from embykeeper.telegram.checkiner.micu import MICUCheckin, pick_answer_button, solve_quiz


class FakeButton:
    def __init__(self, text):
        self.text = text


class FakeMarkup:
    def __init__(self, rows):
        self.inline_keyboard = [[FakeButton(text) for text in row] for row in rows]


class FakeMessage:
    def __init__(self, text, rows, msg_id=1):
        self.id = msg_id
        self.text = text
        self.caption = None
        self.reply_markup = FakeMarkup(rows) if rows is not None else None
        self.clicked = []

    async def click(self, button_text):
        self.clicked.append(button_text)
        return True


class FakeAsyncIO:
    @staticmethod
    async def sleep(_):
        return None


@pytest.fixture(autouse=True)
def fast_sleep(monkeypatch):
    monkeypatch.setattr(micu, "asyncio", FakeAsyncIO)


def make_checkin():
    inst = object.__new__(MICUCheckin)
    inst._answered_quizzes = set()
    inst.log = logger.bind(name="test")
    return inst


def test_solve_quiz_basic_operations():
    assert solve_quiz("8 - 5 = ?") == "3"
    assert solve_quiz("10 - 3 = ?") == "7"
    assert solve_quiz("17 - 15 = ?") == "2"
    assert solve_quiz("7 + 8 = ?") == "15"
    assert solve_quiz("7 * 8 = ?") == "56"
    assert solve_quiz("7 × 8 = ?") == "56"
    assert solve_quiz("12 ÷ 3 = ?") == "4"


def test_solve_quiz_handles_fullwidth_and_prompt_text():
    prompt = "【🎯 每日签到\n请完成下面这道题后签到：\n８－５＝？\n答错会重新生成一题。】"
    assert solve_quiz(prompt) == "3"


def test_solve_quiz_returns_none_for_non_quiz():
    assert solve_quiz("签到成功: 获得奖励 5, 当前余额 347.") is None
    assert solve_quiz("7 / 2 = ?") is None
    assert solve_quiz("") is None


def test_pick_answer_button():
    assert pick_answer_button(["6", "3", "4", "11"], "3") == "3"
    assert pick_answer_button(["7", "4", "13", "22"], "3") is None
    # 带空格的按钮文本也要能匹配
    assert pick_answer_button([" 3 ", "4"], "3") == " 3 "


def test_answer_quiz_clicks_correct_button():
    inst = make_checkin()
    message = FakeMessage(
        "【🎯 每日签到\n请完成下面这道题后签到：\n8 - 5 = ?\n答错会重新生成一题。】",
        [["6", "3", "4", "11"], ["主界面"]],
    )

    assert asyncio.run(inst._answer_quiz(message)) is True
    assert message.clicked == ["3"]


def test_answer_quiz_skips_when_no_matching_option():
    inst = make_checkin()
    message = FakeMessage("8 - 5 = ?", [["7", "4", "13", "22"]])
    assert asyncio.run(inst._answer_quiz(message)) is False
    assert message.clicked == []


def test_answer_quiz_is_idempotent():
    inst = make_checkin()
    message = FakeMessage("8 - 5 = ?", [["3", "4"]], msg_id=42)
    assert asyncio.run(inst._answer_quiz(message)) is True
    assert asyncio.run(inst._answer_quiz(message)) is False
    assert message.clicked == ["3"]


def test_answer_quiz_ignores_messages_without_buttons():
    inst = make_checkin()
    message = FakeMessage("8 - 5 = ?", None)
    assert asyncio.run(inst._answer_quiz(message)) is False


def test_micu_disables_captcha_ocr_and_button_answer():
    # 题目图片不能被当成验证码去 OCR, 回调 toast 也不能直接当结果
    assert MICUCheckin.bot_use_captcha is False
    assert MICUCheckin.use_button_answer is False
