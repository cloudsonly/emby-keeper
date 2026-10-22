"""保活账号的客户端信息可以只配一次.

背景: 几十个 Emby 账号每个都要写 device / client / client_version / useragent /
device_id 五行, 而它们通常是同一台"手机"的指纹. 现在可以在 [emby] 段统一配置,
账号未单独指定时自动继承; 账号级配置仍然优先.
"""

import pytest

from embykeeper.schema import EmbyConfig, SubsonicConfig

CLIENT = {
    "device": "Pixel 7",
    "client": "Fileball",
    "client_version": "1.2.3",
    "useragent": "Fileball/1.2.3",
    "device_id": "0123456789abcdef0123456789abcdef",
}


def build(accounts, **global_client):
    return EmbyConfig.model_validate(
        {
            "concurrency": 3,
            **global_client,
            "account": accounts,
        }
    )


def test_global_client_info_applies_to_all_accounts():
    cfg = build(
        [
            {"url": "https://a.example.com", "username": "u1", "password": "p1"},
            {"url": "https://b.example.com", "username": "u2", "password": "p2"},
        ],
        **CLIENT,
    )
    for account in cfg.account:
        assert account.device == "Pixel 7"
        assert account.client == "Fileball"
        assert account.client_version == "1.2.3"
        assert account.useragent == "Fileball/1.2.3"
        assert account.device_id == "0123456789abcdef0123456789abcdef"


def test_account_level_value_wins():
    cfg = build(
        [
            {"url": "https://a.example.com", "username": "u1", "password": "p1"},
            {
                "url": "https://b.example.com",
                "username": "u2",
                "password": "p2",
                "device": "Pixel 7 Pro",
            },
        ],
        **CLIENT,
    )
    assert cfg.account[0].device == "Pixel 7"
    # 账号里写了的字段不被全局值覆盖, 没写的字段照常继承
    assert cfg.account[1].device == "Pixel 7 Pro"
    assert cfg.account[1].client == "Fileball"


def test_no_global_value_leaves_account_untouched():
    """不配全局值时保持原行为: 留空交给运行时生成并缓存."""
    cfg = build([{"url": "https://a.example.com", "username": "u1", "password": "p1"}])
    assert cfg.account[0].device is None
    assert cfg.account[0].client is None
    assert cfg.account[0].device_id is None


def test_empty_account_list_is_fine():
    cfg = EmbyConfig.model_validate({**CLIENT})
    assert cfg.account == []


def test_unknown_field_still_rejected():
    """全局客户端字段是显式声明的, 拼错仍然报错, 不会静默失效."""
    with pytest.raises(Exception) as exc:
        EmbyConfig.model_validate({"device_name": "iPhone"})
    assert "未知设置项" in str(exc.value)


def test_subsonic_global_client_info():
    cfg = SubsonicConfig.model_validate(
        {
            "client": "Symfonium",
            "client_version": "5.6.0",
            "useragent": "Symfonium/5.6.0",
            "account": [{"url": "https://s.example.com", "username": "u1", "password": "p1"}],
        }
    )
    assert cfg.account[0].client == "Symfonium"
    assert cfg.account[0].client_version == "5.6.0"
    assert cfg.account[0].useragent == "Symfonium/5.6.0"
