"""OpenAI 兼容的对话接口（**只用标准库**，与 `http.py` 的零依赖口径一致）。

为什么不用官方 SDK：这个项目对第三方依赖是"能不引就不引"（`pyproject` 里跑提取链路
一个依赖都没有）。翻译只需要"一次 POST + 取 `choices[0].message.content`"，
上 SDK 换来的是几十 MB 的依赖树和另一套重试/超时语义。

默认打 DeepSeek（`https://api.deepseek.com`，`deepseek-chat`）。换任何 OpenAI 兼容服务
（自建 vLLM、通义、Moonshot…）只要改 `B2T_LLM_BASE_URL` / `B2T_LLM_MODEL`。

**Key 只从环境变量读**，不落日志、不进产出：`.env` 由 `config.load_env_file` 注入。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any, Mapping

from .errors import DependencyMissing, SourceError
from .http import HttpClient, RequestPolicy

#: 默认端点与模型（DeepSeek 的 OpenAI 兼容接口）
DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-chat"
#: **本项目的名字优先**，其次兼容直接叫 DEEPSEEK_API_KEY 的环境（根目录那个 agent.py 就用它）
API_KEY_ENVS = ("B2T_LLM_API_KEY", "DEEPSEEK_API_KEY")
#: 单次对话超时：翻译一段 2000 字的英文，正常几秒到几十秒
DEFAULT_TIMEOUT = 120.0


@dataclass
class LlmCfg:
    """一次翻译/对话要用的连接参数。字段少是刻意的：多一个都算配置负担。"""

    api_key: str = ""
    base_url: str = DEFAULT_BASE_URL
    model: str = DEFAULT_MODEL
    temperature: float = 0.2
    max_tokens: int = 4096

    @classmethod
    def from_env(cls) -> LlmCfg:
        """从环境变量读（`load_env_file` 已经把 `.env` 注进去了）。"""
        key = ""
        for name in API_KEY_ENVS:
            value = (os.environ.get(name) or "").strip()
            if value:
                key = value
                break
        return cls(
            api_key=key,
            base_url=(os.environ.get("B2T_LLM_BASE_URL") or DEFAULT_BASE_URL).strip().rstrip("/"),
            model=(os.environ.get("B2T_LLM_MODEL") or DEFAULT_MODEL).strip() or DEFAULT_MODEL,
            temperature=_as_float(os.environ.get("B2T_LLM_TEMPERATURE"), 0.2),
            max_tokens=int(_as_float(os.environ.get("B2T_LLM_MAX_TOKENS"), 4096)),
        )

    @property
    def endpoint(self) -> str:
        """`/v1/chat/completions`。DeepSeek 的 base 不带 `/v1`，两种写法都兼容。"""
        base = self.base_url
        if base.endswith("/chat/completions"):
            return base
        return base + ("" if base.endswith("/v1") else "/v1") + "/chat/completions"

    def require_key(self) -> str:
        if not self.api_key:
            raise DependencyMissing(
                code="LLM_KEY_MISSING",
                message="没有配置对话模型的 API Key",
                hint=(
                    "把 key 写进项目根 `.env`：B2T_LLM_API_KEY=sk-…（或 DEEPSEEK_API_KEY=…）；"
                    "已有的 key 不会自动继承 —— 只有 `.env` 与本进程环境变量会被读到"
                ),
            )
        return self.api_key


def chat(
    messages: list[Mapping[str, str]],
    *,
    cfg: LlmCfg | None = None,
    json_mode: bool = False,
    client: HttpClient | None = None,
    policy: RequestPolicy | None = None,
) -> str:
    """一次对话，返回 `choices[0].message.content` 原文。

    `json_mode=True` 时请求 `response_format={"type":"json_object"}`（OpenAI 兼容服务普遍支持）。
    这里**不解析 JSON**：解析失败要说清是哪一段出的问题，交给调用方带着上下文报错。
    """
    cfg = cfg or LlmCfg.from_env()
    key = cfg.require_key()
    payload: dict[str, Any] = {
        "model": cfg.model,
        "messages": [dict(item) for item in messages],
        "temperature": cfg.temperature,
        "max_tokens": cfg.max_tokens,
        "stream": False,
    }
    if json_mode:
        payload["response_format"] = {"type": "json_object"}

    http = client or HttpClient(None, min_interval=0.0)
    response = http.post_json(
        cfg.endpoint,
        payload,
        policy=policy or RequestPolicy(timeout=DEFAULT_TIMEOUT),
        headers={"Authorization": f"Bearer {key}"},
    )
    try:
        body = json.loads(response.text)
    except ValueError as exc:
        raise SourceError(
            code="LLM_NOT_JSON",
            message="对话接口返回的不是 JSON",
            hint="多半是 base_url 指错了（打到了网页/代理页）；核对 B2T_LLM_BASE_URL",
            detail={"status": response.status, "head": response.text[:200]},
        ) from exc

    choices = body.get("choices") or []
    content = ""
    if isinstance(choices, list) and choices:
        message = choices[0].get("message") or {}
        content = str(message.get("content") or "")
    if not content.strip():
        raise SourceError(
            code="LLM_EMPTY",
            message="对话接口返回了空内容",
            hint="换一次请求；持续为空检查模型名（B2T_LLM_MODEL）是否与端点匹配",
            detail={"status": response.status, "upstream": str(body)[:300]},
        )
    return content


def _as_float(raw: Any, default: float) -> float:
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default
