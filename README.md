# web-chat-bridge — 多模型 Actor-Critic 协同工厂

> 让一个 LLM 调度其他 LLM 做交叉代码评审 —— 零 API Key，纯网页 UI。

[![Python](https://img.shields.io/badge/Python-3.10+-blue)](https://www.python.org/)
[![License](https://img.shields.io/badge/License-MIT-green)](LICENSE)

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
| **接入方式** | 需要 API Key | 网页 UI（零成本） |
| **多模型协同** | ❌ 单 Agent | ✅ Actor-Critic，3+ 模型 |
| **自动迭代修复** | ❌ | ✅ 评审→修复→再评审 |
| **人类化输入** | ❌ | ✅ 逐字打字，随机延迟 |
| **非 API 模型** | ❌ 无法接入 | ✅ 豆包/Kimi/通义全覆盖 |

---

## 30 秒体验

```bash
# 安装
pip install playwright cachetools
playwright install chromium

# 启动 daemon（浏览器长驻）
python web_chat_bridge.py --serve --site deepseek

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

## 架构

```
┌──────────────┐     HTTP :19999      ┌──────────────────────────┐
│   DeepSeek   │ ─── POST /review ──→ │   web-chat-bridge        │
│   (Actor)    │ ←── JSON result ──── │   (Daemon)               │
└──────────────┘                      │                          │
                                      │  Playwright ────────────→│ DeepSeek Chat
┌──────────────┐     HTTP :19999      │  (Chromium 长驻)         │ 豆包 / Kimi
│   CLI / 用户  │ ─── --screenshot ──→ │                          │ 通义 / ChatGPT
└──────────────┘                      └──────────────────────────┘
```

### 核心能力

| 层 | 功能 |
|----|------|
| **Actor-Critic** | DeepSeek 产出 → 网页 LLM 评审 → 迭代直到通过 |
| **Browser Agent v5** | 截图 / 导航 / 点击 / 滚动 / 输入 / 读文本 / 执行 JS |
| **人类化输入** | 逐字键盘输入，30-150ms 随机延迟 |
| **评审缓存** | L1（精确）+ L2（语义去噪）— 不重复评审 |

### 已适配 6 个平台

DeepSeek Chat（专家+深度思考+识图）· 豆包（识图）· Kimi ·
通义千问 · ChatGPT · Claude Web

---

## CLI 速查

```bash
# Daemon
python web_chat_bridge.py --serve [--site deepseek|doubao]

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

- [x] v5 — Actor-Critic + Browser Agent
- [x] v6 — 自动迭代闭环 + 评审历史
- [ ] v7 — 多 Critic 投票 + 隐身浏览器
- [ ] v8 — 插件系统 + 社区贡献

---

## License

MIT — see [LICENSE](LICENSE)