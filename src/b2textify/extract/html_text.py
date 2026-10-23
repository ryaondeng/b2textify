"""HTML 片段 → `Block` 序列（知乎回答正文用）。

**为什么不用第三方解析器**：项目对依赖的口径是"能零就零"（提取链路一个依赖都没有），
而这些**由官方 API 返回的正文**结构极其规整（只有 `p`/`b`/`ul`/`li`/`blockquote`/`br`，
2026-10-23 实测 3013 字 39 段），用 `html.parser` 按块切就够。上 BeautifulSoup 换来的是
"能解析任意网页"，那是另一条路（网页通道）该操心的事，这里不需要。

**只做块切分，不做"像不像正文"的判断**：知乎的 content 是 API 的正文段字段，
一句话一段是常见排版（高赞回答平均 12 字/段）。拿"短行比"这类网页启发式去筛它只会误杀。
"""

from __future__ import annotations

import re
from html.parser import HTMLParser

from ..models import Block

#: 这些标签一出现就要断块（段落级）
BLOCK_TAGS = {"p", "li", "blockquote", "h1", "h2", "h3", "h4", "h5", "h6", "pre", "figcaption"}
#: 这些标签只是容器：进入/离开不产块，靠里面的段落级标签切
CONTAINER_TAGS = {
    "div", "section", "article", "main", "ul", "ol", "figure", "table", "tbody", "tr", "td", "span",
}
#: 整段丢弃的内容（脚本、样式、图片里塞的说明文字）
SKIP_TAGS = {"script", "style", "noscript", "svg", "iframe"}
#: 标题级标签 → `heading` 块
HEADING_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6"}
_WS = re.compile(r"[ \t\r\f\v\u00a0]+")


class _BlockParser(HTMLParser):
    """把 HTML 切成（kind, text）序列。`<br>` 当空格，块内空白折叠。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.blocks: list[tuple[str, str]] = []
        self._buffer: list[str] = []
        self._kind = "text"
        self._skip = 0

    # -- 切块 ---------------------------------------------------------------
    def _flush(self) -> None:
        text = _WS.sub(" ", "".join(self._buffer)).strip()
        self._buffer = []
        if text:
            self.blocks.append((self._kind, text))
        self._kind = "text"

    # -- HTMLParser 钩子 ----------------------------------------------------
    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in SKIP_TAGS:
            self._skip += 1
            return
        if tag == "br":
            self._buffer.append(" ")
            return
        if tag in BLOCK_TAGS:
            self._flush()
            self._kind = "heading" if tag in HEADING_TAGS else ("code" if tag == "pre" else "text")
        elif tag in CONTAINER_TAGS:
            # 容器里的第一段文字也别丢：块内容结束于容器的结束标签时由 `_flush` 兜住
            pass

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        # `<br/>`、`<img/>` 这类自闭合标签：`br` 当空格，其余忽略（图片不产出文本）
        if tag == "br":
            self._buffer.append(" ")

    def handle_endtag(self, tag: str) -> None:
        if tag in SKIP_TAGS:
            self._skip = max(0, self._skip - 1)
            return
        if tag in BLOCK_TAGS or tag in CONTAINER_TAGS:
            self._flush()

    def handle_data(self, data: str) -> None:
        if self._skip:
            return
        # `pre` 里的换行要留：代码块折成一行就没法读了
        self._buffer.append(data if self._kind == "code" else data.replace("\n", " "))

    def close(self) -> None:
        super().close()
        self._flush()


def split_blocks(html: str) -> list[tuple[str, str]]:
    """HTML → [(kind, text)]。空块与纯空白块会被丢掉。"""
    parser = _BlockParser()
    parser.feed(html or "")
    parser.close()
    return parser.blocks


def blocks_from_html(html: str, *, url: str = "") -> list[Block]:
    """HTML → `Block` 序列（编号 `b-000001` 起，与字幕通道一致）。

    知乎回答没有时间轴，所以 `anchor` 留空 —— **不编一个假的锚点**：
    后面整理的人拿它去对时间会直接对错。
    """
    del url  # 保留参数：来源 URL 不参与切块，但调用点都带着它，签名先统一
    blocks: list[Block] = []
    for index, (kind, text) in enumerate(split_blocks(html), start=1):
        blocks.append(Block(block_id=f"b-{index:06d}", kind=kind, text=text))
    return blocks
