# web-chat-bridge v5.2 — 追问循环·智力放大器

> 单次回答只有 60 分，追问 N 轮推到 80-85 分。不靠模型强，靠追问深。

[![Python](https://img.shields.io/badge/Python-3.10+-blue)](https://www.python.org/)
[![License](https://img.shields.io/badge/License-MIT-green)](LICENSE)
[![Status](https://img.shields.io/badge/Status-Stable-brightgreen)]()
[![PRs](https://img.shields.io/badge/PRs-Welcome-orange)]()
[![GitHub Stars](https://img.shields.io/github/stars/mels2/web-chat-bridge?style=social)]()
[![Code style](https://img.shields.io/badge/code%20style-black-000000)]()

```
              你的任务
                  │
        ┌─────────┴─────────┐
        │   分层路由器       │
        │ (mcp/router.py)    │
        └─────────┬─────────┘
                  │
        ┌─────────┴─────────┐
        ▼                   ▼
    API 直连           浏览器模式
    (deepseek-v4-       (网页 UI)
     flash / pro)           │
                    ┌───────┴──────┐
                    ▼              ▼
              浏览器常驻池       Actor-Critic
              (预启动2-3实例)     (异步评审)
                    │              │
                    └──────┬───────┘
                           ▼
                     最终输出
```

---

## 核心理念

**web-chat-bridge 不再只是一个"浏览器 API 桥接器"。** 它是一个**能力增强层**：默认使用 API 直连获得最快速度，当 API 能力不足（需要深度思考、识图、联网搜索、自定义指令等）时，自动升级到浏览器模式。

慢不再是 bug——**慢，是因为它在做 API 做不了的事。**

---

## 架构

v4.1 架构：

```
┌──────────────┐  MCP stdio  ┌──────────────────────────────────┐
│  MCP Client  │ ──────────→ │   web-chat-bridge v4.1          │
│  (Cline/     │ ←────────── │   server.py                     │
│   CodeWhale)  │  12 tools   │                                  │
└──────────────┘             │  ┌────────────────────────────┐  │
                              │  │ 分层路由器 (router.py)     │  │
                              │  │  auto / api / browser     │  │
                              │  └───────────┬────────────────┘  │
                              │              │                    │
                              │  ┌───────────▼────────────────┐  │
                              │  │ 浏览器常驻池 (browser_     │  │
                              │  │ pool.py)                   │  │
                              │  │ 预启动 N 个 Edge 实例      │  │
                              │  │ 任务取用 → 归还            │  │
                              │  │ 心跳维护 + 自动修复        │  │
                              │  └───────────┬────────────────┘  │
                              │              │                    │
                              │  ┌───────────▼────────────────┐  │
                              │  │ 打字策略分层                │  │
                              │  │ human → fast → instant     │  │
                              │  └───────────┬────────────────┘  │
                              │              │                    │
                              │  ┌───────────▼────────────────┐  │
                              │  │ Actor-Critic 异步评审       │  │
                              │  │ (review_engine.py)         │  │
                              │  └────────────────────────────┘  │
                              │                                  │
                              │  Playwright ────────────→ DeepSeek│
                              │  (Chromium 长驻)         豆包/Kimi│
                              └──────────────────────────────────┘ 通义/ChatGPT
```

### 模块结构 (`mcp/`)

| 文件 | 说明 |
|------|------|
| `server.py` | MCP Server 入口（12 Tool + CLI） |
| **`router.py`** | **分层路由器 — API 优先，浏览器兜底** |
| **`browser_pool.py`** | **浏览器常驻池 — 预启动 N 个实例** |
| `daemon.py` | HTTP daemon 向后兼容 + 打字策略 |
| `browser_controller.py` | Playwright 浏览器控制 |
| `chat_adapters.py` | 6 平台适配器 |
| `review_engine.py` | Actor-Critic 评审引擎（新增异步评审） |
| `image_handler.py` | 图片上传处理 |
| `cache.py` | TTLCache + singleflight |
| `config.py` | 配置常量（新增路由/池/策略配置） |

---

## v4.1 新特性

### 1. 浏览器常驻池 (BrowserPool)

预启动 2-3 个 Edge/CDP 实例，维持登录态。任务时直接从池子取实例，**启动开销 → 0**。

```bash
# 启用 3 个浏览器实例的池子
python mcp/server.py --serve --cdp --pool-size 3

# 日志输出
[Pool] 浏览器池就绪: 3/3 实例健康
[Daemon] 浏览器池: 3 实例 | 策略: human
```

- 心跳维护线程每 15 秒检测实例健康
- 不健康实例自动重新初始化
- 并发任务使用不同 page，不冲突

### 2. 分层路由 (Router)

| 模式 | 行为 |
|------|------|
| `auto`（默认） | 智能决策：纯文本走 API，复杂任务走浏览器 |
| `api` | 强制 API 直连，跳过浏览器 |
| `browser` | 强制浏览器模式 |

**路由决策规则：**
- 纯文本对话（无特殊需求）→ API（deepseek-v4-flash / pro）
- 需要深度思考链展示 → 浏览器
- 需要专家模式 / 自定义指令 → 浏览器
- 需要联网搜索 + 深度推理 → 浏览器
- 需要多模态识图（图片理解）→ 浏览器
- 触发 Actor-Critic 评审 → 浏览器
- 任意网页聊天 UI 交互 → 浏览器

```bash
# 强制 API 模式（跳过浏览器）
python mcp/server.py --route api

# 强制浏览器模式
python mcp/server.py --route browser
```

### 3. 打字策略分层

| 策略 | 延迟 | 适用场景 |
|------|------|----------|
| `human`（默认） | 30-150ms/字 + 随机停顿 | 需要人类化行为防止检测 |
| `fast` | 50ms/字，无随机停顿 | 需要速度，不关心检测 |
| `instant` | <1s | 调试/测试，最快速度 |

```bash
python mcp/server.py --serve --type-strategy fast
python mcp/server.py --serve --type-strategy instant  # 输入 <1s
```

### 4. Actor-Critic 异步评审

不阻塞主流程，后台线程执行评审，通过 callback 返回结果：

```python
from review_engine import do_review_async

def on_review_result(result):
    print(f"评审完成: {result['verdict']}")

t = do_review_async(content, context="嵌入式固件", callback=on_review_result)
# 主流程继续执行，不等待
```

---

## 快速开始

```bash
# 安装
pip install -r mcp/requirements.txt
playwright install chromium

# 初始化浏览器会话
python mcp/server.py --init --site deepseek

# 启动 daemon + 浏览器池 (2 个实例)
python mcp/server.py --serve --cdp --pool-size 2

# 以快速打字模式启动
python mcp/server.py --serve --cdp --type-strategy fast

# 以 API 优先模式（完全不启动浏览器）
python mcp/server.py --serve --route api
```

### MCP 模式

注册到 MCP 客户端：
```json
{
  "mcpServers": {
    "web-chat-bridge": {
      "command": "python",
      "args": ["mcp/server.py"]
    }
  }
}
```

---

## MCP Tools

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

## 平台支持

DeepSeek Chat（专家+深度思考+识图）· 豆包（识图）· Kimi ·
通义千问 · ChatGPT · Claude Web

---

## License

MIT — see [LICENSE](LICENSE)