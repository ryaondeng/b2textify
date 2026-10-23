"""字幕轨 → 文本单元（阶段 2.2）。

面向两类输入，形状相同：B 站的字幕 JSON（CC / AI 字幕）与官方文稿的
`subtitle[]`。产出是我们自己的 `Track`（**只有秒与文本**）+ `Block` 列表。

**单独一个模块，而不是塞进 `sources/bilibili.py`**，两个理由：
1. `docs/04` §3 的扩展点树里 `extract/` 就有 `subtitle.py`；
2. 阶段 2.4 的通用视频源（yt-dlp 字幕）要用同一套解析与合句规则 ——
   写两份就是"同一件事两个口径"（阶段 1 复核 N2/N3 踩过的坑）。

清洗规则照 `docs/90-附录` §5.5 的四条（都是真实问题，不是想当然）：

| 问题 | 处理 |
|---|---|
| AI 字幕断句碎 | 按标点 + 时间间隔（>0.8 s）合并成句 |
| 滚动字幕重复 | 相邻片段**边界重叠**去重（AI 字幕常见：片 N 是 "A B"、片 N+1 是 "B C"） |
| 术语听错 | 不在这一层做（热词纠错属于转写，见 2.3） |
| 时间轴漂移 | 保留原时间戳不强行修正，便于回查 |

⚠️ **本模块只吃"已经取回来的文本"**：`Track` 里不含 `subtitle_url`。
字幕地址带签名 token，`docs/01` §9.2 明令它不得进缓存与 manifest。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Mapping

from ..models import Anchor, Block

#: 相邻片段间隔超过它才断句（`docs/90-附录` §5.5 的 0.8 s）
SEGMENT_GAP = 0.8
#: 句末标点：遇到就把缓冲区收成一句
SENTENCE_END = "。！？!?…"
#: 也当停顿的标点（半句边界）：句末符之外的逗号类不在此列 —— 逗号太密，会把句子切碎
CLAUSE_END = "；;"
#: 单句最长字数：字幕一整段没有标点时（演讲很常见）靠它兜底断句
MAX_SENTENCE_CHARS = 120


@dataclass
class Segment:
    """一个字幕片段（原始粒度，可能半句都不到）。"""

    start: float
    end: float
    text: str
    #: 转写置信度（faster-whisper 的 `avg_logprob`，越接近 0 越可信）。
    #: **只有本地转写（L4）有**，B 站字幕轨没有 → `None`。
    #: 它一路传到 `Block.confidence`，最后进 `pack/suspicious.md`（`docs/01` §6 的存疑清单）。
    #: 属 I3「只加字段」。
    confidence: float | None = None
    #: 转写时被判可疑（判据在 `extract/asr.py::is_flagged`，**只有一份**）：
    #: 低 logprob **或**高 no_speech。合句/去重时按"或"传播 —— 句子里只要有一片
    #: 可疑，整句都该被看一眼。字幕轨恒为 False。
    flagged: bool = False


@dataclass
class Track:
    """一条解析后的字幕轨。

    `source` 取值与 `config.text.prefer_fulltext` 对齐：`cc` / `ai_subtitle`
    （阶段 2.3 起还会有 `whisper`）。`lang` 是上游语言标签（如 `zh-CN` / `ai-zh`）。
    """

    source: str
    lang: str = ""
    segments: list[Segment] = field(default_factory=list)

    @property
    def chars(self) -> int:
        """有效字数（去空白）—— 多路择优就用它比大小。"""
        return sum(len(seg.text.replace(" ", "").replace("\n", "")) for seg in self.segments)

    @property
    def text(self) -> str:
        return "".join(seg.text for seg in self.segments)

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "lang": self.lang,
            "segments": [
                {
                    "start": round(seg.start, 3),
                    "end": round(seg.end, 3),
                    "text": seg.text,
                    # 置信度只在实际有值时才写：字幕轨的段落不该假装有个"置信度"
                    **({"confidence": round(seg.confidence, 4)} if seg.confidence is not None else {}),
                    **({"flagged": True} if seg.flagged else {}),
                }
                for seg in self.segments
            ],
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> Track:
        def _segment(item: Mapping[str, Any]) -> Segment:
            raw = item.get("confidence")
            return Segment(
                start=float(item.get("start") or 0.0),
                end=float(item.get("end") or 0.0),
                text=str(item.get("text") or ""),
                confidence=float(raw) if isinstance(raw, (int, float)) else None,
                flagged=bool(item.get("flagged")),
            )

        return cls(
            source=str(payload.get("source") or ""),
            lang=str(payload.get("lang") or ""),
            segments=[_segment(item) for item in payload.get("segments") or [] if isinstance(item, Mapping)],
        )


def parse_track(payload: Mapping[str, Any], *, source: str, lang: str = "") -> Track:
    """解析字幕 JSON：`{"body":[{"from":0.0,"to":2.5,"content":"…"}]}`。

    **转写产物用的是同一个 `body` 形状**（见 `extract/asr.py`），所以这里一份解析
    同时服务字幕与转写；转写多带的 `avg_logprob` 会被读成 `Segment.confidence`，
    字幕没有它就留 `None` —— 一条解析路径，两个来源，不各写一份。

    容错但不假装成功：`body` 不是列表就返回空轨（调用方据此判"这一路没内容"），
    不做任何猜测性解析 —— 猜出来的字幕会污染整条账本。
    """
    body = payload.get("body")
    if not isinstance(body, list):
        return Track(source=source, lang=lang)
    segments: list[Segment] = []
    for item in body:
        if not isinstance(item, Mapping):
            continue
        text = str(item.get("content") or "").strip()
        if not text:
            continue
        raw_confidence = item.get("avg_logprob")
        segments.append(
            Segment(
                start=float(item.get("from") or 0.0),
                end=float(item.get("to") or 0.0),
                text=text,
                confidence=float(raw_confidence) if isinstance(raw_confidence, (int, float)) else None,
                # flagged 由转写侧判定（extract/asr.py，判据只有一份）；字幕轨没有它 → False
                flagged=bool(item.get("flagged")),
            )
        )
    return Track(source=source, lang=lang or str(payload.get("lang") or ""), segments=segments)


# --------------------------------------------------------------------------
# 清洗：去重叠 → 合句
# --------------------------------------------------------------------------
def _min_confidence(*values: float | None) -> float | None:
    """取最差的那个置信度（全为 None 时返回 None）。

    合并后的单元「一句话」只要有一片读错，整句的可靠性就由**最差那一片**决定 ——
    取平均会把一处明显的听错摊平成一个"看起来还行"的数，存疑清单就抓不住它了。
    """
    present = [value for value in values if value is not None]
    return min(present) if present else None


def _absorb(previous: Segment, segment: Segment) -> Segment:
    """把 `segment` 并进 `previous`：只把结束时间往后延，文本以 `previous` 为准。"""
    return Segment(
        previous.start,
        max(previous.end, segment.end),
        previous.text,
        confidence=_min_confidence(previous.confidence, segment.confidence),
        flagged=previous.flagged or segment.flagged,
    )


def _overlap_size(previous: str, current: str) -> int:
    """`previous` 的**后缀**与 `current` 的**前缀**最长的重叠长度。

    滚动字幕就是这样重复的：片 N 是 "A B"、片 N+1 是 "B C"。取最长重叠，
    再从 current 里切掉那一段，句子才不会被念两遍。
    """
    limit = min(len(previous), len(current))
    for size in range(limit, 0, -1):
        if previous.endswith(current[:size]):
            return size
    return 0


def dedup_segments(segments: list[Segment]) -> list[Segment]:
    """去掉相邻片段的重复（完全重复 / 边界重叠）。保留时间戳不变。"""
    result: list[Segment] = []
    for segment in segments:
        text = segment.text.strip()
        if not text:
            continue
        if not result:
            result.append(Segment(segment.start, segment.end, text, confidence=segment.confidence, flagged=segment.flagged))
            continue
        previous = result[-1]
        if text == previous.text:
            # 完全重复：只把结束时间往后并，其余丢弃
            result[-1] = _absorb(previous, segment)
            continue
        size = _overlap_size(previous.text, text)
        if size >= len(text):
            # 整片都是上一片的后缀 → 纯重复
            result[-1] = _absorb(previous, segment)
            continue
        if size >= 4:  # 太短的重叠（1–3 字）多是巧合，切了反而丢字
            text = text[size:].strip()
            if not text:  # 切完什么都不剩 = 整片都是重复
                result[-1] = _absorb(previous, segment)
                continue
        result.append(Segment(segment.start, segment.end, text, confidence=segment.confidence, flagged=segment.flagged))
    return result


def _is_break(text: str, *, gap_to_next: float, buffered: str) -> bool:
    if not text:
        return True
    if text[-1] in SENTENCE_END:
        return True
    if text[-1] in CLAUSE_END and len(buffered) >= 20:
        return True
    if gap_to_next > SEGMENT_GAP:
        return True
    return len(buffered) >= MAX_SENTENCE_CHARS


def merge_into_sentences(segments: list[Segment]) -> list[Segment]:
    """把碎片段合成句：**成句的时间戳取这句的第一片**（起播点）。

    起止时间只留"句首"这一条，是为了让 `[mm:ss]` 锚点点过去正好是这句话的开头；
    用句尾或中点都会让人跳过去发现"话已经说完了"。
    """
    merged: list[Segment] = []
    buffer: list[Segment] = []

    def flush() -> None:
        if not buffer:
            return
        text = "".join(item.text for item in buffer).strip()
        if text:
            merged.append(
                Segment(
                    buffer[0].start,
                    buffer[-1].end,
                    text,
                    confidence=_min_confidence(*(item.confidence for item in buffer)),
                    flagged=any(item.flagged for item in buffer),
                )
            )
        buffer.clear()

    for index, segment in enumerate(segments):
        buffer.append(segment)
        buffered = "".join(item.text for item in buffer)
        following = segments[index + 1] if index + 1 < len(segments) else None
        gap = (following.start - segment.end) if following is not None else SEGMENT_GAP * 2
        if _is_break(segment.text, gap_to_next=gap, buffered=buffered):
            flush()
    flush()
    return merged


def clean_track(track: Track) -> Track:
    """去重 + 合句，一次到位（两条顺序不能反：先合句会把重复片段焊进句子里）。"""
    return Track(
        source=track.source,
        lang=track.lang,
        segments=merge_into_sentences(dedup_segments(track.segments)),
    )


# --------------------------------------------------------------------------
# 成块
# --------------------------------------------------------------------------
def blocks_from_track(track: Track, *, page: int = 1, first_index: int = 0) -> list[Block]:
    """把一条轨变成文本单元。`block_id` 用 `first_index` 续编号（多分P 拼起来仍连续）。

    锚点用 `Anchor.video(句首秒, page)`。`confidence` 原样带上：字幕轨是 `None`，
    转写有值 —— 后者会被 `pack/suspicious.md` 列出来（低置信度片段，`docs/01` §6）。
    **不给字幕编一个置信度**：假数据比没有更坏，它会让存疑清单失去意义。

    ⚠️ 官方文稿（L3）**不走这里**：它是二手压缩信息（实测完整度低到 L2 的 0.4%，
    `docs/10` §1 结论 4），混进正文块会让账本把"压缩过的摘要"当"原文"来核对，
    R1 的可追溯性当场破产（`docs/01` 决策 D4）。它进 `Document.digest`。
    """
    blocks: list[Block] = []
    for offset, segment in enumerate(clean_track(track).segments, start=1):
        index = first_index + offset
        blocks.append(
            Block(
                block_id=f"b-{index:06d}",
                kind="text",
                text=segment.text,
                anchor=Anchor.video(segment.start, page=page),
                confidence=segment.confidence,
                flagged=segment.flagged,
            )
        )
    return blocks


# --------------------------------------------------------------------------
# 通用时间轴文本（WebVTT / SRT）：阶段 2.4 的 yt-dlp 字幕用
# --------------------------------------------------------------------------
#: `00:00:01.000` / `00:01:02,500` / `01:02.5` —— VTT 用点、SRT 用逗号，一并收
_TIMESTAMP_RE = re.compile(r"(?:(\d{1,2}):)?(\d{1,2}):(\d{2})[.,](\d{1,3})")
#: VTT 的行内标记：`<c>`、`<c.colorE5C513>`、`<00:00:03.119>`（滚动字幕的入点标记）
_MARKUP_RE = re.compile(r"<[^>]+>")
_ENTITIES = {"&amp;": "&", "&lt;": "<", "&gt;": ">", "&quot;": '"', "&apos;": "'"}


def _timestamp_seconds(token: str) -> float:
    match = _TIMESTAMP_RE.fullmatch(token.strip())
    if not match:
        return 0.0
    hours = int(match.group(1) or 0)
    return hours * 3600 + int(match.group(2)) * 60 + int(match.group(3)) + int(match.group(4)) / 10 ** len(match.group(4))


def parse_timed_text(text: str, *, source: str, lang: str = "") -> Track:
    """解析 WebVTT / SRT 字幕（两者结构相同：时间行 + 文本行，空行分块）。

    **为什么单独于 `parse_track`**：那是 B 站/转写的 `body` JSON 形状；yt-dlp 落盘的
    是 vtt/srt 纯文本。两条入口在**这里汇合**成同一个 `Track`，下游的清洗与成块
    只有一份（N2/N3 教训：同一件事不写两份口径）。

    容错但不猜：没有时间行的块（`WEBVTT` 头 / `NOTE` / SRT 的序号）直接跳过，
    清不掉标记的文本也原样保留 —— 猜出来的字幕会污染账本。
    """
    segments: list[Segment] = []
    for block in re.split(r"\n\s*\n", text.strip()):
        lines = [line for line in block.splitlines() if line.strip()]
        timing = next((i for i, line in enumerate(lines) if "-->" in line), None)
        if timing is None:
            continue
        pair = [match.group(0) for match in _TIMESTAMP_RE.finditer(lines[timing])][:2]
        if len(pair) < 2:
            continue
        body = _MARKUP_RE.sub("", "\n".join(lines[timing + 1 :]))
        for entity, char in _ENTITIES.items():
            body = body.replace(entity, char)
        body = body.strip()
        if body:
            segments.append(Segment(_timestamp_seconds(pair[0]), _timestamp_seconds(pair[1]), body))
    return Track(source=source, lang=lang, segments=segments)


def _lang_rank(lang: str, prefer_langs: list[str]) -> int:
    """语言命中档位：命中排 0，未命中排 1。

    匹配规则（刻意只两条，够用且可解释）：
    - 完全相等（`ai-zh` == `ai-zh`）；
    - `zh*` 这种**尾通配**按前缀匹配（`zh-Hans`、`zh-CN` 都命中 `zh*`）。
    比较一律 casefold —— 上游 lan 的大小写不保证（`AI-ZH` 见过）。
    """
    key = lang.strip().casefold()
    for pattern in prefer_langs:
        p = pattern.strip().casefold()
        if not p:
            continue
        if p.endswith("*"):
            if key.startswith(p[:-1]):
                return 0
        elif key == p:
            return 0
    return 1


def pick_track(
    tracks: list[Track], prefer: list[str], prefer_langs: list[str] | None = None
) -> Track:
    """多条轨里挑一条当正文。排序键从左到右：

    1. `text.prefer_fulltext` 的来源顺序（`cc` > `ai_subtitle` > `whisper`）；
    2. **语言命中**（`text.prefer_langs`，6.0.0 新增）：命中的排在没命中的前面。
       没有这一维时，B 站的机器翻译轨（西/葡/阿…字数是中文原轨的 5 倍多）会靠字数
       压过原文轨 —— `BV1i3eJ6KEkb` 实测中文视频取了西语轨（`docs/12` §0.1）；
    3. 同层取字数多的。

    **bilibili 与 video_generic 共用这一份** —— 择优是口径，不是实现细节，
    两处各写一份必然漂移（阶段 1 复核 N3 的教训）。没有一条有内容时返回空轨
    （`source=""`），调用方据此判"这一路没货"。
    `prefer_langs` 为 None/空 = 不启用语言维度（与旧行为完全一致）。
    """
    found = [track for track in tracks if track.segments]
    if not found:
        return Track(source="")
    langs = [lang for lang in (prefer_langs or []) if str(lang).strip()]

    def rank(track: Track) -> tuple[int, int, int]:
        order = prefer.index(track.source) if track.source in prefer else len(prefer)
        # ⚠️ order 别取负 —— 那会把优先级整个倒过来（bilibili 第一版踩过：
        # "cc 优先"变成"ai_subtitle 优先"），见 docs/10 §1 结论 3。
        lang_rank = _lang_rank(track.lang, langs) if langs else 0
        return (order, lang_rank, -track.chars)

    return sorted(found, key=rank)[0]


def track_candidates(
    tracks: list[Track], prefer: list[str], prefer_langs: list[str] | None = None
) -> list[dict[str, object]]:
    """候选轨全名单（按最终排名排序），pack 据此在 TASK.md 明示「正文取自哪条轨」。

    每项：`{"source", "lang", "chars", "chosen"}`。**选了译文轨必须让人看见**——
    只记选中的那条（`subtitle_lang`）不够：看不出它打败了谁、凭什么赢（P0-0）。
    """
    found = [track for track in tracks if track.segments]
    langs = [lang for lang in (prefer_langs or []) if str(lang).strip()]

    def rank(track: Track) -> tuple[int, int, int]:
        order = prefer.index(track.source) if track.source in prefer else len(prefer)
        lang_rank = _lang_rank(track.lang, langs) if langs else 0
        return (order, lang_rank, -track.chars)

    return [
        {
            "source": track.source,
            "lang": track.lang,
            "chars": track.chars,
            "chosen": index == 0,
        }
        for index, track in enumerate(sorted(found, key=rank))
    ]


def heading_block(text: str, *, page: int, first_index: int) -> Block | None:
    """分P 的小标题块（多分P 视频用它把各 P 隔开）。空标题返回 None。"""
    body = text.strip()
    if not body:
        return None
    return Block(
        block_id=f"b-{first_index + 1:06d}",
        kind="heading",
        text=body,
        anchor=Anchor.video(0.0, page=page),
    )
