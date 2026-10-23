"""中文译文的回归：解析 → 分块 → 校验 → 回写。

翻译本身要联网，所以模型调用一律 monkeypatch 掉 —— 这里测的是"我们这侧的约定"：
时间锚与低置信标记不能丢、块不能跨 P、模型少给一条必须报错而不是悄悄接受。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from b2textify.config import Config
from b2textify.errors import SourceError, UsageError
from b2textify.llm import LlmCfg
from b2textify import translate

SAMPLE = """# 测试视频

> 来源：https://www.bilibili.com/video/BV1xxxxxxxxx
> 正文取自：本地转写（en）

## 正文

[00:00] First line of the talk.
[00:07] Second line, with a term like gradient descent.
（低置信占位）
[00:12] Third line.
### 第二讲
[00:20] Fourth line after heading.
"""


def _write(tmp_path: Path, text: str = SAMPLE) -> Path:
    path = tmp_path / "BV1xxxxxxxxx-测试视频.md"
    path.write_text(text, encoding="utf-8")
    return path


def test_parse_keeps_structure_and_flags(tmp_path: Path) -> None:
    document = translate.parse_markdown(_write(tmp_path).read_text(encoding="utf-8"))
    kinds = [line.kind for line in document.lines]
    assert kinds.count("text") == 4
    assert "heading" in kinds  # `### 第二讲`
    stamps = [line.stamp for line in document.lines if line.kind == "text"]
    assert stamps == ["00:00", "00:07", "00:12", "00:20"]


def test_parse_rejects_non_report_file(tmp_path: Path) -> None:
    path = tmp_path / "随便.md"
    path.write_text("# 标题\n\n正文而已\n", encoding="utf-8")
    with pytest.raises(UsageError) as excinfo:
        translate.parse_markdown(path.read_text(encoding="utf-8"))
    assert "## 正文" in str(excinfo.value)


def test_low_confidence_marker_is_preserved(tmp_path: Path) -> None:
    text = SAMPLE.replace("（低置信占位）", "")
    text = text.replace("[00:12] Third line.", "[00:12] Third line.（低置信）")
    document = translate.parse_markdown(text)
    third = [line for line in document.lines if line.kind == "text"][2]
    assert third.flagged is True, "标记要摘下来参与翻译"
    assert third.text == "Third line."

    rendered = translate.render_translated(
        document, {}, model="deepseek-chat", source_name="x.md", now="2026-10-23T00:00:00"
    )
    assert "[00:12] Third line.（低置信）" in rendered, "回写时必须补回来"


def test_split_chunks_breaks_at_heading_and_limit(tmp_path: Path) -> None:
    document = translate.parse_markdown(_write(tmp_path).read_text(encoding="utf-8"))
    chunks = translate.split_chunks(document, limit=10_000)
    assert len(chunks) == 2, "`### 第二讲` 必须断开，不能跨 P 合并"
    assert chunks[0] and chunks[1]

    small = translate.split_chunks(document, limit=30)
    assert len(small) > len(chunks), "字符上限要真的生效"


def test_parse_response_accepts_list_object_and_fence() -> None:
    fenced = '```json\n{"lines": [{"id": 0, "zh": "一"}, {"id": 1, "zh": "二"}]}\n```'
    assert translate._parse_response(fenced, expected=2) == {0: "一", 1: "二"}
    bare = json.dumps([{"id": 0, "zh": "一"}], ensure_ascii=False)
    assert translate._parse_response(bare, expected=1) == {0: "一"}

    with pytest.raises(ValueError, match="漏了"):
        translate._parse_response(json.dumps({"lines": [{"id": 0, "zh": "一"}]}), expected=2)


def test_translate_chunk_retries_then_fails_loudly(tmp_path: Path, monkeypatch) -> None:
    document = translate.parse_markdown(_write(tmp_path).read_text(encoding="utf-8"))
    chunk = translate.split_chunks(document, limit=10_000)[0]
    calls: list[int] = []

    def fake_chat(messages, *, cfg=None, json_mode=False):
        calls.append(1)
        return json.dumps({"lines": []})  # 永远漏条

    monkeypatch.setattr(translate, "chat", fake_chat)
    with pytest.raises(SourceError) as excinfo:
        translate.translate_chunk(document, chunk, llm=LlmCfg(api_key="x"))
    assert len(calls) == translate.CHUNK_ATTEMPTS, "必须重试到上限"
    assert "TRANSLATE_FAILED" == excinfo.value.code


def test_translate_file_writes_zh_beside_original(tmp_path: Path, monkeypatch) -> None:
    path = _write(tmp_path)
    seen: list[list[dict]] = []

    def fake_chat(messages, *, cfg=None, json_mode=False):
        payload = json.loads(messages[1]["content"])
        seen.append(payload["lines"])
        return json.dumps(
            {"lines": [{"id": item["id"], "zh": f"中文{item['id']}"} for item in payload["lines"]]},
            ensure_ascii=False,
        )

    monkeypatch.setattr(translate, "chat", fake_chat)
    target = translate.translate_file(
        path,
        cfg=Config(cache_dir=tmp_path / "cache"),
        llm=LlmCfg(api_key="x"),
        workers=1,
    )

    assert target.name == f"{path.stem}-zh.md"
    text = target.read_text(encoding="utf-8")
    assert "> 中文译文：" in text and "> 译自：" in text
    assert "[00:00] 中文0" in text
    assert "### 第二讲" in text, "P 标题要保留"
    assert "## 正文" in text
    assert seen and all(seen), "每一块都要真的发出去"

    # 缓存：同一份输入再跑一遍不应再请求模型
    def boom(*args, **kwargs):
        raise AssertionError("命中缓存时不该再调模型")

    monkeypatch.setattr(translate, "chat", boom)
    translate.translate_file(path, cfg=Config(cache_dir=tmp_path / "cache"), llm=LlmCfg(api_key="x"), workers=1)


def test_translate_file_skips_chinese(tmp_path: Path) -> None:
    chinese = "# 中文视频\n\n## 正文\n\n[00:00] 这是一段已经是中文的正文内容。\n"
    path = tmp_path / "zh.md"
    path.write_text(chinese, encoding="utf-8")
    with pytest.raises(UsageError) as excinfo:
        translate.translate_file(path, cfg=Config(cache_dir=tmp_path / "cache"), llm=LlmCfg(api_key="x"))
    assert excinfo.value.code == "ALREADY_CHINESE"
