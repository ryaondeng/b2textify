"""中文译文（**提取之后**的独立一步，不在提取链路里）。

为什么独立成一个入口，而不是 `b2textify <url> --translate`：

1. 提取与翻译的**失败语义完全不同**：提取失败要重跑网络，翻译失败只要重试模型调用，
   而且原文已经落盘、随时可翻 —— 塞进一条命令会让"重跑一次"变成"再抓一遍视频"；
2. 翻译要花钱、要走外网 API（模型会变、key 会过期），这是**另一个依赖面**，
   不该让"我只想要字幕文本"的人也被要求配 LLM；
3. 用户真正要的形态是"同一份原文旁边多一份 `-zh.md`"，输入是一个**已存在的 md**，
   所以入口就该吃文件，而不是吃链接。

输入是 `report.py` 产出的 md，输出是 `<同名>-zh.md`：时间锚、P 标题、低置信标记全部保留，
只把每一行的正文换成中文，并在头部写明"这是机器翻译、译自哪个文件、用的哪个模型"。

**校验是硬要求**：模型少输出一条、id 对不上、内容为空 —— 都算失败，重试后仍不对就报错停下。
悄悄接受错位的译文会让时间锚和内容整体错位，比报错恶劣得多（`NOTES.md` 的纪律）。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Mapping

from .config import Config, load_config
from .errors import KextError, SourceError, UsageError
from .llm import LlmCfg, chat
# md 的解析/回写在 `mdtext.py`：那里是格式契约，这里只管翻译与校验
from .mdtext import Document, parse_markdown, render_translated

#: 判断"这段文字已经是中文"的阈值：正文里汉字占比超过它就不翻（除非 `--force`）
CJK_RATIO_SKIP = 0.20
#: 单块默认字符数。太小 → 调用次数多、上下文碎；太大 → 模型容易漏行
DEFAULT_CHUNK_CHARS = 2000
#: 单块最多重试次数（校验不过就重来，重来还不过就报错）
CHUNK_ATTEMPTS = 3
#: 译文文件名后缀
ZH_SUFFIX = "-zh"

SYSTEM_PROMPT = """你是专业的视频字幕译者，把逐行字幕翻译成自然的简体中文。

规则：
1. 逐条输出，id 必须与输入一一对应：不许合并、拆分、省略或新增；
2. 忠实原意与语气，不要总结、不要解释、不要加任何评注；
3. 人名、机构名、作品名与专业术语用通行中文译法，必要时在括号里保留原文；
4. 口头禅、语气词照常译出，不要为了通顺而删句；
5. 只输出 JSON：{"lines":[{"id":0,"zh":"……"}]}，不要输出其他任何文字。"""

logger = logging.getLogger("b2textify.translate")


# --------------------------------------------------------------------------
# 分块 / 翻译
# --------------------------------------------------------------------------
def split_chunks(document: Document, limit: int = DEFAULT_CHUNK_CHARS) -> list[list[int]]:
    """把可翻译行按顺序切成若干块（返回行号）。

    遇到 `### 第N讲` 这类标题就**强制断开**：跨 P 的上下文对模型没帮助，
    反而容易让它把两 P 的内容连成一句。
    """
    chunks: list[list[int]] = []
    current: list[int] = []
    size = 0
    for position, line in enumerate(document.lines):
        if line.kind == "heading":
            if current:
                chunks.append(current)
            current, size = [], 0
            continue
        if not line.translatable:
            continue
        length = len(line.text)
        if current and size + length > limit:
            chunks.append(current)
            current, size = [], 0
        current.append(position)
        size += length
    if current:
        chunks.append(current)
    return chunks


def _chunk_payload(document: Document, positions: list[int]) -> list[dict[str, Any]]:
    return [
        {"id": index, "text": document.lines[position].text}
        for index, position in enumerate(positions)
    ]


def _parse_response(content: str, *, expected: int) -> dict[int, str]:
    """解析模型返回的 JSON，返回 {块内 id: 译文}。形状不对就当失败。"""
    text = content.strip()
    # 有的服务会在 JSON 外面裹 ```json … ``` —— 剥掉再解析，不算"模型答错了"
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", text).strip()
    try:
        payload = json.loads(text)
    except ValueError as exc:
        raise ValueError(f"返回的不是 JSON：{content[:120]!r}") from exc

    items = payload.get("lines") if isinstance(payload, Mapping) else payload
    if not isinstance(items, list):
        raise ValueError(f"缺少 lines 数组：{str(payload)[:120]}")

    result: dict[int, str] = {}
    for item in items:
        if not isinstance(item, Mapping):
            continue
        try:
            key = int(item.get("id"))
        except (TypeError, ValueError):
            continue
        value = str(item.get("zh") or item.get("text") or "").strip()
        if value:
            result[key] = value

    missing = [index for index in range(expected) if index not in result]
    if missing:
        raise ValueError(f"漏了 {len(missing)} 条（id {missing[:5]}…）")
    return result


def translate_chunk(
    document: Document,
    positions: list[int],
    *,
    llm: LlmCfg,
    chunk_no: int = 1,
    chunk_total: int = 1,
) -> dict[int, str]:
    """翻译一块；校验不过就重来，重来还不过就抛错（不吞）。"""
    payload = _chunk_payload(document, positions)
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": json.dumps({"lines": payload}, ensure_ascii=False)},
    ]
    first, last = document.lines[positions[0]].stamp, document.lines[positions[-1]].stamp
    problem = ""
    for attempt in range(1, CHUNK_ATTEMPTS + 1):
        content = chat(messages, cfg=llm, json_mode=True)
        try:
            result = _parse_response(content, expected=len(positions))
        except ValueError as exc:
            problem = str(exc)
            logger.warning("第 %d/%d 块第 %d 次校验失败（%s），重试", chunk_no, chunk_total, attempt, problem)
            # 把失败原因回灌给模型：比原样重问一次更容易过
            messages = messages[:2] + [
                {"role": "assistant", "content": content},
                {
                    "role": "user",
                    "content": f"上面的输出不合格：{problem}。请按规定格式重新输出全部 {len(positions)} 条。",
                },
            ]
            continue
        logger.info("第 %d/%d 块完成（%d 条，%s–%s）", chunk_no, chunk_total, len(positions), first, last)
        return {positions[index]: value for index, value in result.items()}

    raise SourceError(
        code="TRANSLATE_FAILED",
        message=f"第 {chunk_no}/{chunk_total} 块连续 {CHUNK_ATTEMPTS} 次未通过校验（{first}–{last}）：{problem}",
        hint=(
            "多半是模型没按 JSON 逐条返回。先重跑一次（已完成的块有缓存，不会重花钱）；"
            "仍失败就调小 --chunk-chars 让每次条数少一点，或换模型（B2T_LLM_MODEL）"
        ),
        detail={"chunk": chunk_no, "total": chunk_total, "lines": len(positions)},
    )


def translate_document(
    document: Document,
    *,
    llm: LlmCfg,
    chunk_chars: int = DEFAULT_CHUNK_CHARS,
    workers: int = 3,
    cache_dir: Path | None = None,
) -> dict[int, str]:
    """整篇翻译。块之间并行（默认 3 路），顺序由行号保证，与完成先后无关。"""
    chunks = split_chunks(document, chunk_chars)
    if not chunks:
        return {}
    logger.info("共 %d 块（每块 ≤ %d 字），并行 %d 路", len(chunks), chunk_chars, max(1, workers))

    def run(index: int) -> dict[int, str]:
        positions = chunks[index]
        key = _cache_key(llm, _chunk_payload(document, positions))
        cached = _cache_read(cache_dir, key)
        if cached is not None:
            logger.info("第 %d/%d 块命中缓存", index + 1, len(chunks))
            return {positions[item]: text for item, text in enumerate(cached)}
        result = translate_chunk(
            document, positions, llm=llm, chunk_no=index + 1, chunk_total=len(chunks)
        )
        ordered = [result[position] for position in positions]
        _cache_write(cache_dir, key, ordered)
        return result

    merged: dict[int, str] = {}
    if workers <= 1:
        for index in range(len(chunks)):
            merged.update(run(index))
        return merged
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for part in pool.map(run, range(len(chunks))):
            merged.update(part)
    return merged


# --------------------------------------------------------------------------
# 缓存（块级）：翻译到一半失败时，重跑不必从第一块开始
# --------------------------------------------------------------------------
def _cache_key(llm: LlmCfg, payload: list[dict[str, Any]]) -> str:
    material = llm.model + "\x00" + json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]


def _cache_read(cache_dir: Path | None, key: str) -> list[str] | None:
    if cache_dir is None:
        return None
    path = cache_dir / f"{key}.json"
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        return None
    return data if isinstance(data, list) and all(isinstance(item, str) for item in data) else None


def _cache_write(cache_dir: Path | None, key: str, values: list[str]) -> None:
    if cache_dir is None:
        return
    cache_dir.mkdir(parents=True, exist_ok=True)
    (cache_dir / f"{key}.json").write_text(json.dumps(values, ensure_ascii=False), encoding="utf-8")


# --------------------------------------------------------------------------
# 对外入口
# --------------------------------------------------------------------------
def translate_file(
    path: Path,
    *,
    cfg: Config | None = None,
    llm: LlmCfg | None = None,
    out_dir: Path | None = None,
    chunk_chars: int = DEFAULT_CHUNK_CHARS,
    workers: int = 3,
    force: bool = False,
) -> Path:
    """把一个提取产物翻成中文，写到 `-zh.md`，返回新路径。"""
    document = parse_markdown(path.read_text(encoding="utf-8"))
    if document.cjk_ratio >= CJK_RATIO_SKIP and not force:
        raise UsageError(
            code="ALREADY_CHINESE",
            message=f"正文里汉字占 {document.cjk_ratio:.0%}，看起来已经是中文了",
            hint="确实要重译就加 --force；或者这份文件本来就不需要译文",
        )

    # 先 load_config 再读 key：`.env` 是 load_config 注入的，顺序反了会读不到 key
    cfg = cfg or load_config()
    llm = llm or LlmCfg.from_env()
    translated = translate_document(
        document,
        llm=llm,
        chunk_chars=chunk_chars,
        workers=workers,
        cache_dir=cfg.cache_dir / "translate" / path.stem,
    )
    text = render_translated(document, translated, model=llm.model, source_name=path.name)

    target_dir = out_dir or path.parent
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"{path.stem}{ZH_SUFFIX}.md"
    target.write_text(text, encoding="utf-8")
    return target


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="b2textify-translate",
        description="把 b2textify 产出的 md 翻成中文，写到同名的 -zh.md（机器翻译，不是提取环节）",
    )
    parser.add_argument("file", help="提取产物 md（b2textify 输出的那份）")
    parser.add_argument("--out", metavar="DIR", help="译文目录（默认与原文件同目录）")
    parser.add_argument("--chunk-chars", type=int, default=DEFAULT_CHUNK_CHARS, help="单次请求的字符上限")
    parser.add_argument("--workers", type=int, default=3, help="并行请求数（默认 3）")
    parser.add_argument("--force", action="store_true", help="正文已经是中文也照样翻")
    parser.add_argument("--quiet", action="store_true", help="只输出译文路径")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.WARNING if args.quiet else logging.INFO,
        format="  %(message)s",
        stream=sys.stderr,
    )
    try:
        path = Path(args.file).expanduser()
        if not path.is_file():
            raise UsageError(
                code="FILE_NOT_FOUND",
                message=f"找不到文件：{path}",
                hint="先跑 `b2textify <链接>` 产出 md，再把那份文件喂给这里",
            )
        cfg = load_config()  # 只为拿到 cache_dir 并把 .env 注进环境（key 从那里读）
        target = translate_file(
            path,
            cfg=cfg,
            out_dir=Path(args.out).expanduser() if args.out else None,
            chunk_chars=args.chunk_chars,
            workers=args.workers,
            force=args.force,
        )
        print(target)
        return 0
    except KextError as exc:
        print(str(exc), file=sys.stderr)
        return exc.exit_code
    except KeyboardInterrupt:
        print("已中断", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
