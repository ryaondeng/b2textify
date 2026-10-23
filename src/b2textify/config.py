"""极简配置：凭证 + 取文本口径 + 缓存目录。

只有三层（够用，刻意不做当初那套五级发现链）：
    内置默认  <  b2textify.toml  <  环境变量
`.env` 是"给环境变量供值"的便捷层，只补缺、不覆盖真实环境变量。
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

#: 凭证键。优先读本项目的名字，**兼容上一版项目的 KEXT_ 前缀** ——
#: 这样过渡期可以直接复用已有的 `.env`，不用重新拷贝 token。
CRED_BILI_ENVS = ("B2T_BILI_SESSDATA", "KEXT_BILI_SESSDATA")
CRED_ZHIHU_ENVS = ("B2T_ZHIHU_COOKIE", "KEXT_ZHIHU_COOKIE")

CONFIG_NAME = "b2textify.toml"
ENV_FILE = ".env"
#: 缓存目录的环境变量名（优先级：命令行 --cache > 它 > toml 的 paths.cache_dir > 默认）
CACHE_DIR_ENV = "B2T_CACHE_DIR"

#: 凭证只发给这些域（写成常量，保证"凭证发给谁"只有一个答案）
BILI_HOST = "bilibili.com"
ZHIHU_HOST = "zhihu.com"


@dataclass
class TextCfg:
    #: 字幕轨**语言偏好**（同层先按语言、再按字数）。默认中文原文轨优先。
    #: 为什么必须有这一维：B 站同一视频有多条 AI 翻译轨，机器译文会膨胀到原文的
    #: 5–6 倍字数，纯按字数选必然选中译文（实测：中文 16,394 字 vs 西语 86,229 字）。
    prefer_langs: list[str] = field(default_factory=lambda: ["ai-zh", "zh*"])
    #: 来源类型优先级：官方字幕 > AI 字幕 > 本地转写
    prefer_fulltext: list[str] = field(default_factory=lambda: ["cc", "ai_subtitle", "whisper"])


@dataclass
class WhisperCfg:
    """本地转写参数（M2 用）。默认值都是踩过坑的结论，别随手改。"""

    model: str = "medium"
    device: str = "cuda"
    compute_type: str = "float16"
    #: 转写语言。**默认空 = 自动检测**：B 站视频不都是中文，写死 `zh` 会让外语视频
    #: 的元信息错报成中文（实测：3B1B 英文视频被标成 zh 且 language_probability=1.0）。
    #: 想强制中文再在 b2textify.toml 里写 `[whisper] language = "zh"`。
    language: str = ""
    initial_prompt_prefix: str = "以下是普通话的句子。"
    hotwords: list[str] = field(default_factory=list)
    #: VAD 默认开：不开会把训练语料里的口播结尾硬编到纯音乐上（"请不吝点赞订阅…"）
    vad_filter: bool = True
    low_logprob: float = -0.6
    high_no_speech: float = 0.5


def _default_cache_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA")
    if base:
        return Path(base) / "b2textify" / "cache"
    return Path.home() / ".cache" / "b2textify"


@dataclass
class Config:
    text: TextCfg = field(default_factory=TextCfg)
    whisper: WhisperCfg = field(default_factory=WhisperCfg)
    cache_dir: Path = field(default_factory=_default_cache_dir)
    out_dir: Path = field(default_factory=Path.cwd)
    source_path: Path | None = None
    env_file: Path | None = None

    # -- 凭证 ---------------------------------------------------------------
    def credential(self, host: str) -> str:
        """取某个域的凭证原文（SESSDATA 值 / z_c0 值）；没有返回空串。"""
        if host.endswith(BILI_HOST):
            return _first_env(CRED_BILI_ENVS)
        if host.endswith(ZHIHU_HOST):
            return _first_env(CRED_ZHIHU_ENVS)
        return ""

    def cookie_header(self, host: str) -> str:
        """该域应带的 Cookie 头。B 站的 SESSDATA 走 Cookie；知乎的 z_c0 同理。"""
        value = self.credential(host)
        if not value:
            return ""
        if host.endswith(BILI_HOST):
            return f"SESSDATA={value}"
        if host.endswith(ZHIHU_HOST):
            return f"z_c0={value}"
        return ""


def _first_env(names: tuple[str, ...]) -> str:
    for name in names:
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return ""


# --------------------------------------------------------------------------
# 加载
# --------------------------------------------------------------------------
def load_env_file(path: Path | None) -> dict[str, str]:
    """把 `.env` 注入进程环境（只补缺、不覆盖），返回实际注入的键值。"""
    if path is None or not path.is_file():
        return {}
    injected: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if not key.isidentifier() or not value:
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key in os.environ:
            continue
        os.environ[key] = value
        injected[key] = value
    return injected


def _discover_config(explicit: str | Path | None) -> Path | None:
    if explicit is not None:
        path = Path(explicit).expanduser()
        if not path.is_file():
            raise FileNotFoundError(f"配置文件不存在：{path}")
        return path
    for candidate in (
        Path.cwd() / CONFIG_NAME,
        Path.home() / ".config" / "b2textify" / CONFIG_NAME,
    ):
        if candidate.is_file():
            return candidate
    return None


def _apply_file(cfg: Config, path: Path) -> None:
    data: dict[str, Any] = tomllib.loads(path.read_text(encoding="utf-8"))
    text = data.get("text") or {}
    if isinstance(text.get("prefer_langs"), list):
        cfg.text.prefer_langs = [str(item) for item in text["prefer_langs"]]
    if isinstance(text.get("prefer_fulltext"), list):
        cfg.text.prefer_fulltext = [str(item) for item in text["prefer_fulltext"]]
    whisper = data.get("whisper") or {}
    for key in ("model", "device", "compute_type", "language", "initial_prompt_prefix"):
        if isinstance(whisper.get(key), str):
            setattr(cfg.whisper, key, whisper[key])
    if isinstance(whisper.get("hotwords"), list):
        cfg.whisper.hotwords = [str(item) for item in whisper["hotwords"]]
    if isinstance(whisper.get("vad_filter"), bool):
        cfg.whisper.vad_filter = whisper["vad_filter"]
    paths = data.get("paths") or {}
    if isinstance(paths.get("cache_dir"), str):
        cfg.cache_dir = Path(paths["cache_dir"]).expanduser()
    if isinstance(paths.get("out_dir"), str):
        cfg.out_dir = Path(paths["out_dir"]).expanduser()


def load_config(
    explicit: str | Path | None = None,
    *,
    cache_dir: str | Path | None = None,
    out_dir: str | Path | None = None,
) -> Config:
    path = _discover_config(explicit)
    env_path = None
    for candidate in (Path.cwd() / ENV_FILE, (path.parent / ENV_FILE) if path else None):
        if candidate and candidate.is_file():
            env_path = candidate
            break
    load_env_file(env_path)

    cfg = Config(source_path=path, env_file=env_path)
    if path is not None:
        _apply_file(cfg, path)
    # 环境变量层（`.env` 就是靠它供值的）：优先级高于 toml，低于命令行显式参数。
    # 缓存目录特别说明：写在 `.env` 里比写 toml 方便（不用每个项目目录放一份配置），
    # 而且它天然是"本机相关"的东西，不该跟着仓库走。
    env_cache = (os.environ.get(CACHE_DIR_ENV) or "").strip()
    if env_cache:
        cfg.cache_dir = Path(env_cache).expanduser()
    if cache_dir is not None:
        cfg.cache_dir = Path(cache_dir).expanduser()
    if out_dir is not None:
        cfg.out_dir = Path(out_dir).expanduser()
    return cfg
