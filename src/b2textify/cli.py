"""命令行入口：一个命令，按链接类型分流。

刻意只有一个动词（`b2textify <链接>`）：取文本不需要子命令树，
子命令一多就会开始长功能，最后又变成上一版那种工具。
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from .config import load_config
from .errors import KextError, UsageError
from .report import write_report
from .sources.bilibili import BilibiliSource, is_bilibili
from .sources.zhihu import ZhihuSource, is_zhihu


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="b2textify",
        description="把 B 站视频 / 知乎回答取成纯文本（只取文本，不做整理）",
    )
    parser.add_argument("url", help="B 站视频链接（支持 ?p=N）或知乎回答链接")
    parser.add_argument("--out", metavar="DIR", help="输出目录（默认当前目录）")
    parser.add_argument(
        "--cache",
        metavar="DIR",
        help="缓存目录（默认取 .env 的 B2T_CACHE_DIR，没配就用 %%LOCALAPPDATA%%\\b2textify\\cache）",
    )
    parser.add_argument("--config", metavar="FILE", help="指定 b2textify.toml")
    parser.add_argument(
        "--no-asr",
        action="store_true",
        help="没有字幕轨时不下载音频转写，直接报错（默认会转写）",
    )
    parser.add_argument("--quiet", action="store_true", help="只输出结果路径")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    # 转写/下载是长任务（40 分钟视频几分钟起步），进度必须看得见；--quiet 只留错误
    logging.basicConfig(
        level=logging.WARNING if args.quiet else logging.INFO,
        format="  %(message)s",
        stream=sys.stderr,
    )
    try:
        cfg = load_config(args.config, cache_dir=args.cache, out_dir=args.out or Path.cwd())

        if is_bilibili(args.url):
            result = BilibiliSource(cfg, asr=not args.no_asr).fetch(args.url)
        elif is_zhihu(args.url):
            # 知乎只走官方回答 API（HTML 页按客户端特征 403，见 sources/zhihu.py）
            result = ZhihuSource(cfg).fetch(args.url)
        else:
            raise UsageError(
                code="UNSUPPORTED_URL",
                message=f"暂不支持这个链接：{args.url[:80]}",
                hint="支持 bilibili.com/video/BV… 与知乎回答页 /question/{qid}/answer/{aid}",
            )
        path = write_report(result, cfg.out_dir)

        if not args.quiet:
            _summary(result, path)
        print(path)
        return 0
    except KextError as exc:
        print(str(exc), file=sys.stderr)
        return exc.exit_code
    except KeyboardInterrupt:
        print("已中断", file=sys.stderr)
        return 130


def _summary(result, path: Path) -> None:
    label = {
        "cc": "官方字幕",
        "ai_subtitle": "平台 AI 字幕",
        "whisper": "本地转写",
        "html": "页面正文（知乎回答）",
    }.get(result.text_source, result.text_source or "未知")
    print(f"✓ {result.title}", file=sys.stderr)
    print(f"  正文取自：{label}（{result.subtitle_lang or '语言未知'}），{result.chars} 字", file=sys.stderr)
    if result.warnings:
        for warning in result.warnings:
            print(f"  ⚠️ {warning}", file=sys.stderr)
    if result.raw_dir is not None:
        print(f"  原始物：{result.raw_dir}", file=sys.stderr)
    print(f"  输出：{path}", file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
