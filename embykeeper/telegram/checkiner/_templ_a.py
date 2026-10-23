import asyncio
import random
import re
from typing import List, Optional, Tuple, Union

from loguru import logger
from pydantic import BaseModel, ValidationError
from pyrogram.types import Message
from pyrogram.errors import MessageIdInvalid
from pyrogram.raw.types.messages import BotCallbackAnswer

from embykeeper.utils import to_iterable

from . import BotCheckin
from ._base import normalize_chat_id

__ignore__ = True


class TemplateACheckinConfig(BaseModel):
    # fmt: off
    name: Optional[str] = None  # 签到器的名称
    chat_name: Optional[Union[str, int]] = None  # 在指定群组中向机器人签到, 置空则在私聊中签到
    use_button_answer: bool = None  # 点击按钮后等待并识别响应
    bot_text_ignore_answer: Union[str, List[str]] = None  # 忽略的响应文本
    bot_fail_keywords: Union[str, List[str]] = None  # 签到错误将重试时检测的关键词 (暂不支持regex), 置空使用内置关键词表
    bot_success_keywords: Union[str, List[str]] = None  # 成功时检测的关键词 (暂不支持regex), 置空使用内置关键词表
    bot_challenge_keywords: Union[str, List[str]] = None  # 验证问题关键词, 命中后交给智能回答, 置空使用内置关键词表
    bot_success_pat: Optional[str] = None  # 当接收到成功消息后, 从消息中提取数字的模式
    bot_captcha_len: Optional[int] = None  # 验证码长度的可能范围
    bot_text_ignore: Union[str, List[str]] = None  # 当含有列表中的关键词, 即忽略该消息, 置空不限制
    bot_checkin_caption_pat: Optional[str] = None  # 当 Bot 返回图片时, 仅当符合该 regex 才识别为验证码, 置空不限制
    bot_checkin_cmd: Optional[str] = None  # Bot 依次执行的签到命令
    bot_use_captcha: Optional[bool] = None  # 当 Bot 返回图片时, 识别验证码并调用 on_captcha
    bot_checkin_button: Union[str, List[str]] = None  # 签到按钮文本
    bot_checkin_button_pat: Optional[str] = None  # 签到按钮文本的正则, 文案不固定时使用
    bot_checkin_button_index: Optional[Union[int, List[int]]] = None  # 签到按钮位置, 1 起
    bot_checkin_path: Union[str, List[str]] = None  # 依次点击的按钮文本, 用于需先进子菜单的机器人
    templ_panel_keywords: Union[str, List[str]] = None  # 面板关键词
    # fmt: on


class TemplateACheckin(BotCheckin):
    bot_text_ignore_answer = ["Done"]
    use_button_answer = True
    bot_checkin_cmd = "/start"
    bot_checkin_button = ["签到", "簽到"]
    bot_checkin_button_pat = None
    bot_checkin_button_index = None
    bot_checkin_path = None
    templ_panel_keywords = None

    def __init__(self, *args, **kw):
        super().__init__(*args, **kw)
        # init() 在 start() 解析目标会话之后才执行, 而 chat_name 必须在解析前就绪,
        # 否则会先去私聊机器人. 因此这里提前把群组配置读出来.
        site_config = self.config if isinstance(self.config, dict) else {}
        if site_config.get("chat_name"):
            self.chat_name = normalize_chat_id(site_config["chat_name"])
            if self.chat_name == self.bot_username:
                # 这样过滤器会变成 "群组发的消息 + 发信人是群组本身", 永远匹配不到,
                # 表现是静默等到超时. 该会话本身就是群组时应改用模板 B 的 is_chat.
                self.log.warning(
                    "chat_name 与站点名里的会话相同, 机器人过滤会永远匹配不到消息. "
                    "若该会话本身就是群组, 请改用 templ_b 并设置 is_chat = true."
                )

    async def init(self):
        try:
            self.t_config = TemplateACheckinConfig.model_validate(self.config)
        except ValidationError as e:
            self.log.warning(f"初始化失败: 签到自定义模板 A 的配置错误:\n{e}")
            return False

        self.name = self.t_config.name or self.name
        self.use_button_answer = (
            self.t_config.use_button_answer
            if self.t_config.use_button_answer is not None
            else self.use_button_answer
        )
        self.bot_text_ignore_answer = self.t_config.bot_text_ignore_answer or self.bot_text_ignore_answer
        self.bot_fail_keywords = self.t_config.bot_fail_keywords or self.bot_fail_keywords
        self.bot_success_keywords = self.t_config.bot_success_keywords or self.bot_success_keywords
        self.bot_challenge_keywords = self.t_config.bot_challenge_keywords or self.bot_challenge_keywords
        self.bot_success_pat = self.t_config.bot_success_pat or self.bot_success_pat
        self.bot_captcha_len = self.t_config.bot_captcha_len or self.bot_captcha_len
        self.bot_text_ignore = self.t_config.bot_text_ignore or self.bot_text_ignore
        self.bot_checkin_caption_pat = self.t_config.bot_checkin_caption_pat or self.bot_checkin_caption_pat
        self.bot_checkin_cmd = self.t_config.bot_checkin_cmd or self.bot_checkin_cmd
        self.bot_checkin_button = self.t_config.bot_checkin_button or self.bot_checkin_button
        self.bot_checkin_button_pat = self.t_config.bot_checkin_button_pat or self.bot_checkin_button_pat
        if self.t_config.bot_checkin_button_index is not None:
            self.bot_checkin_button_index = self.t_config.bot_checkin_button_index
        if self.bot_checkin_button_pat:
            try:
                re.compile(self.bot_checkin_button_pat)
            except re.error as e:
                self.log.warning(f"初始化失败: bot_checkin_button_pat 不是合法正则 ({e}).")
                return False
        self.bot_checkin_path = self.t_config.bot_checkin_path or self.bot_checkin_path
        self.templ_panel_keywords = self.t_config.templ_panel_keywords or self.templ_panel_keywords
        self.bot_use_captcha = (
            self.t_config.bot_use_captcha
            if self.t_config.bot_use_captcha is not None
            else self.bot_use_captcha
        )

        self.log = logger.bind(scheme="telechecker", name=self.name, username=self.client.me.full_name)
        self._path_index = 0
        return True

    async def send_checkin(self, **kw):
        # 每次重新发起签到都从头走按钮路径, 避免重试时路径已走完而卡住.
        self._path_index = 0
        return await super().send_checkin(**kw)

    async def _advance_button_path(self, message: Message) -> bool:
        """按 bot_checkin_path 依次点击按钮.

        部分机器人需要先点一级菜单 (如 "用户功能") 才能看到签到按钮 (如 "数据存档"),
        单次点击匹配不到就只会报 "账户错误". 配置路径后逐级点击, 最后一级视为签到按钮.

        同时支持两种按钮: 消息内联按钮 (inline_keyboard) 和底部回复键盘 (keyboard).
        后者没有回调, 直接把按钮文字当消息发出去.
        """
        path = to_iterable(self.bot_checkin_path) if self.bot_checkin_path else []
        markup = message.reply_markup
        if not path or not markup:
            return False
        index = self._path_index
        if index >= len(path):
            return False
        target = path[index]

        inline = getattr(markup, "inline_keyboard", None)
        if inline:
            keys = [k.text for r in inline for k in r]
            matched = next((k for k in keys if target in k), None)
            if matched is None:
                return False
            self._path_index = index + 1
            self.log.debug(f"点击菜单按钮 [{matched}] ({index + 1}/{len(path)}).")
            await asyncio.sleep(random.uniform(0.5, 1.5))
            try:
                answer: BotCallbackAnswer = await message.click(matched)
            except TimeoutError:
                self.log.debug("点击菜单按钮无响应, 可能按钮未正确处理点击回复. 一般来说不影响签到.")
                return True
            except MessageIdInvalid:
                return True
            if self._path_index >= len(path):
                await self.on_button_answer(answer, from_path=True)
            return True

        keyboard = getattr(markup, "keyboard", None)
        if keyboard:
            keys = [k.text for r in keyboard for k in r]
            matched = next((k for k in keys if target in k), None)
            if matched is None:
                return False
            self._path_index = index + 1
            self.log.debug(f"发送键盘按钮 [{matched}] ({index + 1}/{len(path)}).")
            await asyncio.sleep(random.uniform(0.5, 1.5))
            await message.reply(matched)
            return True

        return False

    def _resolve_button_index(self, inline, index) -> Optional[Tuple[int, int]]:
        """把 bot_checkin_button_index 解析成 (行, 列) 索引, 均从 0 起.

        支持两种写法:
          - 单个数字: 扁平序号, 从左到右、从上到下, 1 起 (如 8)
          - 两个数字: 行列坐标, 1 起 (如 [4, 2])
        """
        values = list(to_iterable(index))
        if len(values) == 1:
            flat = int(values[0]) - 1
            if flat < 0:
                return None
            counter = 0
            for row_index, row in enumerate(inline):
                for col_index in range(len(row)):
                    if counter == flat:
                        return (row_index, col_index)
                    counter += 1
            return None
        if len(values) != 2:
            return None
        row_index, col_index = int(values[0]) - 1, int(values[1]) - 1
        if row_index < 0 or col_index < 0:
            return None
        if row_index >= len(inline) or col_index >= len(inline[row_index]):
            return None
        return (row_index, col_index)

    def _find_checkin_button(self, inline) -> Optional[Tuple[int, int]]:
        """定位签到按钮, 返回 (行, 列) 索引, 找不到返回 None.

        优先级: bot_checkin_button_index (位置) > bot_checkin_button_pat (正则)
        > bot_checkin_button (文本包含).

        有些机器人的签到按钮文案每天换 (如 "今朝已至"), 靠文本匹配必然失效,
        只能用位置或正则定位.
        """
        if self.bot_checkin_button_index is not None:
            return self._resolve_button_index(inline, self.bot_checkin_button_index)
        pattern = re.compile(self.bot_checkin_button_pat) if self.bot_checkin_button_pat else None
        keywords = to_iterable(self.bot_checkin_button) if self.bot_checkin_button else ()
        for row_index, row in enumerate(inline):
            for col_index, key in enumerate(row):
                text = key.text or ""
                if pattern and pattern.search(text):
                    return (row_index, col_index)
                if any(keyword in text for keyword in keywords):
                    return (row_index, col_index)
        return None

    def _log_panel_buttons(self, inline):
        """匹配失败时打印面板按钮, 便于判断是文案变了还是位置变了."""
        flat = [key.text for row in inline for key in row]
        if not flat:
            self.log.warning("签到失败: 账户错误. 该消息没有任何内联按钮.")
            return
        self.log.warning(
            "签到失败: 账户错误. 未找到签到按钮, 当前面板按钮 (序号可用于 bot_checkin_button_index):"
        )
        self.log.warning("  " + " | ".join(f"{i}.{text}" for i, text in enumerate(flat, start=1)))
        self.log.debug(f"按钮回调数据: {[(key.text, key.callback_data) for row in inline for key in row]}")

    async def message_handler(self, client, message: Message):
        text = message.caption or message.text
        if await self._advance_button_path(message):
            return
        if (
            text
            and message.reply_markup
            and not self.is_challenge_text(text)
            and (
                (
                    self.templ_panel_keywords
                    and any(keyword in text for keyword in to_iterable(self.templ_panel_keywords))
                )
                or (getattr(message, "is_first_response", False) and not message.edit_date)
            )
        ):
            inline = getattr(message.reply_markup, "inline_keyboard", None) or []
            position = self._find_checkin_button(inline)
            if position is None:
                self._log_panel_buttons(inline)
                return await self.fail()
            row_index, col_index = position
            self.log.debug(f"点击签到按钮 [{inline[row_index][col_index].text}].")
            await asyncio.sleep(random.uniform(0.5, 1.5))
            try:
                # 注意参数顺序: pyrogram 的 click(x, y) 里 x 是列、y 是行
                # (内部取 keyboard[y][x]), 与 (行, 列) 的直觉相反. 传反了会报
                # "The button at position (行, 列) doesn't exist".
                answer: BotCallbackAnswer = await message.click(col_index, row_index)
            except ValueError:
                # 消息在这期间被编辑 / 机器人改了布局, 位置失效.
                self.log.warning("签到失败: 签到按钮位置已失效, 面板可能被机器人改过.")
                self._log_panel_buttons(getattr(message.reply_markup, "inline_keyboard", None) or [])
                return await self.fail()
            except TimeoutError:
                self.log.debug(f"点击签到按钮无响应, 可能按钮未正确处理点击回复. 一般来说不影响签到.")
            except MessageIdInvalid:
                pass
            else:
                await self.on_button_answer(answer)
            return

        if message.text and "请先点击下面加入我们的" in message.text:
            self.log.warning(f"签到失败: 账户错误.")
            return await self.fail()

        await super().message_handler(client, message)

    def _is_meaningful_answer(self, text: str) -> bool:
        """回调提示是否值得处理.

        进菜单这类导航按钮的提示往往是一句 "✅ 用户界面", 既不是结果也不是题目.
        若原样交给 on_text, 会落到"无法识别 → 智能回答"的兜底分支, 而该分支一旦
        让模型点了按钮就会以"避免风险"为由中止整个签到, 因此这里直接过滤.
        """
        return self.is_challenge_text(text) or self.is_result_text(text)

    async def on_button_answer(self, answer: BotCallbackAnswer, from_path: bool = False):
        if self.use_button_answer:
            if not isinstance(answer, BotCallbackAnswer):
                self.log.warning(f"签到失败: 签到按钮指向 URL, 不受支持.")
                return await self.fail()
            if answer.message and not any(ignore in answer.message for ignore in self.bot_text_ignore_answer):
                if from_path and not self._is_meaningful_answer(answer.message):
                    self.log.debug(f"忽略导航按钮的提示: {answer.message}")
                    return
                await self.on_text(Message(id=0, text=answer.message), answer.message)


def use(**kw):
    return type("TemplatedClass", (TemplateACheckin,), kw)
