# 声画工坊 / VisuSound Workshop — 本地优先的音视频生产工作台

> 一个把「字幕提取 · 语音转写 · 图片 OCR · 系统录音 · AI 配音 · 声音克隆 · 视频评论」收进同一个工作台的全功能媒体处理工具。数据默认不出本机，适合创作者、字幕组、短视频运营做本地化的二创流水线。

---

## ✨ 功能矩阵（13 个模块）

| 模块 | 说明 | 状态 |
|---|---|---|
| **仪表盘** | 概览指标、视频本地化流水线入口、最近活动、项目列表 | ✅ 已完成 |
| **项目管理** | 多项目归档、状态跟踪（SQLite 持久化） | 🧩 占位 / 规划中 |
| **字幕提取** | ffprobe 探测 → ffmpeg 提取 SRT/VTT/ASS，支持 B站/YouTube 等 9 平台 + 弹幕兜底 + 双语合并 | ✅ 已有（`/api/extract`） |
| **图片识别** | Tesseract OCR，中英双语 | ✅ 已有（`/api/ocr`） |
| **语音转写** | Whisper 语音识别，自动简繁转换，输出带时间轴文本 | ✅ 已有（`/api/transcribe`） |
| **系统录音** | macOS 系统音频采集（BlackHole 虚拟声卡） | ✅ 已有（`/api/record/*`） |
| **AI 配音** | 文本 → 语音，情绪 + 语速/音调/音量参数，声音卡片网格，预览播放器 | 🧪 Mock 先行（TTS 引擎待接入） |
| **多人批量配音** | 多说话人分角色批量合成 | 🧪 Mock 先行 |
| **声音库** | 音色元数据管理（音色卡、分类筛选） | 🧪 Mock 先行 |
| **声音克隆** | 上传人声片段 → 波形可视化选段 → 克隆；内置合规授权提醒（呼应《深度合成管理规定》） | 🧪 Mock 先行 |
| **视频评论** | 抖音链接 → 分析视频文案 → 生成**字数可控**的评论（已接真实大模型） | ✅ 已接 LLM（DeepSeek 等） |
| **任务队列** | 批量任务异步排队 + 进度持久化 | 🧩 占位 / 规划中 |
| **设置** | 偏好与密钥持久化 | 🧩 占位 / 规划中 |

> 图例：✅ 可用　🧪 Mock 阶段（UI 完整，后端为占位/模板，引擎待接入）　🧩 规划中

---

## 🎨 视觉风格

统一深色「专业录音棚控制台」调性，全站共享一套设计 Token：

- **主强调色**：青绿 `#00d4aa`（VU 表灯意象）
- **底色**：`#0a0a0f`（深灰蓝，非纯黑护眼）、侧栏 `#0f0f1a`、卡片 `#14142a`
- **辅助色**：紫 `#7c5cfc`（创意功能）、琥珀 `#f59e0b`（进行中）、红 `#ef4444`（错误）
- **字体**：Inter（英文/数字）+ PingFang SC（中文）+ JetBrains Mono（等宽：时间码/字幕/日志）
- **侧栏**：220px 图标 + 文字宽栏，顶栏 + 底部状态栏

设计 Token 集中在 `static/style.css`，所有页面共用；工作台外壳由 `app.py` 的 `page_shell()` 统一生成。

---

## 🚀 快速开始

### 1. 环境要求

- **Python 3.10+**
- **FFmpeg** (ffmpeg + ffprobe) — [下载](https://ffmpeg.org/)
- **Tesseract OCR**（可选，图片 OCR 用） — [下载](https://github.com/tesseract-ocr/tesseract)

```bash
# macOS
brew install ffmpeg tesseract tesseract-lang

# Ubuntu / Debian
sudo apt install ffmpeg tesseract-ocr tesseract-ocr-chi-sim

# Windows (choco)
choco install ffmpeg tesseract
```

### 2. 安装 Python 依赖

```bash
cd /path/to/project
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 可选能力
pip install yt-dlp            # 平台视频（B站/YouTube/…）
pip install openai-whisper    # AI 语音转写
```

### 3. 配置大模型密钥（视频评论模块）

视频评论已接真实 LLM，密钥通过 `.env` 文件注入（**服务启动时读取，改完需重启**）：

```bash
cp .env.example .env
# 编辑 .env，取消注释并填入任意一个厂商的 key
```

支持的变量（放任意一个即可，程序自动探测 provider）：

| 厂商 | `.env` 变量 |
|---|---|
| 豆包 / 火山方舟（与抖音同源，推荐） | `ARK_API_KEY` 或 `DOUBAO_API_KEY` |
| DeepSeek | `DEEPSEEK_API_KEY` |
| 通义千问 | `QWEN_API_KEY` |
| OpenAI | `OPENAI_API_KEY` |

> 未配置密钥时，视频评论自动降级为 Mock 模板（接口签名不变，UI 顶部提示框会标明当前模式）。`.env` 已在 `.gitignore` 中，不会误提交。

### 4. 启动

```bash
uvicorn app:app --reload --port 8080
```

打开 **http://localhost:8080/**（落地页）或 **http://localhost:8080/app**（工作台）。

---

## 🧭 Web UI 路由

| 路径 | 页面 |
|---|---|
| `/` | 营销落地页 |
| `/app` | 仪表盘 |
| `/app/{page}` | 各功能页：`dashboard` `project` `subtitle` `ocr` `transcribe` `record` `tts` `batch-dubbing` `sound-library` `voice-clone` `comment` `queue` `settings` |
| `/tools` | 旧版字幕提取工具界面（保留，不影响现有能力） |

---

## 💬 视频评论模块（已接真实大模型）

输入抖音链接（或直接粘贴文案）→ 分析视频文案（复用字幕提取 / Whisper 转写）→ 生成**字数可控**的多条评论。

- **字数可控**：UI 滑块 20–300 字，prompt 约束 + 后处理截断兜底，确保不超限。
- **语气可选**：走心 / 有趣 / 提问 / 犀利 / 客观，或自动轮换。
- **降级安全**：抖音链接解析失败 / 无字幕时，自动回退通用模板，不报错。

```bash
# 接口
POST /api/comment/generate    # 入参: text|video_url, max_words, count, tone
GET  /api/comment/status      # 返回是否已接入真实 LLM（configured / provider）
```

> 注：「分析视频」当前为 Mock 阶段——粘贴文案时由真实 LLM 生成评论；填抖音链接自动下载取字幕这步，抖音反爬 + 短链解析仍在实测打通中。

---

## 🔌 API 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| `GET` | `/` | 落地页 |
| `GET` | `/app` | 工作台（仪表盘） |
| `POST` | `/api/extract` | 字幕提取 |
| `POST` | `/api/ocr` | 图片文字识别 |
| `POST` | `/api/transcribe` | 语音转文字 |
| `GET`/`POST` | `/api/record/*` | 系统录音 |
| `POST` | `/api/comment/generate` | 视频评论生成（真实 LLM） |
| `GET` | `/api/comment/status` | 视频评论 LLM 接入状态 |
| `GET` | `/api/download/{filename}` | 下载文件 |

---

## 🖥 CLI 使用

```bash
# 提取本地文件字幕
python main.py extract video.mp4

# 批量处理目录
python main.py extract /path/to/videos/

# 从 URL 提取
python main.py extract https://www.bilibili.com/video/BV1xx

# 访问预检
python main.py check https://example.com/video.mp4

# 需要 Cookie 的场景
python main.py extract https://example.com/video.mp4 --cookies "session_id=abc123"
```

---

## 🏗 架构

```
用户输入 (文件 / URL / 抖音链接)
   │
   ├─ 字幕提取     src/ (classify → check_access → fetcher → extractor → formatter)
   ├─ 语音转写     app.py → Whisper
   ├─ 图片 OCR     app.py → Tesseract
   ├─ 系统录音     app.py → BlackHole
   └─ 视频评论     app.py → (复用 extract/transcribe 取文案) → LLM 生成（.env 驱动）
        │
        ▼
   FastAPI 多页面（page_shell 共享外壳 + static/style.css 设计 Token）
        │
        ▼
   仪表盘 / 各功能页（原生 HTML + CSS + 少量 vanilla JS，无前端框架）
```

后端以 FastAPI 提供 `/api/*`；前端为服务端渲染的多页面，统一深色青绿主题，零构建步骤。

---

## 📁 项目结构

```
.
├── app.py                 # FastAPI 主入口（路由 + page_shell 外壳 + 视频评论）
├── main.py                # CLI 入口 (argparse)
├── requirements.txt       # Python 依赖
├── .env.example           # 大模型密钥模板（复制为 .env 使用）
├── README.md
├── src/
│   ├── models.py          # 数据模型（枚举 / 数据类）
│   ├── source.py          # 源类型检测分类
│   ├── gateway.py         # 权限预检（HTTP / DRM / Cookie）
│   ├── extractor.py       # 提取引擎（ffprobe + ffmpeg）
│   ├── fetcher.py         # 远程视频下载（httpx / yt-dlp）
│   └── formatter.py       # 输出格式化 + 双语合并 + 弹幕解析
├── templates/
│   ├── landing.html       # 营销落地页（深色青绿）
│   └── index.html         # 旧版工具界面（保留于 /tools）
├── static/
│   ├── style.css          # 设计 Token + 组件样式（全站共用）
│   ├── uploads/           # 用户上传文件
│   └── downloads/         # 提取产物
└── .venv/                 # 虚拟环境
```

---

## 📦 依赖

| 依赖 | 用途 | 必选 |
|---|---|---|
| Python 3.10+ | 运行环境 | ✅ |
| FFmpeg | 视频探测 + 字幕提取 | ✅ |
| FastAPI + uvicorn | Web 服务 | ✅ |
| httpx | HTTP 请求 / LLM 调用 | ✅ |
| python-multipart | 表单上传 | ✅ |
| yt-dlp | 平台视频下载（B站/YouTube/…） | ❌ |
| openai-whisper | AI 语音转写 | ❌ |
| pytesseract + Pillow | 图片 OCR | ❌ |
| Tesseract OCR（系统） | OCR 引擎 | ❌ |
| zhconv | 繁简中文转换 | ❌ |

---

## ⚖️ 合规提醒

- **声音克隆**属于《互联网信息服务深度合成管理规定》监管范围：使用他人声音克隆需取得明确授权，不得用于造假、冒用。
- 声音克隆模块已内置授权提醒；后续将加入**深度合成标识**（导出音频自动加显性声明 / 不可闻水印）与**敏感内容过滤**。
- 视频评论生成内容由用户自行负责，请勿用于侵犯他人权益或违规场景。

---

## 🗺 路线图

- [x] 阶段 0：设计系统（CSS Token）+ 工作台外壳 + 仪表盘 + 落地页改色
- [x] 视频评论模块（真实 LLM 接入）
- [ ] 阶段 1：字幕提取 / 转写 / OCR / 录音 迁入工作台真实页面
- [ ] 阶段 2：SQLite 数据层 + 任务队列异步化
- [ ] 阶段 3：AI 配音 / 批量配音 / 声音库 / 声音克隆（接真实 TTS / 克隆引擎）
- [ ] 阶段 4：仪表盘「视频本地化流水线」编排跑通（提取 → 翻译改写 → 配音 → 替换音轨）
- [ ] 抖音真实链接解析打通（视频评论的分析层）

---

## 📄 许可证

基于 MIT 开源许可，可自由使用与二次开发。
