"""
web-chat-bridge MCP — 配置常量
"""

from pathlib import Path

# ── Daemon ──
DAEMON_PORT = 19999

# ── 路径 ──
SESSION_FILE = Path.home() / ".web_chat_session.json"
SCREENSHOT_DIR = Path.home() / ".web_chat_screenshots"
CACHE_FILE = Path.home() / ".web_chat_cache.jsonl"

# ── 缓存 ──
CACHE_MAXSIZE = 500
CACHE_TTL = 3600          # 闲置 1 小时过期
CACHE_MAX_AGE = 14400     # 硬上限 4 小时
CACHE_SAVE_INTERVAL = 30  # 每 30 秒自动落盘

# ── 人类化输入 ──
HUMAN_MIN_DELAY = 0.03    # 最短 30ms/字
HUMAN_MAX_DELAY = 0.15    # 最长 150ms/字
HUMAN_PAUSE_MIN = 0.3     # 段落间歇最短
HUMAN_PAUSE_MAX = 1.2     # 段落间歇最长
HUMAN_CHUNK_MIN = 15      # 每 N 个字停一次（下限）
HUMAN_CHUNK_MAX = 35      # 每 N 个字停一次（上限）
PUNCTUATION_PAUSE = 0.35  # 标点后额外停顿
PASTE_THRESHOLD = 200     # 超过此字符数用粘贴代替逐字打字

# ── 打字策略 ──
TYPE_STRATEGY = "human"   # "human" | "fast" | "instant"
TYPE_FAST_DELAY = 0.05    # 快速模式的每字延迟

# ── 浏览器池 ──
POOL_SIZE = 2             # 预启动实例数
POOL_IDLE_TTL = 600       # 空闲 10 分钟自动回收

# ── 路由 ──
ROUTE_MODE = "auto"       # "auto" | "api" | "browser"
API_MODEL = "deepseek-v4-flash"

# ── 评审 ──
REVIEW_ASYNC = True       # 是否异步评审
REVIEW_TIMEOUT = 120      # 单次评审超时（秒）

# ── 评审 ──
MAX_CONTENT_CHARS = 8000  # 超过此长度自动截断

# ── 平台配置 ──
SITE_CONFIGS = {
    "deepseek": {
        "name": "DeepSeek Chat",
        "url": "https://chat.deepseek.com",
        "input_selector": 'textarea, [contenteditable="true"], #chat-input',
        "send_button": 'button[aria-label="发送"], .send-btn, [class*="send"]',
        "response_selector": '.message-bubble:last-child, [class*="message"]:last-child, .ds-markdown',
        "wait_selector": '.stop-btn, [class*="stop"], .thinking-indicator',
        "wait_disappear": True,
    },
    "openai": {
        "name": "ChatGPT",
        "url": "https://chat.openai.com",
        "input_selector": '#prompt-textarea, textarea[placeholder*="Message"]',
        "send_button": 'button[data-testid="send-button"]',
        "response_selector": '[data-message-author-role="assistant"]:last-child',
        "wait_selector": 'button[data-testid="stop-button"]',
        "wait_disappear": True,
    },
    "claude": {
        "name": "Claude",
        "url": "https://claude.ai",
        "input_selector": '[contenteditable="true"], .ProseMirror',
        "send_button": 'button[aria-label="Send Message"]',
        "response_selector": '.claude-response:last-child, [class*="message"]:last-child',
        "wait_selector": '[class*="stop"]',
        "wait_disappear": True,
    },
    "kimi": {
        "name": "Kimi",
        "url": "https://kimi.moonshot.cn",
        "input_selector": 'textarea, [contenteditable="true"]',
        "send_button": 'button[class*="send"], .send-btn',
        "response_selector": '[class*="message"]:last-child, [class*="answer"]',
        "wait_selector": '[class*="stop"], .loading',
        "wait_disappear": True,
    },
    "tongyi": {
        "name": "通义千问",
        "url": "https://tongyi.aliyun.com",
        "input_selector": 'textarea, [contenteditable="true"]',
        "send_button": 'button[class*="send"]',
        "response_selector": '[class*="answer"]:last-child, [class*="bot"]',
        "wait_selector": '[class*="stop"], .loading',
        "wait_disappear": True,
    },
    "doubao": {
        "name": "豆包",
        "url": "https://www.doubao.com/chat/",
        "input_selector": 'textarea.semi-input-textarea, textarea[placeholder*="发消息"], textarea[placeholder*="消息"]',
        "send_button": 'button[class*="send"], [class*="send-btn"], button.semi-button-primary',
        "response_selector": '[class*="assistant"], [class*="bot"], [class*="reply"], [class*="response"], [class*="message"]:last-child',
        "wait_selector": '[class*="stop"], [class*="loading"], [class*="thinking"], [class*="generating"]',
        "wait_disappear": True,
    },
    "custom": {
        "name": "Unknown",
        "url": "",
        "input_selector": 'textarea, [contenteditable="true"]',
        "send_button": 'button[aria-label*="send" i], button[class*="send"]',
        "response_selector": '[class*="message"]:last-child',
        "wait_selector": '[class*="loading"], [class*="thinking"], .spinner',
        "wait_disappear": True,
    },
}