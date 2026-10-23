"""本地转写通道的回归：判可疑、拼 prompt、模型缓存、payload 与字幕同形。

**为什么这些能测**：转写的重活（模型推理）走 `factory` 注入，测的是"我们这侧的约定"——
判据口径、payload 形状、与 `parse_track` 的接缝。模型本身准不准不在这里管。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from b2textify.config import WhisperCfg
from b2textify.extract.asr import (
    build_prompt,
    clear_model_cache,
    is_flagged,
    load_model,
    transcribe,
)
from b2textify.extract.subtitle import parse_track


class _Seg:
    def __init__(self, start: float, end: float, text: str, avg_logprob: float = -0.2, no_speech_prob: float = 0.1):
        self.start, self.end, self.text = start, end, text
        self.avg_logprob, self.no_speech_prob = avg_logprob, no_speech_prob


class _Info:
    duration = 10.0
    language = "zh"
    language_probability = 0.99


class _Model:
    def __init__(self, segments: list[_Seg]) -> None:
        self._segments = segments
        self.calls: list[tuple[str, dict[str, object]]] = []

    def transcribe(self, path: str, **kwargs: object):
        self.calls.append((path, kwargs))
        return iter(self._segments), _Info()


@pytest.fixture(autouse=True)
def _clean_models():
    clear_model_cache()
    yield
    clear_model_cache()


def test_is_flagged_uses_both_signals() -> None:
    cfg = WhisperCfg()
    assert is_flagged(-0.9, 0.1, cfg), "低 logprob 要标"
    assert is_flagged(-0.1, 0.9, cfg), "高 no_speech 也要标 —— 只看 logprob 会漏掉背景嘈杂的硬编"
    assert not is_flagged(-0.1, 0.1, cfg)


def test_build_prompt_puts_hotwords_title_and_desc() -> None:
    cfg = WhisperCfg(hotwords=["光栅化", "切线空间"])
    prompt = build_prompt(cfg, title="第 3 讲 渲染管线", extra="本节讲延迟着色\n以及 MSAA")
    assert "光栅化" in prompt and "切线空间" in prompt
    assert "渲染管线" in prompt
    assert "\n" not in prompt, "换行必须压掉：whisper 的 prompt 要单行"


def test_load_model_caches_per_config() -> None:
    calls: list[tuple] = []

    def factory(*args: object, **kwargs: object) -> object:
        calls.append((args, kwargs))
        return object()

    cfg = WhisperCfg(model="medium", device="cuda", compute_type="float16")
    first = load_model(cfg, factory=factory)
    second = load_model(cfg, factory=factory)
    assert first is second, "同一档位/设备/精度必须复用，重复加载纯属浪费"
    assert len(calls) == 1

    clear_model_cache()
    load_model(WhisperCfg(model="small"), factory=factory)
    assert len(calls) == 2, "换档位要重新加载"


def test_transcribe_payload_is_subtitle_shaped(tmp_path: Path) -> None:
    audio = tmp_path / "a.m4a"
    audio.write_bytes(b"\0")
    model = _Model(
        [
            _Seg(0.0, 1.5, "第一句"),
            _Seg(1.5, 3.0, "第二句", avg_logprob=-1.2),
            _Seg(3.0, 4.0, "   "),  # 空白段要丢掉，不能占位
        ]
    )
    cfg = WhisperCfg(model="medium")
    payload = transcribe(audio, cfg, prompt="以下是普通话的句子。", factory=lambda *a, **k: model)

    assert payload["lang"] == "zh"
    assert [item["content"] for item in payload["body"]] == ["第一句", "第二句"]
    assert payload["asr"]["segments"] == 2
    assert payload["asr"]["flagged"] == 1
    assert payload["asr"]["model"] == "medium"
    # prompt 与 VAD 开关必须真的传下去：VAD 不开会把训练语料口播硬编到纯音乐上
    _, kwargs = model.calls[0]
    assert kwargs["vad_filter"] is True
    assert kwargs["initial_prompt"] == "以下是普通话的句子。"
    # 默认不指定语言（= 自动检测）：写死 zh 会把英文视频的元信息标成中文
    assert kwargs["language"] is None

    # 接缝：同一份 parse_track 能吃转写产物，并把 avg_logprob 读成 confidence / flagged
    track = parse_track(payload, source="whisper", lang=str(payload["lang"]))
    assert track.source == "whisper"
    assert [segment.text for segment in track.segments] == ["第一句", "第二句"]
    assert track.segments[0].confidence == pytest.approx(-0.2)
    assert track.segments[1].flagged is True
    assert track.segments[0].flagged is False


def test_empty_transcript_is_warned_not_silent(tmp_path: Path, caplog) -> None:
    audio = tmp_path / "a.m4a"
    audio.write_bytes(b"\0")
    model = _Model([])
    with caplog.at_level("WARNING"):
        payload = transcribe(audio, WhisperCfg(), factory=lambda *a, **k: model)
    assert payload["body"] == []
    assert any("转写结果为空" in record.getMessage() for record in caplog.records)
