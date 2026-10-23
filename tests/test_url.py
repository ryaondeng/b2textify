from __future__ import annotations

import pytest

from b2textify.errors import UsageError
from b2textify.sources.bilibili import is_bilibili, parse_url


def test_parse_full_url_with_page() -> None:
    bvid, page = parse_url("https://www.bilibili.com/video/BV1i3eJ6KEkb?p=3&vd_source=x")
    assert (bvid, page) == ("BV1i3eJ6KEkb", 3)


def test_page_one_means_unspecified() -> None:
    """浏览器落在第一 P 会带 ?p=1 —— 那不是"只要第一 P"的意思。"""
    assert parse_url("https://www.bilibili.com/video/BV1i3eJ6KEkb?p=1")[1] == 0


def test_bare_bvid_is_accepted() -> None:
    assert parse_url("BV1i3eJ6KEkb") == ("BV1i3eJ6KEkb", 0)


def test_short_link_is_rejected_with_actionable_hint() -> None:
    with pytest.raises(UsageError) as excinfo:
        parse_url("https://b23.tv/abcd123")
    assert excinfo.value.code == "SHORT_LINK_UNSUPPORTED"
    assert "地址栏" in (excinfo.value.hint or "")


def test_other_host_is_not_bilibili() -> None:
    with pytest.raises(UsageError) as excinfo:
        parse_url("https://www.zhihu.com/question/123/answer/456")
    assert excinfo.value.code == "NOT_BILIBILI"
    assert is_bilibili("https://www.zhihu.com/question/123") is False


def test_expired_credential_fails_early_with_actionable_message(monkeypatch) -> None:
    """过期 token 打 player 接口时 B 站回 412（不是 -101），光看状态码会误判成风控。
    所以取文本前先跑 nav 自检，把失败挪到最前面。"""
    from types import SimpleNamespace

    from b2textify.config import Config
    from b2textify.errors import CredentialMissing
    from b2textify.sources.bilibili import BilibiliSource

    source = BilibiliSource(Config())
    monkeypatch.setattr(
        source.signer, "keys", lambda: SimpleNamespace(logged_in=False, nav_code=-101)
    )
    with pytest.raises(CredentialMissing) as excinfo:
        source._check_credential()
    assert "nav" in excinfo.value.message
    assert "-101" in excinfo.value.message
    assert "SESSDATA" in (excinfo.value.hint or "")


def test_valid_credential_passes_the_check(monkeypatch) -> None:
    from types import SimpleNamespace

    from b2textify.config import Config
    from b2textify.sources.bilibili import BilibiliSource

    source = BilibiliSource(Config())
    monkeypatch.setattr(
        source.signer, "keys", lambda: SimpleNamespace(logged_in=True, nav_code=0)
    )
    assert source._check_credential() is None
