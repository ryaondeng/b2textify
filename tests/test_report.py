from __future__ import annotations

from b2textify.models import Anchor, Block, TextResult
from b2textify.report import render


def _result(**overrides) -> TextResult:
    base = dict(
        url="https://www.bilibili.com/video/BV1i3eJ6KEkb",
        bvid="BV1i3eJ6KEkb",
        title="中国小说的起源",
        author="某频道",
        duration=612.0,
        fetched_at="2026-10-23T10:00:00+08:00",
        blocks=[
            Block(block_id="b-000001", text="中国近代以来我们熟悉的小说。", anchor=Anchor.video(3.0)),
            Block(block_id="b-000002", text="三要素：人物、情节、环境。", anchor=Anchor.video(65.0)),
        ],
        tracks=[{"source": "ai_subtitle", "lang": "ai-zh", "chars": 16394, "chosen": True}],
        subtitle_lang="ai-zh",
        text_source="ai_subtitle",
    )
    base.update(overrides)
    return TextResult(**base)


def test_head_reports_where_the_text_came_from() -> None:
    text = render(_result())
    assert "正文取自：平台 AI 字幕（ai-zh）" in text
    assert "| ✓ | ai_subtitle | ai-zh | 16394 |" in text


def test_body_carries_timestamps() -> None:
    text = render(_result())
    assert "[00:03] 中国近代以来我们熟悉的小说。" in text
    assert "[01:05] 三要素：人物、情节、环境。" in text


def test_degradation_is_visible_not_silent() -> None:
    text = render(_result(warnings=["没有命中偏好语言，已退回按字数择优"], subtitle_lang="ai-es"))
    assert "取轨时发生降级" in text
    assert "没有命中偏好语言" in text
