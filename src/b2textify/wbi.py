"""WBI 签名（`docs/90-附录-B站通道技术细节.md` §5.1；落点见 `docs/02-开发计划.md` 的 2.1）。

签名是 B 站"风控后"接口的入场券（`/x/player/wbi/v2`、`/x/web-interface/view/conclusion/get`、
搜索 / 排行榜等）。**本模块是唯一实现**：2.0 的探测脚本与 2.2 的来源实现都从这里取，
不各写一份 —— 阶段 1 复核 N2/N3 的教训就是"同一件事写两处，口径慢慢分家"。

三个实测确认的坑：

1. **密钥未登录也能取**：`/x/web-interface/nav` 会返回 `code=-101`（账号未登录），
   但 `data.wbi_img` 照样有值。所以**取密钥只校验 `data.wbi_img` 在不在，不能看 `code`**
   —— 2026-10-03 未登录实测：
   `{"code":-101,"message":"账号未登录",...,"data":{"wbi_img":{"img_url":...}}}`
2. **密钥会轮换**（B 站按天换），所以要有 TTL，以及"签名被拒后刷新重试"的入口。
3. **值里的 `!'()*` 必须先剔掉**：`urlencode` 会把这些字符百分号编码，
   而服务端算签名前先剔 —— 不剔必然 `-352`。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from hashlib import md5
from typing import Any, Mapping
from urllib.parse import urlencode, urlparse

from .errors import SourceError
from .http import HttpClient, HttpResponse, RequestPolicy

#: B 站域：凭证只发给它（`docs/90-附录` §5.2）
BILI_HOSTS = ("bilibili.com",)

#: nav 接口：签发密钥的入口（不携带凭证也能拿到）
WBI_NAV_URL = "https://api.bilibili.com/x/web-interface/nav"
#: 元信息（标题 / UP / 时长 / `pages[]`）。匿名可用，不需要签名
API_VIEW = "https://api.bilibili.com/x/web-interface/view"
#: 字幕轨清单（L1 CC / L2 AI 字幕）。**需要签名 + 登录态**
API_PLAYER = "https://api.bilibili.com/x/player/wbi/v2"
#: ⚠️ 另有一条 `/x/player/v2`（非签名）**不要用**：它不报错，`code=0`、`data.cid` 也回显，
#: 但 `subtitles` 给的是**别的视频的字幕**（诱饵）。2026-10-23 实测：对 `BV1i3eJ6KEkb`
#: WBI 版返回 8 条正确轨，非签名版返回 1 条内容完全无关的 `ai-zh`。
#: 所以"取不到轨"只能信 WBI 版的空列表，不能拿非签名版兜底。
#: 官方 AI 文稿（"课代表"总结，L3）。需要签名 + 登录态
API_CONCLUSION = "https://api.bilibili.com/x/web-interface/view/conclusion/get"
#: 未登录时 nav 的返回码：**不代表失败**，只代表"没登录"，密钥照样有效
NAV_NOT_LOGGED_IN = -101
#: B 站前端固定的 64 位重排表（`docs/90-附录` §5.1，照抄）
MIXIN_TAB: tuple[int, ...] = (
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27, 43, 5, 49,
    33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13, 37, 48, 7, 16, 24, 55, 40,
    61, 26, 17, 0, 1, 60, 51, 30, 4, 22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11,
    36, 20, 34, 44, 52,
)
#: 参与签名计算前要剔掉的字符（B 站服务端算法如此）
SANITIZE_CHARS = "!'()*"
#: 密钥缓存时长。B 站按天轮换，6 小时是"够省请求、又不至于整天用旧钥"的折中
KEY_TTL_SECONDS = 6 * 3600
#: 这些返回码意味着"签名可能失效"，值得刷新密钥重试一次
WBI_RETRY_CODES = frozenset({-352, -403})


def _basename_token(url: str) -> str:
    """`.../7cd084941338484aae1ad9425b84077c.png` → `7cd0849...c`（去扩展名）。"""
    name = urlparse(url).path.rsplit("/", 1)[-1]
    return name.split(".")[0]


def extract_keys(payload: Mapping[str, Any]) -> tuple[str, str]:
    """从 nav 的 JSON 里取 `(img_key, sub_key)`。

    ⚠️ **只校验 `data.wbi_img`，不看 `code`**：未登录时 `code == -101`，密钥依然可用。
    真正要报错的情况是 `wbi_img` 缺失（接口改版 / 被风控），那时给出去哪查的提示。
    """
    data = payload.get("data") or {}
    wbi_img = data.get("wbi_img") or {}
    img_url = str(wbi_img.get("img_url") or "")
    sub_url = str(wbi_img.get("sub_url") or "")
    if not img_url or not sub_url:
        raise SourceError(
            code="WBI_KEY_MISSING",
            message="nav 接口没有返回 wbi_img（取不到签名密钥）",
            hint="可能是接口改版或请求被风控；先用 yt-dlp 备用通道，并核对 docs/90-附录 §5.1",
            detail={"upstream_code": payload.get("code"), "upstream_message": payload.get("message", "")},
        )
    return _basename_token(img_url), _basename_token(sub_url)


def derive_mixin_key(img_key: str, sub_key: str) -> str:
    """`mixin_key = shuffle(img_key + sub_key)[:32]`。

    真实向量（2026-10-03 未登录 `/nav` 返回值，见 `tests/test_wbi.py`）：
    `img+sub` → `ea1db124af3c7062474693fa704f4ff8`。
    """
    raw = f"{img_key}{sub_key}"
    if len(raw) < len(MIXIN_TAB):
        raise SourceError(
            code="WBI_KEY_MALFORMED",
            message=f"密钥长度不足：拼接后 {len(raw)} 字符，重排表需要 {len(MIXIN_TAB)}",
            hint="服务端返回值已变；核对 docs/90-附录 §5.1 的重排表与取法",
            detail={"img_key": img_key, "sub_key": sub_key},
        )
    return "".join(raw[index] for index in MIXIN_TAB)[:32]


def sanitize(value: Any) -> str:
    """按 B 站算法剔掉 `!'()*`（缺失的 None 不当参数传，故这里只处理非 None）。"""
    text = str(value)
    for char in SANITIZE_CHARS:
        text = text.replace(char, "")
    return text


def sign_params(
    params: Mapping[str, Any],
    mixin_key: str,
    *,
    timestamp: int | float | None = None,
) -> dict[str, str]:
    """生成带 `wts` / `w_rid` 的参数字典（键值全部转字符串）。

    步骤（照 `docs/90-附录` §5.1）：补 `wts` → 按 key 升序 → urlencode → 剔 `!'()*`
    → `md5(query + mixin_key)`。值为 None 的参数直接丢弃（不发无意义的空参数）。
    """
    stamp = int(time.time() if timestamp is None else timestamp)
    signed = {str(key): sanitize(value) for key, value in params.items() if value is not None}
    signed["wts"] = str(stamp)
    query = urlencode(sorted(signed.items()))
    signed["w_rid"] = md5((query + mixin_key).encode("utf-8")).hexdigest()
    return signed


def parse_json(response: HttpResponse, *, hint: str = "", code: str = "API_NOT_JSON") -> dict[str, Any]:
    """把响应当 JSON 解析成 dict（顶层不是对象也算失败）。

    失败时**抛错而不是返回 `{}`**：`{}` 与"上游真的返回了空对象"在产物上分不出来，
    会把风控返回的 HTML 拦截页读成"这个接口没有数据"。

    `code` 可换成更具体的错误码（如 nav 用自己的 `WBI_NAV_NOT_JSON`），
    但**解析逻辑只有这一份** —— 各写一份就是阶段 1 复核 N3 那个坑。
    """
    try:
        payload = json.loads(response.text)
    except (ValueError, TypeError) as exc:
        raise SourceError(
            code=code,
            message="上游返回的不是 JSON",
            hint=hint or "多半是被风控拦截（拿到的是 HTML 拦截页）；稍后重试或降低频率",
            detail={"status": response.status},
        ) from exc
    if not isinstance(payload, dict):
        raise SourceError(
            code="API_NOT_OBJECT",
            message=f"上游 JSON 顶层不是对象，而是 {type(payload).__name__}",
            hint=hint or "接口形态可能已变；核对 docs/90-附录 的接口说明",
            detail={"status": response.status},
        )
    return payload


#: 存档时顶替 `subtitle_url` 的占位符
REDACTED_URL = "<已抹除：签名 token 不入缓存（docs/01 §9.2）>"


def redact_tracks(tracks: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """把一批字幕轨里的 `subtitle_url` 抹成占位符，**其余字段原样保留**。

    为什么必须抹（`docs/01` §9.2）：那个地址**自带签名 token**，拿到它就能一直取那份字幕。
    它是"缓存与 manifest 的禁区"，而 `raw/` 是原文快照、会被长期保留 —— 一样不能落。

    保留其余字段是**有意的**：存档要能回答"这个 P 当时有哪几条字幕轨、什么语言"，
    抹成一整团就失去了审计价值。
    """
    return [
        {**track, "subtitle_url": REDACTED_URL}
        if isinstance(track, Mapping) and "subtitle_url" in track
        else dict(track)
        for track in tracks
    ]


def redact_subtitle_urls(payload: Mapping[str, Any]) -> dict[str, Any]:
    """整个 `player/wbi/v2` 响应版的抹除（探测脚本直接存响应体，用这个）。"""
    data = payload.get("data")
    if not isinstance(data, Mapping):
        return dict(payload)
    subtitle = data.get("subtitle")
    if not isinstance(subtitle, Mapping) or not isinstance(subtitle.get("subtitles"), list):
        return dict(payload)
    return {
        **payload,
        "data": {**data, "subtitle": {**subtitle, "subtitles": redact_tracks(subtitle["subtitles"])}},
    }


@dataclass(frozen=True)
class WbiKeys:
    """一次 nav 取到的密钥对（含取到的时间，用于 TTL 判断）。"""

    img_key: str
    sub_key: str
    fetched_at: float
    #: nav 返回的 code（-101 = 未登录，属正常；只作证据留存，不参与判断）
    nav_code: int = 0

    @property
    def mixin_key(self) -> str:
        return derive_mixin_key(self.img_key, self.sub_key)

    @property
    def logged_in(self) -> bool:
        return self.nav_code != NAV_NOT_LOGGED_IN

    def age(self, now: float | None = None) -> float:
        return max(0.0, (time.time() if now is None else now) - self.fetched_at)

    def is_stale(self, ttl: float = KEY_TTL_SECONDS, now: float | None = None) -> bool:
        return self.age(now) > ttl


class WbiSigner:
    """带缓存的签名器：省掉"每个请求先打一次 nav"。

    用法（探测脚本与 2.2 的来源实现都走这条）：

        signer = WbiSigner(client)
        params = signer.sign({"bvid": bvid, "cid": cid})
    """

    def __init__(self, client: HttpClient, *, ttl: float = KEY_TTL_SECONDS) -> None:
        self.client = client
        self.ttl = ttl
        self._keys: WbiKeys | None = None
        self.fetches = 0
        #: 因签名被拒而换钥重签的次数（观测用：持续增长说明重排表或密钥取法有问题）
        self.signature_retries = 0

    @property
    def keys_cached(self) -> WbiKeys | None:
        return self._keys

    def keys(self, *, force: bool = False) -> WbiKeys:
        """取密钥（有缓存则复用；过期或 `force` 才重新 nav）。"""
        if self._keys is not None and not force and not self._keys.is_stale(self.ttl):
            return self._keys
        return self.refresh()

    def nav_policy(self) -> RequestPolicy:
        """nav 的请求策略：**登记过凭证就带上**。

        为什么 nav 也要带凭证（设计上"取密钥不需要登录"仍然成立）：
        带上之后 `nav` 会返回 `code=0` 且 `data.isLogin=true`，于是这一次请求
        顺带就是**凭证有效性的自检** —— 否则只能等到某个业务接口报 `-101` 才发现过期
        （B 站的风险清单第一条就是"`SESSDATA` 过期"，见 `docs/90-附录` §7）。
        没登记凭证时仍是匿名请求（这是探测 0.1 要测的形态）。
        """
        if self.client.has_credential("api.bilibili.com"):
            return RequestPolicy(with_credentials=True, credential_hosts=BILI_HOSTS)
        return RequestPolicy()

    def refresh(self) -> WbiKeys:
        """强制重新取一次密钥（签名被拒后调用）。"""
        response = self.client.get(WBI_NAV_URL, policy=self.nav_policy())
        payload = parse_json(
            response,
            hint="检查网络 / 代理，或稍后重试（风控时可能返回 HTML 拦截页）",
            code="WBI_NAV_NOT_JSON",
        )
        img_key, sub_key = extract_keys(payload)
        code = payload.get("code")
        self._keys = WbiKeys(
            img_key=img_key,
            sub_key=sub_key,
            fetched_at=time.time(),
            nav_code=code if isinstance(code, int) else 0,
        )
        self.fetches += 1
        return self._keys

    def sign(self, params: Mapping[str, Any]) -> dict[str, str]:
        """签名一组参数（用缓存密钥，必要时自动取一次）。"""
        return sign_params(params, self.keys().mixin_key)

    def sign_retry(self, params: Mapping[str, Any]) -> dict[str, str]:
        """签名被上游拒绝后使用：**换一把新密钥**再签一次。

        为什么单独一个方法：直接重签没用 —— 缓存里还是那把旧钥，重试只会再被拒一次。
        """
        return sign_params(params, self.refresh().mixin_key)

    def get_json(
        self,
        url: str,
        params: Mapping[str, Any] | None = None,
        *,
        policy: RequestPolicy | None = None,
        headers: Mapping[str, Any] | None = None,
        max_bytes: int | None = None,
    ) -> dict[str, Any]:
        """签名 GET + 解析 JSON；**被 `-352` / `-403` 拒时换钥重签一次**。

        为什么必须有这一步：密钥按天轮换，缓存里那把一旦过期，**所有**签名请求会集体
        `-352`。这时"重签"是没用的（用的还是旧钥），必须换钥（`sign_retry`）。

        只重试一次：换钥之后还是 `-352` / `-403`，说明问题不在密钥上（重排表变了、
        内容受限、风控），应当把原始返回交给上层去判断 —— 继续重试只会加重风控。
        """
        query = dict(params or {})
        response = self.client.get(
            url, params=self.sign(query), policy=policy, headers=headers, max_bytes=max_bytes
        )
        payload = parse_json(response)
        if payload.get("code") not in WBI_RETRY_CODES:
            return payload

        self.signature_retries += 1
        response = self.client.get(
            url, params=self.sign_retry(query), policy=policy, headers=headers, max_bytes=max_bytes
        )
        return parse_json(response)
