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
TYPE_STRATEGY = "instant" # "human" | "fast" | "instant"
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

# ── 追问引擎 v5.3 ──
INQUIRY_MAX_ROUNDS = 5           # 最大追问轮数
INQUIRY_STABILITY_WAIT = 3       # 回复稳定性检测秒数
INQUIRY_MAX_WAIT_PER_ROUND = 120 # 单轮最大等待秒数
INQUIRY_COOLDOWN_S = 3           # 损卦䷨: 轮间强制冷却，不过度消耗免费算力
INQUIRY_DAILY_LIMIT = 50         # 损卦䷨: 每日追问上限，防止滥用

INQUIRY_STRATEGIES = {
    "deep": {
        "name": "深度追问",
        "description": "每一轮基于上一轮回答的核心论点继续深挖，适合技术调研、知识提取",
        "prompt_template": "你上一轮提到了「{key_point}」。请深入展开这一点，给出具体细节、原理和实例。",
        "max_rounds": 4,
    },
    "debate": {
        "name": "辩论模式",
        "description": "从对立面挑战上一轮的回答，迫使模型进行自我批判和思维修正",
        "prompt_template": "对于你上一轮的观点「{key_point}」，请站在反对立场进行批判。指出其中的漏洞、局限和反例。",
        "max_rounds": 5,
    },
    "socratic": {
        "name": "苏格拉底式",
        "description": "用连续提问引导模型自己发现答案，每轮只问一个问题",
        "prompt_template": "关于你提到的「{key_point}」，如果把这个逻辑推到极端会怎样？",
        "max_rounds": 5,
    },
    "verify": {
        "name": "交叉验证",
        "description": "把同一个问题发给两个不同模型，用一个评审另一个，迭代优化",
        "prompt_template": "以下是另一个AI模型对同一问题的回答，请评审并指出差异和优劣：\n{other_response}",
        "max_rounds": 3,
    },
}

# 交叉评审配置
CROSS_REVIEW_SITES = ["deepseek", "doubao"]  # 交叉评审使用的站点

# ── 地图 ──
MAP_REBUILD_INTERVAL = 300  # 每 5 分钟全量重建地图

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
        "input_selector": "textarea.semi-input-textarea",
        "send_button": ".send-btn-wrapper button",
        "response_selector": "div.my-0.w-full.mx-auto, [class*=message]:not([class*=suggest])",
        "wait_selector": '[class*="stop"], [class*=loading], [class*=thinking]',
        "wait_disappear": False,
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