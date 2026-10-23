"""关键词支持 "re:" 正则前缀的回归测试.

背景: Ask Ash 的签到回执每天换一句吉祥话 ("稳如老狗, 奖励稳稳的!"), 固定关键词
追不上. 关键词表里以 "re:" 开头的条目按正则处理, 其余仍按子串匹配.
"""

import asyncio

from embykeeper.telegram.checkiner._base import BotCheckin, keyword_match


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------- 纯函数


def test_plain_keywords_still_do_substring_match():
    assert keyword_match("签到成功, 获得 5 币", ["成功"]) is True
    assert keyword_match("签到成功, 获得 5 币", ["失败"]) is False


def test_regex_prefix_matches():
    assert keyword_match("稳如老狗, 奖励稳稳的!", ["re:奖励"]) is True
    assert keyword_match("稳如老狗, 奖励稳稳的!", ["re:红包"]) is False


def test_regex_alternation_covers_flavour_texts():
    keywords = ["re:奖励|红包|到账|恭喜"]
    for text in ("稳如老狗, 奖励稳稳的!", "红包已到账", "恭喜获得 3 币"):
        assert keyword_match(text, keywords) is True, text


def test_invalid_regex_is_ignored_without_crashing():
    # 写错的正则当作没匹配, 但不能让整次签到崩掉
    assert keyword_match("奖励稳稳的", ["re:([unclosed", "奖励"]) is True
    assert keyword_match("奖励稳稳的", ["re:([unclosed"]) is False


def test_empty_text_never_matches():
    assert keyword_match("", ["奖励", "re:奖励"]) is False


def test_only_the_prefix_triggers_regex_mode():
    # 只有开头才是正则标记, 出现在中间的 "re:" 仍按子串匹配
    assert keyword_match("回复 abc re:def 结束", ["c re:d"]) is True
    # 以 "re:" 开头则一定按正则解释
    assert keyword_match("回复 abc re:def 结束", ["re:def"]) is True


# ---------------------------------------------------------------- 站点判定


def make_checkin(**kw):
    inst = object.__new__(BotCheckin)
    for key, value in kw.items():
        setattr(inst, key, value)
    return inst


def test_is_result_text_honours_regex_keywords():
    inst = make_checkin(bot_success_keywords=["re:奖励|红包"])
    assert inst.is_result_text("稳如老狗, 奖励稳稳的!") is True
    assert inst.is_result_text("今天没有中奖") is False


def test_is_challenge_text_honours_regex_keywords():
    inst = make_checkin(bot_challenge_keywords=["re:第\\s*\\d+\\s*题"])
    assert inst.is_challenge_text("请完成第 3 题") is True
    assert inst.is_challenge_text("签到成功") is False


def test_regex_success_keyword_is_used_by_on_text():
    """走真实 on_text, 确认 "re:" 关键词能直接判成功."""
    inst = make_checkin(bot_success_keywords=["re:奖励|红包"])
    inst.bot_text_ignore = None
    inst.bot_challenge_keywords = None
    inst.bot_account_fail_keywords = None
    inst.bot_too_many_tries_fail_keywords = None
    inst.bot_checked_keywords = None
    inst.bot_fail_keywords = None
    inst.bot_success_pat = None
    inst.bot_captcha_error_keywords = None
    inst._challenge_pending = False
    inst.finished = []

    async def finish(status=None, message=None):
        inst.finished.append((status, message))

    async def before_success():
        return True

    async def after_success():
        return None

    async def fail(status=None, message=None):
        inst.finished.append(("fail", message))

    async def retry():
        inst.finished.append(("retry", None))

    inst.finish = finish
    inst.before_success = before_success
    inst.after_success = after_success
    inst.fail = fail
    inst.retry = retry
    inst._log_success_basis = lambda text: None

    from loguru import logger

    inst.log = logger.bind(name="test")

    run(inst.on_text(None, "稳如老狗, 奖励稳稳的!"))
    assert inst.finished and inst.finished[0][0] == 5  # RunStatus.SUCCESS
