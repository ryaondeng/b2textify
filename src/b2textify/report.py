"""输出：一份给人看的纯文本 md。

只写三件事：从哪来的、正文是什么、取轨时有没有降级。
**不写事实账本、不写校验结论** —— 后续整理由别人做，这里只保证"文本可信且可回查"。

格式契约（`## 正文` / `[mm:ss] ` / `（低置信）`）定义在 `mdtext.py`：这边负责写、
译文通道负责读，契约不属于任何一方，放在中间那一层两边共用。
"""

from __future__ import annotations

import re
from pathlib import Path

from .mdtext import BODY_HEADING, FLAG_MARK
from .models import TextResult, format_timestamp


def render(result: TextResult) -> str:
    lines: list[str] = [f"# {result.title}", ""]
    lines.append(f"> 来源：{result.url}")
    lines.append(f"> 抓取时间：{result.fetched_at}")
    if result.author:
        lines.append(f"> 作者：{result.author}")
    if result.duration:
        lines.append(f"> 时长：{format_timestamp(result.duration)}")
    source_label = {
        "cc": "官方字幕（CC）",
        "ai_subtitle": "平台 AI 字幕",
        "whisper": "本地转写",
        "html": "页面正文（知乎回答）",
    }.get(result.text_source, result.text_source or "未知")
    lines.append(f"> 正文取自：{source_label}（{result.subtitle_lang or '语言未知'}）")
    lines.append(f"> 字数：{result.chars}")
    lines.append("")

    if result.warnings:
        lines.append("> ⚠️ 取轨时发生降级：")
        for warning in result.warnings:
            lines.append(f"> - {warning}")
        lines.append("")

    if result.tracks:
        lines.append("## 候选字幕轨")
        lines.append("")
        lines.append("| 选中 | 来源 | 语言 | 字数 |")
        lines.append("|---|---|---|---|")
        for item in result.tracks:
            mark = "✓" if item.get("chosen") else ""
            lines.append(
                f"| {mark} | {item.get('source') or '—'} | {item.get('lang') or '—'} | {item.get('chars')} |"
            )
        lines.append("")
        lines.append("> 多语言轨并列时**按语言偏好选**，不按字数 —— 机器译文会膨胀到原文数倍。")
        lines.append("")

    lines.append(BODY_HEADING)
    lines.append("")
    for block in result.blocks:
        if block.kind == "heading":
            lines.append(f"### {block.text}")
            lines.append("")
            continue
        stamp = ""
        if block.anchor is not None and block.anchor.sec is not None:
            stamp = f"[{format_timestamp(block.anchor.sec)}] "
        suffix = FLAG_MARK if block.flagged else ""
        lines.append(f"{stamp}{block.text}{suffix}")

    lines.append("")
    return "\n".join(lines)


def write_report(result: TextResult, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    # `slug` 优先：B 站是 bvid，知乎是 `zhihu-<answer_id>`
    name = f"{result.slug or result.bvid}-{_safe_name(result.title)}.md"
    path = out_dir / name
    path.write_text(render(result), encoding="utf-8")
    return path


def _safe_name(title: str, limit: int = 40) -> str:
    """标题 → 安全文件名。

    **中文弯引号也要处理**：`“”` 在 Windows 上合法，但 PowerShell / bash 会把它当引号，
    于是 `Get-Content "out\\xxx“鱼灾”.md"` 直接报"位置形式参数"错（2026-10-23 实测）。
    文件名里出现它只会给下游添麻烦。
    """
    cleaned = re.sub(r'[\\/:*?"<>|\r\n\t]+', "-", title)
    cleaned = re.sub(r"[“”‘’]", "", cleaned).strip(" -")
    return cleaned[:limit] or "untitled"
