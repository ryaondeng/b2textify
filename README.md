# b2textify

把 **B 站视频**与**知乎回答**取成纯文本。字幕优先；没有字幕就下载音频做本地转写。取完之后，可以另起一步把英文稿翻成中文。

只做"取文本"这一件事，不做摘要、不做改写、不臆测内容 —— 拿到的是能回查的原文。

## 能力

| 场景 | 状态 | 说明 |
|---|---|---|
| B 站视频 → 文本 | ✅ | 官方 CC 字幕 / 平台 AI 字幕优先，按语言偏好选轨 |
| 无字幕的视频 | ✅ | `yt-dlp` 取音轨 → `faster-whisper` 本地转写（GPU 可选） |
| 多分 P 视频 | ✅ | 默认全部 P 顺序拼接；`?p=N` 只取一 P |
| 知乎回答 → 文本 | ✅ | 只走官方回答 API（HTML 页与专栏都会被 403） |
| 英文稿 → 中文译文 | ✅ | 独立入口，机器翻译，产出 `<同名>-zh.md` |

**不支持**（会明确报错，不会静默返回空文本）：

- 知乎**专栏**文章（`zhuanlan.zhihu.com/p/…`）——接口要 `x-zse-96` 签名
- `b23.tv` 短链、`av` 号 —— 先在浏览器打开换成 BV 链接
- 字幕与转写双源互校（未实现）

## 安装

需要 **Python ≥ 3.11**（用了标准库 `tomllib`）。

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1

# 只要字幕通道：零第三方依赖
pip install -e .

# 需要"没字幕时本地转写"：装可选依赖（faster-whisper / yt-dlp / onnxruntime）
pip install -e ".[asr]"
```

依赖分层是刻意的：**取数链路一个第三方包都不引入**（`urllib` 够用），重依赖只挂在可选路径上。

## 配置

```powershell
Copy-Item .env.example .env
```

然后按需填 `.env`：

| 变量 | 必填 | 用途 |
|---|---|---|
| `B2T_BILI_SESSDATA` | 取 B 站时必填 | 浏览器登录 → F12 → Application → Cookies → `bilibili.com` → 复制 `SESSDATA` 的值 |
| `B2T_ZHIHU_COOKIE` | 取知乎时必填 | 同上，复制 `z_c0` 的值 |
| `B2T_LLM_API_KEY` | 只翻译文时必填 | OpenAI 兼容接口的 key（默认 DeepSeek） |
| `HF_HUB_CACHE` | 可选 | whisper 模型缓存目录；指到已有缓存就不用重下 medium（约 1.5G） |
| `B2T_CACHE_DIR` | 可选 | 缓存目录，默认 `%LOCALAPPDATA%\b2textify\cache` |
| `B2T_LLM_BASE_URL` / `B2T_LLM_MODEL` | 可选 | 换翻译服务（自建 vLLM / Ollama / 通义都行） |

配置优先级：**命令行参数 > 环境变量（`.env` 供值）> `b2textify.toml` > 内置默认**。

> `.env` 含真实凭证，已在 `.gitignore` 里；`.env.example` 是模板，**只会放占位符**。

## 用法

```powershell
# 取一个视频（缺字幕时自动转写）
b2textify "https://www.bilibili.com/video/BV1HBnH6JEAZ/"

# 只取第 2 P，输出到指定目录
b2textify "https://www.bilibili.com/video/BV1HBnH6JEAZ/?p=2" --out out

# 取知乎回答
b2textify "https://www.zhihu.com/question/2095655093907482113/answer/2096724148315856915"

# 把英文稿翻成中文（吃 md，不吃链接）
b2textify-translate "out/BV1HBnH6JEAZ-xxx.md"
```

常用参数：

| 参数 | 作用 |
|---|---|
| `--out DIR` | 输出目录（默认当前目录） |
| `--cache DIR` | 缓存目录（原始物留在这里） |
| `--config FILE` | 指定 `b2textify.toml`（默认找当前目录或 `~/.config/b2textify/`） |
| `--no-asr` | 没有字幕就直接报错，不下载音频转写 |
| `--quiet` | 只打印结果路径 |

`b2textify-translate` 另有 `--workers`（默认 3 路并行）、`--chunk-chars`（默认 2000）、`--force`（正文已是中文也重译）。

## 产出长什么样

````markdown
# 【官方双语】投身数学，从未有过比现在更好的时候

> 来源：https://www.bilibili.com/video/BV1HBnH6JEAZ/
> 抓取时间：2026-10-23T12:18:43+08:00
> 作者：3Blue1Brown
> 时长：40:44
> 正文取自：本地转写（en）
> 字数：48003

> ⚠️ 取轨时发生降级：
> - P1 没有字幕轨，正文是**本地转写**（medium/cuda，RTF 0.0791）—— 不是官方字幕……

## 正文

[00:00] Well, let's talk about it. It feels like the whole math world is on fire…
[00:07] So I put up this post last week soliciting questions…
````

几处刻意的设计：

- 每段带 `[mm:ss]` 时间锚，能直接回视频核对（知乎正文没有时间轴，就不编锚点）
- **降级必须可见**：用了转写、跳过图片、选不到偏好语言，都会写进头部警告
- 转写稿的低置信片段带「（低置信）」标记，不悄悄给一段"看起来很顺"的文本
- 译文文件保留原稿全部结构，只在头部加"机器翻译、译自哪份、用的哪个模型"

## 目录与缓存

**产出**：`<slug>-<标题>.md`（B 站 slug 是 `BV…`，知乎是 `zhihu-<回答ID>`），默认写当前目录。

**缓存**（可再生的中间物，默认在项目外）：

```
%LOCALAPPDATA%\b2textify\cache\
├─ raw\<slug>\            原始物归档，可离线复查
│   ├─ view.json          B 站元信息
│   ├─ p001-subtitle-*.json   字幕原文
│   ├─ p001-asr.json      转写产物（含逐段置信度）
│   └─ answer.json        知乎回答原始 JSON
├─ media\                 临时音频（转写完立即删）
└─ translate\<md 名>\     译文块缓存（重跑不重复花钱）
```

改位置：`--cache DIR`、或 `.env` 里的 `B2T_CACHE_DIR`、或 `b2textify.toml` 的 `[paths] cache_dir`。

## 架构

```
b2textify <url>
└─ cli.py ──┬─ sources/bilibili.py ─┬─ wbi.py      WBI 签名（w_rid / wts）
            │                       ├─ extract/subtitle.py  解析 / 去重 / 合句 / 选轨
            │                       ├─ extract/asr.py ── media.py   yt-dlp 下载 + whisper
            │                       └─ http.py     零依赖 HTTP（凭证白名单 / 重试 / 限速）
            ├─ sources/zhihu.py ──── extract/html_text.py   HTML → 段落块
            └─ report.py ── mdtext.py ── models/   渲染与格式契约、数据模型

b2textify-translate <md>
└─ translate.py ── mdtext.py（解析/回写） + llm.py（OpenAI 兼容） + 块缓存
```

几条硬约束（改代码前请先读 `NOTES.md`）：

- **依赖单向**：`sources / report / translate → models`，表现层不认识具体来源
- **凭证只发白名单域**：B 站只发 `bilibili.com`，知乎只发 `zhihu.com`，字幕 CDN 不带 Cookie
- **规则只有一份**：语言过滤、选轨、md 格式契约、错误码都不许各写一遍
- **硬失败优先**：宁可报错并给下一步，也不产出"看起来成功"的空文本或错文本

## 开发

```powershell
pip install -e ".[dev]"
pytest -q          # 45 passed
```

测试全部用假客户端/假模型，不联网、不加载真模型。

## 已知限制

- 知乎只支持**回答**；专栏文章报 `ZHUANLAN_UNSUPPORTED`
- 译文是**机器翻译**，未人工校对；专名与术语可能不准
- 转写质量受模型与音质影响，`faster-whisper` medium 在消费级 GPU 上约 0.08 实时倍速
- 字幕与本地转写的互校（`--crosscheck`）尚未实现，参数已移除
