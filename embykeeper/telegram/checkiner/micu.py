"""MICU 股东会 (micu_user_bot) 签到.

该机器人点签到后会出算术题, 必须点中正确选项才算签上, 例如:

    【🎯 每日签到
    请完成下面这道题后签到：
    8 - 5 = ?
    答错会重新生成一题。】
    [7] [4] [13] [22]
    [主界面]

题面含 "完成" (属于成功关键词), 走通用逻辑会被误判成 "签到成功". 这里单独实现:
解析算式 -> 在按钮里找等值选项 -> 点击 -> 等真实成功消息.

另外它可能把结果放在按钮回调的 toast 里, 所以关闭 use_button_answer,
只认机器人真正发出来的消息, 避免 toast 抢先收工.
"""

import asyncio
import random
import re
import unicodedata
from typing import Optional

from pyrogram.errors import MessageIdInvalid
from pyrogram.types import Message

from ._templ_a import TemplateACheckin

QUIZ_PATTERN = re.compile(r"(-?\d+)\s*([+\-*/×÷])\s*(-?\d+)\s*[=＝]")


def solve_quiz(text: str) -> Optional[str]:
    """从题面解析算式并返回答案字符串; 无法解析或除不尽时返回 None."""
    if not text:
        return None
    match = QUIZ_PATTERN.search(unicodedata.normalize("NFKC", text))
    if not match:
        return None
    left, operator, right = int(match.group(1)), match.group(2), int(match.group(3))
    if operator == "+":
        value = left + right
    elif operator == "-":
        value = left - right
    elif operator in ("*", "×"):
        value = left * right
    else:
        if right == 0:
            return None
        value = left / right
        if value != int(value):
            return None
        value = int(value)
    return str(value)


def pick_answer_button(buttons: list, answer: str) -> Optional[str]:
    """在按钮文本里找出等于答案的那一个."""
    for text in buttons:
        if text.strip() == answer:
            return text
    for text in buttons:
        try:
            if float(text.strip()) == float(answer):
                return text
        except ValueError:
            continue
    return None


class MICUCheckin(TemplateACheckin):
    name = "MICU 股东会"
    bot_username = "micu_user_bot"
    # 题目以图片+文字形式发来, 当成验证码去做 OCR 只会失败重试
    bot_use_captcha = False
    # 结果只认机器人真正发出的消息, 不解析按钮回调 toast
    use_button_answer = False

    def __init__(self, *args, **kw):
        super().__init__(*args, **kw)
        self._answered_quizzes = set()

    async def message_handler(self, client, message: Message):
        if await self._answer_quiz(message):
            return
        await super().message_handler(client, message)

    async def _answer_quiz(self, message: Message) -> bool:
        """识别算术题并点中正确答案, 处理了返回 True."""
        markup = getattr(message, "reply_markup", None)
        if not markup or not getattr(markup, "inline_keyboard", None):
            return False
        text = message.text or message.caption or ""
        answer = solve_quiz(text)
        if not answer:
            return False

        key = (message.id, answer)
        if key in self._answered_quizzes:
            return False

        buttons = [button.text for row in markup.inline_keyboard for button in row]
        target = pick_answer_button(buttons, answer)
        if target is None:
            self.log.warning(f"算术题答案为 {answer}, 但按钮里没有对应选项: {buttons}")
            return False

        self._answered_quizzes.add(key)
        self.log.info(f"识别到算术题, 答案 {answer}, 点击 [{target}].")
        await asyncio.sleep(random.uniform(0.5, 1.5))
        try:
            await message.click(target)
        except (TimeoutError, MessageIdInvalid):
            pass
        return True
