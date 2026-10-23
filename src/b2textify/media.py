"""音频获取（yt-dlp）：下载 → 交给转写 → 立刻删。

为什么单独一层，而不是塞进 `sources/bilibili.py`：

1. 它是**联网**的那一半（转写是纯本地计算），与 `http.py` 同一性质：出网的东西要收口；
2. 以后加别的视频源时要复用**同一份**下载与清理逻辑 —— 两处各写一份必然分家
   （下载参数、重试策略、临时文件清理，每一项都能各自漂移）。

四个参数不是随手写的，每个都对应一次真实故障：

| 参数 | 为什么必须有 |
|---|---|
| `--no-playlist` | 不带它，多分P 视频会把每个 P 都下一遍（77 P 直接拖到超时） |
| `encoding="utf-8"` | Windows 下 `text=True` 默认按 GBK 解，yt-dlp 的中文标题会让它抛 `UnicodeDecodeError` |
| `-f bestaudio/best` | 只要音轨（B 站是 DASH 分离流，1 小时约 50–60 MB）。但**不能只写 `bestaudio`**：不是所有站点都有标准格式体系，那时会 "Requested format is not available"，`/best` 回落到合并流 |
| 重试整个提取 | B 站会下发**解析不了的 PCDN 节点**（`*.mcdn.bilivideo.cn`），同一视频连着两次失败；重跑一次通常换到能用的节点 |

音频一律落 `cache/media/`：它是**可再生**的中间物，不是原文 ——
转写产物（`raw/<bvid>/pNNN-asr.json`）才是要长期留的那份。
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import sys
import time
from pathlib import Path

from .errors import DependencyMissing, SourceError

#: 单次下载超时（秒）。1 小时视频在正常网络下 1–2 分钟，600 s 是宽裕值
DEFAULT_TIMEOUT = 600.0
#: 重试次数：**整个提取重跑**（重新取流会换 CDN 节点），不是重试同一个地址
DEFAULT_ATTEMPTS = 3
#: 两次尝试之间的等待：给源站/CDN 一点时间
RETRY_WAIT = 2.0
#: media/ 下的文件都长这样（`{stem}.m4a`）
MEDIA_SUBDIR = "media"
#: 音频格式选择器。`bestaudio` 是首选；`/best` 是给"没有标准格式体系"的站点留的回落
DEFAULT_FORMAT = "bestaudio/best"


def ytdlp_command() -> list[str]:
    """拼出调用 yt-dlp 的命令前缀。**不改依赖项目录**：先看同 venv，再看 PATH，最后用模块。

    最后那条兜底是踩出来的：venv 里 `pip install yt-dlp` 之后没生成 `yt-dlp.exe`（或 PATH
    没刷新）时，`shutil.which` 找不到，但 `python -m yt_dlp` 是好的 —— 不该因此判死。
    """
    for name in ("yt-dlp.exe", "yt-dlp"):
        local = Path(sys.executable).with_name(name)
        if local.is_file():
            return [str(local)]
    found = shutil.which("yt-dlp")
    if found:
        return [found]
    try:
        import yt_dlp  # noqa: F401,PLC0415 - 只在兜底路径上探测
    except ImportError as exc:
        raise DependencyMissing(
            message="找不到 yt-dlp（本地转写需要它下载音频）",
            hint='装它：pip install -e ".[asr]"（或单独 pip install yt-dlp）',
        ) from exc
    return [sys.executable, "-m", "yt_dlp"]


def download_audio(
    url: str,
    dest_dir: Path,
    *,
    stem: str = "",
    attempts: int = DEFAULT_ATTEMPTS,
    timeout: float = DEFAULT_TIMEOUT,
    fmt: str = DEFAULT_FORMAT,
    logger: logging.Logger | None = None,
) -> Path:
    """下载一路音频，返回落盘路径。失败时抛 `SourceError`（含最后一次的原始输出）。

    `stem` 是文件名主体（调用方给 `{bvid}_{cid}` 这类稳定名字）：产物名可预测，
    "上次下到一半"的残留才识别得出来。
    """
    log = logger or logging.getLogger("b2textify.media")
    command = ytdlp_command()
    dest_dir.mkdir(parents=True, exist_ok=True)
    pattern = str(dest_dir / (f"{stem}.%(ext)s" if stem else "%(id)s.%(ext)s"))
    glob = f"{stem}.*" if stem else "*"

    last_error = ""
    for attempt in range(1, max(1, attempts) + 1):
        done = subprocess.run(
            [*command, "-f", fmt, "--no-playlist", "--no-warnings", "-o", pattern, url],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
        files = [path for path in dest_dir.glob(glob) if path.is_file()]
        if done.returncode == 0 and files:
            return max(files, key=lambda path: path.stat().st_size)
        # 失败时把残留清掉：半截文件比没有更危险（后面会当成"下好了"）
        for path in files:
            path.unlink(missing_ok=True)
        last_error = " / ".join((done.stderr or "").strip().splitlines()[-2:]) or f"退出码 {done.returncode}"
        if attempt < attempts:
            log.warning("音频下载第 %d 次失败，%.0fs 后重试：%s", attempt, RETRY_WAIT, last_error)
            time.sleep(RETRY_WAIT)

    raise SourceError(
        code="AUDIO_DOWNLOAD_FAILED",
        message=f"下载音频失败（已试 {attempts} 次）：{url}",
        hint=(
            f"最后一次的错误：{last_error}。"
            "B 站偶发下发解析不了的 PCDN 节点（*.mcdn.bilivideo.cn），重试通常能换到可用的节点；"
            "一直失败就稍后再跑，或核对链接是否可达"
        ),
        detail={"url": url, "attempts": attempts},
    )


def discard(path: Path | None) -> None:
    """删掉临时音频（**转写完立即删**）。

    删不掉不算失败：它是中间物，残留也只是占磁盘 —— 但**要说出来**，
    否则"音频一直堆着"这件事没人会注意到。
    """
    if path is None:
        return
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:  # pragma: no cover - 依赖文件系统状态
        logging.getLogger("b2textify.media").warning("临时音频删不掉（不影响结果）：%s（%s）", path, exc)
