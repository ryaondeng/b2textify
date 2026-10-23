"""知乎回答来源：`z_c0` 登录态 → 回答 API → 正文抽取。

## 实测的边界（2026-10-05 探测，2026-10-23 复测仍成立）

知乎的防护是**按路径分的**，同一枚 `z_c0`：

| 路径 | 匿名 | 带 `z_c0` | 结论 |
|---|---|---|---|
| `/api/v4/answers/{aid}`（单条，含 content） | ❌ | ✅ 200 | **主路径** |
| `/question/…`、`zhuanlan.zhihu.com/p/…` 的 HTML | ❌ 403 | ❌ 403 | 被按客户端特征拦，不走 |
| `/api/v4/articles/{id}`（专栏） | ❌ 403 | ❌ 403 | 需 `x-zse-96` 签名，**明确报不可达** |

所以知乎只走 API 一条路；专栏文章宁可报错，也不退回网页硬抓（那同样 403，
只会产出"看起来像成品"的错东西）。

## 凭证纪律

- 一上来就要 `z_c0`（缺 → `AUTH_MISSING`），不许匿名试探；
- 401/403 或返回 `error.code ∈ {100,401,403}` → `AUTH_EXPIRED`：**指向"重新取 z_c0"**，
  不降级成"这篇文章没内容"（Cookie 过期在知乎是常态）。
"""

from __future__ import annotations

import json
import re
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping

from ..config import Config
from ..errors import CredentialMissing, HttpError, NotImplementedYet, SourceError, UsageError
from ..extract.html_text import blocks_from_html
from ..http import HttpClient, RequestPolicy
from ..models import TextResult

#: 单条回答 API。`include` 这组是实测可用的（多写少写都可能 400）
API_ANSWER = (
    "https://www.zhihu.com/api/v4/answers/{aid}"
    "?include=content,voteup_count,author,created,updated"
)
CREDENTIAL_HOST = "zhihu.com"
ANSWER_NAME = "answer.json"
#: 回答 API 认为"凭证有问题"的错误码（HTTP 200 + error body 的形态也要接住）
AUTH_ERROR_CODES = {100, 401, 403}
CST = timezone(timedelta(hours=8))

ANSWER_RE = re.compile(r"/question/(\d+)/answer/(\d+)")
QUESTION_RE = re.compile(r"/question/(\d+)")
ZHUANLAN_RE = re.compile(r"zhuanlan\.zhihu\.com/p/\d+")


def is_zhihu(url: str) -> bool:
    host = (urllib.parse.urlsplit((url or "").strip()).hostname or "").lower()
    return host == "zhihu.com" or host.endswith(".zhihu.com")


def parse_url(raw: str) -> tuple[str, str]:
    """链接 →（question_id, answer_id）。只支持回答页；专栏与问题页给明确提示。"""
    text = (raw or "").strip()
    if not text:
        raise UsageError(code="URL_EMPTY", message="没有给出链接或回答号")

    match = ANSWER_RE.search(text)
    if match:
        return match.group(1), match.group(2)
    if ZHUANLAN_RE.search(text):
        raise NotImplementedYet(
            code="ZHUANLAN_UNSUPPORTED",
            message="知乎**专栏文章**抓不了（不是链接的问题）",
            hint=(
                "实测专栏接口要 `x-zse-96` 签名，HTML 页也按客户端特征 403。"
                "当前只支持回答页：/question/{qid}/answer/{aid}"
            ),
        )
    if QUESTION_RE.search(text):
        raise UsageError(
            code="ANSWER_ID_MISSING",
            message="这是问题页链接，没说要哪个回答",
            hint="打开目标回答，地址栏是 /question/{qid}/answer/{aid} 形态，复制那个",
        )
    raise UsageError(
        code="ANSWER_ID_MISSING",
        message=f"没能从链接里取到回答 ID：{text[:80]}",
        hint="支持 https://www.zhihu.com/question/{qid}/answer/{aid}",
    )


def _auth_error(status: int, payload: Mapping[str, Any]) -> CredentialMissing | None:
    """凭证问题要显式到能照着做 —— 别把它混成"接口报错"。"""
    error = payload.get("error") if isinstance(payload, Mapping) else None
    code = int((error or {}).get("code") or 0) if isinstance(error, Mapping) else 0
    message = str((error or {}).get("message") or "") if isinstance(error, Mapping) else ""
    if status in (401, 403) or code in AUTH_ERROR_CODES or "登录" in message:
        return CredentialMissing(
            code="AUTH_EXPIRED",
            message=f"知乎凭证无效或已过期（HTTP {status}{f'，error code={code}' if code else ''}）",
            hint=(
                "重新获取 z_c0：浏览器登录 zhihu.com → F12 → Application → Cookies → "
                "zhihu.com → 复制 z_c0 的值，写进 .env 的 B2T_ZHIHU_COOKIE"
            ),
        )
    return None


class ZhihuSource:
    """知乎回答取正文。"""

    name = "zhihu"

    def __init__(self, cfg: Config, client: HttpClient | None = None) -> None:
        self.cfg = cfg
        self.client = client or HttpClient(cfg.cookie_header)

    def fetch(self, url: str) -> TextResult:
        question_id, answer_id = parse_url(url)
        canonical = f"https://www.zhihu.com/question/{question_id}/answer/{answer_id}"
        if not self.cfg.credential(CREDENTIAL_HOST):
            raise CredentialMissing(
                code="AUTH_MISSING",
                message="知乎通道需要 z_c0 —— 匿名请求连回答 API 都会被 403",
                hint=(
                    "登录 zhihu.com → F12 → Application → Cookies → zhihu.com → "
                    "复制 z_c0 的值，写进 .env 的 B2T_ZHIHU_COOKIE"
                ),
            )

        payload = self._answer(answer_id, canonical)
        html = str(payload.get("content") or "")
        if not html.strip():
            raise SourceError(
                code="ZHIHU_EMPTY_ANSWER",
                message=f"回答存在但没有可取的正文：{canonical}",
                hint="可能已被删除/折叠，或 `content` 字段形状变了；原始 JSON 已归档到 raw/",
            )

        blocks = blocks_from_html(html, url=canonical)
        if not blocks:
            raise SourceError(
                code="EXTRACT_EMPTY",
                message=f"没能从回答里抽出正文：{canonical}",
                hint="content 可能是空壳（已删除/折叠），或抽取规则要更新；原始 JSON 在 raw/",
            )

        warnings: list[str] = []
        if "<img" in html:
            warnings.append("正文里有图片，本次只取文字（图片内容不在文本里）")

        return TextResult(
            url=canonical,
            bvid="",
            slug=f"zhihu-{answer_id}",
            title=_question_title(payload) or f"知乎回答 {answer_id}",
            author=str((payload.get("author") or {}).get("name") or ""),
            duration=None,
            fetched_at=datetime.now(CST).isoformat(timespec="seconds"),
            blocks=blocks,
            tracks=[],
            subtitle_lang="zh",
            text_source="html",
            warnings=warnings,
            raw_dir=self._raw_dir(answer_id),
        )

    # -- 接口 ---------------------------------------------------------------
    def _answer(self, answer_id: str, canonical: str) -> dict[str, Any]:
        policy = RequestPolicy(with_credentials=True, credential_hosts=(CREDENTIAL_HOST,))
        try:
            response = self.client.get(
                API_ANSWER.format(aid=answer_id), policy=policy, max_bytes=4_000_000
            )
        except HttpError as exc:
            # 401/403 在这里几乎总是"z_c0 过期"，必须说清而不是当网络错
            status = int(exc.detail.get("status") or 0) if getattr(exc, "detail", None) else 0
            auth = _auth_error(status, {})
            if auth is not None:
                raise auth from exc
            raise SourceError(
                code="ZHIHU_API_ERROR",
                message=f"知乎回答接口请求失败：{exc.message}",
                hint="核对链接是否仍有效；确认 z_c0 是否过期（过期常见，不是 bug）",
            ) from exc

        try:
            payload = json.loads(response.text)
        except ValueError as exc:
            raise SourceError(
                code="ZHIHU_NOT_JSON",
                message="知乎返回的不是 JSON",
                hint="多半是被风控拦了（拿到 HTML 拦截页）；稍后再试",
                detail={"status": response.status},
            ) from exc
        if not isinstance(payload, dict):
            raise SourceError(code="ZHIHU_NOT_OBJECT", message="知乎返回的 JSON 顶层不是对象")
        if response.status != 200 or "error" in payload:
            auth = _auth_error(response.status, payload)
            if auth is not None:
                raise auth
            raise SourceError(
                code="ZHIHU_API_ERROR",
                message=f"知乎回答接口返回错误：{str((payload.get('error') or {}).get('message') or '')[:120]}",
                hint="核对链接；若知乎改了接口，更新 API_ANSWER 的 include 参数",
            )

        self._write_cache(answer_id, payload)
        return payload

    # -- 缓存 ---------------------------------------------------------------
    def _raw_dir(self, answer_id: str) -> Path:
        return self.cfg.cache_dir / "raw" / f"zhihu-{answer_id}"

    def _write_cache(self, answer_id: str, payload: Mapping[str, Any]) -> None:
        target = self._raw_dir(answer_id) / ANSWER_NAME
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(target.name + ".tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(target)


def _question_title(payload: Mapping[str, Any]) -> str:
    question = payload.get("question")
    return str((question or {}).get("title") or "") if isinstance(question, Mapping) else ""
