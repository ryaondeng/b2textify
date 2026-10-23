"""锚点：追溯的最小单位。

视频用秒 + 分P；文章用**三级方案**（阶段 1.2 实测后修正，见 docs/01 §2.1）：

| 优先级 | 字段 | 形式 | 为什么 |
|---|---|---|---|
| 1 | `fragment` | `原链接#:~:text=…` | 现代浏览器原生高亮定位，精度最高 |
| 2 | `heading_id` | `原链接#coroutines` | 挂到最近的标题 id，精度到节 |
| 3 | `quote` | 锚点旁附 5–10 字原文引文 | 兜底：即使不能跳，也能搜索定位 |

`para`（段落序号）保留为**内部定位信息**（写进 fact 的 `anchor`），
但**不再作为面向用户的唯一锚点** —— 原文页面上根本没有段落号，
`[¶14]` 点不动也找不到，达不到"能跳回去核对"。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal
from urllib.parse import quote as urlquote

from .base import Serializable

AnchorKind = Literal["time", "para"]

BILIBILI_VIDEO_URL = "https://www.bilibili.com/video/{bvid}"


def format_timestamp(seconds: float) -> str:
    """秒 → 显示用时间戳。≥1 小时用 h:mm:ss，否则 mm:ss。"""
    total = max(0, int(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


@dataclass(frozen=True)
class Anchor(Serializable):
    kind: AnchorKind
    sec: float | None = None   # 视频：秒
    page: int | None = None    # 视频：分P（从 1 开始，与 B 站 pages[].page 一致）
    para: int | None = None    # 文章：段落序号（从 1 开始）——**内部定位**，非面向用户的锚点
    # -- 以下三个字段属"只加字段"（I3）：老存档没有它们，取默认值即可 --
    heading_id: str | None = None   # 文章：最近标题的 id（如 coroutines）
    fragment: str | None = None     # 文章：文本片段锚点用文本（#:~:text=…）
    quote: str | None = None        # 文章：5–10 字原文引文（兜底定位）

    # -- 构造 -------------------------------------------------------------
    @classmethod
    def video(cls, sec: float, page: int = 1) -> Anchor:
        return cls(kind="time", sec=float(sec), page=int(page))

    @classmethod
    def article(
        cls,
        para: int,
        *,
        heading_id: str | None = None,
        fragment: str | None = None,
        quote: str | None = None,
    ) -> Anchor:
        return cls(
            kind="para", para=int(para), heading_id=heading_id, fragment=fragment, quote=quote
        )

    # -- 展示 -------------------------------------------------------------
    def display(self) -> str:
        if self.kind == "time" and self.sec is not None:
            return format_timestamp(self.sec)
        if self.kind == "para" and self.para is not None:
            return f"¶{self.para}"
        return "?"

    def to_url(self, source_url: str, bvid: str | None = None) -> str:
        """定位回原文的链接。

        - 视频：必须带 `p=`（否则跳到第一 P，核对位置全错）与 `t=`（秒）；
        - 文章：三级降级 —— 文本片段 → 标题锚点 → 原链接（此时靠 `quote` 兜底定位）。
          站内锚点信息不足时**不臆造**，宁可退回原链接。
        """
        if self.kind == "time" and self.sec is not None:
            base = BILIBILI_VIDEO_URL.format(bvid=bvid) if bvid else source_url
            separator = "&" if "?" in base else "?"
            page = self.page or 1
            return f"{base}{separator}p={page}&t={int(self.sec)}"

        if self.fragment:
            return f"{source_url}#:~:text={urlquote(self.fragment, safe='')}"
        if self.heading_id:
            return f"{source_url}#{self.heading_id}"
        return source_url
