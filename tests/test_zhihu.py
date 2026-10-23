"""知乎通道的回归：链接解析、HTML 切块、凭证错误的翻译、正文落块。

网络一律用假客户端替掉 —— 这里测的是"我们这侧的约定"：
支持哪种链接、`-zse` 拿不到时怎么报、401 必须翻成"去换 z_c0"而不是"没内容"。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from b2textify.config import Config
from b2textify.errors import CredentialMissing, HttpError, NotImplementedYet, UsageError
from b2textify.extract.html_text import blocks_from_html, split_blocks
from b2textify.http import HttpResponse
from b2textify.sources.zhihu import ZhihuSource, is_zhihu, parse_url

ANSWER_URL = "https://www.zhihu.com/question/2095655093907482113/answer/2096724148315856915"


# -- 链接 ------------------------------------------------------------------
@pytest.mark.parametrize(
    "url",
    [
        ANSWER_URL,
        ANSWER_URL + "?utm_source=copy",
        "https://zhihu.com/question/1/answer/2",
        "https://www.zhihu.com/question/1/answer/2/",
    ],
)
def test_parse_url_accepts_answer_links(url: str) -> None:
    question_id, answer_id = parse_url(url)
    assert question_id.isdigit() and answer_id.isdigit()


def test_parse_url_rejects_question_and_zhuanlan() -> None:
    with pytest.raises(UsageError) as question:
        parse_url("https://www.zhihu.com/question/2095655093907482113")
    assert question.value.code == "ANSWER_ID_MISSING"

    with pytest.raises(NotImplementedYet) as zhuanlan:
        parse_url("https://zhuanlan.zhihu.com/p/123456789")
    assert zhuanlan.value.code == "ZHUANLAN_UNSUPPORTED"


def test_is_zhihu_only_zhihu_hosts() -> None:
    assert is_zhihu(ANSWER_URL)
    assert not is_zhihu("https://www.bilibili.com/video/BV1i3eJ6KEkb")
    assert not is_zhihu("https://zhihu.com.evil.example/question/1/answer/2")


# -- HTML 切块 -------------------------------------------------------------
def test_split_blocks_keeps_paragraphs_and_drops_scripts() -> None:
    html = (
        "<p>第一段</p><p>第二段 <b>加粗</b> 结尾</p>"
        "<script>alert(1)</script>"
        "<blockquote><p>引用的话</p></blockquote>"
        "<h3>小标题</h3><ul><li>条目一</li><li>条目二</li></ul>"
        "<p>换<br>行</p>"
    )
    blocks = split_blocks(html)
    texts = [text for _, text in blocks]
    assert "第一段" in texts
    assert "第二段 加粗 结尾" in texts, "行内标签不能把文字切碎"
    assert "引用的话" in texts
    assert "小标题" in texts
    assert ("heading", "小标题") in blocks, "h1–h6 要标成 heading"
    assert "条目一" in texts and "条目二" in texts
    assert "换 行" in texts, "`<br>` 要变成空格，不能粘成一个词"
    assert not any("alert" in text for text in texts), "script 内容必须整段丢掉"


def test_blocks_from_html_numbers_blocks_without_fake_anchors() -> None:
    blocks = blocks_from_html("<p>一</p><p>二</p>")
    assert [block.block_id for block in blocks] == ["b-000001", "b-000002"]
    assert all(block.anchor is None for block in blocks), "知乎没有时间轴，不许编锚点"


# -- 取数 ------------------------------------------------------------------
class _FakeClient:
    """替掉 HttpClient：只实现 `get`，按 URL 返回预置响应。"""

    def __init__(self, payload: dict | None = None, *, status: int = 200, text: str | None = None):
        self.payload = payload
        self.status = status
        self.text = text
        self.calls: list[str] = []

    def get(self, url, *, params=None, policy=None, headers=None, max_bytes=None):
        self.calls.append(url)
        body = self.text if self.text is not None else json.dumps(self.payload or {}, ensure_ascii=False)
        return HttpResponse(url=url, status=self.status, content=body.encode("utf-8"))


class _Cfg(Config):
    """带假凭证的配置（真实路径读环境变量，测试里不碰进程环境）。"""

    def credential(self, host: str) -> str:
        return "z-c0-token" if host.endswith("zhihu.com") else ""

    def cookie_header(self, host: str) -> str:
        return f"z_c0={self.credential(host)}" if host.endswith("zhihu.com") else ""


def _config(tmp_path: Path) -> Config:
    return _Cfg(cache_dir=tmp_path / "cache")


def test_fetch_requires_credential(tmp_path: Path) -> None:
    cfg = Config(cache_dir=tmp_path / "cache")
    with pytest.raises(CredentialMissing) as excinfo:
        ZhihuSource(cfg, client=_FakeClient({})).fetch(ANSWER_URL)
    assert excinfo.value.code == "AUTH_MISSING"
    assert "B2T_ZHIHU_COOKIE" in str(excinfo.value)


def test_fetch_builds_result_and_archives_json(tmp_path: Path) -> None:
    payload = {
        "content": "<p>第一段</p><p>第二段<b>重点</b></p><img src='x.png'>",
        "question": {"title": "问题标题"},
        "author": {"name": "某作者"},
        "voteup_count": 1711,
    }
    cfg = _config(tmp_path)
    client = _FakeClient(payload)
    result = ZhihuSource(cfg, client=client).fetch(ANSWER_URL)

    assert result.slug == "zhihu-2096724148315856915"
    assert result.title == "问题标题"
    assert result.author == "某作者"
    assert result.text_source == "html"
    assert [block.text for block in result.blocks][:2] == ["第一段", "第二段重点"]
    assert result.chars == len("第一段") + len("第二段重点")
    assert any("图片" in warning for warning in result.warnings), "图片跳过要说出来"
    archived = (tmp_path / "cache" / "raw" / result.slug / "answer.json").is_file()
    assert archived, "原始 JSON 要归档（可离线复查）"
    assert "include=content" in client.calls[0]


def test_fetch_maps_401_to_auth_expired(tmp_path: Path) -> None:
    cfg = _config(tmp_path)

    class _Denied(_FakeClient):
        def get(self, url, **kwargs):
            raise HttpError(code="HTTP_STATUS", message="HTTP 401", detail={"status": 401})

    with pytest.raises(CredentialMissing) as excinfo:
        ZhihuSource(cfg, client=_Denied({})).fetch(ANSWER_URL)
    assert excinfo.value.code == "AUTH_EXPIRED"
    assert "z_c0" in str(excinfo.value), "错误里必须说清是去换 z_c0"


def test_fetch_reports_empty_answer(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    with pytest.raises(Exception) as excinfo:
        ZhihuSource(cfg, client=_FakeClient({"content": "  ", "question": {"title": "x"}})).fetch(ANSWER_URL)
    assert getattr(excinfo.value, "code", "") == "ZHIHU_EMPTY_ANSWER"
