# 声画工坊 / VisuSound Workshop — 音视频生产工作台

本地优先的全功能音视频生产工作台：**字幕提取、语音转写、图片 OCR、AI 配音、声音克隆、视频本地化流水线** 一站完成。支持本地文件、直链视频、B站 / YouTube 等平台视频，内建权限预检，数据默认不出本机。

---

## 功能一览

| 功能 | 说明 | 状态 |
|---|---|---|
| 本地视频内嵌字幕提取 | ffprobe 探测 → ffmpeg 提取 SRT / VTT / ASS | ✅ |
| 外挂字幕文件探测 | 自动发现同目录下的 `.srt` / `.ass` 字幕 | ✅ |
| 直链 URL 下载 + 提取 | 自动下载远程视频并提取字幕 | ✅ |
| 平台视频 (B站 / YouTube / Vimeo …) | 通过 yt-dlp 下载并获取字幕 | ✅ |
| 流媒体 (HLS / DASH) | 通过 yt-dlp 下载 | ✅ |
| 权限预检 | HTTP 状态 / DRM / Cookie / 区域限制检测 | ✅ |
| 多格式输出 | SRT / VTT / ASS / TXT | ✅ |
| AI 语音转写 | Whisper 语音识别，支持多语言模型 | ✅ |
| 图片 OCR | Tesseract 图片文字识别，中英双语 | ✅ |
| 双语合并 | 中英字幕自动对齐合并 | ✅ |
| B站弹幕解析 | 从 `.danmaku.xml` 提取文本 | ✅ |
| Cookie 认证 | 支持 Cookie 字符串 / 浏览器 Cookie | ✅ |

---

## 快速开始

### 1. 环境要求

- **Python 3.10+**
- **FFmpeg** (ffmpeg + ffprobe) — [下载](https://ffmpeg.org/)
- **Tesseract OCR** (可选，用于图片 OCR) — [下载](https://github.com/tesseract-ocr/tesseract)

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

# 创建虚拟环境（推荐）
python3 -m venv .venv
source .venv/bin/activate

# 安装依赖
pip install -r requirements.txt

# 可选：平台视频支持
pip install yt-dlp

# 可选：AI 语音转写
pip install openai-whisper
```

### 3. 启动 Web UI

```bash
uvicorn app:app --reload --port 8080
```

浏览器打开 **http://localhost:8080**（落地页）或 **http://localhost:8080/app**（主应用）

---

## Web UI 使用指南

### 界面布局

```
┌──────────────────────────────────────────────────┐
│  Subtitle Extractor                               │
│  视频字幕提取 · 语音转写 · OCR                     │
├──────────────────────────────────────────────────┤
│  [字幕提取]  [图片文字识别]  [语音转文字]           │  ← 模块 Tab
├──────────────────────┬───────────────────────────┤
│                      │                           │
│  当前模块的表单       │  结果面板                  │
│  (输入链接/上传文件)  │  (展示提取的字幕/文本)      │
│                      │                           │
│  [提取字幕]           │  [📋 复制] [📄 Markdown]   │
│                      │  [下载]                    │
└──────────────────────┴───────────────────────────┘
```

### 三大模块

| 模块 | 功能 | 输入方式 |
|---|---|---|
| **字幕提取** | 从视频提取内嵌/外挂字幕，支持 B站弹幕 | 链接 / 文件上传 |
| **图片文字识别** | OCR 识别图片中的文字，中英双语 | 图片上传 |
| **语音转文字** | Whisper 将语音转为带时间戳的文本 | 链接 / 文件上传 |

### 特色功能

- **Tab 切换**：三个模块用 Tab 切换，页面不堆叠，选中状态自动记忆（刷新不丢失）
- **输入历史**：所有输入框保留最近 5 条历史记录，聚焦时弹出下拉列表
- **复制 & 导出**：结果卡片支持一键复制文本、导出 Markdown 文件
- **结果面板**：左右分栏布局，右侧结果面板始终占满浏览器高度

---

## CLI 使用

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

## 架构设计

```
用户输入 (文件/URL)
    │
    ▼
┌─────────────────┐
│  Source 分类     │  ← LOCAL_FILE / DIRECT_VIDEO_URL / PLATFORM_URL / …
│  classify_source │
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│  权限预检        │  ← HTTP 状态 / DRM 检测 / Cookie / 区域限制
│  check_access   │
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│  源解析 / 下载   │  ← 本地直接探针 / 直链下载 / yt-dlp
│  fetcher        │
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│  FFmpeg 提取     │  ← ffprobe 探测 → ffmpeg 提取
│  extractor      │
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│  格式输出        │  ← SRT / VTT / ASS / TXT / 双语合并
│  formatter      │
└─────────────────┘
         │
         ▼
┌─────────────────┐
│  AI 语音转写     │  ← Whisper (可选)
│  (app.py)       │
└─────────────────┘
         │
         ▼
┌─────────────────┐
│  图片 OCR        │  ← Tesseract (可选)
│  (app.py)       │
└─────────────────┘
```

---

## 项目结构

```
.
├── app.py                 # Web UI (FastAPI) — 主入口
├── main.py                # CLI 入口 (argparse)
├── requirements.txt       # Python 依赖
├── README.md
├── src/
│   ├── __init__.py
│   ├── models.py          # 数据模型 (枚举/数据类)
│   ├── source.py          # 源类型检测分类
│   ├── gateway.py         # 权限预检 (HTTP / DRM / Cookie)
│   ├── extractor.py       # 提取引擎 (ffprobe + ffmpeg)
│   ├── fetcher.py         # 远程视频下载 (httpx / yt-dlp)
│   └── formatter.py       # 输出格式化 + 双语合并 + 弹幕解析
├── templates/
│   ├── index.html         # Web UI 主应用页面
│   └── landing.html       # 落地页
├── static/
│   ├── downloads/         # 已提取的字幕/弹幕文件
│   └── uploads/           # 用户上传的文件
└── .venv/                 # 虚拟环境
```

---

## API 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| `GET` | `/` | 落地页 |
| `GET` | `/app` | 主应用页面 |
| `POST` | `/api/extract` | 提取字幕 |
| `POST` | `/api/ocr` | 图片文字识别 |
| `POST` | `/api/transcribe` | 语音转文字 |
| `GET` | `/api/download/{filename}` | 下载文件 |

---

## 依赖

| 依赖 | 用途 | 必选 |
|---|---|---|
| Python 3.10+ | 运行环境 | ✅ |
| FFmpeg | 视频探测 + 字幕提取 | ✅ |
| httpx | HTTP 请求 | ✅ |
| FastAPI + uvicorn | Web UI 服务 | ✅ |
| yt-dlp | 平台视频下载 (B站/YouTube/…) | ❌ |
| openai-whisper | AI 语音转写 | ❌ |
| pytesseract + Pillow | 图片 OCR | ❌ |
| Tesseract OCR (系统) | OCR 引擎 | ❌ |
| zhconv | 繁简中文转换 | ❌ |

---

## 权限预检场景处理

| 场景 | 检测方式 | 行为 |
|---|---|---|
| 文件不存在 | `os.path.exists` | 拒绝，提示路径错误 |
| 无读权限 | `os.access(R_OK)` | 拒绝，提示权限不足 |
| HTTP 401 | HEAD 请求 | 拒绝，提示需要 `--cookies` |
| HTTP 403 | HEAD 请求 | 拒绝，提示访问被禁止 |
| HTTP 404 | HEAD 请求 | 拒绝，提示资源不存在 |
| HTTP 429 | HEAD 请求 | 拒绝，提示频率限制，稍后重试 |
| DRM 加密 | 响应头 / URL 关键词 | 拒绝，提示 DRM 保护不支持提取 |
| 非视频 Content-Type | Content-Type 检查 | 拒绝，提示非视频资源 |
| 连接超时 / 拒绝 | httpx 异常捕获 | 拒绝，提示网络错误 |
| 平台会员视频 | yt-dlp + Cookie | 降级失败，告知缺少会员权限 |
| 地区限制 | 响应头 geo 关键词 | 标记限制，尝试代理方案 |

---

## 许可证

基于 MIT 开源许可，可自由使用与二次开发。