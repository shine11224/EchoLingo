# EchoLingo

**English version → [README.md](README.md)**

**本地优先、可自托管的英语学习工作台**：把自己的视频、播客、文章变成交互式课程——隐藏原文精听、整句复述 + AI 对比、句式提取复用 + AI 批改、词汇复习一站完成。本地 Whisper 转写、本地翻译、MDX 词典，AI 兼容任意 OpenAI 接口（DeepSeek / OpenAI / Groq / Ollama…）。

## 为什么是 EchoLingo

- **数据不出本机**：课程、词汇库、缓存全部存在你自己电脑上；无账号、无订阅
- **真实语料，不是预制教材**：学你真正感兴趣的内容——YouTube、B 站、外刊文章、本地音视频都可以
- **输入到输出的完整闭环**：隐藏原文精听 → 整句复述 + AI 对比 → 句式提取复用 + AI 批改 → 词汇记忆故事
- **AI 可插拔**：任意 OpenAI 兼容接口随你换；转写、翻译、词典全部本地运行，离线也能用核心功能

## 它是怎么工作的

1. **创建课程**：粘贴 YouTube / B 站链接、文章 URL，或上传本地音视频、粘贴文本。系统自动抓取字幕或用本地 faster-whisper 转写，然后断句、翻译、标注音标和连读现象。
2. **精学课程**：逐句循环播放；点击任意单词查看 MDX 词典（牛津 / 朗文）释义；遇到好句子、生词一键收藏。
3. **句子库复习**：默认隐藏原文，先听音自评"听懂 / 听不懂"；然后整句复述，AI 从信息完整度、遗漏点、语法、表达四个维度对比你的版本和原句；再提取可复用句式造句，AI 批改并给出参考改写和更地道的表达。
4. **词汇复习**：按"不认识 → 模糊 → 认识 → 已掌握"的生命周期管理；可以生成 AI 记忆故事把当天的词编进 narrative 里，还能边读故事边和 AI 讨论；随时导出 Markdown / HTML / Anki。

## 功能一览

- **多种来源**：YouTube、Bilibili、文章 URL、本地音视频、纯文本 / Markdown
- **课程生成**：字幕抓取或本地 faster-whisper 转写、智能断句、翻译、音标标注、连读与口语分析
- **交互式课程页**：逐句循环播放、MDX 词典查词（牛津 / 朗文）、AI 提示与练习
- **句子库**：从课程或 AI 输出中收藏句子；隐藏原文精听；整句复述 + AI 四象限对比；句式练习由 AI 批改并给出改写 + 更地道表达（这些 AI 生成句也能再收藏）
- **词汇系统**：CEFR 分级高亮、个人词汇本与复习生命周期、AI 记忆故事（支持对话提问）、导出（Markdown / HTML / Anki）
- **本地优先**：课程、词汇数据库、缓存都在本机；AI 功能兼容任意 OpenAI 接口

## 快速开始

需要 Python 3.11+ 和 [ffmpeg](https://ffmpeg.org/)（加入 PATH）。

```bash
git clone https://github.com/shine11224/EchoLingo.git
cd EchoLingo
python -m venv .venv
.venv\Scripts\activate        # Windows
# source .venv/bin/activate   # macOS / Linux
pip install -r requirements.txt
```

配置 API key：

```bash
cp .env.example .env          # 然后编辑 .env，只填 AI_API_KEY 就能跑
```

启动：

```bash
python backend/fastapi_server.py
# 浏览器打开 http://localhost:5173
```

## 使用指南

**第一课**：在首页粘贴 YouTube 或 B 站链接创建课程。精学建议选 2–10 分钟的短视频；本地文件和文章链接用法相同。

**课程页**：点击句子循环播放；点击单词查词典并加入词汇本；AI 面板可以翻译、讲解语法、针对当前句子自由提问。

**句子库**（首页标签页）：你收藏的所有句子都在这里。推荐的每日流程：

1. **精听**：原文默认隐藏，先播放音频，自评"听懂 / 听不懂"，系统据此排序复习优先级
2. **复述**：点开 🎙 复述，凭记忆说出整句（也可以打字），AI 对比分析你的复述与原句的差异
3. **句式练习**：提取这句的可复用句式，自由造句（或让 AI 生成中文情景提示），提交后 AI 批改，给出参考改写和更地道的表达——改写得好可以顺手收藏

**词汇**（首页标签页）：复习卡先给语境、自评后才显示释义，避免"看着眼熟"的假熟练。想要叙事强化时，用当天的词生成 AI 记忆故事；随时导出（Markdown / HTML / Anki）。

**设置**（应用内）：设置页写的就是同一个 `.env` 文件——AI key、Whisper 模型大小、词典目录、词表启停都可以在页面上改，不用手动编辑文件。

## 配置说明

所有配置都在 `.env`（或应用内设置页，两者等价）。只有 `AI_API_KEY` 是 AI 功能必需的，其余都有降级方案：

- `AI_API_KEY` / `AI_BASE_URL` / `AI_MODEL`：任意 OpenAI 兼容聊天接口（默认 DeepSeek）
- `GROQ_API_KEY`：可选，云端快速转写备选（没有它本地 Whisper 照常工作）
- `DICT_DIR`：可选，MDX 词典目录（自动探测欧路词典目录）；详见 `docs/DICTIONARIES.md`
- 第三方词表（BNC/COCA、BSL、NAWL…）不随仓库分发，下载与编译方法见 `docs/WORDLISTS.md`

## 文档

- `docs/WORDLISTS.md`——词表来源与编译方法
- `docs/DICTIONARIES.md`——MDX 词典来源与配置

## 许可证

[PolyForm Noncommercial 1.0.0](LICENSE)——个人、教育和非营利使用免费；商业使用需另行获得作者授权。第三方组件遵循其各自许可证，详见 [THIRD_PARTY_LICENSES.md](THIRD_PARTY_LICENSES.md)。
