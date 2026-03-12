<div align="center">

# 🌉 AI-iMsg-Bridge

**Turn your iPhone into an AI Supercomputer — No App, No Subscription, No Cloud.**

*iMessage & 钉钉 × Claude × Gemini × Codex — 本地运行，零云端依赖*

[![Python](https://img.shields.io/badge/Python-3.10+-blue?style=flat-square&logo=python)](https://python.org)
[![macOS](https://img.shields.io/badge/macOS-12+-black?style=flat-square&logo=apple)](https://apple.com/macos)
[![License](https://img.shields.io/badge/License-MIT-green?style=flat-square)](LICENSE)
[![Models](https://img.shields.io/badge/Models-Claude%20%7C%20Gemini%20%7C%20Codex-purple?style=flat-square)](https://github.com/fmoncn/AI-imsg-bridge)
[![Platform](https://img.shields.io/badge/Platform-iMessage%20%7C%20DingTalk-orange?style=flat-square)](https://github.com/fmoncn/AI-imsg-bridge)

[English](#english) · [中文](#中文)

<img width="900" alt="architecture" src="https://raw.githubusercontent.com/fmoncn/AI-imsg-bridge/main/assets/demo.png?v=2">

</div>

---

<a name="english"></a>

## ✨ What is this?

Send an iMessage to yourself. Get a response from Claude, Gemini, or Codex — **running entirely on your own Mac**. Zero cloud dependency. Zero monthly fee. Zero data leakage.

This is a lightweight Python bridge that turns macOS's native iMessage into a **persistent AI terminal** you can access from any Apple device, anywhere in the world.

```
You (iPhone) ──iMessage──▶ Mac ──▶ Claude / Gemini / Codex
                ◀──iMessage──────────────────────────────
```

## 🔥 Why better than OpenClaw?

| | OpenClaw | AI-iMsg-Bridge |
|---|---|---|
| Setup | Docker + config hell | `git clone` + 3 commands |
| Cost | Invite code required | Free (use your own API keys) |
| Privacy | Data goes to cloud | **100% local, zero telemetry** |
| Token waste | 93% wasted on workspace | Direct CLI, zero overhead |
| Models | Locked to one provider | Claude + Gemini + Codex, switch on the fly |
| Reliability | Breaks on app updates | Pure Python + launchd, always-on |

## ⚡ Features

- **🤖 3 Models, 1 Interface** — Switch between Claude Code, Gemini CLI, and OpenAI Codex with a single command (`/c`, `/g`, `/x`)
- **🧠 Persistent Memory** — Conversations carry over across sessions. Claude uses `--continue`, Gemini uses `--resume latest`, Codex gets injected history
- **🔍 Real-Time Web Search** — Powered by Tavily. Only time-sensitive external questions auto-search; `/web ...` and `/local ...` override it explicitly
- **🖼️ Image Understanding** — Send a screenshot or photo. HEIC auto-converts, Gemini analyzes it
- **⏳ Progress Notifications** — Long tasks send staged progress updates every 40s instead of noisy 20s pings
- **🛡️ Secret Key Auth** — Optional passphrase locks the bridge to only you
- **📋 Task Queue** — Multiple unseen messages are fetched and queued in order, so bursts do not get dropped
- **🔄 Always-On Service** — macOS `launchd` keeps it alive 24/7, auto-restarts on crash
- **📝 Rotating Logs** — 5MB log files with 2 backups, never fills your disk
- **🛑 Safer Task Control** — `/stop` kills the whole process group; dangerous tasks require `/confirm`

## 🚀 Quick Start

### Prerequisites
- macOS 12+ with iMessage signed in
- Python 3.10+
- At least one of: [Claude Code](https://claude.ai/code), [Gemini CLI](https://github.com/google-gemini/gemini-cli), [OpenAI Codex CLI](https://github.com/openai/codex)
- Terminal with **Full Disk Access** enabled (System Settings → Privacy & Security)

### Install

```bash
git clone https://github.com/fmoncn/AI-imsg-bridge.git
cd AI-imsg-bridge
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env
```

### Configure `.env`

```env
SENDER_IDS=you@icloud.com,+1234567890   # your iMessage accounts
SENDER_ID=you@icloud.com                # reply-to address
CLAUDE_PATH=/path/to/claude
GEMINI_PATH=/path/to/gemini
CODEX_PATH=/path/to/codex
TAVILY_API_KEY=tvly-...                 # optional, for web search
BRIDGE_SECRET=your_passphrase           # optional, recommended
TIMEOUT_NORMAL=120
TIMEOUT_SEARCH=160
TIMEOUT_CODE=300
TIMEOUT_IMAGE=240
TAVILY_TIMEOUT=25
PROGRESS_INTERVAL=40
MAX_QUEUE_SIZE=20
AUTO_ROUTE_IMAGES=1
DANGEROUS_CONFIRMATION=1
```

### Run

```bash
./manage.sh install   # install launchd service
./manage.sh start     # start background daemon
./manage.sh status    # check it's running
```

Send yourself an iMessage: **`Hello!`** — you should get a reply within seconds.

## 📱 Commands

| Command | Action |
|---------|--------|
| `/g` | Switch to Gemini CLI |
| `/c` | Switch to Claude Code |
| `/x` | Switch to OpenAI Codex |
| `/web hello` | Force web search before answering |
| `/local hello` | Answer locally without web search |
| `/status` | Show current model, queue, memory |
| `/service status` | Show launchd service status |
| `/queue` | Show queued tasks |
| `/tasks` | Show task dashboard |
| `/task 123` | Show task detail |
| `/task cancel 123` | Cancel a running or queued task |
| `/task retry 123` | Requeue a finished/failed task |
| `/review` | Review the latest completed task with two models |
| `/review 123` | Review a specific completed task |
| `/memory` | Show conversation history stats |
| `/reset` | Clear current model's history |
| `/reset all` | Clear all models' history |
| `/stop` | Kill running task |
| `/cancel all` | Stop running task and clear queued tasks |
| `/clear queue` | Clear queued tasks |
| `/restart` | Restart the bridge service |
| `/confirm` | Confirm a dangerous queued task |
| `/ping` | Health check → Pong! |
| `/help` | Show all commands |

## 🏗️ Architecture

```
iMessage (iPhone/Mac)
    │
    ▼
chat.db (SQLite, WAL mode)   ← bridge polls every 1s and fetches all unseen messages
    │
    ▼
main.py (async Python)
    ├── verify_secret()          auth gate
    ├── router.py                command normalization / directive parsing
    ├── engine.py                routing / timeout / command building
    │   └── tavily_search()      real-time web context
    ├── prepare_image()          HEIC→JPEG via sips
    ├── ConversationMemory       per-model history (JSON)
    ├── process registry         orphan-safe process cleanup
    ├── store.py                 sqlite task / offset / review state
    └── run_ai_task()            subprocess → Claude/Gemini/Codex
            │
            ▼
    send_chunked_message()       → osascript → iMessage reply
```

## 📂 Project Structure

```
AI-imsg-bridge/
├── main.py                    # bridge entrypoint and orchestration
├── config.py                  # env-based configuration
├── engine.py                  # model routing and execution policy
├── router.py                  # command normalization and directive parsing
├── state.py                   # app state, memory, health persistence
├── store.py                   # sqlite-backed task and offset state
├── message_store.py           # chat.db polling and attachment lookup
├── process_utils.py           # process-group lifecycle and registry
├── transport.py               # iMessage sending and chunking
├── manage.sh                  # service management CLI
├── com.fmon.claude_bridge.plist  # launchd service definition
├── .env.example               # configuration template
├── requirements.txt
└── requirements-dev.txt
```

---

<a name="中文"></a>

## ✨ 这是什么？

给自己发一条 iMessage，让 Mac 上的 Claude、Gemini 或 Codex 来回复——**完全运行在你自己的机器上**，零云端依赖，零月费，零数据泄露。

这是一个轻量 Python 桥接服务，把 macOS 原生 iMessage 变成一个**持久化 AI 终端**，全球任意 Apple 设备随时访问。

## 🔥 为什么比 OpenClaw 更香？

| | OpenClaw | AI-iMsg-Bridge |
|---|---|---|
| 安装 | Docker + 复杂配置 | `git clone` + 3 条命令 |
| 费用 | 需要邀请码 | 免费（用自己的 API Key） |
| 隐私 | 数据上传云端 | **100% 本地，零遥测** |
| Token 浪费 | 93% 浪费在 workspace | 直接调 CLI，零 overhead |
| 模型 | 绑定单一提供商 | Claude + Gemini + Codex 随时切换 |
| 稳定性 | 第三方 App 更新即崩溃 | 纯 Python + launchd，7×24 自动重启 |

## ⚡ 功能一览

- **🤖 三模型一个入口** — `/c` `/g` `/x` 随时切换 Claude Code / Gemini / Codex
- **🧠 对话记忆** — 跨会话保持上下文。Claude 用 `--continue`，Gemini 用 `--resume latest`，Codex 注入历史
- **🔍 实时联网搜索** — Tavily 驱动。只对“时效性外部问题”自动搜索，也支持 `/web ...` 强制联网、`/local ...` 禁止联网
- **🖼️ 图片理解** — 直接发截图或照片，HEIC 自动转换，Gemini 多模态分析
- **⏳ 进度通知** — 长任务每 40s 推送分阶段提示，减少弱网环境下的无效打扰
- **🛡️ 口令认证** — 可选密语锁定，只有你能控制
- **📋 任务队列** — 多条消息自动排队，不丢失
- **🔄 永久后台** — launchd 守护进程，崩溃自动重启
- **📝 滚动日志** — 5MB 轮转，永不撑满磁盘
- **🛑 更安全的中断** — `/stop` 终止整个任务进程组，高风险请求需 `/confirm`

## 🚀 快速开始

### 环境要求
- macOS 12+，已登录 iMessage
- Python 3.10+
- 至少安装其中一个：[Claude Code](https://claude.ai/code)、[Gemini CLI](https://github.com/google-gemini/gemini-cli)、[OpenAI Codex CLI](https://github.com/openai/codex)
- 终端已开启**完全磁盘访问权限**（系统设置 → 隐私与安全性）

### 安装

```bash
git clone https://github.com/fmoncn/AI-imsg-bridge.git
cd AI-imsg-bridge
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env
```

### 配置 `.env`

```env
SENDER_IDS=you@icloud.com,+8613800000000  # 你的 iMessage 账号
SENDER_ID=you@icloud.com                  # 回复目标账号
CLAUDE_PATH=/path/to/claude
GEMINI_PATH=/path/to/gemini
CODEX_PATH=/path/to/codex
TAVILY_API_KEY=tvly-...                   # 可选，开启联网搜索
BRIDGE_SECRET=你的密语                    # 可选，强烈建议设置
TIMEOUT_NORMAL=120
TIMEOUT_SEARCH=160
TIMEOUT_CODE=300
TIMEOUT_IMAGE=240
TAVILY_TIMEOUT=25
PROGRESS_INTERVAL=40
MAX_QUEUE_SIZE=20
AUTO_ROUTE_IMAGES=1
DANGEROUS_CONFIRMATION=1
```

### 启动

```bash
./manage.sh install   # 安装 launchd 服务
./manage.sh start     # 启动后台守护进程
./manage.sh status    # 确认运行状态
```

给自己发一条 iMessage：**`你好！`** — 几秒内应收到回复。

## 📱 指令列表

| 指令 | 功能 |
|------|------|
| `/g` | 切换至 Gemini CLI |
| `/c` | 切换至 Claude Code |
| `/x` | 切换至 OpenAI Codex |
| `/web 内容` | 强制联网搜索后回答 |
| `/local 内容` | 禁止联网搜索 |
| `/status` | 查看当前模型、队列、记忆状态 |
| `/service status` | 查看 launchd 服务状态 |
| `/queue` | 查看排队任务 |
| `/tasks` | 查看任务面板 |
| `/task 123` | 查看任务详情 |
| `/task cancel 123` | 取消运行中或排队中的任务 |
| `/task retry 123` | 重新入队已结束任务 |
| `/review` | 双模型复审最近完成任务 |
| `/review 123` | 双模型复审指定任务 |
| `/memory` | 查看各模型对话历史统计 |
| `/reset` | 清空当前模型对话历史 |
| `/reset all` | 清空所有模型历史 |
| `/stop` | 中断当前任务 |
| `/cancel all` | 清空排队任务并中断当前任务 |
| `/clear queue` | 仅清空排队任务 |
| `/restart` | 重启 Bridge 服务 |
| `/confirm` | 确认执行高风险任务 |
| `/ping` | 心跳检测 → Pong! |
| `/help` | 查看所有指令 |

## 🔒 安全建议

1. **设置 `BRIDGE_SECRET`**：每条消息需以密语开头，防止未授权访问
2. **最小化 `SENDER_IDS`**：只填你自己的账号
3. **`.env` 永不提交 git**：已在 `.gitignore` 中排除
4. **保留高风险确认**：默认启用 `DANGEROUS_CONFIRMATION=1`
5. **定期查看日志**：`./manage.sh logs`

## 🧪 Testing

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python3 -m pytest -q
```

## 🛠️ 常用命令

```bash
./manage.sh start     # 启动
./manage.sh stop      # 停止
./manage.sh restart   # 重启
./manage.sh status    # 状态
./manage.sh logs      # 实时日志
./manage.sh uninstall # 卸载
```

## 🤝 Contributing

PR 和 Issue 欢迎！特别期待：
- 更多 AI CLI 适配（如 DeepSeek、Ollama）
- 语音消息支持
- 群聊多用户权限

## 📜 License

MIT — 为极致自动化而生。

---

<div align="center">

**如果这个项目帮到了你，点个 ⭐ 就是最好的支持**

*Built with Claude Code · Powered by iMessage*

</div>
