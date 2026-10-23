"""移植件的核心行为：**语言优先于字数**。

这是本项目最贵的一条教训：B 站同一视频的中文原轨 16,394 字、西语译文轨 86,229 字，
纯按字数择优必然选中译文轨，而且事后看不出正文是译文。
"""

from __future__ import annotations

from b2textify.extract.subtitle import Segment, Track, pick_track, track_candidates

PREFER = ["cc", "ai_subtitle", "whisper"]


def _track(lang: str, chars: int, source: str = "ai_subtitle") -> Track:
    return Track(source=source, lang=lang, segments=[Segment(0.0, 1.0, "字" * chars)])


def test_language_beats_char_count() -> None:
    zh = _track("ai-zh", 100)
    es = _track("ai-es", 5000)
    assert pick_track([es, zh], PREFER, ["ai-zh", "zh*"]).lang == "ai-zh"


def test_empty_preference_keeps_old_behaviour() -> None:
    zh = _track("ai-zh", 100)
    es = _track("ai-es", 5000)
    assert pick_track([es, zh], PREFER, []).lang == "ai-es"


def test_wildcard_and_casefold() -> None:
    hans = _track("zh-Hans", 100, source="cc")
    en = _track("en", 900, source="cc")
    assert pick_track([en, hans], PREFER, ["zh*"]).lang == "zh-Hans"
    upper = _track("AI-ZH", 100)
    english_ai = _track("ai-en", 900)
    assert pick_track([english_ai, upper], PREFER, ["ai-zh"]).lang == "AI-ZH"


def test_source_tier_outranks_language() -> None:
    """官方 CC 在英文、AI 字幕在中文时，**取官方轨**：语言是档内择优，不是跨档夺权。

    这条刻意固定：官方字幕的可信度高于任何机器轨；语言偏好只解决"同一档里选哪条"。
    """
    english_cc = _track("en", 100, source="cc")
    chinese_ai = _track("ai-zh", 900, source="ai_subtitle")
    assert pick_track([chinese_ai, english_cc], PREFER, ["ai-zh"]).lang == "en"


def test_candidates_are_ranked_and_marked() -> None:
    report = track_candidates([_track("ai-es", 5000), _track("ai-zh", 100)], PREFER, ["ai-zh"])
    assert [item["lang"] for item in report] == ["ai-zh", "ai-es"]
    assert report[0]["chosen"] is True and report[1]["chosen"] is False
