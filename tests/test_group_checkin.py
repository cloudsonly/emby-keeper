"""群组签到配置的回归测试.

背景: 很多站点只在群里签到 —— 要么在群里 @ 机器人发命令等它回复, 要么干脆在群里发个
关键字就算签到. 模板 A 以前完全没有群组参数 (写进配置会被静默忽略), 模板 B 只有
is_chat 一个开关 (它会把机器人过滤丢掉, 群友的闲聊都可能被误判为签到结果).
"""

import asyncio

from loguru import logger

from embykeeper.telegram.checkiner._base import normalize_chat_id
from embykeeper.telegram.checkiner._templ_a import TemplateACheckin
from embykeeper.telegram.checkiner._templ_b import TemplateBCheckin
from embykeeper.telegram.dynamic import get_cls


class FakeMe:
    full_name = "Test User"


class FakeClient:
    me = FakeMe()


def make(cls, config, bot_username=None):
    """绕开 __init__ 构造一个最小实例, 只用来跑 init()."""
    inst = object.__new__(cls)
    inst.client = FakeClient()
    inst.config = config
    inst.bot_username = bot_username
    inst.chat_name = None
    inst.log = logger.bind(name="test")
    return inst


def run_init(inst):
    assert asyncio.run(inst.init()) is True
    return inst


def test_normalize_chat_id():
    assert normalize_chat_id("-1001234567890") == -1001234567890
    assert normalize_chat_id(" -1001234567890 ") == -1001234567890
    assert normalize_chat_id("SomeGroup") == "SomeGroup"
    assert normalize_chat_id(-1001234567890) == -1001234567890
    assert normalize_chat_id(None) is None


def test_templ_b_is_chat_turns_target_into_group():
    """is_chat: 尖括号里的是群组, 不再当机器人过滤."""
    inst = run_init(make(TemplateBCheckin, {"is_chat": True}, bot_username="somegroup"))
    assert inst.chat_name == "somegroup"
    assert inst.bot_username is None


def test_templ_b_chat_name_keeps_bot_filter():
    """chat_name: 在群里跟机器人对话, 机器人过滤必须保留."""
    inst = run_init(make(TemplateBCheckin, {"chat_name": "somegroup"}, bot_username="SomeEmbyBot"))
    assert inst.chat_name == "somegroup"
    assert inst.bot_username == "SomeEmbyBot"


def test_templ_b_chat_name_accepts_numeric_group_id():
    inst = run_init(make(TemplateBCheckin, {"chat_name": "-1001234567890"}))
    assert inst.chat_name == -1001234567890


def test_templ_b_is_chat_accepts_numeric_group_id():
    inst = run_init(make(TemplateBCheckin, {"is_chat": True}, bot_username="-1001234567890"))
    assert inst.chat_name == -1001234567890


def test_templ_b_without_group_stays_private():
    inst = run_init(make(TemplateBCheckin, {}, bot_username="SomeEmbyBot"))
    assert inst.chat_name is None
    assert inst.bot_username == "SomeEmbyBot"


def test_templ_a_reads_chat_name_before_start():
    """模板 A 的 init() 在 start() 解析会话之后才跑, chat_name 必须更早生效."""
    inst = TemplateACheckin(FakeClient(), config={"chat_name": "-1001234567890"})
    assert inst.chat_name == -1001234567890


def test_templ_a_without_chat_name_stays_private():
    inst = TemplateACheckin(FakeClient(), config={})
    assert inst.chat_name is None


def test_templ_a_config_declares_chat_name():
    """配置里写 chat_name 不能被静默丢弃."""
    from embykeeper.telegram.checkiner._templ_a import TemplateACheckinConfig

    cfg = TemplateACheckinConfig.model_validate({"chat_name": "somegroup"})
    assert cfg.chat_name == "somegroup"


def test_dynamic_accepts_numeric_group_in_site_name():
    clses = get_cls("checkiner", ["templ_b<-1001234567890>"])
    assert len(clses) == 1
    assert clses[0].bot_username == -1001234567890
    assert clses[0].name == "@-1001234567890"


def test_dynamic_accepts_named_group_in_site_name():
    clses = get_cls("checkiner", ["templ_b<SomeGroup>"])
    assert len(clses) == 1
    assert clses[0].bot_username == "SomeGroup"


def leaves(flt):
    """把 filters 的与运算树摊平成叶子."""
    from pyrogram import filters as f

    if isinstance(flt, f.AndFilter):
        return leaves(flt.base) + leaves(flt.other)
    return [flt]


def test_chat_name_equal_to_site_name_does_not_crash():
    """这种写法匹配不到任何消息 (群组不会以自己身份发消息), 但至少不能崩."""
    inst = run_init(make(TemplateBCheckin, {"chat_name": "SomeGroup"}, bot_username="SomeGroup"))
    assert inst.chat_name == "SomeGroup"


def real(config, bot_username=None):
    """走真实 __init__, 拿到 ctx / finished 以便验证 send_checkin 的收尾行为."""
    inst = TemplateBCheckin(FakeClient(), config=config)
    inst.bot_username = bot_username
    assert asyncio.run(inst.init()) is True
    return inst


def capture_sent(inst):
    calls = []

    async def fake_send_message(ident, cmd):
        calls.append((ident, cmd))

    inst.client.send_message = fake_send_message
    return calls


def test_keyword_goes_to_group_not_private():
    inst = run_init(make(TemplateBCheckin, {"chat_name": "somegroup"}, bot_username="SomeEmbyBot"))
    calls = capture_sent(inst)
    asyncio.run(inst.send("签到"))
    assert calls == [("somegroup", "签到")]


def test_send_only_group_skips_waiting():
    """wait_response = false: 发完关键字就算签到完成, 不干等机器人回复."""
    inst = real({"is_chat": True, "bot_checkin_cmd": "签到", "wait_response": False}, "somegroup")
    calls = capture_sent(inst)
    asyncio.run(inst.send_checkin())
    assert calls == [("somegroup", "签到")]
    assert inst.finished.is_set()


def test_group_send_waits_for_reply_by_default():
    inst = real({"is_chat": True, "bot_checkin_cmd": "签到"}, "somegroup")
    calls = capture_sent(inst)
    asyncio.run(inst.send_checkin())
    assert calls == [("somegroup", "签到")]
    assert not inst.finished.is_set()


def test_get_filter_combines_chat_and_bot():
    inst = run_init(make(TemplateBCheckin, {"chat_name": "somegroup"}, bot_username="SomeEmbyBot"))
    found = leaves(inst.get_filter())
    kinds = {type(x).__name__: x for x in found}
    # 群组过滤和机器人过滤都要在, 缺一个就会把群友的闲聊当成签到结果.
    assert "somegroup" in kinds["chat"]
    # pyrogram 把用户名统一转小写保存.
    assert "someembybot" in kinds["user"]


def test_get_filter_is_chat_drops_bot_filter():
    """is_chat 模式没有机器人可过滤, 只剩群组."""
    inst = run_init(make(TemplateBCheckin, {"is_chat": True}, bot_username="somegroup"))
    found = leaves(inst.get_filter())
    kinds = {type(x).__name__: x for x in found}
    assert "somegroup" in kinds["chat"]
    assert "user" not in kinds
