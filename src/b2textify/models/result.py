"""一次取文本的结果（`TextResult`）。

**为什么放在 models 而不是 `sources/bilibili.py`**：`report.py` 只负责渲染，
却要 import 具体来源模块才能拿到结果类型 —— 那让"表现层"反向依赖"取数层"，
以后加知乎/通用视频源时，report 还得认识每个来源。放进 models 后依赖只有一个方向：
`sources/report/translate → models`。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .blocks import Block


@dataclass
class TextResult:
    """一次取文本的结果。字段刻意少：只保留"别人整理时用得上"的信息。"""

    url: str
    bvid: str
    title: str
    author: str
    duration: float | None
    fetched_at: str
    blocks: list[Block] = field(default_factory=list)
    tracks: list[dict[str, object]] = field(default_factory=list)
    subtitle_lang: str = ""
    text_source: str = ""
    warnings: list[str] = field(default_factory=list)
    raw_dir: Path | None = None
    #: 输出文件名用的稳定标识：B 站是 bvid，知乎是 `zhihu-<answer_id>`
    slug: str = ""

    @property
    def chars(self) -> int:
        return sum(len(block.text) for block in self.blocks)

    @property
    def degraded_reason(self) -> str:
        """取到的不是偏好语言时，把原因一句话说清（降级必须可见）。"""
        return self.warnings[0] if self.warnings else ""
