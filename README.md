# web-chat-bridge — 多模型 Actor-Critic 协同工厂

> 让一个 LLM 调度其他 LLM 做交叉代码评审 —— 零 API Key，纯网页 UI。现已原生支持 MCP 协议。

[![Python](https://img.shields.io/badge/Python-3.10+-blue)](https://www.python.org/)
[![License](https://img.shields.io/badge/License-MIT-green)](LICENSE)
[![Status](https://img.shields.io/badge/Status-Stable-brightgreen)]()
[![PRs](https://img.shields.io/badge/PRs-Welcome-orange)]()
[![GitHub Stars](https://img.shields.io/github/stars/mels2/web-chat-bridge?style=social)]()
[![Code style](https://img.shields.io/badge/code%20style-black-000000)]()

```
              你的代码
                 │
    ┌────────────┼────────────┐
    ▼            ▼            ▼
  豆包评审    Kimi评审    通义评审
    └────────────┼────────────┘
                 ▼
           加权投票结论
           pass / warn / fail
                 │
          (fail)→ DeepSeek 自动修复 → 再审
```

---

## 为什么需要它

你写了代码，不确定有没有 bug。你没时间让三个人帮你审，但你有一台电脑。

**web-chat-bridge 让三个不同的大模型通过网页版交叉审查你的代码**，
发现你一个人发现不了的问题，然后自动修复 —— 直到两个不同模型都说"通过"。

不需要 API Key，不需要付费。只需要大家每天都在用的网页版大模型。

---

## 与 browser-use 的对比

| | browser-use | **web-chat-bridge** |
|---|:-----------:|:-------------------:|
| **定位** | "帮我订机票" | "帮我审代码" |
| **接入方式** | 需要 API Key | 网页 UI + MCP 协议（零成本） |
| **多模型协同** | ❌ 单 Agent | ✅ Actor-Critic，3+ 模型 |
| **自动迭代修复** | ❌ | ✅ 评审→修复→再评审 |
| **人类化输入** | ❌ | ✅ 逐字打字，随机延迟 |
| **非 API 模型** | ❌ 无法接入 | ✅ 豆包/Kimi/通义全覆盖 |
| **MCP 原生支持** | ❌ | ✅ 12 个标准化 Tool |

---

## MCP 模式（v4，推荐）

作为标准 MCP Server 运行，直接供 Cline / CodeWhale / Cursor 等 MCP 客户端调用：

```bash
# 安装（含 MCP SDK）
pip install -r mcp/requirements.txt
playwright install chromium

# 初始化浏览器会话
python mcp/server.py --init --site deepseek

# 启动 daemon + MCP（后台）
python mcp/server.py --serve

# 注册到 MCP 客户端
# 在客户端的 mcp.json 中添加：
# { "mcpServers": { "web-chat-bridge": { "command": "python", "args": ["mcp/server.py"] } } }
```

**12 个 MCP Tools：**

| Tool | 功能 |
|------|------|
| `send_message` | 向聊天平台发送消息并获取 AI 回复 |
| `review_text` | Actor-Critic 结构化评审（缓存） |
| `review_image` | 识图模式上传图片评审 |
| `screenshot` | 浏览器截图 |
| `navigate` | 导航到 URL |
| `click` | 点击页面元素 |
| `scroll` | 滚动页面 |
| `read_page` | 提取页面文本 |
| `type_text` | 人类化输入文本 |
| `execute_js` | 执行 JS 代码 |
| `daemon_status` | 查看运行状态 |
| `clear_cache` | 清空评审缓存 |

---

## 经典模式（向后兼容）

```bash
# 评审文件（在另一个终端）
python web_chat_bridge.py --review-file my_code.py
```

---

## 自动迭代评审（v6）

提交代码，自动评审 → 修复 → 再评审，直到两个不同模型都说"通过"：

```bash
# 自动评审（默认 2 个 Critic 轮换）
python scripts/auto_review.py my_code.py

# 三个 Critic 一起审
python scripts/auto_review.py my_code.py --critics doubao,kimi,tongyi

# 指定上下文
python scripts/auto_review.py my_code.py --context "嵌入式固件，关注内存安全"

# 输出修复后的版本
python scripts/auto_review.py my_code.py --output fixed_code.py
```

流程：
```
你的代码 → 豆包评审 → 发现问题 → DeepSeek 修复
                                    ↓
                               Kimi 再评审 → 通过 ✅
```

---

## 多 Critic 并行评审（v7）

三个评审员同时审，加权投票出结论。免去串行等待，一次拿到所有意见：

```bash
# 默认三个 Critic 并行
python scripts/multi_critic.py my_code.py

# 自定义评审团
python scripts/multi_critic.py my_code.py --critics doubao,kimi,tongyi

# 指定 Critic 权重（doubao 最靠谱，权重高）
python scripts/multi_critic.py my_code.py --weights critic_weights.json

# 纯 JSON 输出（给脚本调用）
python scripts/multi_critic.py my_code.py --json --output result.json
```

权重文件示例 (`critic_weights.json`)：
```json
{"doubao": 1.2, "kimi": 1.0, "tongyi": 0.9}
```

---

## 架构

v4 MCP 架构：

```
┌──────────────┐  MCP stdio  ┌──────────────────────────┐
│  MCP Client  │ ──────────→ │   web-chat-bridge v4     │
│  (Cline/     │ ←────────── │   server.py              │
│   CodeWhale)  │  12 tools   │                          │
└──────────────┘             │  ┌────────────────────┐  │
                              │  │  HTTP :19999       │  │
┌──────────────┐  HTTP :19999 │  │  (Daemon 向后兼容)  │  │
│   CLI / 用户  │ ──────────→ │  └────────────────────┘  │
└──────────────┘             │                          │
                              │  Playwright ────────────→│ DeepSeek Chat
                              │  (Chromium 长驻)         │ 豆包 / Kimi
                              └──────────────────────────┘ 通义 / ChatGPT
```

### 模块结构 (`mcp/`)

| 文件 | 说明 |
|------|------|
| `server.py` | MCP Server 入口（12 Tool + CLI） |
| `daemon.py` | HTTP daemon 向后兼容 |
| `browser_controller.py` | Playwright 浏览器控制 |
| `chat_adapters.py` | 6 平台适配器 |
| `review_engine.py` | Actor-Critic 评审引擎 |
| `image_handler.py` | 图片上传处理 |
| `cache.py` | TTLCache + singleflight |
| `config.py` | 配置常量 |

### 核心能力

| 层 | 功能 |
|----|------|
| **MCP Tools** | 12 个标准化 Tool，供 AI Agent 直接调用 |
| **Actor-Critic** | DeepSeek 产出 → 网页 LLM 评审 → 迭代直到通过 |
| **Browser Agent v5** | 截图 / 导航 / 点击 / 滚动 / 输入 / 读文本 / 执行 JS |
| **人类化输入** | 逐字键盘输入，30-150ms 随机延迟 |
| **评审缓存** | TTLCache + singleflight 并发合并 |

### 已适配 6 个平台

DeepSeek Chat（专家+深度思考+识图）· 豆包（识图）· Kimi ·
通义千问 · ChatGPT · Claude Web

---

## CLI 速查

```bash
# MCP Server（推荐）
python mcp/server.py

# Daemon 模式
python mcp/server.py --serve [--site deepseek|doubao]

# 评审
python web_chat_bridge.py --review-file path/to/code.py
python web_chat_bridge.py --review-image screenshot.png

# Browser Agent
python web_chat_bridge.py --screenshot
python web_chat_bridge.py --read --read-selector ".main-content"
python web_chat_bridge.py --navigate "https://github.com/..."
python web_chat_bridge.py --click-text "Submit"
```

---

## 路线图

- [x] v4 — MCP Server（12 Tool 标准化，模块化拆分）
- [x] v5 — Actor-Critic + Browser Agent
- [x] v6 — 自动迭代闭环 + 评审历史
- [x] v7 — 多 Critic 投票 + 隐身浏览器
- [ ] v8 — 插件系统 + 社区贡献

---

## License

MIT — see [LICENSE](LICENSE)
