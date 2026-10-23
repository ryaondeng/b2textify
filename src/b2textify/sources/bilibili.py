"""B 站：视频 → 字幕轨 → 带时间锚的纯文本块。

**改这个文件前先读五条教训**（都是实际踩出来的，不是理论）：

1. **未登录必须硬失败**：字幕接口返回 `-101` 时抛 `CredentialMissing`，绝不退化成
   "这个视频没字幕" —— 那会产出一份看起来成功、其实空白的文本。
2. **选轨按语言，不按字数**：同一视频有多条 AI 翻译轨，机器译文会膨胀到原文的
   5–6 倍字数（实测：中文原轨 16,394 字 vs 西语译文轨 86,229 字）。纯按字数择优
   必然选中译文轨，且专名会被译坏（`史官` → `Historiador 1644`）。
   因此：**先按 `text.prefer_langs` 过滤，只在命中的轨之间比字数**；
   一条都没命中才退回全量比字数，并且必须给出显式警告。
3. **`subtitle_url` 是协议相对的**（`//aisubtitle.hdslb.com/…`），要补 `https:`；
   字幕落在 CDN 上，**请求时不带 Cookie** —— 凭证只发给 api.bilibili.com。
4. **不要用非签名的 `/x/player/v2` 兜底**（2026-10-23 实测）：它不报错，返回 `code=0`、
   `data.cid` 也回显你的 cid，但 `subtitles` 里给的是**别的视频的字幕**（诱饵）。
   拿它兜底会产出"看起来成功、正文全无关"的文本 —— 比报错恶劣得多。
   判断"这个视频有没有字幕"，只认 WBI 版；页面 `__INITIAL_STATE__.videoData.subtitle.list`
   可作旁证（`BV1HBnH6JEAZ` 发布 2 小时时两处都是空，确属还没生成）。
5. **没字幕轨 ≠ 没正文**：大量视频（尤其刚发布的）根本没有字幕轨，但音轨取得到 ——
   默认路径是下载音频做本地转写（`extract/asr.py` + `media.py`）。但**不许**拿官方文稿、
   页面文案凑数（那是二手压缩）。转写是降级，必须把"这是转写不是官方字幕 + 可疑段数"
   写进输出的警告里；`--no-asr` 才退回"没字幕就失败"。
"""

from __future__ import annotations

import json
import logging
import re
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping

from ..config import Config
from ..errors import CredentialMissing, SourceError, UsageError, bili_error
from ..extract.asr import build_prompt, transcribe
from ..extract.asr import summarize as asr_summary
from ..extract.subtitle import (
    _lang_rank,  # 语言匹配规则只有一份（在移植件里）：这里复用，避免两处漂移
    Track,
    blocks_from_track,
    heading_block,
    parse_track,
    pick_track,
    track_candidates,
)
from ..http import HttpClient, RequestPolicy
from ..media import MEDIA_SUBDIR, discard, download_audio
from ..models import Block, TextResult
from ..wbi import API_PLAYER, API_VIEW, WbiSigner, parse_json

#: 转写是很长的本地计算（40 分钟视频几分钟量级），进度必须能看见
logger = logging.getLogger("b2textify.bilibili")

PREHEAT_URL = "https://www.bilibili.com/"
DEFAULT_REFERER = "https://www.bilibili.com/"
CREDENTIAL_HOST = "bilibili.com"
VIEW_NAME = "view.json"


def _asr_name(page: int) -> str:
    """本地转写产物（`raw/<bvid>/p001-asr.json`）。形状与字幕同形，所以同一份 `parse_track` 够用。"""
    return f"p{page:03d}-asr.json"

BV_RE = re.compile(r"(BV[0-9A-Za-z]{10})")
PAGE_RE = re.compile(r"[?&]p=(\d+)")
CST = timezone(timedelta(hours=8))


def is_bilibili(url: str) -> bool:
    if "//" not in url:
        return bool(BV_RE.search(url))
    host = urllib.parse.urlsplit(url).hostname or ""
    return host == "bilibili.com" or host.endswith(".bilibili.com")


def parse_url(raw: str) -> tuple[str, int]:
    """URL / 裸号 → (bvid, 分P)。`?p=1` 按"未指定"处理（浏览器落在第一 P 会带它）。"""
    text = (raw or "").strip()
    if not text:
        raise UsageError(code="URL_EMPTY", message="没有给出链接或视频号")

    if "//" in text:
        host = urllib.parse.urlsplit(text).hostname or ""
        if host.endswith("b23.tv"):
            raise UsageError(
                code="SHORT_LINK_UNSUPPORTED",
                message="b23.tv 短链里没有 BV 号",
                hint="浏览器打开短链后，把地址栏里的完整链接（含 /video/BV…）复制过来",
            )
        if not (host == "bilibili.com" or host.endswith(".bilibili.com")):
            raise UsageError(
                code="NOT_BILIBILI",
                message=f"这是 {host}，不是 B 站链接",
                hint="本工具当前只处理 B 站视频与知乎回答",
            )

    page = 0
    match = PAGE_RE.search(text)
    if match and int(match.group(1)) > 1:
        page = int(match.group(1))

    bv = BV_RE.search(text)
    if not bv:
        raise UsageError(
            code="BV_NOT_FOUND",
            message=f"链接里没有 BV 号：{text[:80]}",
            hint="支持 /video/BV… 完整链接；av 号请先在浏览器打开换成 BV 链接",
        )
    return bv.group(1), page


class BilibiliSource:
    """B 站取文本。一个实例可复用（保留匿名 cookie 与 WBI 密钥缓存）。"""

    name = "bilibili"

    def __init__(self, cfg: Config, client: HttpClient | None = None, *, asr: bool = True) -> None:
        self.cfg = cfg
        self.client = client or HttpClient(cfg.cookie_header)
        self.signer = WbiSigner(self.client)
        #: 没有字幕轨时要不要下载音频做本地转写（`--no-asr` 关掉，退回"只取字幕"的老行为）
        self.asr_enabled = asr

    # -- 主流程 -------------------------------------------------------------
    def fetch(self, url: str) -> TextResult:
        bvid, page = parse_url(url)
        self._preheat()
        self._check_credential()
        view = self._view(bvid)
        data = dict(view.get("data") or {})
        title = str(data.get("title") or bvid)
        owner = data.get("owner") or {}

        pages = [item for item in (data.get("pages") or []) if isinstance(item, Mapping)]
        if not pages and data.get("cid"):
            pages = [
                {
                    "page": 1,
                    "cid": data["cid"],
                    "part": title,
                    "duration": data.get("duration"),
                }
            ]
        if not pages:
            raise SourceError(
                code="NO_PAGES",
                message=f"接口没有返回分P 列表：{bvid}",
                hint="可能视频不存在或已删除；核对链接",
            )
        if page:
            chosen = [item for item in pages if int(item.get("page") or 0) == page]
            if not chosen:
                raise UsageError(
                    code="PAGE_NOT_FOUND",
                    message=f"这个视频没有第 {page} P",
                    hint=f"它一共有 {len(pages)} 个分P",
                )
            pages = chosen

        blocks: list[Block] = []
        tracks: list[dict[str, object]] = []
        warnings: list[str] = []
        langs: list[str] = []
        sources: list[str] = []

        for item in pages:
            number = int(item.get("page") or 1)
            cid = int(item.get("cid") or 0)
            if not cid:
                raise SourceError(code="NO_CID", message=f"P{number} 缺少 cid", hint="上游返回异常，稍后再试")

            raw_tracks = self._subtitle_tracks(bvid, cid, number)
            if raw_tracks:
                parsed, candidates, degraded = self._load_tracks(bvid, raw_tracks, number)
                if degraded:
                    warnings.append(degraded)
                track = pick_track(parsed, self.cfg.text.prefer_fulltext, self.cfg.text.prefer_langs)
                if not track.segments:
                    raise SourceError(
                        code="EMPTY_SUBTITLE",
                        message=f"P{number} 的字幕轨都是空的",
                        hint="可能是占位字幕（只有标题）；换一条轨或在配置里调 prefer_langs",
                    )
            elif self.asr_enabled:
                track, candidates, note = self._transcribe_track(bvid, cid, number, data)
                warnings.append(note)
                if not track.segments:
                    raise SourceError(
                        code="ASR_EMPTY",
                        message=f"P{number} 没有字幕轨，本地转写也没有产出文本",
                        hint="这段音频大概率没有语音（纯音乐 / 环境音）—— 没有内容就是没有内容，不要按标题推测",
                    )
            else:
                raise SourceError(
                    code="NO_SUBTITLE",
                    message=f"P{number} 没有任何字幕轨",
                    hint="`--no-asr` 关掉了本地转写；去掉它就会下载音频做转写",
                )

            if len(pages) > 1:
                heading = heading_block(
                    str(item.get("part") or f"P{number}"), page=number, first_index=len(blocks)
                )
                if heading is not None:
                    blocks.append(heading)
            blocks.extend(
                blocks_from_track(track, page=number, first_index=len(blocks))
            )
            tracks.extend(candidates)
            langs.append(track.lang)
            sources.append(track.source)

        return TextResult(
            url=url,
            bvid=bvid,
            title=title,
            author=str(owner.get("name") or ""),
            duration=_as_float(data.get("duration")),
            fetched_at=datetime.now(CST).isoformat(timespec="seconds"),
            blocks=blocks,
            tracks=tracks,
            subtitle_lang=_dominant(langs),
            text_source=_dominant(sources),
            warnings=warnings,
            raw_dir=self._raw_dir(bvid),
        )

    # -- 接口 ---------------------------------------------------------------
    def _preheat(self) -> None:
        """先访问站点根拿匿名 cookie（`buvid3`）。不预热会 `-352`（风控）。

        失败不判死：可能只是抖动；真被风控拒时，后续请求会给出真实错误。
        """
        try:
            self.client.get(
                PREHEAT_URL,
                policy=RequestPolicy(follow_redirects=True),
                max_bytes=300_000,
            )
        except Exception:  # noqa: BLE001 - 预热是尽力而为
            pass

    def _check_credential(self) -> None:
        """`nav` 自检：`code=0` 才算凭证有效（未登录/过期都是 `-101`）。

        **为什么必须在最前面做**（实测）：拿过期 SESSDATA 直接打 `player` 接口，
        B 站回的是 **HTTP 412**，而不是 `-101` —— 光看这个状态码根本猜不到是凭证过期，
        很容易误以为是风控/网络，然后去"降频重试"。先把失败挪到这里，错误说明才准确。
        """
        keys = self.signer.keys()
        if keys.logged_in:
            return
        raise CredentialMissing(
            code="AUTH_EXPIRED",
            message=f"B 站凭证无效或已过期（nav 返回 code={keys.nav_code}）",
            hint=(
                "重新获取 B2T_BILI_SESSDATA：浏览器登录 B 站 → F12 → Application → "
                "Cookies → bilibili.com → 复制 SESSDATA 的值（裸 token），写进 .env。"
                "**不要**用过期凭证继续跑：字幕接口会返回空列表，"
                "那会被误读成『这个视频没字幕』"
            ),
        )

    def _view(self, bvid: str) -> dict[str, Any]:
        cached = self._read_cache(bvid, VIEW_NAME)
        if cached is not None:
            return json.loads(cached)
        payload = parse_json(
            self.client.get(
                API_VIEW,
                params={"bvid": bvid},
                headers={"Referer": DEFAULT_REFERER},
            )
        )
        code = payload.get("code")
        if code != 0:
            raise bili_error(int(code or 0), str(payload.get("message") or ""))
        self._write_cache(bvid, VIEW_NAME, json.dumps(payload, ensure_ascii=False).encode("utf-8"))
        return payload

    def _subtitle_tracks(self, bvid: str, cid: int, number: int) -> list[dict[str, Any]]:
        """字幕轨清单。`-101` 直接判凭证问题 —— 这是"硬失败"的关键落点。

        **只信 WBI 版**：空就是空，不拿非签名接口兜底（见文件头教训 4，那边给的是诱饵）。
        """
        payload = self.signer.get_json(
            API_PLAYER,
            {"bvid": bvid, "cid": cid},
            policy=RequestPolicy(
                with_credentials=True, credential_hosts=(CREDENTIAL_HOST,)
            ),
            headers={"Referer": f"https://www.bilibili.com/video/{bvid}?p={number}"},
        )
        code = payload.get("code")
        if code == -101:
            raise CredentialMissing(
                code="AUTH_EXPIRED",
                message="字幕接口返回 -101（账号未登录）",
                hint=(
                    "B2T_BILI_SESSDATA 缺失或已过期：浏览器登录 B 站 → F12 → Application → "
                    "Cookies → bilibili.com → 复制 SESSDATA 的值，写进 .env"
                ),
            )
        if code != 0:
            raise bili_error(int(code or 0), str(payload.get("message") or ""))
        raw = ((payload.get("data") or {}).get("subtitle") or {}).get("subtitles") or []
        return [dict(item) for item in raw if isinstance(item, Mapping)]

    def _load_tracks(
        self, bvid: str, raw_tracks: list[dict[str, Any]], number: int
    ) -> tuple[list[Any], list[dict[str, object]], str]:
        """下载候选轨并解析。**先按语言过滤，再比字数**（见文件头教训 2）。"""
        entries = [(str(item.get("lan") or ""), item) for item in raw_tracks]
        matched = [entry for entry in entries if _lang_rank(entry[0], self.cfg.text.prefer_langs) == 0]
        if matched:
            pool, degraded = matched, ""
        else:
            pool = entries
            langs = ", ".join(lang or "?" for lang, _ in entries)
            degraded = (
                f"没有命中偏好语言（{self.cfg.text.prefer_langs}）的字幕轨，"
                f"已退回按字数择优 —— 候选：{langs}。"
                "这**可能取到机器译文轨**：检查输出的『正文取自』再看内容语言是否对"
            )

        parsed: list[Any] = []
        for lang, item in pool:
            url = str(item.get("subtitle_url") or "")
            if not url:
                continue
            payload = self._download_subtitle(bvid, url, number, lang)
            track = parse_track(payload, source=self._kind_of(lang, item), lang=lang)
            if track.segments:
                parsed.append(track)

        candidates = track_candidates(
            parsed, self.cfg.text.prefer_fulltext, self.cfg.text.prefer_langs
        )
        return parsed, candidates, degraded

    def _download_subtitle(
        self, bvid: str, url: str, number: int, lang: str
    ) -> dict[str, Any]:
        """下载字幕 JSON。**不带 Cookie**：凭证只发给 api.bilibili.com。"""
        absolute = url if url.startswith("http") else f"https:{url}"
        response = self.client.get(
            absolute,
            policy=RequestPolicy(follow_redirects=True),
            headers={"Referer": DEFAULT_REFERER},
            max_bytes=4 * 1024 * 1024,
        )
        payload = parse_json(response)
        self._write_cache(
            bvid,
            f"p{number:03d}-subtitle-{_safe_token(lang)}.json",
            json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        )
        return payload

    def _transcribe_track(
        self, bvid: str, cid: int, number: int, meta: Mapping[str, Any]
    ) -> tuple[Track, list[dict[str, object]], str]:
        """没有字幕轨 → 下载音频 → 本地转写（L4），返回（轨、候选清单、降级说明）。

        **为什么不再"没字幕就报错"**：B 站大量视频根本没有字幕轨（尤其刚发布的），
        但音轨是取得到的 —— 转写是这类视频拿到正文的唯一路。代价是错字与耗时，
        所以降级说明必须写进输出（`NOTES.md` 纪律：降级必须可见）。
        """
        cfg = self.cfg.whisper
        stem = f"{bvid.lower()}_{cid}"
        # 多分P 必须带 ?p=N：yt-dlp 不带它只会下第一 P 的音频（那样每个 P 都转出同一段话）
        url = f"https://www.bilibili.com/video/{bvid}?p={number}"
        logger.info("P%d 没有字幕轨 → 下载音频并本地转写（%s/%s）", number, cfg.model, cfg.device)
        audio = download_audio(url, self.cfg.cache_dir / MEDIA_SUBDIR, stem=stem, logger=logger)
        try:
            payload = transcribe(
                audio,
                cfg,
                prompt=build_prompt(
                    cfg, title=str(meta.get("title") or ""), extra=str(meta.get("desc") or "")
                ),
                logger=logger,
            )
        finally:
            discard(audio)  # 转写完立即删，成败都删

        logger.info("P%d 转写完成：%s", number, asr_summary(payload))
        self._write_cache(bvid, _asr_name(number), json.dumps(payload, ensure_ascii=False).encode("utf-8"))

        track = parse_track(payload, source="whisper", lang=str(payload.get("lang") or ""))
        info = payload.get("asr") or {}
        note = (
            f"P{number} 没有字幕轨，正文是**本地转写**（{info.get('model')}/{info.get('device')}，"
            f"音频 {info.get('audio_seconds')}s，RTF {info.get('realtime_factor')}）—— "
            f"不是官方字幕，错字与断句都可能不准；其中 {info.get('flagged')} 段被判可疑（正文带 confidence）"
        )
        return (
            track,
            track_candidates([track], self.cfg.text.prefer_fulltext, self.cfg.text.prefer_langs),
            note,
        )

    # -- 缓存 ---------------------------------------------------------------
    def _raw_dir(self, bvid: str) -> Path:
        return self.cfg.cache_dir / "raw" / bvid

    def _read_cache(self, bvid: str, name: str) -> bytes | None:
        path = self._raw_dir(bvid) / name
        return path.read_bytes() if path.is_file() else None

    def _write_cache(self, bvid: str, name: str, data: bytes) -> None:
        if not bvid:
            return
        target = self._raw_dir(bvid) / name
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(target.name + ".tmp")
        temporary.write_bytes(data)
        temporary.replace(target)

    @staticmethod
    def _kind_of(lang: str, item: Mapping[str, Any]) -> str:
        """`ai-zh` 这类是平台 AI 字幕；其余（`zh-CN` 等）按官方 CC 对待。

        这条映射直接决定"来源优先级"里的档位：官方 CC 排在 AI 字幕之前。
        """
        del item  # 保留参数以便上游若加字段时不必改调用点
        return "ai_subtitle" if lang.lower().startswith("ai") else "cc"


def _as_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _safe_token(value: str) -> str:
    return re.sub(r"[^0-9A-Za-z_-]", "", value) or "unknown"


def _dominant(values: list[str]) -> str:
    if not values:
        return ""
    counts: dict[str, int] = {}
    for value in values:
        counts[value] = counts.get(value, 0) + 1
    return max(counts.items(), key=lambda item: item[1])[0]
