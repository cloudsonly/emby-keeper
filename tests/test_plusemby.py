"""結束バンド (plusemby) 网页签到器的纯逻辑测试.

只覆盖可独立验证的纯函数: 状态分类、验证配置解析、URL 归一化.
网络流程依赖真实站点, 不在单元测试范围内.
"""

from embykeeper.runinfo import RunStatus
from embykeeper.telegram.checkiner.plusemby import (
    ACCESS_BLOCKED_MESSAGE,
    DEFAULT_BASE_URL,
    build_login_payload,
    classify_checkin_response,
    classify_status_response,
    extract_message,
    normalize_base_url,
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
