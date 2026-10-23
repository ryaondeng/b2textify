"""本地转写（faster-whisper）：音频 → 与字幕**同形**的文本单元。

**为什么产物故意做成字幕的形状**：`extract/subtitle.py::parse_track` 一份解析同时吃两边，
`blocks_from_track` 也不用改。转写多带一个 `avg_logprob`（置信度），它被读成
`Segment.confidence` → `Block.confidence`（低置信度片段在正文里可查）。

## 三个真问题，各自的处理

| 问题 | 处理 |
|---|---|
| 一段音频要转很多次（77 P 的视频 = 77 次） | 模型**进程内缓存**（`_MODELS`）：重复加载一次几秒纯属浪费 |
| 没有语音时**会幻觉**（实测：音乐视频转出"请不吝点赞订阅转发打赏支持…"，`logprob −1.26`） | ① `vad_filter` 先切掉非语音；② 低 `avg_logprob` / 高 `no_speech_prob` 的片段照样**保留**，但标 `flagged`（**不静默丢弃** —— 丢掉就没人知道那里出过事） |
| 缺 CUDA 运行库时报 `Library cublas64_12.dll is not found`（对用户毫无指向性） | 认出这条错误并翻译成"换成 CPU 跑"的可执行提示 |

## 热词只能靠标题与简介

转写时 blocks 还**不存在**，所以 `initial_prompt` 的热词来源只能是：视频标题 + UP 主简介 +
配置里的领域词。想在转写阶段就用上"正文里发现的词"是做不到的（那正是转写要产出的东西）。

## 模型从哪来

`faster-whisper` 走 HuggingFace 缓存（默认落在 `~/.cache/huggingface`）。如果这台机器上
已经有别的项目下过 medium，把 `HF_HUB_CACHE` 指到那个缓存目录就不用再下 1.5G
—— 写法见 `.env.example`。
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any

from ..config import WhisperCfg
from ..errors import DependencyMissing

#: 进程内模型缓存，键是（模型档位, 设备, 精度）。测试要能清空它。
_MODELS: dict[tuple[str, str, str], Any] = {}
#: `initial_prompt` 的长度上限（whisper 侧约 224 token）：给标题/简介留的字符预算
PROMPT_TITLE_CHARS = 120
PROMPT_EXTRA_CHARS = 200
#: 进度日志的步长（每过 25% 打一行，别把长视频变成黑洞）
PROGRESS_STEP = 0.25


def load_model(cfg: WhisperCfg, *, factory: Any = None) -> Any:
    """按配置取（并缓存）模型。`factory` 只为测试注入。"""
    if factory is None:
        try:
            from faster_whisper import WhisperModel  # noqa: PLC0415 - 重依赖，按需导入
        except ImportError as exc:
            raise DependencyMissing(
                message="本地转写需要 faster-whisper",
                hint='装它：pip install -e ".[asr]"（GPU 还要 CUDA 运行库，见 README）',
            ) from exc
        factory = WhisperModel

    key = (cfg.model, cfg.device, cfg.compute_type)
    if key not in _MODELS:
        _MODELS[key] = factory(cfg.model, device=cfg.device, compute_type=cfg.compute_type)
    return _MODELS[key]


def clear_model_cache() -> None:
    """丢弃缓存（测试用）。"""
    _MODELS.clear()


def build_prompt(cfg: WhisperCfg, *, title: str = "", extra: str = "") -> str:
    """拼 `initial_prompt`：引导句 + 配置热词 + 视频标题 + 简介。

    这里**不做**术语纠错 —— 转写只负责"尽量听对"，写法统一留给整理环节。
    """
    parts = [cfg.initial_prompt_prefix.strip()]
    if cfg.hotwords:
        parts.append("、".join(word.strip() for word in cfg.hotwords if word.strip()))
    if title.strip():
        parts.append(f"视频标题：{title.strip()[:PROMPT_TITLE_CHARS]}")
    if extra.strip():
        parts.append(extra.strip()[:PROMPT_EXTRA_CHARS].replace("\n", " "))
    return " ".join(part for part in parts if part)


def is_flagged(avg_logprob: float, no_speech_prob: float, cfg: WhisperCfg) -> bool:
    """这条片段要不要被标可疑。两个信号任一命中就算（与 `WhisperCfg` 对齐）。"""
    return avg_logprob < cfg.low_logprob or no_speech_prob > cfg.high_no_speech


def transcribe(
    audio: Path,
    cfg: WhisperCfg,
    *,
    prompt: str = "",
    logger: logging.Logger | None = None,
    factory: Any = None,
) -> dict[str, Any]:
    """转写一个音频，返回**可直接归档**的 payload。

    payload 形状与 B 站字幕同形（`{"lang": …, "body": [{"from","to","content"}]}`），
    另加 `asr` 元信息（模型 / 参数 / 耗时 / 可疑段数）—— 转写结果的可追溯性靠它：
    "这段文字是哪个模型的哪次运行产出的"必须能回答。
    """
    log = logger or logging.getLogger("b2textify.asr")
    model = load_model(cfg, factory=factory)

    started = time.monotonic()
    try:
        segments, info = model.transcribe(
            str(audio),
            # 空 = 让 whisper 自己判（B 站视频不都是中文；写死 zh 会把外语视频标错，
            # 见 `WhisperCfg.language` 的注释）
            language=cfg.language or None,
            initial_prompt=prompt or None,
            vad_filter=cfg.vad_filter,
        )
        body: list[dict[str, Any]] = []
        flagged = 0
        duration = float(getattr(info, "duration", 0.0) or 0.0)
        milestone = 0.0
        for segment in segments:  # 生成器：边转边出，所以能报进度
            text = (segment.text or "").strip()
            if not text:
                continue
            avg_logprob = round(float(segment.avg_logprob), 4)
            no_speech_prob = round(float(segment.no_speech_prob), 4)
            item = {
                "from": round(float(segment.start), 3),
                "to": round(float(segment.end), 3),
                "content": text,
                "avg_logprob": avg_logprob,
                "no_speech_prob": no_speech_prob,
                #: **存疑判据在这里定死**（低 logprob 或高 no_speech，见 `is_flagged`）——
                #: 下游只认这个标记，不再各判一遍。实测教训：体育解说 10 段 no_speech 高
                #: （背景欢呼）而 avg_logprob 全部正常 → 只看 logprob 会漏掉"硬编话"的片段。
                "flagged": is_flagged(avg_logprob, no_speech_prob, cfg),
            }
            body.append(item)
            if item["flagged"]:
                flagged += 1
            milestone = _log_progress(log, segment.end, duration, milestone, started)
    except RuntimeError as exc:
        raise _translate_runtime_error(exc) from exc
    elapsed = time.monotonic() - started

    if not body:
        # 空转写是**要报出来的**：它意味着这段音频没有可用语音（或 VAD 全切了）。
        # 静默返回空表会让调用方以为"转写成功但内容少"。
        log.warning("转写结果为空（模型 %s，%.1fs 音频）—— 可能是纯音乐/静音，或 VAD 全切掉了", cfg.model, duration)

    return {
        "schema_version": 1,
        "kind": "asr",
        "lang": str(getattr(info, "language", "") or cfg.language),
        "asr": {
            "model": cfg.model,
            "device": cfg.device,
            "compute_type": cfg.compute_type,
            "vad_filter": cfg.vad_filter,
            "prompt": prompt,
            "language_probability": round(float(getattr(info, "language_probability", 0.0) or 0.0), 4),
            "audio_seconds": round(duration, 3),
            "elapsed_seconds": round(elapsed, 3),
            "realtime_factor": round(elapsed / duration, 4) if duration else None,
            "low_logprob": cfg.low_logprob,
            "high_no_speech": cfg.high_no_speech,
            "segments": len(body),
            "flagged": flagged,
        },
        "body": body,
    }


def _log_progress(log: logging.Logger, end: float, duration: float, milestone: float, started: float) -> float:
    """按 25% 打一行进度（含剩余时间估计）。返回新的里程碑。"""
    if duration <= 0:
        return milestone
    ratio = min(1.0, max(0.0, end / duration))
    if ratio < milestone + PROGRESS_STEP:
        return milestone
    elapsed = time.monotonic() - started
    remaining = (elapsed / ratio - elapsed) if ratio > 0 else 0.0
    log.info(
        "转写中 %d%%（已用 %.0f s，预计还剩 %.0f s）",
        round(ratio * 100),
        elapsed,
        max(0.0, remaining),
    )
    return milestone + PROGRESS_STEP


def _translate_runtime_error(exc: RuntimeError) -> Exception:
    """把"缺 CUDA 运行库"这类无法自查的报错翻成能照做的提示。

    原文（`Library cublas64_12.dll is not found or cannot be loaded`）对用户没有指向性 ——
    它既没说缺什么包，也没说怎么办。而这条**只在推理那一刻**才炸（导入时不炸）。
    """
    message = str(exc)
    lowered = message.lower()
    if "cublas" in lowered or "cudnn" in lowered or "cuda" in lowered:
        return DependencyMissing(
            code="ASR_RUNTIME",
            message=f"转写时 CUDA 运行库加载失败：{message}",
            hint=(
                "先确认装了 CUDA 版依赖（pip install -e \".[asr]\" + ctranslate2 的 CUDA 运行库）。"
                "想先跑通也可以换 CPU：在 b2textify.toml 写 `[whisper]` → `device = \"cpu\"`、"
                "`compute_type = \"int8\"` —— 慢，但不依赖 CUDA"
            ),
            detail={"error": message[:400]},
        )
    return exc


def summarize(payload: dict[str, Any]) -> str:
    """一行摘要（日志与输出元信息用）：这段文字是怎么转出来的。

    长视频转一次要好几分钟，用户事后要能回答"这稿子是哪个模型、哪套参数、什么速度产出的"。
    """
    info = payload.get("asr") or {}
    return (
        f"{info.get('model')}/{info.get('device')}（{info.get('compute_type')}） · "
        f"音频 {info.get('audio_seconds')}s · RTF {info.get('realtime_factor')} · "
        f"{info.get('segments')} 段 · 可疑 {info.get('flagged')} 段"
    )
