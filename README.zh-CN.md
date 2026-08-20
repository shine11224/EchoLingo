# EchoLingo

把你真正感兴趣的英文视频、音频和文章，整理成一套可以听懂、精读、练习和复习的个人课程。

[English](README.md) · [快速开始](#快速开始) · [安装与配置](#安装与配置) · [功能说明](#功能说明) · [常见问题](#常见问题) · [项目文档](#项目文档)

> EchoLingo 是一个本地优先、面向个人学习的开源英语学习工具。它不会替你选择“标准教材”，而是尽量保留原始素材的声音、句子和上下文，再把 AI 用在整理、解释与反馈上。

![EchoLingo 课程导入页](docs/screenshots/import-sources.png)

## 快速开始

```bash
git clone https://github.com/shine11224/EchoLingo.git
cd EchoLingo
python -m venv .venv        # macOS 用 python3.11 -m venv .venv（见“运行环境”）
source .venv/bin/activate   # Windows PowerShell: .\.venv\Scripts\Activate.ps1
pip install -r requirements.txt   # 首次安装需下载数 GB，20–30 分钟属正常
cp .env.example .env        # Windows: Copy-Item .env.example .env
python backend/fastapi_server.py  # 然后打开 http://localhost:5173
```

需要 Python 3.11 和 FFmpeg。各平台细节见[安装与配置](#安装与配置)。

## 平台支持

| 能力 | Windows | macOS | Linux |
| --- | --- | --- | --- |
| 课程导入、听读、词汇 | ✓ | ✓ | ✓ |
| 本地 Whisper / Groq 转写 | ✓ | ✓ | ✓ |
| 扫描版 PDF OCR（Tesseract / EasyOCR） | ✓ | ✓ | ✓ |
| 本地翻译一键安装（HY-MT） | ✓ | 手动配置 | 手动配置 |
| Release 安装包 | ✓ | —（源码安装） | —（源码安装） |

## 为什么做这个工具

我收藏过很多英文课程、YouTube / B 站视频和文章，但它们大多一直躺在网盘和收藏夹里。真正开始看时，我又很容易依赖一键翻译：内容似乎看懂了，声音仍然听不出来，想表达时也用不出来。

EchoLingo 最开始只是一个很简单的想法：把自己感兴趣的英文内容，整理成可以逐句播放、对照字幕和反复听的课程。后来在开发和使用过程中，我逐渐把它整理成一条完整的学习路径：

**选择感兴趣的素材 → 先听懂 → 在句子里学词 → 精读表达 → 复述与造句 → 回到语境中复习**

这套流程背后有五个原则：

1. **先听懂。** 先建立“声音 → 词 → 意义”的连接，再进入更细的词汇和句式分析。
2. **Learn words in sentences。** 单词不脱离原句学习；查询、收藏和复习都尽量带回第一次遇见它的语境。
3. **兴趣驱动。** 能长期学习的素材，往往是自己本来就想看的内容。
4. **i + 1 难度递进。** 用字幕模式和自定义词表控制提示量，让每次学习只比当前水平多一步。
5. **在使用中掌握语言。** 复述、情境造句和 AI 反馈用于把“看懂了”推进到“自己会用”。

AI 在这里不是学习主体。它主要负责生成导航、解释词句、整理重点和提供反馈；原声、原句、你的判断和你的输出始终是学习的中心。

## 学习流程

### 1. 导入自己想学的内容

支持 YouTube、Bilibili、文章链接、本地音视频、文本 / PDF，以及可选的百度网盘导入。课程生成过程会根据你的配置完成转写、翻译和词汇标记。

### 2. 先把内容听懂

Listening 模式支持逐句播放、倍速、循环和句子收藏，并提供盲听、中文字幕、英文字幕、中英对照、英文字幕 + 生词注释等显示方式。推荐顺序是先盲听，再用中文确认意思，随后回到英文找出没有听出来的词和表达；你也可以按自己的习惯自由切换。

![Listening 学习页](docs/screenshots/listening-workspace.png)

### 3. 回到完整语境阅读

Reading 模式保留整篇字幕文稿。你可以点击任意句子播放原声，连续播放一段内容，查询单词，收藏句子，并看到已启用词表的高亮与释义。

![Reading 模式与词汇高亮](docs/screenshots/reading-mode.png)

### 4. 精学词汇、发音和句式

精读页按句子展开音标、重读与连读、重点词汇、口语表达和句式结构。AI 可以从素材中筛选值得学习的词与表达，但分析结果仍应结合原文和词典判断。

![逐句精读与 Sentence Workshop](docs/screenshots/intensive-study.png)

### 5. 用复述和造句完成输出

收藏的句子可以进入整句复述：先听原声、隐藏原文、凭记忆复述，再让 AI 对比遗漏和误听。重点句式还可以生成情境造句练习，获得纠错、改写和更自然表达的建议。

<p>
  <img src="docs/screenshots/retelling-practice.png" alt="整句复述与 AI 对比" width="49%">
  <img src="docs/screenshots/pattern-practice.png" alt="句式复用与 AI 批改" width="49%">
</p>

### 6. 在原句中复习词汇

重点词可以进入词汇记忆工坊，按掌握程度、词频、考试标签和自定义标签筛选。每个词都保留素材中的原句与原声，也可以继续展开词卡或进行情境造句。你还可以选择多个词生成一段记忆故事，把孤立词汇重新放回可理解的上下文中。

<p>
  <img src="docs/screenshots/vocabulary-workshop.png" alt="词汇记忆工坊" width="49%">
  <img src="docs/screenshots/vocabulary-story.png" alt="词汇记忆故事" width="49%">
</p>

## 功能说明

### 不配 AI Key 也能用

在配置任何 AI 服务之前，EchoLingo 已经可以完成很多事：内置 ECDICT 词典（释义、音标、词频、词形变化）、内置常用词 / 四六级 / 考研 / 雅思 / 托福 / GRE / COCA 七套词表、听读模式、句子收藏和本地学习数据都不需要 Key。AI 大纲、问答、深度分析、批改和记忆故事需要 `AI_API_KEY`。转写和翻译是否需要网络，取决于你选择本地组件还是云端服务。

### 素材与课程

- YouTube、Bilibili 和普通文章链接导入
- 本地音频、视频、TXT / Markdown、DOCX 和 PDF 导入
- PDF 自动路由：优先读取文本层，扫描页使用 Tesseract，低置信度时可升级到 EasyOCR
- 百度网盘分享链接或应用数据目录导入（可选）
- 文本素材可生成朗读音频（TTS）
- 本地 Whisper 或 Groq 云端转写
- 本地 HY-MT 或已配置接口生成中文字幕

### 听力与阅读

- 盲听、中文字幕、英文字幕、中英对照、英文 + 生词注释
- 逐句播放、整段连续播放、循环和倍速
- 点击字幕单词查询，句子收藏与标签
- AI 内容大纲与时间导航
- 选中字幕向 AI 提问，并导出问答记录

### 词汇与句式

- 内置 ECDICT：释义、音标、词频和词形变化，无需 API Key
- 内置常用词、四六级、考研、雅思、托福、GRE、COCA 等词表
- 上传 `.txt` / `.csv` 自定义词表，可选择自动扩展常见词形
- 词表命中后在之后的课程中自动高亮并显示释义
- 重点词卡、近义词、搭配、例句和素材内例句筛选
- 句式结构、口语表达和发音现象分析

### 输出与复习

- 整句听力复述与 AI 对比
- 情境造句、句式复用、AI 批改和参考改写
- 词汇熟悉度、掌握状态、标签和词频筛选
- 多词记忆故事、朗读和继续造句
- 导出 Markdown、HTML、CSV / JSON / TXT，以及 Anki 词汇文件

## 安装与配置

### 运行环境

- Python 3.11 — 用 `python --version` 确认。如果系统只有较旧的 `python3`（macOS 常见），先安装 3.11：例如 `uv python install 3.11`（然后 `uv venv --python 3.11` 创建环境）、Homebrew 或 python.org 安装包。
- Git
- [FFmpeg](https://ffmpeg.org/download.html)（音视频处理必需；安装后确保 `ffmpeg` 在 `PATH` 中，或在 `.env` 里用 `FFMPEG_PATH` 指向可执行文件的完整路径）
- 现代浏览器
- 可选：NVIDIA GPU。没有 GPU 也可以使用较小的本地 Whisper 模型，或配置 Groq 转写。

### 1. 克隆并安装依赖

```bash
git clone https://github.com/shine11224/EchoLingo.git
cd EchoLingo
```

Windows PowerShell：

```powershell
py -3.11 -m venv .venv      # 或：python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env
```

macOS / Linux：

```bash
python3.11 -m venv .venv    # 任何能得到 Python 3.11 解释器的命令都可以
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

首次 `pip install` 会下载数 GB 依赖（PyTorch 等大包），通常需要 20–30 分钟。长时间没有输出是正常现象，不是卡死。

### 2. 配置 AI 接口

编辑 `.env`，或启动后在“设置”页填写同样的内容：

```dotenv
AI_API_KEY=your_key
AI_BASE_URL=https://api.deepseek.com
AI_MODEL=deepseek-v4-flash

# 可选：Groq 云端 Whisper 转写, 注册Groq账号后具备免费额度可覆盖使用
GROQ_API_KEY=

# 可选：本地 MDX 词典目录；ECDICT 已内置，无需配置
DICT_DIR=
```

`AI_API_KEY` 用于大纲、AI 伴读、词句分析、批改和记忆故事等功能。EchoLingo 通过 OpenAI Chat Completions 兼容接口调用模型；设置页提供 DeepSeek、Qwen、Kimi Platform 和 Kimi Code 的预设，也可以手动填写其他兼容服务的 Base URL 与模型 ID。

> 模型名和可用区域可能随服务商调整，请以你所使用平台的控制台为准。Kimi Platform 与 Kimi Code 使用不同的地址和 Key，不要混用。

### 3. 启动

```bash
python backend/fastapi_server.py
```

浏览器打开 [http://localhost:5173](http://localhost:5173)。首次使用建议先进入“设置”，确认 AI、转写和翻译三项状态。交互式 API 文档在 [http://localhost:5173/api/docs](http://localhost:5173/api/docs)。

> **网络暴露提示：** 默认服务监听 `0.0.0.0:5173`，无鉴权、CORS 放开，同一网络下的其他设备都能访问（启动日志会打印局域网地址）。如果只想本机使用，在 `.env` 中设置 `ELT_HOST=127.0.0.1`。改端口用 `ELT_PORT`。

### Windows Release 安装包

Release 目前只提供 Windows 包（`EchoLingo-<版本>-windows.zip`）；macOS 和 Linux 请按上文从源码安装。

每个 `v*` 标签都会生成一个 `EchoLingo-<版本>-windows.zip` Release 资产。
下载并解压后，在解压目录中用 PowerShell 执行：

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\installer\install.ps1 -InstallAll
.\installer\start.ps1
```

安装包只包含公开源码和安装脚本，不包含私有 `.env`、Python 虚拟环境、
原生二进制或模型权重。`-InstallAll` 会通过 `winget` 安装 Python 3.11、
Docling/EasyOCR 可选组件、LGPL FFmpeg shared 版本和 Tesseract。想要更小的
安装，可以分别使用 `-InstallOptional` 和/或 `-InstallNativeTools`。已有的
`.env` 默认不会被覆盖，只有显式传入 `-ForceEnv` 才会覆盖。

## 可选组件

### 本地 Whisper 转写

在“设置 → 本地 Whisper”中选择并下载模型。模型越大通常精度越高，也会占用更多磁盘、内存和计算时间。没有本地模型时，可以填写 `GROQ_API_KEY` 使用云端 Whisper 转写，注册groq后可赠送免费额度使用

### 本地中文字幕翻译

Windows x64 可在设置页安装 Tencent HY-MT1.5 与配套的 llama.cpp 运行时。安装前会显示来源、固定版本和 SHA-256，安装完成后可以在本机生成中文字幕。该模型受[腾讯混元社区许可](https://github.com/Tencent-Hunyuan/HY-MT/blob/main/License.txt)约束，并有地区限制；完整说明见 [THIRD_PARTY_LICENSES.md](THIRD_PARTY_LICENSES.md)。

如果没有安装本地翻译组件，翻译需要使用已配置的翻译 / AI 接口。macOS 与 Linux 暂不提供同样的一键安装流程，可按 llama.cpp 与 HY-MT 的官方说明手动配置。

### PDF 深度解析

PDF 导入会按下面的链路自动选择：

1. 如果已安装 Docling，优先使用它进行版面感知解析；未安装或无法提取有效文本时，回退到内置的 `pdfplumber` 文本层解析。
2. 如果没有可用文本层，对扫描页渲染图片，再使用 Tesseract OCR。
3. 如果 Tesseract 置信度低于阈值，且已安装 EasyOCR，则自动升级到 EasyOCR。

默认环境包含轻量的文本提取依赖和 Tesseract 的 Python 封装。需要扫描 PDF 支持时，可一次安装可选的 PDF/OCR 组件：

```bash
pip install -r requirements-optional.txt
```

`pytesseract` 只是 Python 封装，仍需自行安装 Tesseract 可执行程序，并将它加入 `PATH`。EasyOCR 是可选的增强回退方案，首次使用会下载识别模型。普通的文字型 PDF 不需要安装这两个 OCR 后端。

设置 `ELT_DOCLING=off` 可以跳过 Docling；设置 `ELT_OCR_ENGINE=tesseract` 或 `easyocr` 可以强制指定 OCR 后端；保持 `auto` 即使用上面的自动路由。

### 百度网盘

百度网盘是可选能力。进入“设置 → 百度网盘”，确认安装官方 `bdpan` 组件并完成本地 OAuth 授权。EchoLingo 不读取百度密码或浏览器 Cookie，授权信息由 `bdpan` 保存在当前电脑；访问范围限制在 `/apps/bdpan/`。详细步骤见 [docs/BAIDU_PAN.md](docs/BAIDU_PAN.md)。

## 配置速查

| 配置 | 是否必需 | 用途 |
| --- | --- | --- |
| `AI_API_KEY` | AI 功能必需 | 大纲、问答、分析、批改、故事生成 |
| `AI_BASE_URL` | AI 功能必需 | OpenAI 兼容接口地址 |
| `AI_MODEL` | AI 功能必需 | 模型 ID |
| `GROQ_API_KEY` | 可选 | Groq 云端 Whisper 转写 |
| `HY_TRANSLATE_API_KEY` | 可选 | 独立的 HY 翻译接口；可在设置页保存 |
| `HY_TRANSLATE_MODEL` | 可选 | HY 翻译模型名 |
| `DICT_DIR` | 可选 | 额外的本地 MDX 词典目录 |
| `ELT_HOST` | 可选 | 监听地址，默认 `0.0.0.0`（所有网卡）；只限本机用 `127.0.0.1` |
| `ELT_PORT` | 可选 | 端口，默认 `5173` |
| `ELT_AUTH_ENABLED` | 可选 | 在包含认证模块的构建中设为 `1` 可开启登录/多用户模式；公开版始终是无鉴权的单用户模式 |
| `FFMPEG_PATH` | 可选 | ffmpeg 可执行文件完整路径；优先于内置副本和 `PATH` |
| `ELT_CONFIG_DIR` | 可选 | 读取 `.env` 和平台 Cookie 文件的目录；默认为项目根目录 |
| `ELT_TIMEZONE` | 可选 | 定时任务的 IANA 时区，默认 `Asia/Shanghai` |
| `ELT_TTS_CONCURRENCY` | 可选 | TTS 并发生成数，默认 `3` |
| `ELT_MEDIA_UPLOAD_MAX_MB` | 可选 | 浏览器上传大小上限（MB），默认 `500` |
| `ELT_BAIDU_PAN_ENABLED` | 可选 | 设为 `0` 关闭百度网盘集成 |
| `ELT_DOCLING=off` | 可选 | 禁用已安装的 Docling |
| `ELT_OCR_ENGINE` | 可选 | 扫描 PDF 的 OCR 后端：`auto`、`tesseract` 或 `easyocr` |
| `ELT_OCR_CONFIDENCE_THRESHOLD` | 可选 | 自动模式升级 EasyOCR 的 Tesseract 置信度阈值（0–100，默认 `65`） |
| `ELT_OCR_LANG` | 可选 | Tesseract 语言，默认 `eng` |
| `ELT_EASYOCR_LANGS` | 可选 | EasyOCR 语言列表（逗号分隔），默认 `en` |
| `ELT_EASYOCR_GPU` | 可选 | EasyOCR GPU 模式：`auto`、`true` 或 `false` |
| `ELT_EASYOCR_MODEL_DIR` | 可选 | EasyOCR 模型下载目录 |

不配 `AI_API_KEY` 时哪些功能可用，见[不配 AI Key 也能用](#不配-ai-key-也能用)。

## 常见问题

- **`git clone` 报 `unexpected disconnect while reading sideband packet`。** 拉取中途断连。重试，或直接下载源码快照：`https://codeload.github.com/shine11224/EchoLingo/zip/refs/heads/main`。
- **macOS / Linux 提示 `python: command not found`。** 显式使用 3.11 解释器：`python3.11 -m venv .venv`，或 `uv venv --python 3.11`。
- **`pip install` 像卡住了。** 首次安装要下载数 GB 依赖，20–30 分钟没有多少输出是正常的。
- **提示找不到 ffmpeg。** 安装 FFmpeg 并加入 `PATH`，或用 `FFMPEG_PATH` 指向可执行文件完整路径。Windows Release 安装器可以自动安装。
- **5173 端口被占用。** 在 `.env` 中设置 `ELT_PORT=<空闲端口>`。
- **Whisper 模型存在哪里？** `~/.cache/huggingface/hub` 或项目目录下的 `.cache/huggingface/hub`；可用 `HUGGINGFACE_HUB_CACHE` 或 `HF_HOME` 改位置。
- **`http://localhost:5173/docs` 返回 404。** API 文档已搬到 `/api/docs`（ReDoc 在 `/api/redoc`，schema 在 `/api/openapi.json`）。

## 数据与使用边界
- 公开版是单用户本地应用，不包含账号、订阅、多用户协作或云同步。
- 课程、收藏和学习记录保存在运行 EchoLingo 的电脑上；第三方 AI / 转写接口会接收完成请求所需的文本或音频，请自行了解所选服务商的隐私政策。
- YouTube、Bilibili、百度网盘等第三方来源可能因登录、Cookie、地区或平台策略变化而导入失败。
- AI 生成的词义、句式分析和批改可能出错；重要内容请结合原文和词典复核。
- 请只处理你有权使用的内容，并遵守素材来源平台的条款与版权要求。

## 项目文档

- [词典说明](docs/DICTIONARIES.md)
- [词表说明](docs/WORDLISTS.md)
- [百度网盘配置](docs/BAIDU_PAN.md)
- [贡献指南](CONTRIBUTING.md)
- [安全策略](SECURITY.md)
- [第三方许可证](THIRD_PARTY_LICENSES.md)
- [Windows Release 安装器](installer/README.md)
- 交互式 API 文档：服务运行时的 `/api/docs`

## 许可证

[PolyForm Noncommercial 1.0.0](LICENSE)：个人、教育与非营利使用免费；商业使用需要另行获得作者授权。第三方组件继续遵循各自的许可证，详见 [THIRD_PARTY_LICENSES.md](THIRD_PARTY_LICENSES.md)。
