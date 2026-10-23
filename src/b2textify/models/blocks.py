"""文本单元：来源无关的有序文本序列。

所有来源（字幕 / 网页正文 / 转写）都必须归化到这里，后续清点与重写才有统一输入。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from .anchors import Anchor
from .base import Serializable

BlockKind = Literal["text", "heading", "code", "table", "image"]


@dataclass
class Block(Serializable):
    block_id: str                       # b-000123（稳定标识，供账本引用）
    kind: BlockKind = "text"
    text: str = ""
    anchor: Anchor | None = None
    asset_id: str | None = None         # kind == "image" 时指向 Asset
    confidence: float | None = None     # 转写置信度（仅本地转写有），低值进存疑清单
    #: 转写时被标记可疑（低 logprob **或**高 no_speech —— 后者意味着这段可能是
    #: 静音/音乐处硬编出来的话，恰是幻觉高发形态）。**判据只有一份**（extract.asr），
    #: 这里只是携带；列进 `pack/suspicious.md` 第 3 节。属 I3「只加字段」。
    flagged: bool = False
