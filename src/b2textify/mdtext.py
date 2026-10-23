"""md 的格式契约：解析（读回）与回写（填译文）。

**为什么单独一个模块**：`report.py` 负责**写**、译文通道负责**读**，两边必须认同一套格式
（`## 正文`、`[mm:ss] `、行尾 `（低置信）`）。契约放在这里，写的一侧与读的一侧都只是使用者 ——
谁也不比谁"权威"，两处各写一份正则迟早对不上（改了报告那边，译文会静默丢时间锚）。

解析原则：
- 认不出的行（引用、表格、分隔线）**原样保留、不参与替换**。猜着改会把"来源/警告"翻掉；
- 时间锚、P 标题（`### …`）、低置信标记都当结构，只有 `[mm:ss] 正文` 的正文部分可替换；
- 没有 `## 正文` 就不是本工具产出的文件 → 直接报 `NOT_REPORT_FILE`，不硬猜。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Mapping

from .errors import UsageError

#: 正文段的标题行
BODY_HEADING = "## 正文"
#: 低置信标记（只出现在正文行尾）
FLAG_MARK = "（低置信）"
#: 正文行形状：`[00:12] 正文`（小时级 `hh:mm:ss` 也接受）
LINE_STAMP_RE = re.compile(r"^\[(\d{1,2}:\d{2}(?::\d{2})?)\]\s?(.*)$")
#: 汉字（判断"这段是不是已经是中文"）
CJK_RE = re.compile(r"[\u4e00-\u9fff]")


@dataclass
class Line:
    """正文里的一行。`kind` 决定它是否参与翻译。"""

    kind: str  # heading / text / blank / raw
    raw: str = ""
    stamp: str = ""
    text: str = ""
    flagged: bool = False

    @property
    def translatable(self) -> bool:
        return self.kind == "text" and bool(self.text.strip())


@dataclass
class Document:
    """一份已提取的 md：头部元信息 + 正文行。"""

    head: list[str] = field(default_factory=list)
    lines: list[Line] = field(default_factory=list)

    @property
    def body_chars(self) -> int:
        return sum(len(line.text) for line in self.lines if line.kind == "text")

    @property
    def cjk_ratio(self) -> float:
        """正文里汉字占比。用来判断"是不是已经是中文，不必再翻"。"""
        text = "".join(line.text for line in self.lines if line.kind == "text")
        if not text:
            return 0.0
        return len(CJK_RE.findall(text)) / len(text)


def parse_markdown(text: str) -> Document:
    """拆成（头部, 正文行）。没有 `## 正文` 就报错 —— 那不是本工具产出的文件。"""
    raw_lines = text.splitlines()
    try:
        index = raw_lines.index(BODY_HEADING)
    except ValueError as exc:
        raise UsageError(
            code="NOT_REPORT_FILE",
            message=f"这个文件里没有 `{BODY_HEADING}` 段，不像是提取产物",
            hint="输入应该是 `b2textify <链接>` 生成的 md（同目录下的 `BVxxxx-标题.md`）",
        ) from exc

    document = Document(head=raw_lines[: index + 1])
    for raw in raw_lines[index + 1 :]:
        document.lines.append(_parse_line(raw))
    return document


def _parse_line(raw: str) -> Line:
    stripped = raw.strip()
    if not stripped:
        return Line(kind="blank", raw=raw)
    if stripped.startswith("#"):
        return Line(kind="heading", raw=stripped)
    match = LINE_STAMP_RE.match(stripped)
    if not match:
        # 认不出来的行（表格、引用、分隔线）原样保留，**不参与翻译**
        return Line(kind="raw", raw=raw)
    stamp, text = match.group(1), match.group(2)
    flagged = text.endswith(FLAG_MARK)
    if flagged:
        text = text[: -len(FLAG_MARK)]
    return Line(kind="text", raw=raw, stamp=stamp, text=text.strip(), flagged=flagged)


def render_translated(
    document: Document,
    translated: Mapping[int, str],
    *,
    model: str,
    source_name: str,
    now: str | None = None,
) -> str:
    """把译文填回原结构，生成译文全文。

    头部元信息**原样保留**（来源、作者、时长、正文取自……），只追加几行译文说明 ——
    原文的"这段话是哪来的"不能因为翻译过一次就丢了。
    """
    lines = list(document.head)
    lines.append("")
    lines.append(f"> 中文译文：{model}（机器翻译，未人工校对）")
    lines.append(f"> 译自：{source_name}")
    lines.append(f"> 翻译时间：{now or datetime.now().astimezone().isoformat(timespec='seconds')}")
    lines.append("")

    for position, line in enumerate(document.lines):
        if line.kind == "heading":
            lines.append(line.raw)
            lines.append("")
            continue
        if line.kind in ("raw", "blank"):
            lines.append(line.raw if line.kind == "raw" else "")
            continue
        text = translated.get(position, line.text)
        suffix = FLAG_MARK if line.flagged else ""
        prefix = f"[{line.stamp}] " if line.stamp else ""
        lines.append(f"{prefix}{text}{suffix}")
    lines.append("")
    return "\n".join(lines)
