"""签到进行中的中间状态不能被当成异常返回信息.

背景: @laoyou_bot 与 @lgembybot 收到 /checkin 后先回一条 "签到中...", 几秒后才回
"🎉 签到成功! 获得 21 积分". 旧实现把 "签到中..." 当成异常返回信息交给智能回答,
模型答不出这种状态消息, 连续 3 次拿不到答案就 "为了避免风险签到器将停止" 直接判失败
—— 而真正的成功消息随后才到, 却已经没人处理了.
"""

import asyncio
from types import SimpleNamespace

from embykeeper.telegram.checkiner._base import BotCheckin, RunStatus
from embykeeper.utils import to_iterable


def make_checkin() -> BotCheckin:
    context = SimpleNamespace(bind_logger=lambda log: log)
    client = SimpleNamespace(me=SimpleNamespace(full_name="tester"))
    return BotCheckin(client=client, context=context, config={})


def record_unexpected(checkin):
    calls = []

    async def fake(message):
        calls.append(getattr(message, "text", None))

    checkin.on_unexpected_text = fake
    return calls


def record_finish(checkin):
    calls = []

    async def fake(status, message=None, fail=False):
        calls.append((status, message))

    async def fake_before_success():
        return True

    async def fake_after_success():
        return None

    checkin.finish = fake
    checkin.before_success = fake_before_success
    checkin.after_success = fake_after_success
    return calls


# ---------------------------------------------------------------- 模式识别


def test_in_progress_pattern_matches_real_status_messages():
    checkin = make_checkin()
    for text in (
        "签到中...",
        "正在签到, 请稍候",
        "正在处理中",
        "排队中, 请耐心等待",
        "正在查询账户信息",
    ):
        assert checkin.is_in_progress_text(text) is True, text


def test_result_messages_are_not_in_progress():
    checkin = make_checkin()
    for text in (
        "🎉 签到成功! 获得 21 积分",
        "你今天已签到或签到机会已用完",
        "签到失败: 验证码错误",
        "请完成诗句填空进行验证",
        "",
    ):
        assert checkin.is_in_progress_text(text) is False, text


def test_custom_keywords_override_the_builtin_pattern():
    checkin = make_checkin()
    checkin.bot_in_progress_keywords = ["正在跑路"]
    assert checkin.is_in_progress_text("正在跑路, 等一下") is True
    # 配了自定义关键词后, 内置模式让位
    assert checkin.is_in_progress_text("签到中...") is False


# ---------------------------------------------------------------- on_text 分流


def test_status_message_is_not_sent_to_gpt():
    checkin = make_checkin()
    unexpected = record_unexpected(checkin)
    finished = record_finish(checkin)

    asyncio.run(checkin.on_text(SimpleNamespace(), "签到中..."))

    assert unexpected == []
    assert finished == []


def test_success_after_status_message_is_recognized():
    checkin = make_checkin()
    unexpected = record_unexpected(checkin)
    finished = record_finish(checkin)

    asyncio.run(checkin.on_text(SimpleNamespace(), "签到中..."))
    asyncio.run(
        checkin.on_text(
            SimpleNamespace(),
            "🎉 签到成功!\n💰 获得 21 积分\n🔥 连续签到 7 天，额外奖励 50 积分\n💰 当前余额: 3609 积分",
        )
    )

    assert unexpected == []
    assert [c[0] for c in finished] == [RunStatus.SUCCESS]


def test_success_keyword_wins_over_in_progress_phrase():
    """一条消息同时含 "成功" 与 "正在处理" 时, 必须按成功处理, 不能被中间状态吞掉."""
    checkin = make_checkin()
    unexpected = record_unexpected(checkin)
    finished = record_finish(checkin)

    asyncio.run(checkin.on_text(SimpleNamespace(), "签到成功, 正在处理奖励"))

    assert unexpected == []
    assert [c[0] for c in finished] == [RunStatus.SUCCESS]


# ---------------------------------------------------------------- 终点站


def test_terminus_does_not_send_cancel():
    """/cancel 只会换来一句 "没有活跃的会话", 白占一次往返和首条响应."""
    from embykeeper.telegram.checkiner.terminus import TerminusCheckin

    cmds = to_iterable(TerminusCheckin.bot_checkin_cmd)
    assert "/cancel" not in cmds
    assert "/checkin" in cmds
