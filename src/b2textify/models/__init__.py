"""最小模型集：只保留取文本所需的锚点与文本单元。

`anchors.py` / `blocks.py` / `base.py` 是从上一版项目原样移植的
（它们是纯数据类，零外部依赖）。移植而非重写的原因：`[mm:ss]` 锚点的格式化、
序号稳定规则都在里面，重写只会引入差异。
"""

from .anchors import Anchor, AnchorKind, format_timestamp
from .base import Serializable
from .blocks import Block, BlockKind
from .result import TextResult

__all__ = [
    "Anchor",
    "AnchorKind",
    "Block",
    "BlockKind",
    "Serializable",
    "TextResult",
    "format_timestamp",
]
