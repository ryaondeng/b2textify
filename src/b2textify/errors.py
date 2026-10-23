"""结构化错误：错误码 → 人话提示（见 docs/01-架构设计.md §9.1）。

对外契约：退出码语义是冻结的（见 docs/04-可扩展性与健壮性.md §8），
改动需走文档升版，不在这里随手调整。

原则：**可以降级，但降级必须可见**。任何"猜一个结果凑过去"的实现都应当抛错。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, ClassVar

# --------------------------------------------------------------------------
# 退出码（对外契约）
# --------------------------------------------------------------------------
EXIT_OK = 0               # 成功，且（如涉及）校验硬错误清零
EXIT_HARD = 1             # 运行失败 / 校验硬错误未清零
EXIT_USAGE = 2            # 用法错误（参数不对、work 不存在等）
EXIT_ENV = 3              # 环境问题（依赖缺失、凭证缺失、目录不可写）
EXIT_NOT_IMPLEMENTED = 4  # 功能尚未实现（按排期在后续阶段交付）


@dataclass
class KextError(Exception):
    """所有可预期错误的基类。

    code    机器可读错误码（上游业务码或自定义字符串）
    message 面向人的一句话说明
    hint    怎么修。永远不要让用户自己猜
    detail  附加结构化信息（进 JSON 输出，不进日志时注意不要放凭证）

    字段全部带默认值，这样各子类可以安全地覆盖 code 默认值
    （否则 dataclass 会因为"非默认参数跟在默认参数后"而报错）。
    """

    code: str = "ERROR"
    message: str = ""
    hint: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)

    exit_code: ClassVar[int] = EXIT_HARD

    def __str__(self) -> str:
        if self.hint:
            return f"[{self.code}] {self.message}\n  → {self.hint}"
        return f"[{self.code}] {self.message}"


@dataclass
class UsageError(KextError):
    """参数或引用对象有问题。"""

    code: str = "USAGE"
    exit_code: ClassVar[int] = EXIT_USAGE


@dataclass
class EnvError(KextError):
    """环境不满足要求（依赖 / 凭证 / 目录）。"""

    exit_code: ClassVar[int] = EXIT_ENV


@dataclass
class DependencyMissing(EnvError):
    """缺少运行依赖。hint 必须给出可直接执行的安装命令。"""

    code: str = "DEP_MISSING"


@dataclass
class CredentialMissing(EnvError):
    """凭证缺失或过期（B 站 SESSDATA / 知乎 z_c0）。

    绝不静默降级成"这个页面没有内容"——那会让用户以为源站没数据。
    """

    code: str = "AUTH_MISSING"


@dataclass
class SourceError(KextError):
    """外部来源报错。code 直接沿用上游错误码（如 B 站的 -101 / -412）。"""


@dataclass
class VerifyFailed(KextError):
    """覆盖校验未通过（漏项 / 数字冲突 / 疑似杜撰）。"""

    code: str = "VERIFY_FAILED"


@dataclass
class NotImplementedYet(KextError):
    """功能尚未实现。message 里带上计划阶段，避免用"未知错误"糊过去。"""

    code: str = "NOT_IMPLEMENTED"
    exit_code: ClassVar[int] = EXIT_NOT_IMPLEMENTED


@dataclass
class RegistryError(KextError):
    """扩展点注册出错（重名注册、查不到实现）。"""

    code: str = "REGISTRY"


@dataclass
class SchemaError(KextError):
    """数据结构版本问题（缺版本、版本超前、迁移器缺失）。"""

    code: str = "SCHEMA"


@dataclass
class StorageError(KextError):
    """存档读写问题（产物缺失、目录不可写、不允许的操作）。"""

    code: str = "STORAGE"


@dataclass
class HttpError(KextError):
    """网络层问题：请求失败、被风控、响应超限、请求策略冲突。"""

    code: str = "HTTP"


# --------------------------------------------------------------------------
# B 站上游错误码 → 可执行提示（见 docs/01-架构设计.md §9.1 与 90-附录 §5.3）
# --------------------------------------------------------------------------
BILI_ERRORS: dict[int, tuple[str, str]] = {
    -412: ("触发风控", "已按 1.5/3/4.5s 退避重试；仍失败请降低频率或稍后再试"),
    -352: ("风控校验失败", "多为 WBI 签名失效，可尝试走 yt-dlp 通道"),
    -101: (
        "账号未登录",
        "凭证缺失或已过期：设置环境变量 KEXT_BILI_SESSDATA（浏览器 DevTools → Application → "
        "Cookies → bilibili.com → 复制 SESSDATA 的值，裸 token）",
    ),
    -403: ("访问权限不足", "可能是签名失效或内容受限（充电专属 / 仅粉丝可见）"),
    -404: ("内容不存在", "检查链接是否有效、视频是否已删除"),
}


def bili_error(code: int, message: str = "") -> KextError:
    """把 B 站返回码翻译成带修复指引的错误。"""
    summary, hint = BILI_ERRORS.get(
        code, (message or "未知错误", "请在 docs/90-附录-B站通道技术细节.md 中查证并补充该错误码")
    )
    return SourceError(
        code=str(code),
        message=f"{summary}（{message}）" if message else summary,
        detail={"upstream_code": code, "upstream_message": message},
        hint=hint,
    )
