import asyncio
from datetime import datetime, timedelta
import random
import re
from abc import ABC, abstractmethod
from contextlib import asynccontextmanager
from enum import Flag, auto
import time
import unicodedata
from typing import Iterable, List, Optional, Union

from loguru import logger
from pyrogram import filters
from pyrogram.errors import (
    UsernameNotOccupied,
    FloodWait,
    UsernameInvalid,
    ChannelInvalid,
    ChannelPrivate,
    MessageIdInvalid,
    DataInvalid,
)
from pyrogram.handlers import EditedMessageHandler, MessageHandler
from pyrogram.types import InlineKeyboardMarkup, Message, ReplyKeyboardMarkup
from pyrogram.raw.functions.account import GetNotifySettings
from pyrogram.raw.types import PeerNotifySettings, InputNotifyPeer
from thefuzz import fuzz, process

from embykeeper import __name__ as __product__
from embykeeper.ocr import CharRange, OCRService
from embykeeper.runinfo import RunContext
from embykeeper.utils import (
    AsyncCountPool,
    format_timedelta_human,
    show_exception,
    to_iterable,
    truncate_str,
)
from embykeeper.config import config
from embykeeper.runinfo import RunStatus
from embykeeper.telegram.pyrogram import Client
from embykeeper.telegram.link import Link

__ignore__ = True

logger = logger.bind(scheme="telechecker")

REGEX_KEYWORD_PREFIX = "re:"


def keyword_match(text: str, keywords: Iterable[str]) -> bool:
    """关键词匹配, 以 "re:" 开头的条目按正则处理.

    有些机器人每次回复都是随机文案 (如签到回执 "稳如老狗, 奖励稳稳的!" 每天换一个
    成语), 固定关键词追不上. 写成 "re:奖励|红包|到账" 就能一次覆盖.
    """
    if not text:
        return False
    for keyword in keywords:
        if keyword.startswith(REGEX_KEYWORD_PREFIX):
            pattern = keyword[len(REGEX_KEYWORD_PREFIX) :]
            try:
                if re.search(pattern, text):
                    return True
            except re.error:
                # 写错的正则不该让整次签到崩掉, 当作没匹配.
                continue
        elif keyword in text:
            return True
    return False


default_keywords = {
    "account_fail": (
        "拉黑",
        "黑名单",
        "冻结",
        "未找到用户",
        "无资格",
        "退出群",
        "退群",
        "加群",
        "加入群聊",
        "请先关注",
        "请先加入",
        "請先加入",
        "未注册",
        "先注册",
        "不存在",
        "不在群组中",
        "你有号吗",
    ),
    "too_many_tries_fail": ("已尝试", "过多"),
    "checked": ("只能", "已经", "过了", "签过", "明日再来", "重复签到"),
    "fail": ("失败", "错误", "超时"),
    "success": ("成功", "通过", "完成", "获得"),
    # 验证问题: 这类消息常带 "签到成功" 字样 (如 "签到成功, 请回答: 5+5=?"),
    # 直接按成功关键词判定会漏答, 因此优先交给智能回答处理.
    "challenge": (
        "请回答",
        "请计算",
        "请输入答案",
        "请选择正确答案",
        "回答下列",
        "请作答",
        "请完成下面",
        "这道题",
        "答错",
        "重新生成",
        "补全诗句",
        "依次点击",
    ),
}


def normalize_chat_id(value):
    """把纯数字的会话 ID 字符串转成 int.

    私有群 / 频道只能用数字 ID 表示, 而 pyrogram 的 get_chat 只把 str 当用户名解析,
    因此配置里写的 "-1001234567890" 必须先转成 int, 否则会报 "UsernameNotOccupied".
    """
    if isinstance(value, str) and re.fullmatch(r"-?\d+", value.strip()):
        return int(value.strip())
    return value


# 算术题形式的验证: "17 - 15 = ?" / "5+5=?".
# 这类题面往往同时含 "完成" 等成功关键词 (如 "请完成下面这道题后签到"),
# 只靠关键词会漏判, 因此额外用算式模式兜底.
CHALLENGE_PATTERN = re.compile(r"\d+\s*[+\-*/×÷]\s*\d+\s*=")

# 指令式的验证提示: "请完成诗句填空进行验证" / "请进行人机验证".
# 这类提示只含 "完成" / "进行" 这类动作词, 却会撞上成功关键词表里的 "完成",
# 一旦被判成功, 正在进行的智能回答就会被直接取消, 题目等于没做.
CHALLENGE_INSTRUCTION_PATTERN = re.compile(
    r"(请|需|必须).{0,8}(完成|进行).{0,10}(验证|校验|填空|答题|作答)"
    r"|(填空|补全).{0,4}(诗句|下列|题目|原文)"
)

# 补全诗句类题目: 诗句被挖空, 需要按顺序从按钮里选字填进去.
POEM_FILL_PATTERN = re.compile(r"(补全|填空|填写).{0,4}诗|诗.{0,4}(填空|补全)")

# 智能回答给出的点击指令, 一条回答里可以出现多个 (如 "依次点击补全诗句" 要点两下).
CLICK_PATTERN = re.compile(r"\[CLICK\]\^(.+?)\^")


class MessageType(Flag):
    IGNORE = auto()
    TEXT = auto()
    CAPTION = auto()
    CAPTCHA = auto()
    ANSWER = auto()


class BaseBotCheckin(ABC):
    """基础签到类."""

    name: str = None

    def __init__(
        self,
        client: Client,
        context: RunContext = None,
        retries=None,
        timeout=None,
        config: dict = {},
    ):
        """
        基础签到类.
        参数:
            client: Pyrogram 客户端
            context: 运行时上下文
            retries: 最大重试次数
            timeout: 签到超时时间
            config: 当前签到器的特定配置
        """
        self.client = client
        self.ctx = context or RunContext.prepare()

        self._retries = retries
        self._timeout = timeout

        self.config = config
        self.finished = asyncio.Event()  # 签到完成事件
        self.log = self.ctx.bind_logger(logger.bind(name=self.name, username=client.me.full_name))  # 日志组件

        self._task = None  # 主任务

    @property
    def retries(self):
        return self._retries or config.checkiner.retries

    @property
    def timeout(self):
        return self._timeout or config.checkiner.timeout

    async def _start(self):
        """签到器的入口函数的错误处理外壳."""
        try:
            self.client.stop_handlers.append(self.stop)
            self._task = asyncio.create_task(self.start())
            return await self._task
        except Exception as e:
            if config.nofail:
                self.log.warning(f"初始化异常错误, 签到器将停止.")
                show_exception(e, regular=False)
                return self.ctx.finish(RunStatus.ERROR, "异常错误")
            else:
                raise
        finally:
            self.client.stop_handlers.remove(self.stop)
            self._task = None

    @abstractmethod
    async def start(self) -> RunContext:
        pass

    async def stop(self):
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass


class BotCheckin(BaseBotCheckin):
    """签到类, 用于回复模式签到."""

    group_pool = AsyncCountPool(base=2000)
    interval_pool = {}

    # fmt: off
    name: str = None  # 签到器的名称
    bot_username: Union[int, str] = None  # Bot 的 UserID 或 用户名 (不带 @ 或 https://t.me/)
    bot_checkin_cmd: Union[str, List[str]] = ["/checkin"]  # Bot 依次执行的签到命令
    bot_send_interval: int = 3  # 签到命令间等待的秒数
    bot_use_captcha = True # 当 Bot 返回图片时, 识别验证码并调用 on_captcha
    bot_checkin_caption_pat: str = None  # 当 Bot 返回图片时, 仅当符合该 regex 才识别为验证码, 置空不限制
    bot_text_ignore: Union[str, List[str]] = []  # 当含有列表中的关键词, 即忽略该消息, 置空不限制
    ocr: Optional[str] = None # OCR 模型, None = 默认模型, str = 自定义模型
    bot_captcha_char_range: Optional[Union[CharRange, str]] = None # OCR 字符范围 (正则字符类, 如 "0-9" / "0-9A-Za-z"), None = 不限制
    bot_captcha_len: Union[int, Iterable[int]] = []  # 验证码长度的可能范围, 例如 [1, 2, 3], 置空不限制
    bot_success_pat: str = r"(\d+)[^\d]*(\d+)"  # 当接收到成功消息后, 从消息中提取数字的模式
    bot_retry_wait: int = 2  # 失败时等待的秒数
    bot_use_history: int = None  # 首先尝试识别历史记录中最后一个验证码图片, 最多识别 N 条, 置空禁用
    bot_allow_from_scratch: bool = False  # 允许从未聊天情况下启动
    bot_success_keywords: Union[str, List[str]] = []  # 成功时检测的关键词 (暂不支持regex), 置空使用内置关键词表
    bot_challenge_keywords: Union[str, List[str]] = []  # 验证问题关键词, 命中后交给智能回答, 置空使用内置关键词表
    _challenge_pending = False  # 验证题正在解答中, 期间忽略含成功关键词的提示消息
    _pending_success_text = None  # 解答验证题期间收到的成功消息, 作答结束后补处理
    bot_checked_keywords: Union[str, List[str]] = []  # 今日已签到时检测的关键词, 置空使用内置关键词表
    bot_account_fail_keywords: Union[str, List[str]] = []  # 账户错误将退出时检测的关键词 (暂不支持regex), 置空使用内置关键词表
    bot_too_many_tries_fail_keywords: Union[str, List[str]] = []  # 过多尝试将退出时检测的关键词 (暂不支持regex), 置空使用内置关键词表
    bot_fail_keywords: Union[str, List[str]] = []  # 签到错误将重试时检测的关键词 (暂不支持regex), 置空使用内置关键词表
    bot_captcha_error_keywords: Union[str, List[str]] = []  # 验证码错误时检测的关键词, 命中后交由 on_captcha_error 处理, 置空不启用
    chat_name: str = None  # 在群聊中向机器人签到
    additional_auth: List[str] = []  # 额外认证要求
    max_retries = None  # 验证码错误或网络错误时最高重试次数 (默认无限)
    checked_retries = None # 今日已签到时最高重试次数 (默认不重试)
    init_first: bool = False  # 先执行自定义初始化函数, 再进行加入群组分析
    # fmt: on

    @property
    def valid_retries(self):
        if self.max_retries:
            return min(self.retries, self.max_retries)
        else:
            return self.retries

    def __init__(self, *args, instant=False, **kw):
        super().__init__(*args, **kw)
        self.current_retries = 0  # 当前重试次数
        self._waiting = {}  # 当前等待的消息
        self._first_waiting = False  # 是否在等待首个消息
        self._handler_tasks = set()  # 存储所有正在运行的 message_handler 任务
        if instant:
            self.checked_retries = None

    def get_filter(self):
        """设定要签到的目标."""
        filter = filters.all
        if self.bot_username:
            filter = filter & filters.user(self.bot_username)
        if self.chat_name:
            filter = filter & filters.chat(self.chat_name)
        else:
            filter = filter & filters.private
        return filter

    def get_handlers(self):
        """设定要监控的更新的类型."""
        return [
            MessageHandler(self._message_handler, self.get_filter()),
            EditedMessageHandler(self._message_handler, self.get_filter()),
        ]

    @asynccontextmanager
    async def listener(self):
        """执行监控上下文."""
        group = await self.group_pool.append(self)
        handlers = self.get_handlers()
        for h in handlers:
            await self.client.add_handler(h, group=group)
        try:
            yield
        finally:
            # 取消所有正在运行的message_handler任务
            for task in self._handler_tasks:
                task.cancel()
            # 等待所有任务完成
            if self._handler_tasks:
                await asyncio.gather(*self._handler_tasks, return_exceptions=True)
            # 移除handlers
            for h in handlers:
                try:
                    await self.client.remove_handler(h, group=group)
                except ValueError:
                    pass

    async def start(self):
        """签到器的入口函数."""

        self.ctx.start(RunStatus.INITIALIZING)

        if self.init_first:
            if not await self.init():
                self.log.warning(f"初始化错误.")
                return self.ctx.finish(RunStatus.FAIL, "初始化错误")

        if (not self.chat_name) and (not self.bot_username):
            raise ValueError("未指定 chat_name 或 bot_username")
        ident = self.chat_name or self.bot_username

        while True:
            try:
                chat = await self.client.get_chat(ident)
            except (UsernameNotOccupied, UsernameInvalid, ChannelInvalid, ChannelPrivate) as e:
                self.log.warning(f'初始化错误: 会话 "{ident}" 已不存在.')
                return self.ctx.finish(RunStatus.IGNORE, "会话已不存在")
            except KeyError as e:
                self.log.info(f"初始化错误: 无法访问, 您可能已被封禁: {e}.")
                show_exception(e)
                return self.ctx.finish(RunStatus.FAIL, "无法访问会话")
            except FloodWait as e:
                if e.value < 360:
                    self.log.info(f"初始化信息: Telegram 要求等待 {e.value} 秒.")
                    await asyncio.sleep(e.value)
                else:
                    self.log.info(
                        f"初始化信息: Telegram 要求等待 {e.value} 秒, 您可能操作过于频繁, 签到器将停止."
                    )
                    return self.ctx.finish(RunStatus.FAIL, "操作过于频繁")
            else:
                break

        if self.chat_name:
            self.chat_name = chat.username or chat.id

        is_archived = chat.folder_id == 1

        if await self.client.get_chat_history_count(chat.id) == 0:
            if not self.bot_allow_from_scratch:
                self.log.debug(f'跳过签到: 从未与 "{ident}" 交流.')
                return self.ctx.finish(RunStatus.IGNORE, "从未与该会话交流")

        while True:
            if self.additional_auth:
                for a in self.additional_auth:
                    if not await Link(self.client).auth(a, log_func=self.log.info):
                        return self.ctx.finish(RunStatus.IGNORE, "需要额外认证")

            if not self.init_first:
                if not await self.init():
                    self.log.warning(f"初始化错误.")
                    return self.ctx.finish(RunStatus.FAIL, "初始化错误")

            specs = []
            if self.bot_username:
                try:
                    bot = await self.client.get_users(self.bot_username)
                except IndexError:
                    self.log.warning(f"初始化错误: 用户名 {self.bot_username} 不存在或不是有效用户.")
                    return self.ctx.finish(RunStatus.FAIL, "初始化错误")
                specs.append(f"[green]{bot.full_name}[/] [gray50](@{bot.username})[/]")
            if chat.title:
                specs.append(f"[green]{chat.title}[/] [gray50](@{chat.username})[/]")
            else:
                specs.append(f"[green]@{chat.username}[/]")
            if specs:
                self.log.info(f"开始执行签到: {' @ '.join(specs)}.")

            if not self.chat_name:
                self.log.debug(f"[gray50]禁用提醒 {self.timeout} 秒: {bot.username}[/]")
                peer = InputNotifyPeer(peer=await self.client.resolve_peer(ident))
                try:
                    settings: PeerNotifySettings = await self.client.invoke(GetNotifySettings(peer=peer))
                except FloodWait:
                    self.log.debug(f"[gray50]获取当前提醒设置因访问超限而失败: {bot.username}[/]")
                    old_mute_until = 0
                else:
                    old_mute_until = settings.mute_until
                try:
                    await self.client.mute_chat(ident, time.time() + self.timeout + 10)
                except FloodWait:
                    self.log.debug(f"[gray50]设置禁用提醒因访问超限而失败: {bot.username}[/]")

            self.ctx.status = RunStatus.RUNNING

            try:
                async with self.listener():
                    try:
                        if self.bot_use_history is None:
                            await self.send_checkin()
                        elif not await self.walk_history(self.bot_use_history):
                            await self.send_checkin()
                        await asyncio.wait_for(self.finished.wait(), self.timeout)
                    finally:
                        try:
                            if await asyncio.wait_for(self.cleanup(), 3):
                                self.log.debug(f"[gray50]执行清理成功: {ident}[/]")
                            else:
                                self.log.debug(f"[gray50]执行清理失败: {ident}[/]")
                        except asyncio.TimeoutError:
                            self.log.debug(f"[gray50]执行清理失败: {ident}[/]")
                        if is_archived:
                            self.log.debug(f"[gray50]将会话重新归档: {ident}[/]")
                            try:
                                if await asyncio.wait_for(chat.archive(), 3):
                                    self.log.debug(f"[gray50]重新归档成功: {ident}[/]")
                            except asyncio.TimeoutError:
                                self.log.debug(f"[gray50]归档失败: {ident}[/]")
                            except FloodWait:
                                self.log.debug(f"[gray50]归档因访问超限而失败: {ident}[/]")
                        if not self.chat_name:
                            self.log.debug(f"[gray50]将会话设为已读: {ident}[/]")
                            try:
                                if await asyncio.wait_for(self.client.read_chat_history(ident), 3):
                                    self.log.debug(f"[gray50]设为已读成功: {ident}[/]")
                            except asyncio.TimeoutError:
                                self.log.debug(f"[gray50]设为已读失败: {ident}[/]")
                            except FloodWait:
                                self.log.debug(f"[gray50]设为已读因访问超限而失败: {ident}[/]")
            except asyncio.TimeoutError:
                # Python 3.11 起 asyncio.TimeoutError 是 OSError 的子类, 必须放在 OSError 之前捕获,
                # 否则超时会被误判为"初始化错误", 并打印一段没有任何信息的空 traceback.
                pass
            except OSError as e:
                self.log.warning(f'初始化错误: "{e}", 签到器将停止.')
                show_exception(e)
                return self.ctx.finish(RunStatus.FAIL, f"初始化错误: {e}")
            finally:
                if not self.chat_name:
                    if old_mute_until:
                        try:
                            await asyncio.wait_for(self.client.mute_chat(ident, until=old_mute_until), 3)
                        except asyncio.TimeoutError:
                            self.log.debug(f"[gray50]重新设置通知设置失败: {ident}[/]")
                        except FloodWait:
                            self.log.debug(f"[gray50]重新设置通知设置因访问超限而失败: {ident}[/]")
                        else:
                            self.log.debug(f"[gray50]重新设置通知设置成功: {ident}[/]")
            if not self.finished.is_set():
                self.log.warning(f"无法在时限内完成签到 ({self.timeout} 秒内无响应), 已跳过.")
                return self.ctx.finish(RunStatus.FAIL, "无法在时限内完成签到")
            elif self.current_retries <= self.valid_retries:
                if (
                    self.ctx.status == RunStatus.NONEED
                ):  # 已签到的情况, 如果设置了 checked_retries, 则返回重计划请求
                    if self.checked_retries:
                        if self.ctx.reschedule and self.ctx.reschedule > self.checked_retries:
                            return self.ctx.finish(RunStatus.NONEED)
                        else:
                            now = datetime.now()
                            midnight = now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(
                                days=1
                            )
                            max_sleep = midnight - now
                            sleep = timedelta(hours=min((self.ctx.reschedule or 0 + 1) * 1, 6))
                            if sleep > max_sleep:
                                return self.ctx.finish(RunStatus.NONEED)
                            else:
                                self.log.info(f"今日已签到, 即将在 {format_timedelta_human(sleep)} 后重试.")
                                self.ctx.next_time = now + sleep
                                return self.ctx.finish(RunStatus.RESCHEDULE, "等待重新尝试签到")
                return self.ctx.finish()
            else:
                # 智能回答等路径会调用 fail() 把重试次数置为无限, 以"停止"后续流程;
                # 但机器人仍可能随后返回"签到成功". 此时成功结果不应被覆盖为失败.
                if self.ctx.status in (RunStatus.SUCCESS, RunStatus.NONEED):
                    return self.ctx.finish()
                return self.ctx.finish(RunStatus.FAIL, "重试次数超限")

    async def init(self):
        """可重写的初始化函数, 在读取聊天后运行, 在执行签到前运行, 返回 False 将视为初始化错误."""
        return True

    async def cleanup(self):
        """可重写的签到结束后的清理函数, 执行签到成功或失败后运行, 返回 False 将视为清理错误."""
        return True

    async def walk_history(self, limit=0):
        """处理 limit 条历史消息, 并检测是否有验证码."""
        try:
            async for m in self.client.get_chat_history(self.chat_name or self.bot_username, limit=limit):
                if MessageType.CAPTCHA in self.message_type(m):
                    await self.on_photo(m)
                    return True
            return False
        except Exception as e:
            self.log.warning("读取历史消息失败, 将不再读取历史消息.")
            show_exception(e)
            return False

    async def send(self, cmd):
        """向机器人发送命令."""
        if self.chat_name:
            return await self.client.send_message(self.chat_name, cmd)
        else:
            return await self.client.send_message(self.bot_username, cmd)

    async def send_checkin(self, retry=False):
        """发送签到命令, 或依次发送签到命令序列."""
        cmds = to_iterable(self.bot_checkin_cmd)
        for i, cmd in enumerate(cmds):
            if retry and not i:
                await asyncio.sleep(self.bot_retry_wait)
            if i < len(cmds):
                await asyncio.sleep(self.bot_send_interval)
            self._first_waiting = True
            await self.send(cmd)

    async def _message_handler(self, client: Client, message: Message):
        """消息处理入口函数的错误处理外壳."""
        try:
            if self._first_waiting:
                self._first_waiting = False
                message.is_first_response = True
            # 创建新的任务并存储
            task = asyncio.create_task(self.message_handler(client, message))
            self._handler_tasks.add(task)
            await task
        except asyncio.TimeoutError:
            # 同上: TimeoutError 属于 OSError, 需先于 OSError 捕获, 避免刷出空 traceback.
            self.log.info("消息处理超时, 正在重试.")
            await self.retry()
            message.continue_propagation()
        except OSError as e:
            self.log.info(f'发生错误: "{e}", 正在重试.')
            show_exception(e)
            await self.retry()
            message.continue_propagation()
        except asyncio.CancelledError:
            task.cancel()
            try:
                await asyncio.wait_for(task, 5)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass
            finally:
                raise
        except Exception as e:
            if not config.nofail:
                await self.fail()
                raise
            else:
                self.log.warning(f"发生错误, 签到器将停止.")
                show_exception(e, regular=False)
                await self.fail()
                message.continue_propagation()
        else:
            message.continue_propagation()
        finally:
            if task in self._handler_tasks:
                self._handler_tasks.remove(task)

    async def message_handler(self, client: Client, message: Message, type=None):
        """消息处理入口函数."""
        text = message.text or message.caption
        if text:
            for p, k in self._waiting.items():
                if re.search(p, text):
                    k.set()
                    self._waiting[p] = message
        type = type or self.message_type(message)
        if type == MessageType.IGNORE:
            # 无文字内容的消息 (贴纸 / 纯图片等). 记一笔, 便于排查 "结果消息没被处理".
            self.log.debug(f"忽略无文字内容的消息 (id={getattr(message, 'id', None)}).")
            return
        if type:
            if MessageType.TEXT in type:
                await self.on_text(message, message.text)
            if MessageType.CAPTION in type:
                await self.on_text(message, message.caption)
            if MessageType.CAPTCHA in type:
                await self.on_photo(message)

    def is_result_text(self, text: str) -> bool:
        """判断文字是否为签到结果 (成功 / 失败 / 已签到 / 账户错误).

        用于避免把 "带说明文字的签到结果卡片" 当成验证码去 OCR:
        这类卡片被识别成乱码后会被当作答案发出去, 而真正的结果消息反而被忽略.
        """
        if not text:
            return False
        for name in ("success", "fail", "checked", "account_fail", "too_many_tries_fail"):
            keywords = to_iterable(getattr(self, f"bot_{name}_keywords", [])) or default_keywords[name]
            if keyword_match(text, keywords):
                return True
        return False

    def message_type(self, message: Message):
        """分析传入消息的类型为验证码或文字."""
        if message.photo:
            if message.caption:
                # 图片只是配图, 真正的验证在文字和按钮里 (如 "防機器签到验证: 请依次点击下方
                # 按钮补全诗句"). 这类消息当验证码 OCR 只会识别失败并反复重试.
                if self.is_challenge_text(message.caption):
                    return MessageType.CAPTION
                # 同理: "✅ 验证通过 / 🐮 签到成功" 这类结果卡片也不是验证码.
                if self.is_result_text(message.caption):
                    return MessageType.CAPTION
                if self.bot_use_captcha:
                    if self.bot_checkin_caption_pat:
                        if re.search(self.bot_checkin_caption_pat, message.caption):
                            return MessageType.CAPTCHA
                        else:
                            return MessageType.CAPTION
                    else:
                        return MessageType.CAPTCHA
                else:
                    return MessageType.CAPTION
            else:
                if self.bot_use_captcha:
                    return MessageType.CAPTCHA
                else:
                    return MessageType.IGNORE
        elif message.text:
            return MessageType.TEXT
        elif message.caption:
            # 文档 / 视频 / 动图 / 语音等非图片媒体也可能带说明文字.
            # 旧实现只认 .photo, 这类消息会被静默丢弃, 签到结果就再也等不到了.
            return MessageType.CAPTION
        else:
            return MessageType.IGNORE

    async def on_photo(self, message: Message):
        """分析传入的验证码图片并返回验证码."""
        data = await self.client.download_media(message, in_memory=True)
        ocr = OCRService.get(
            ocr_name=self.ocr,
            char_range=self.bot_captcha_char_range,
        )

        try:
            captcha = await ocr.run(data, expected_len=self.bot_captcha_len)
        except Exception as e:
            self.log.info(f"签到失败: 验证码识别失败 ({e}), 正在重试.")
            await self.retry()
            return

        if not captcha:
            self.log.info(f"签到失败: 无法识别验证码, 正在重试.")
            await self.retry()
            return

        self.log.debug(f"[gray50]接收验证码: {captcha}.[/]")
        await asyncio.sleep(random.uniform(2, 4))
        await self.on_captcha(message, captcha)

    async def on_captcha(self, message: Message, captcha: str):
        """
        可修改的回调函数.
            message: 包含验证码图片的消息
            captcha: OCR 识别的验证码
        """
        await message.reply(captcha)

    async def on_captcha_error(self, message: Message, text: str) -> bool:
        """验证码错误时的回调, 返回 True 表示已处理, 不再执行默认的重试逻辑.

        适用于机器人判错验证码后会补发新图的站点; 默认不处理.
        """
        return False

    def _log_success_basis(self, text: str) -> None:
        """成功但没解析出数字时, 把判定依据打出来.

        "签到成功." 这种无数字的成功最可疑: 很可能命中的是按钮回调 toast 或
        验证题面里的 "完成" 等词, 打一行原文便于事后排查误报.
        """
        self.log.info(f"判定成功依据: {truncate_str(text, 60)}")

    def is_challenge_text(self, text: str) -> bool:
        """判断是否为验证问题 (算术题 / 请答题), 而不是签到结果.

        先看关键词, 再看算式与指令模式. 后者能覆盖
        "请完成下面这道题后签到: 17 - 15 = ?" 与 "请完成诗句填空进行验证"
        这类同时含 "完成" 的题面 —— 否则会被当成签到成功而漏答.
        """
        if not text:
            return False
        keywords = to_iterable(self.bot_challenge_keywords) or default_keywords["challenge"]
        if keyword_match(text, keywords):
            return True
        # NFKC 把全角 "８－５＝" 归一成 "8-5=", 否则中文站点用全角符号会漏判.
        normalized = unicodedata.normalize("NFKC", text)
        if CHALLENGE_PATTERN.search(normalized):
            return True
        return bool(CHALLENGE_INSTRUCTION_PATTERN.search(normalized))

    async def on_text(self, message: Message, text: str):
        """接收非验证码消息时, 检测关键词并确认签到成功或失败, 发送用户提示."""
        if not text:
            return
        if self.bot_captcha_error_keywords and keyword_match(
            text, to_iterable(self.bot_captcha_error_keywords)
        ):
            if await self.on_captcha_error(message, text):
                return
        if keyword_match(text, to_iterable(self.bot_text_ignore)):
            pass
        elif self.is_challenge_text(text):
            if self._challenge_pending:
                # 题面和 "请完成…验证" 提示可能前后脚到达, 重复作答只会把按钮点乱.
                self.log.info(f"验证题正在解答中, 忽略重复提示: {truncate_str(text, 40)}")
                return
            # 例: MICU 点签到后要答算术题, EmbyYing_2 要输入答案发送.
            # 这类消息往往也含 "签到成功" / "请完成" 等字样, 若按成功处理就会漏答并误报成功.
            self.log.info("检测到验证问题, 正在尝试智能回答.")
            self._challenge_pending = True
            try:
                await self.gpt_handle_message(message, unexpected=False)
            finally:
                self._challenge_pending = False
            # 机器人往往在答对的瞬间就发出成功消息, 那时智能回答还没返回.
            # 暂存下来在此刻补处理, 否则会一直等到超时.
            pending, self._pending_success_text = self._pending_success_text, None
            if pending:
                self.log.info(f"处理作答期间收到的成功消息: {truncate_str(pending, 40)}")
                await self.on_text(None, pending)
        elif keyword_match(
            text, to_iterable(self.bot_account_fail_keywords) or default_keywords["account_fail"]
        ):
            self.log.warning(f"签到失败: 账户错误.")
            await self.fail()
        elif keyword_match(
            text,
            to_iterable(self.bot_too_many_tries_fail_keywords) or default_keywords["too_many_tries_fail"],
        ):
            self.log.warning(f"签到失败: 尝试次数过多.")
            await self.fail()
        elif keyword_match(text, to_iterable(self.bot_checked_keywords) or default_keywords["checked"]):
            self.log.info(f"今日已经签到过了.")
            await self.finish(RunStatus.NONEED, "今日已签到")
        elif keyword_match(text, to_iterable(self.bot_fail_keywords) or default_keywords["fail"]):
            self.log.info(f"签到失败: 验证码错误或网络错误, 正在重试.")
            await self.retry()
        elif keyword_match(text, to_iterable(self.bot_success_keywords) or default_keywords["success"]):
            if self._challenge_pending:
                # 机器人常把 "请完成诗句填空进行验证" 这类提示单独发一条, 里面带 "完成".
                # 此时智能回答可能还在点按钮, 直接判成功会取消它, 直接丢弃又会一直等到超时,
                # 所以先暂存, 等作答结束后补处理.
                self._pending_success_text = text
                self.log.info(f"验证题解答中, 暂存疑似成功消息: {truncate_str(text, 40)}")
                return
            if await self.before_success():
                if self.bot_success_pat:
                    matches = re.search(self.bot_success_pat, text)
                    if matches:
                        try:
                            self.log.info(
                                f"[yellow]签到成功[/]: + {matches.group(1)} 分 -> {matches.group(2)} 分."
                            )
                        except IndexError:
                            try:
                                self.log.info(f"[yellow]签到成功[/]: 当前/增加 {matches.group(1)} 分.")
                            except IndexError:
                                self.log.info(f"[yellow]签到成功[/].")
                    else:
                        matches = re.search(r"\d+", text)
                        if matches:
                            self.log.info(f"[yellow]签到成功[/]: 当前/增加 {matches.group(0)} 分.")
                        else:
                            self.log.info(f"[yellow]签到成功[/].")
                            self._log_success_basis(text)
                else:
                    self.log.info(f"[yellow]签到成功[/].")
                    self._log_success_basis(text)
                await self.after_success()
                await self.finish(RunStatus.SUCCESS, "签到成功")
        else:
            await self.on_unexpected_text(message)

    async def on_unexpected_text(self, message: Message):
        return await self.gpt_handle_message(message, unexpected=True)

    async def gpt_handle_message(self, message: Message, unexpected: bool = True):
        content = message.text or message.caption
        if content:
            spec = content.replace("\n", " ")
            if unexpected:
                self.log.warning(f"接收到异常返回信息: {spec}, 正在尝试智能回答.")
            else:
                self.log.info(f"正在使用智能接口回答问题.")
            if (
                message.reply_markup
                and isinstance(message.reply_markup, InlineKeyboardMarkup)
                and message.reply_markup.inline_keyboard
            ):
                buttons = [b.text for r in message.reply_markup.inline_keyboard for b in r]
            else:
                buttons = []
            button_specs = [f"'{b}'" for b in buttons]
            prompt = (
                "我正在进行签到, 机器将显示指令或状态, 我需要通过回答问题以避免被识别为机器人或自动化程序, 并希望完成签到, 现在机器给出的值为:\n\n"
                f"{content}\n\n"
            )
            poem_fill = bool(POEM_FILL_PATTERN.search(content))
            if poem_fill:
                # 补全诗句是知识题, 泛泛地问模型容易答成 "这是状态信息".
                # 明确告诉它挖空位置与作答顺序, 命中率会高很多.
                prompt += (
                    "这是一道补全诗句的题目: 诗句中若干位置被挖空 (可能显示为方框、下划线或空格), "
                    "需要按挖空出现的先后顺序, 从按钮中选出对应的字填回去。\n"
                    "请先在心里还原完整原句, 再按顺序输出每个挖空对应的按钮。\n"
                )
            if buttons:
                prompt += (
                    f"你可选: {', '.join(button_specs)} 作为回答.\n"
                    "形式为: [CLICK]^XXX^, 其中XXX为回答\n"
                    "如果题目要求按顺序点击多个按钮 (如补全诗句、依次点击、按顺序点出答案), "
                    "请按顺序输出多个 [CLICK]^XXX^, 例如: [CLICK]^丛^[CLICK]^在^\n"
                )
            prompt += (
                "如果您认为不应该进行任何操作, 请输出 [NO_RESP], 禁止输出其他内容.\n"
                "如果这是一个指令, 请输出您需要发送或点击的内容.\n"
                "形式为: [SEND]^XXX^, 其中XXX为内容\n"
                "不要说明这是一个指令, 不要说明需要发送文本消息, 仅仅按上述形式输出.\n"
                "如果这是一个状态, 请输出 [IS_STATUS], 禁止输出其他内容."
            )
            for _ in range(3):
                answer, by = await Link(self.client).gpt(prompt)
                if answer:
                    self.log.debug(f"智能回答 ({by}): {answer}")
                    targets = CLICK_PATTERN.findall(answer)
                    if not targets and buttons and poem_fill:
                        # 补全诗句时模型常直接给出要填的字 (如 "起足"), 没按 [CLICK] 格式输出.
                        # 按出现顺序挑出命中的按钮字; 超过 4 个说明多半是在解释, 不予采信.
                        picked = list(dict.fromkeys(c for c in answer if c in buttons))
                        if 1 <= len(picked) <= 4:
                            self.log.info(f"智能回答未按格式输出, 按字符匹配: {''.join(picked)}")
                            targets = picked
                    if "[NO_RESP]" in answer:
                        if unexpected:
                            self.log.info(f"智能回答认为无需进行操作, 为了避免风险签到器将停止.")
                            await self.fail()
                            return False
                        else:
                            self.log.info(f"智能回答认为无需进行操作.")
                            return True
                    elif "[IS_STATUS]" in answer:
                        if unexpected:
                            self.log.info(
                                f"智能回答认为这是一条状态信息, 无需进行操作, 为了避免风险签到器将停止."
                            )
                            await self.fail()
                            return False
                        else:
                            self.log.info(f"智能回答认为这是一条状态信息, 无需进行操作.")
                            return True
                    elif buttons and targets:
                        self.log.debug(f"当前按钮: {', '.join(button_specs)}")
                        # 先把所有目标都匹配到按钮上, 再按顺序点击.
                        # 部分机器人要求先点对前一个才允许点下一个, 中途匹配失败时整体重来.
                        matched_buttons = []
                        for target in targets:
                            b, s = process.extractOne(target, buttons, scorer=fuzz.partial_ratio)
                            if s < 70:
                                matched_buttons = None
                                break
                            matched_buttons.append(b)
                        if not matched_buttons:
                            self.log.info(f"找不到对应回答的按钮, 正在重试.")
                            await asyncio.sleep(3)
                            continue
                        for index, b in enumerate(matched_buttons):
                            if index:
                                # 多级点击之间留出间隔, 避免机器人把连续点击判定为脚本.
                                await asyncio.sleep(random.uniform(0.8, 2.0))
                            try:
                                await message.click(b)
                            except (TimeoutError, MessageIdInvalid):
                                pass
                            if len(matched_buttons) > 1:
                                self.log.info(
                                    f'智能回答点击了按钮 "{b}" ({index + 1}/{len(matched_buttons)}).'
                                )
                        if len(matched_buttons) == 1:
                            self.log.info(f'智能回答点击了按钮 "{matched_buttons[0]}".')
                        if unexpected:
                            self.log.warning(f"智能回答已点击按钮, 为了避免风险签到器将停止.")
                            await self.fail()
                            return False
                        else:
                            return True
                    elif "[SEND]" in answer:
                        answer_content = re.search(r"\[SEND\]\^(.+?)\^", answer)
                        if not answer_content:
                            if unexpected:
                                self.log.warning(f"智能回答失败, 为了避免风险签到器将停止.")
                                await self.fail()
                                return False
                            else:
                                self.log.warning(f"智能回答失败.")
                                return False
                        answer_content = answer_content.group(1)
                        await message.reply(answer_content)
                        if unexpected:
                            self.log.warning(f'智能回答回复了 "{answer_content}", 为了避免风险签到器将停止.')
                            await self.fail()
                            return False
                        else:
                            self.log.info(f'智能回答回复了 "{answer_content}".')
                            return True
            else:
                if unexpected:
                    self.log.warning(f"智能回答失败, 为了避免风险签到器将停止.")
                    await self.fail()
                    return False
                else:
                    self.log.warning(f"智能回答失败.")
                    return False

    async def retry(self):
        """执行重试, 重新发送签到指令."""
        self.current_retries += 1
        if self.current_retries <= self.valid_retries:
            await asyncio.sleep(self.bot_retry_wait)
            await self.send_checkin(retry=True)
        else:
            self.log.warning("超过最大重试次数.")
            await self.finish(RunStatus.FAIL, "超过最大重试次数")

    async def fail(self, status: RunStatus = RunStatus.FAIL, message: str = None):
        """设定签到器失败."""
        return await self.finish(status, message, fail=True)

    async def finish(self, status: RunStatus = RunStatus.SUCCESS, message: str = None, fail=False):
        """设定签到器结束."""

        if fail:
            self.current_retries = float("inf")
        # 终态保护: 已成功或已判定无需签到时, 不再被失败类状态覆盖.
        # 典型场景: 智能回答点击按钮后 fail() 停止流程, 但机器人随后仍返回"签到成功".
        if status not in (RunStatus.SUCCESS, RunStatus.NONEED) and self.ctx.status in (
            RunStatus.SUCCESS,
            RunStatus.NONEED,
        ):
            return self
        self.ctx.status = status
        self.ctx.status_info = message
        self.finished.set()
        return self

    async def wait_until(self, pattern: str = ".", timeout: float = None):
        """等待特定消息出现."""
        self._waiting[pattern] = e = asyncio.Event()
        try:
            await asyncio.wait_for(e.wait(), timeout)
        except asyncio.TimeoutError:
            return None
        else:
            msg: Message = self._waiting[pattern]
            return msg

    async def before_success(self):
        """签到成功前钩子, 返回值为 True 将继续执行并显示成功信息."""
        return True

    async def after_success(self):
        """签到成功后钩子."""
        pass


class AnswerBotCheckin(BotCheckin):
    """签到类, 用于按钮模式签到."""

    bot_answer_button_message_pat: str = None  # 回答按键消息内容的 regex 条件
    bot_answer_button_pat: str = None  # 所有按键需要满足的 regex 条件

    def __init__(self, *args, **kw):
        super().__init__(*args, **kw)
        self.mutex = asyncio.Lock()  # 实例变量异步锁
        self.operable = asyncio.Condition(self.mutex)  # 当 message 被设定时提醒等待的异步线程.
        self.message: Message = None  # 存储可用于回复的带按钮的消息.

    async def walk_history(self, limit=0):
        """处理 limit 条历史消息, 并检测是否有验证码或按钮."""
        try:
            answer = None
            captcha = None
            async for m in self.client.get_chat_history(self.chat_name or self.bot_username, limit=limit):
                if MessageType.ANSWER in self.message_type(m):
                    answer = answer or m
                if MessageType.CAPTCHA in self.message_type(m):
                    captcha = captcha or m
                if answer and captcha:
                    break
            else:
                return False
            await self.on_answer(answer)
            await self.on_photo(captcha)
            return True
        except Exception as e:
            self.log.warning("读取历史消息失败, 将不再读取历史消息.")
            show_exception(e)
            return False

    def get_keys(self, message: Message):
        """获得所有按钮信息."""
        reply_markup = message.reply_markup
        if isinstance(reply_markup, InlineKeyboardMarkup):
            return [k.text for r in reply_markup.inline_keyboard for k in r]
        elif isinstance(reply_markup, ReplyKeyboardMarkup):
            return [k.text for r in reply_markup.keyboard for k in r]

    def is_valid_answer(self, message: Message):
        """确认消息是回复按钮消息."""
        if not message.reply_markup:
            return False
        if self.bot_answer_button_message_pat:
            text = message.text or message.caption
            if not text:
                return False
            if not re.search(self.bot_answer_button_message_pat, text):
                return False
        if self.bot_answer_button_pat:
            for k in self.get_keys(message):
                if not re.search(self.bot_answer_button_pat, k):
                    return False
        return True

    def message_type(self, message: Message):
        """分析传入消息的类型为验证码或文字."""
        if self.is_valid_answer(message):
            return MessageType.ANSWER | super().message_type(message)
        else:
            return super().message_type(message)

    async def message_handler(self, client: Client, message: Message, type=None):
        """消息处理入口函数."""
        type = type or self.message_type(message)
        if MessageType.ANSWER in type:
            await self.on_answer(message)
        await super().message_handler(client, message, type=type)

    async def on_answer(self, message: Message):
        """当检测到带按钮的用于回答的消息时保存其信息."""
        async with self.mutex:
            if self.message:
                if self.message.date > message.date:
                    return
                else:
                    self.message = message
            else:
                self.message = message
                self.operable.notify()

    async def on_captcha(self, message: Message, captcha: str):
        """
        可修改的回调函数.
        参数:
            message: 包含验证码图片的消息
            captcha: OCR 识别的验证码
        """
        async with self.operable:
            if not self.message:
                await self.operable.wait()
            # Convert captcha to lowercase and compare with lowercase keys
            match = [(k, fuzz.ratio(k.lower(), captcha.lower())) for k in self.get_keys(self.message)]
            max_k, max_r = max(match, key=lambda x: x[1])
            if max_r < 75:
                self.log.info(f'未能找到对应 "{captcha}" 的按键, 正在重试.')
                await self.retry()
            else:
                try:
                    await self.message.click(max_k)
                except (TimeoutError, MessageIdInvalid, DataInvalid):
                    pass
