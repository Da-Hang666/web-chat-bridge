# web-chat-bridge 架构文档

## 整体架构

```
┌──────────────────────────────────────────────────────────┐
│                     web-chat-bridge                       │
├──────────────────────────────────────────────────────────┤
│                                                          │
│  ┌─────────────┐    HTTP :19999    ┌──────────────────┐  │
│  │  CLI / API  │ ────────────────→ │   Daemon (核心)   │  │
│  │             │ ←── JSON ──────── │                  │  │
│  │ --review    │                   │  Playwright      │  │
│  │ --send      │                   │  (Chromium 长驻)  │  │
│  │ --screenshot│                   │                  │  │
│  │ ...         │                   │  网站适配层       │  │
│  └─────────────┘                   │  (6 个平台)      │  │
│                                    └────────┬─────────┘  │
│  ┌─────────────┐                            │            │
│  │ auto_review │ ─── 自动迭代闭环 ──────────→│            │
│  │   .py       │   评审→修复→再评审          │            │
│  └─────────────┘                            ↓            │
│                                    ┌──────────────────┐  │
│  ┌─────────────┐                   │  DeepSeek Chat   │  │
│  │multi_critic │ ─── 并行投票 ────→│  豆包 / Kimi     │  │
│  │   .py       │   同时审→加权结论   │  通义 / ChatGPT  │  │
│  └─────────────┘                   │  Claude Web      │  │
│                                    └──────────────────┘  │
└──────────────────────────────────────────────────────────┘
```

## 核心模块

### 1. web_chat_bridge.py（1901 行）— Daemon 核心

**职责**：HTTP 服务 + Playwright 浏览器管理 + 网站适配

#### HTTP API 端点

| 端点 | 方法 | 功能 |
|------|------|------|
| `/health` | GET | 健康检查（浏览器是否存活） |
| `/status` | GET | Daemon 状态（站点、URL） |
| `/review` | POST | Actor-Critic 评审 |
| `/review-image` | POST | 识图评审 |
| `/send` | POST | 发送任意消息 |
| `/screenshot` | POST | 截图（可视区/整页） |
| `/navigate` | POST | 导航到 URL |
| `/read` | POST | 提取页面可见文本 |
| `/click` | POST | 点击元素 |
| `/scroll` | POST | 滚动页面 |
| `/type` | POST | 人类化输入 |
| `/execute` | POST | 执行 JavaScript |
| `/cache/clear` | POST | 清除评审缓存 |
| `/shutdown` | POST | 关闭 Daemon |

#### 网站适配机制

每个网站在 `SITE_CONFIGS` 字典中定义 CSS 选择器：

```python
"doubao": {
    "name": "豆包",
    "url": "https://www.doubao.com/chat/",
    "input_selector": 'textarea.semi-input-textarea, ...',
    "send_button": 'button[class*="send"], ...',
    "response_selector": '[class*="assistant"], ...',
    "wait_selector": '[class*="stop"], ...',
}
```

新增平台只需添加一个配置块。

#### 评审缓存

```
L1 (精确匹配): content + context + mode → SHA256
L2 (语义匹配): content only（忽略程度副词、空白差异）

底层: TTLCache + singleflight 合并 + JSONL 持久化
```

### 2. auto_review.py（~200 行）— 自动迭代闭环

**职责**：Actor-Critic 循环自动化

```
输入内容 → Critic A 评审 → 发现问题 → DeepSeek 修复 → Critic B 再评审 → 通过 ✅
```

- 奇数轮用 Critic A，偶数轮用 Critic B
- 两个不同 Critic 都 pass 才通过
- 支持 `--max-rounds` 硬上限

### 3. multi_critic.py（~200 行）— 多 Critic 并行投票

**职责**：N 个 Critic 同时评审 + 加权投票

```
同一份内容 → ┬→ 豆包    ┐
             ├→ Kimi    ┼→ 并行 → 加权投票 → 综合判定
             └→ 通义    ┘
```

- `ThreadPoolExecutor` 并行调度
- 多数票决定 + 加权平均分
- 支持自定义权重 JSON

### 4. 人类化输入

避免被网站检测为机器人：

| 输入类型 | 策略 |
|----------|------|
| 短文本 (<200字) | 逐字键盘输入，30-150ms 随机延迟，标点额外停顿 |
| 长文本 (≥200字) | 模拟 Ctrl+V 粘贴 |
| 段落切换 | 300-1200ms 随机间歇 |

## 数据流

```
用户 CLI
  │
  ├── --review-file code.py
  │     └→ POST /review → 网页大模型 → JSON 评审结果
  │
  ├── --serve
  │     └→ HTTP Server :19999 → Playwright 浏览器长驻
  │
  └── scripts/auto_review.py code.py
        └→ 循环调用 --review + --send → 自动迭代
```

## 依赖

| 依赖 | 用途 |
|------|------|
| `playwright` | 浏览器自动化（Chromium） |
| `cachetools` | TTLCache 评审缓存 |
| Python ≥ 3.10 | 标准库 + `concurrent.futures` + `http.server` |