"""最小 HTTP 层：只用标准库（urllib），刻意不引第三方依赖。

需要的三件事，多一件都不做：
1. **凭证只发白名单域**（B 站 SESSDATA 只发给 bilibili.com；字幕 CDN 不带 Cookie）；
2. 匿名 cookie 预热（B 站不先访问站点根会 `-352` 风控）；
3. 限速 + 失败退避（`-412` 是风控，靠重试硬顶只会更糟）。

对 `wbi.py` 的形状承诺（移植件依赖这三样）：
`RequestPolicy(with_credentials, credential_hosts, follow_redirects)`、
`HttpResponse.text / .status`、`HttpClient.get(url, params=, policy=, headers=, max_bytes=)`。
"""

from __future__ import annotations

import gzip
import json
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from dataclasses import dataclass, field
from typing import Any, Mapping

from .errors import HttpError

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


@dataclass(frozen=True)
class RequestPolicy:
    """一次请求的凭证与重定向策略。默认**不带凭证** —— 要带必须显式声明域。"""

    with_credentials: bool = False
    credential_hosts: tuple[str, ...] = ()
    follow_redirects: bool = True
    timeout: float = 20.0


@dataclass
class HttpResponse:
    url: str
    status: int
    headers: dict[str, str] = field(default_factory=dict)
    content: bytes = b""
    #: 原始 `Set-Cookie` 行（匿名 cookie 靠它保留；预热后才有 `buvid3`）
    set_cookies: list[str] = field(default_factory=list)

    @property
    def text(self) -> str:
        for encoding in ("utf-8", "gbk"):
            try:
                return self.content.decode(encoding)
            except UnicodeDecodeError:
                continue
        return self.content.decode("utf-8", errors="replace")

    def json(self) -> Any:
        return json.loads(self.text)


def _domain_key(host: str) -> str:
    """`api.bilibili.com` / `www.bilibili.com` → `bilibili.com`。

    只取最后两段：对本工具涉及的两个站点（bilibili.com、zhihu.com）够用，
    也避免引入公共后缀表那种重依赖。
    """
    parts = [part for part in host.split(".") if part]
    return ".".join(parts[-2:]) if len(parts) >= 2 else host


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[override]
        return None


class HttpClient:
    """极简 HTTP 客户端。低频取文本场景，不需要连接池与并发。"""

    def __init__(self, cookie_header: Any = None, *, min_interval: float = 0.4) -> None:
        #: `cookie_header(host) -> "k=v"`；由 config 提供，凭证不落日志
        self._cookie_header = cookie_header
        self._anonymous: dict[str, dict[str, str]] = {}
        self._min_interval = min_interval
        self._last = 0.0

    # -- 凭证 ---------------------------------------------------------------
    def has_credential(self, host: str) -> bool:
        if self._cookie_header is None:
            return False
        return bool(self._cookie_header(host))

    # -- 请求 ---------------------------------------------------------------
    def get(
        self,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        policy: RequestPolicy | None = None,
        headers: Mapping[str, Any] | None = None,
        max_bytes: int | None = None,
    ) -> HttpResponse:
        policy = policy or RequestPolicy()
        full_url = url
        if params:
            separator = "&" if "?" in url else "?"
            full_url = f"{url}{separator}{urllib.parse.urlencode(params)}"

        host = urllib.parse.urlsplit(full_url).hostname or ""
        request_headers = {
            "User-Agent": UA,
            "Accept": "*/*",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Accept-Encoding": "gzip, deflate",
        }
        for key, value in (headers or {}).items():
            request_headers[str(key)] = str(value)
        cookie = self._cookie_for(host, policy)
        if cookie:
            request_headers["Cookie"] = cookie

        delays = (1.5, 3.0, 4.5)
        last_error: Exception | None = None
        for attempt in range(len(delays) + 1):
            self._throttle()
            try:
                response = self._open(full_url, request_headers, policy)
            except urllib.error.HTTPError as exc:
                last_error = exc
                if exc.code in (412, 429, 500, 502, 503) and attempt < len(delays):
                    time.sleep(delays[attempt])
                    continue
                raise HttpError(
                    code="HTTP_STATUS",
                    message=f"请求失败：HTTP {exc.code}（{host}）",
                    hint="B 站 -412 是风控：降低频率、稍后再试，不要靠反复重试硬顶",
                    detail={"status": exc.code},
                ) from exc
            except urllib.error.URLError as exc:
                last_error = exc
                if attempt < len(delays):
                    time.sleep(delays[attempt])
                    continue
                raise HttpError(
                    code="HTTP_FAILED",
                    message=f"连不上 {host}：{exc.reason}",
                    hint="检查网络与代理；B 站/知乎在部分网络下需要代理",
                ) from exc

            self._remember(host, response.set_cookies)
            return response

        raise HttpError(
            code="HTTP_FAILED",
            message=f"重试后仍失败：{last_error}",
            hint="稍后再试；连续失败多半是网络或风控",
        )

    def post_json(
        self,
        url: str,
        payload: Mapping[str, Any],
        *,
        policy: RequestPolicy | None = None,
        headers: Mapping[str, Any] | None = None,
        max_bytes: int | None = 8 * 1024 * 1024,
    ) -> HttpResponse:
        """POST 一个 JSON 并返回原始响应（**不解析**：解析口径留给调用方）。

        为什么放在这一层而不是在 `translate.py` 里自开 urllib：重试、节流、错误翻译
        （`-412` / 429 / 5xx）都已经在这里，复制一份必然分家 —— 与 `wbi.parse_json`
        只留一份解析是同一个理由。
        """
        policy = policy or RequestPolicy()
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request_headers = {
            "User-Agent": UA,
            "Accept": "application/json",
            "Content-Type": "application/json; charset=utf-8",
            "Accept-Encoding": "gzip, deflate",
        }
        for key, value in (headers or {}).items():
            request_headers[str(key)] = str(value)

        # POST 不重放 body 之外的东西：整段请求重试是安全的（接口都是幂等的对话调用）
        last_error: Exception | None = None
        for attempt in range(2):
            self._throttle()
            try:
                return self._open(url, request_headers, policy, max_bytes=max_bytes, data=body, method="POST")
            except urllib.error.HTTPError as exc:
                last_error = exc
                if exc.code in (429, 500, 502, 503) and attempt == 0:
                    time.sleep(3.0)
                    continue
                raise HttpError(
                    code="HTTP_STATUS",
                    message=f"请求失败：HTTP {exc.code}（{url}）",
                    hint="稍后再试；429 是限流，连续失败就降频",
                    detail={"status": exc.code},
                ) from exc
            except urllib.error.URLError as exc:
                last_error = exc
                if attempt == 0:
                    time.sleep(2.0)
                    continue
                raise HttpError(
                    code="HTTP_FAILED",
                    message=f"连不上 {url}：{exc.reason}",
                    hint="检查网络与代理",
                ) from exc

        raise HttpError(
            code="HTTP_FAILED",
            message=f"重试后仍失败：{last_error}",
            hint="稍后再试；连续失败多半是网络或限流",
        )

    # -- 内部 ---------------------------------------------------------------
    def _throttle(self) -> None:
        wait = self._min_interval - (time.monotonic() - self._last)
        if wait > 0:
            time.sleep(wait)
        self._last = time.monotonic()

    def _cookie_for(self, host: str, policy: RequestPolicy) -> str:
        parts: list[str] = []
        # 匿名 cookie 按**可注册域**共享：预热在 www.bilibili.com 拿到的 buvid3
        # 必须能带到 api.bilibili.com，否则每次都被 -412 风控（这个坑很隐蔽：
        # 响应 200、cookie 也确实存下了，只是没发出去）
        anonymous = self._anonymous.get(_domain_key(host)) or {}
        parts.extend(f"{key}={value}" for key, value in anonymous.items())
        if policy.with_credentials and any(
            host == allowed or host.endswith("." + allowed) for allowed in policy.credential_hosts
        ):
            if self._cookie_header is not None:
                credential = self._cookie_header(host)
                if credential:
                    parts.append(credential)
        return "; ".join(parts)

    def _open(
        self,
        url: str,
        headers: dict[str, str],
        policy: RequestPolicy,
        max_bytes: int | None = None,
        data: bytes | None = None,
        method: str = "GET",
    ) -> HttpResponse:
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        openers: list[Any] = []
        if not policy.follow_redirects:
            openers.append(_NoRedirect())
        opener = urllib.request.build_opener(*openers) if openers else urllib.request.build_opener()
        with opener.open(request, timeout=policy.timeout) as raw:
            status = int(getattr(raw, "status", 200))
            # 超限直接截断会让 JSON 解析失败，所以先挡住"异常大的响应"再读
            declared = raw.headers.get("Content-Length")
            if max_bytes and declared and int(declared) > max_bytes:
                raise HttpError(
                    code="RESPONSE_TOO_LARGE",
                    message=f"响应超限：{declared} > {max_bytes} 字节",
                    hint="接口可能返回了异常内容（如风控页）；稍后再试或降低频率",
                )
            body = raw.read(max_bytes) if max_bytes else raw.read()
            response_headers = {key.lower(): value for key, value in raw.headers.items()}
            set_cookies = list(raw.headers.get_all("Set-Cookie") or [])
        encoding = (response_headers.get("content-encoding") or "").lower()
        if encoding == "gzip":
            body = gzip.decompress(body)
        elif encoding == "deflate":
            body = zlib.decompress(body)
        return HttpResponse(
            url=url,
            status=status,
            headers=response_headers,
            content=body,
            set_cookies=set_cookies,
        )

    def _remember(self, host: str, set_cookies: list[str]) -> None:
        """记住匿名 cookie（`buvid3` 等）——预热一次，后续请求才不被 -352。"""
        if not set_cookies:
            return
        jar = self._anonymous.setdefault(_domain_key(host), {})
        for raw in set_cookies:
            pair = raw.split(";", 1)[0]
            if "=" not in pair:
                continue
            key, _, value = pair.partition("=")
            key, value = key.strip(), value.strip()
            if key and value:
                jar[key] = value
