"""結束バンド (plusemby) 网页签到器的纯逻辑测试.

只覆盖可独立验证的纯函数: 状态分类、验证配置解析、URL 归一化.
网络流程依赖真实站点, 不在单元测试范围内.
"""

import asyncio

from loguru import logger

from embykeeper.runinfo import RunContext, RunStatus
from embykeeper.telegram.checkiner.plusemby import (
    ACCESS_BLOCKED_MESSAGE,
    DEFAULT_BASE_URL,
    PlusembyCheckin,
    build_login_payload,
    classify_checkin_response,
    classify_status_response,
    extract_message,
    normalize_base_url,
    summarize_results,
    verification_required,
    verification_site_key,
)


def test_normalize_base_url_adds_scheme_and_strips_slash():
    assert normalize_base_url("example.com/") == "https://example.com"
    assert normalize_base_url("http://example.com") == "http://example.com"
    assert normalize_base_url(None) == DEFAULT_BASE_URL
    assert normalize_base_url("") == DEFAULT_BASE_URL


def test_build_login_payload_uses_empty_token_when_absent():
    payload = build_login_payload(" user ", "pwd")
    assert payload["userName"] == "user"
    assert payload["password"] == "pwd"
    assert payload["verificationToken"] == ""
    assert payload["recaptchaToken"] == ""

    payload = build_login_payload("u", "p", "tk")
    assert payload["verificationToken"] == "tk"


def test_extract_message_prefers_known_keys():
    assert extract_message({"message": "m"}) == "m"
    assert extract_message({"msg": "m2"}) == "m2"
    assert extract_message({"detail": "d"}) == "d"
    assert extract_message({"error": "e"}) == "e"
    assert extract_message({}, "fallback") == "fallback"
    assert extract_message("not a dict", "fallback") == "fallback"


def test_verification_required_respects_type_and_enabled():
    settings = {
        "login": {"enabled": True, "type": "turnstile", "siteKey": "k1"},
        "checkin": {"enabled": True, "type": "none"},
        "other": {"enabled": False, "type": "turnstile"},
    }
    assert verification_required(settings, "login") is True
    # type = none 视为不需要验证
    assert verification_required(settings, "checkin") is False
    # enabled = false 视为不需要验证
    assert verification_required(settings, "other") is False
    # 未配置的动作不需要验证
    assert verification_required(settings, "missing") is False
    assert verification_required(None, "login") is False


def test_verification_site_key_extraction():
    settings = {"login": {"enabled": True, "type": "turnstile", "siteKey": " key "}}
    assert verification_site_key(settings, "login") == "key"
    assert verification_site_key(settings, "missing") == ""
    assert verification_site_key(None, "login") == ""


def test_classify_status_response():
    assert classify_status_response(401, {}) == (RunStatus.FAIL, "登录已失效")
    assert classify_status_response(403, {}) == (RunStatus.FAIL, ACCESS_BLOCKED_MESSAGE)
    # 今日已签到 -> NONEED
    assert classify_status_response(200, {"hasCheckedInToday": True})[0] == RunStatus.NONEED
    # 尚未签到 -> 继续提交
    assert classify_status_response(200, {"hasCheckedInToday": False})[0] == RunStatus.RUNNING
    # 缺少字段 -> 失败
    assert classify_status_response(200, {})[0] == RunStatus.FAIL
    # success=false -> 失败
    assert classify_status_response(200, {"success": False, "message": "boom"}) == (
        RunStatus.FAIL,
        "boom",
    )


def test_classify_checkin_response_success_with_amount():
    status, message = classify_checkin_response(200, {"success": True, "amount": 12, "currencyUnit": "分"})
    assert status == RunStatus.SUCCESS
    assert message == "+12 分"


def test_classify_checkin_response_success_without_amount():
    status, message = classify_checkin_response(200, {"success": True, "message": "ok"})
    assert status == RunStatus.SUCCESS
    assert message == "ok"


def test_classify_checkin_response_already_checked_in():
    status, message = classify_checkin_response(200, {"success": False, "hasCheckedIn": True})
    assert status == RunStatus.NONEED


def test_classify_checkin_response_failure_cases():
    assert classify_checkin_response(401, {}) == (RunStatus.FAIL, "登录已失效")
    assert classify_checkin_response(403, {}) == (RunStatus.FAIL, ACCESS_BLOCKED_MESSAGE)
    assert classify_checkin_response(500, {"message": "server"}) == (RunStatus.FAIL, "server")
    assert classify_checkin_response(200, {})[0] == RunStatus.FAIL


# ---------------------------------------------------------------------------
# 多站点支持
# ---------------------------------------------------------------------------


def make_checkin(config: dict, entry: dict = None):
    """绕过 __init__ 的依赖, 只装配 entries/entry 需要的最小状态."""
    inst = object.__new__(PlusembyCheckin)
    inst.config = config
    if entry is not None:
        inst._entry = entry
    return inst


def test_entries_falls_back_to_single_site():
    inst = make_checkin({"url": "a.example.com", "username": "u", "password": "p"})
    assert inst.entries() == [inst.config]


def test_entries_uses_accounts_list():
    accounts = [
        {"url": "a.example.com", "username": "u1", "password": "p1"},
        {"url": "b.example.com", "username": "u2", "password": "p2"},
    ]
    inst = make_checkin({"accounts": accounts})
    assert inst.entries() == accounts


def test_entries_ignores_invalid_accounts():
    # accounts 里没有合法 dict 时退化为单站点, 避免静默不签到
    inst = make_checkin({"url": "a.example.com", "accounts": ["bad", 1, None]})
    assert inst.entries() == [inst.config]


def test_entry_property_switches_per_site():
    accounts = [
        {"url": "a.example.com", "username": "u1", "password": "p1"},
        {"url": "b.example.com", "username": "u2", "password": "p2"},
    ]
    inst = make_checkin({"accounts": accounts})
    inst._entry = accounts[0]
    assert inst.base_url == "https://a.example.com"
    assert inst.username == "u1"
    inst._entry = accounts[1]
    assert inst.base_url == "https://b.example.com"
    assert inst.username == "u2"


def test_access_token_cache_key_differs_per_site():
    accounts = [
        {"url": "a.example.com", "username": "same"},
        {"url": "b.example.com", "username": "same"},
    ]
    inst = make_checkin({"accounts": accounts})
    inst._entry = accounts[0]
    key_a = inst._access_token_cache_key()
    inst._entry = accounts[1]
    key_b = inst._access_token_cache_key()
    assert key_a != key_b


def test_summarize_results_passthrough_for_single_site():
    assert summarize_results([("", RunStatus.FAIL, "x")]) == (RunStatus.FAIL, "x")
    assert summarize_results([("", RunStatus.SUCCESS, "+1 分")]) == (RunStatus.SUCCESS, "+1 分")


def test_summarize_results_all_success():
    status, message = summarize_results(
        [
            ("[1/2 https://a] ", RunStatus.SUCCESS, "+1 分"),
            ("[2/2 https://b] ", RunStatus.SUCCESS, "+2 分"),
        ]
    )
    assert status == RunStatus.SUCCESS
    assert "2/2" in message


def test_summarize_results_all_noneed():
    status, message = summarize_results(
        [
            ("[1/2 https://a] ", RunStatus.NONEED, "已签到"),
            ("[2/2 https://b] ", RunStatus.NONEED, "已签到"),
        ]
    )
    assert status == RunStatus.NONEED
    assert "2" in message


def test_summarize_results_fails_when_any_site_fails():
    # 部分失败必须整体判失败, 否则静默漏签不会被通知暴露
    status, message = summarize_results(
        [
            ("[1/2 https://a] ", RunStatus.SUCCESS, "+1 分"),
            ("[2/2 https://b] ", RunStatus.FAIL, "登录已失效"),
        ]
    )
    assert status == RunStatus.FAIL
    assert "1/2 成功" in message
    assert "https://b" in message


def test_start_runs_each_site_and_aggregates(monkeypatch):
    """多站点时 start() 应逐个站点执行, 并在结束时聚合结果."""
    accounts = [
        {"url": "a.example.com", "username": "u1", "password": "p1"},
        {"url": "b.example.com", "username": "u2", "password": "p2"},
    ]
    inst = make_checkin({"accounts": accounts})
    inst.ctx = RunContext.prepare("test")
    inst.log = logger.bind(name="test")

    seen = []

    async def fake_run_single(global_config, label):
        seen.append((label, inst.base_url, inst.username))
        if inst.username == "u1":
            return RunStatus.SUCCESS, "+1 分"
        return RunStatus.FAIL, "登录已失效"

    monkeypatch.setattr(inst, "_run_single", fake_run_single)
    # finish() 会把结果写进 cache, 单元测试里没有初始化全局 config, 直接跳过落盘
    monkeypatch.setattr(RunContext, "save", lambda self: None)
    ctx = asyncio.run(inst.start())

    assert [site for _, site, _ in seen] == ["https://a.example.com", "https://b.example.com"]
    assert seen[0][0].startswith("[1/2 ")
    assert ctx.status == RunStatus.FAIL
    assert "1/2 成功" in ctx.status_info
