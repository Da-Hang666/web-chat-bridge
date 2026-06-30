"""
网页大模型对话桥接 v5 —— Actor-Critic + Browser Agent。
DeepSeek 直接支配浏览器：截图→观察→决策→操作（点击/输入/滚动/导航）。

v5 新增 (Browser Agent):
- POST /navigate — 导航到 URL
- POST /screenshot — 截图（人眼反馈）
- POST /read — 提取页面可见文本
- POST /click — 点击（selector / 文本匹配）
- POST /scroll — 滚动页面
- POST /type — 人类化输入到指定元素
- POST /execute — 执行任意 JS
- CLI 参数: --navigate/--screenshot/--read/--click/--scroll/--type/--execute

v4 基础:
- CLI 端口参数化 (--port)，支持多 daemon 并行
- Daemon 心跳恢复 (/health + CLI 自动检测)
- 评审缓存 (TTLCache + content hash)，避免重复请求

用法：
  # 初始化浏览器会话
  python web_chat_bridge.py --init [--url URL] [--site SITE]

  # ── Actor-Critic 核心 ──
  # 评审一段文本/代码
  python web_chat_bridge.py --review "你的产出内容"
  # 从文件读取产出
  python web_chat_bridge.py --review-file path/to/output.py
  # 自定义评审上下文
  python web_chat_bridge.py --review-file path/to/output.py --context "这是固件代码，关注内存安全和中断处理"

  # ── 通用 ──
  python web_chat_bridge.py --send "任意问题"
  python web_chat_bridge.py --loop

输出格式 (JSON):
  { "verdict": "pass|warn|fail",
    "score": 0-10,
    "issues": ["问题1", "问题2"],
    "suggestions": ["改进1", "改进2"],
    "summary": "总体评价",
    "raw": "原始回复全文" }

依赖：pip install playwright && playwright install chromium
"""

import argparse
import atexit
import sys
import io
import time
import random
import json
import os
import re
import http.server
import socketserver
import urllib.parse
import threading
from pathlib import Path

# Windows 中文环境：强制 stdout/stderr 输出 UTF-8，避免 emoji 等字符导致的 GBK 编码错误
if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

SESSION_FILE = Path.home() / ".web_chat_session.json"
DAEMON_PORT = 19999

# ── 评审缓存 ──────────────────────────────────────────────────
import hashlib
import functools
from cachetools import TTLCache
CACHE_MAXSIZE = 500
CACHE_TTL = 3600  # 闲置 1 小时过期
CACHE_MAX_AGE = 14400  # 硬上限 4 小时
_review_cache = TTLCache(maxsize=CACHE_MAXSIZE, ttl=CACHE_TTL)
_cache_birth = {}  # key → 首次写入时间戳
CACHE_FILE = Path.home() / ".web_chat_cache.jsonl"
CACHE_SAVE_INTERVAL = 30  # 每 30 秒自动落盘
_cache_dirty = False
_cache_last_save = 0.0
# ── 缓存统计
_cache_stats = {"total": 0, "hit_l1": 0, "hit_l2": 0, "miss": 0}

# ── singleflight 合并
import threading
_inflight = {}
_inflight_lock = threading.Lock()

def _normalize_context(ctx: str) -> str:
    """上下文语义归一化：去程度副词，'关注内存安全'和'重点关注内存安全'→同一key。"""
    if not ctx:
        return ""
    for w in ['重点', '特别', '非常', '很', '极其', '十分', '请', '务必', '一定', '尽量', '麻烦']:
        ctx = ctx.replace(w, '')
    return ctx.strip()


def _normalize_content(content: str) -> str:
    """规范化内容：strip前后空白、统一换行符、压缩连续空行。
    让微小格式差异（空格、换行符类型）不影响缓存命中。"""
    # 统一换行符
    content = content.replace("\r\n", "\n").replace("\r", "\n")
    # 压缩连续空行（最多保留1个连续空行）
    import re
    content = re.sub(r"\n{3,}", "\n\n", content)
    # strip 前后空白
    return content.strip()


def _cache_key(content, context="", mode="expert", normalize=True):
    """构建缓存 key。默认规范化 content 和 context。"""
    if normalize:
        content = _normalize_content(content)
        context = _normalize_context(context) if context else ""
    raw = json.dumps({"c": content, "ctx": context, "m": mode}, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(raw.encode()).hexdigest()

def _load_cache():
    """从磁盘恢复缓存。"""
    if not CACHE_FILE.exists():
        return
    try:
        count = 0
        for line in CACHE_FILE.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
                key = entry.get("k", "")
                value = entry.get("v")
                expire = entry.get("e", 0)
                if key and value is not None:
                    # 只恢复未过期的
                    if expire > time.time() or expire == 0:
                        _review_cache[key] = value
                        count += 1
            except (json.JSONDecodeError, KeyError):
                continue
        if count:
            sys.stderr.write(f"[Cache] 从磁盘恢复 {count} 条缓存\n")
    except Exception:
        pass


def _save_cache():
    """将缓存落盘为 JSON Lines 文件。"""
    global _cache_dirty, _cache_last_save
    try:
        lines = []
        now = time.time()
        for key, value in _review_cache.items():
            # TTLCache 的 ttl 是相对时间，这里存绝对过期时间
            expire = now + CACHE_TTL
            lines.append(json.dumps({"k": key, "v": value, "e": expire}, ensure_ascii=False))
        CACHE_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")
        _cache_dirty = False
        _cache_last_save = now
    except Exception:
        pass


def _mark_cache_dirty():
    """标记缓存脏，触发延迟落盘。"""
    global _cache_dirty
    _cache_dirty = True


def _maybe_save_cache():
    """如果缓存脏且超过落盘间隔，自动保存。"""
    global _cache_dirty, _cache_last_save
    if _cache_dirty and (time.time() - _cache_last_save) > CACHE_SAVE_INTERVAL:
        _save_cache()


def _flush_cache():
    """强制落盘。"""
    _save_cache()
    sys.stderr.write("[Cache] 已落盘\n")

# ── 人类化输入参数 ──────────────────────────────────────────
HUMAN_MIN_DELAY = 0.03   # 最短 30ms/字（快速打字）
HUMAN_MAX_DELAY = 0.15   # 最长 150ms/字（正常速度）
HUMAN_PAUSE_MIN = 0.3    # 段落间歇最短
HUMAN_PAUSE_MAX = 1.2    # 段落间歇最长
HUMAN_CHUNK_MIN = 15     # 每 N 个字停一次（下限）
HUMAN_CHUNK_MAX = 35     # 每 N 个字停一次（上限）
PUNCTUATION_PAUSE = 0.35 # 标点后额外停顿
PASTE_THRESHOLD = 200     # 超过此字符数用粘贴代替逐字打字

# ── Actor-Critic 评审提示词模板 ──────────────────────────────
CRITIC_PROMPT = """你是一个严格的技术评审员（Critic）。下面是一个 AI Agent 产出的工作结果，请审查。

评审规则：
1. 检查事实错误、逻辑漏洞、代码 bug
2. 检查遗漏的边界情况
3. 评估是否满足隐含需求
4. 给出 0-10 的评分

请用 JSON 格式回复（不要多余文字，只要 JSON）：
{"verdict": "pass|warn|fail", "score": <0-10>, "issues": ["问题1", ...], "suggestions": ["改进1", ...], "summary": "一句话总结"}

---

产出内容：
"""

MAX_CONTENT_CHARS = 8000  # 超过此长度自动截断


# ── 各网站的 CSS 选择器映射 ──────────────────────────────────
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
        "input_selector": '#prompt-textarea, textarea[placeholder*="Message"]',
        "send_button": 'button[data-testid="send-button"]',
        "response_selector": '[data-message-author-role="assistant"]:last-child',
        "wait_selector": 'button[data-testid="stop-button"]',
        "wait_disappear": True,
    },
    "claude": {
        "name": "Claude",
        "input_selector": '[contenteditable="true"], .ProseMirror',
        "send_button": 'button[aria-label="Send Message"]',
        "response_selector": '.claude-response:last-child, [class*="message"]:last-child',
        "wait_selector": '[class*="stop"]',
        "wait_disappear": True,
    },
    "kimi": {
        "name": "Kimi",
        "input_selector": 'textarea, [contenteditable="true"]',
        "send_button": 'button[class*="send"], .send-btn',
        "response_selector": '[class*="message"]:last-child, [class*="answer"]',
        "wait_selector": '[class*="stop"], .loading',
        "wait_disappear": True,
    },
    "tongyi": {
        "name": "通义千问",
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
        "input_selector": 'textarea, [contenteditable="true"]',
        "send_button": 'button[aria-label*="send" i], button[class*="send"]',
        "response_selector": '[class*="message"]:last-child',
        "wait_selector": '[class*="loading"], [class*="thinking"], .spinner',
        "wait_disappear": True,
    },
}


def get_site_config(url):
    for key, cfg in SITE_CONFIGS.items():
        if key in url.lower():
            return key, cfg
    return "custom", SITE_CONFIGS["custom"]


def init_browser(url, site_id=None):
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        profile_dir = str(Path.home() / ".web_chat_profile")
        browser = p.chromium.launch_persistent_context(
            user_data_dir=profile_dir,
            headless=False,
            args=["--no-sandbox", "--disable-gpu"],
            viewport={"width": 1280, "height": 900},
        )
        page = browser.pages[0] if browser.pages else browser.new_page()
        page.goto(url, wait_until="domcontentloaded", timeout=30000)

        if site_id is None:
            site_id, _ = get_site_config(url)

        session = {
            "url": url,
            "site_id": site_id,
            "profile_dir": str(Path.home() / ".web_chat_profile"),
            "initialized": True,
        }
        SESSION_FILE.write_text(json.dumps(session, ensure_ascii=False, indent=2))

        print(f"[OK] 浏览器已打开: {url}")
        print(f"   站点: {SITE_CONFIGS.get(site_id, {}).get('name', site_id)}")
        print(f"   请在浏览器中完成登录，然后关闭浏览器窗口即可。")
        sys.stdout.flush()

        # 等待浏览器窗口被用户关闭
        try:
            while browser.pages:
                time.sleep(1)
        except Exception:
            pass
        try:
            browser.close()
        except Exception:
            pass


def find_and_fill_input(page, config):
    selectors = config["input_selector"].split(", ")
    for sel in selectors:
        try:
            el = page.locator(sel).first
            if el.is_visible(timeout=1000):
                el.click()
                el.fill("")
                return el
        except Exception:
            continue
    try:
        return page.locator('textarea:visible').first
    except Exception:
        pass
    try:
        return page.locator('[contenteditable="true"]:visible').first
    except Exception:
        pass
    return None


def human_type(page, input_el, text):
    """模拟人类打字：可变延迟、段落间歇、标点停顿。
    
    用 page.keyboard.press() 逐字输入，每个字符触发完整 keydown/keypress/keyup，
    兼容 React/Vue 受控组件的 onChange 监听。
    """
    # 先聚焦输入框
    try:
        input_el.click()
        time.sleep(random.uniform(0.2, 0.5))
    except Exception:
        pass

    total = len(text)
    next_pause_at = random.randint(HUMAN_CHUNK_MIN, HUMAN_CHUNK_MAX)
    
    for i, char in enumerate(text):
        try:
            page.keyboard.press(char)
        except Exception:
            # 某些特殊字符 press 可能失败，回退 insert_text
            try:
                page.keyboard.insert_text(char)
            except Exception:
                pass

        # 基础随机延迟
        delay = random.uniform(HUMAN_MIN_DELAY, HUMAN_MAX_DELAY)
        
        # 中文/英文标点后多停
        if char in '，。！？；：、—…""''）》》《' or char in ',.!?;:':
            delay += PUNCTUATION_PAUSE
        
        # 每 N 个字符停顿（模拟思考/回看）
        if i >= next_pause_at:
            time.sleep(random.uniform(HUMAN_PAUSE_MIN, HUMAN_PAUSE_MAX))
            next_pause_at = i + random.randint(HUMAN_CHUNK_MIN, HUMAN_CHUNK_MAX)
        
        time.sleep(delay)

    # 输入完成后稍等（模拟检查一遍）
    time.sleep(random.uniform(0.3, 0.8))
    sys.stderr.write(f"[输入] {total} 字符逐字输入完成\n")


def human_paste(page, input_el, config, text):
    """模拟人类粘贴：Ctrl+V 一次性粘贴长文本。
    
    使用 document.execCommand('insertText') 而非 navigator.clipboard，
    因为前者不需要浏览器权限，且能正确触发 React 的 onChange。
    """
    try:
        input_el.click()
        time.sleep(random.uniform(0.3, 0.6))  # 聚焦后稍等（模拟手移到鼠标）
    except Exception:
        pass
    
    try:
        first_sel = config["input_selector"].split(", ")[0]
        page.evaluate("""
            (sel, text) => {
                const el = document.querySelector(sel);
                if (!el) return;
                el.focus();
                // 全选现有内容后替换
                document.execCommand('selectAll', false, null);
                document.execCommand('insertText', false, text);
            }
        """, [first_sel, text])
    except Exception:
        # Fallback: keyboard.insert_text
        try:
            page.keyboard.insert_text(text)
        except Exception:
            pass
    
    # 粘贴后稍等（模拟检查粘贴内容）
    time.sleep(random.uniform(0.5, 1.0))
    sys.stderr.write(f"[输入] {len(text)} 字符粘贴完成\n")


def send_message(page, config, text):
    input_el = find_and_fill_input(page, config)
    if input_el is None:
        return {"error": "找不到输入框"}

    # 发送前随机等待（模拟阅读/思考）
    pre_delay = random.uniform(1.0, 3.0)
    sys.stderr.write(f"[等待] 发送前等待 {pre_delay:.1f} 秒...\n")
    time.sleep(pre_delay)

    # 智能选择输入方式：长文本粘贴，短文本逐字打字
    if len(text) >= PASTE_THRESHOLD:
        human_paste(page, input_el, config, text)
    else:
        human_type(page, input_el, text)

    send_selectors = config["send_button"].split(", ")
    sent = False
    for sel in send_selectors:
        try:
            btn = page.locator(sel).first
            if btn.is_visible(timeout=1000) and btn.is_enabled():
                btn.click()
                sent = True
                break
        except Exception:
            continue
    if not sent:
        input_el.press("Enter")

    # 等待回复完成 —— 三重保障
    wait_sel = config.get("wait_selector", "")
    wait_disappear = config.get("wait_disappear", True)
    if wait_sel:
        try:
            page.wait_for_selector(wait_sel, timeout=10000)
        except Exception:
            pass
    if wait_sel and wait_disappear:
        try:
            page.wait_for_selector(wait_sel, state="detached", timeout=300000)
        except Exception:
            pass

    # 响应稳定性检测：等文字停止增长后再取
    resp_selectors = config["response_selector"].split(", ")
    response_text = ""
    stable_count = 0
    for _ in range(180):  # 最多等 180 秒（长回复容忍）
        time.sleep(1)
        current = ""
        for sel in resp_selectors:
            try:
                el = page.locator(sel).last
                if el.is_visible():
                    current = el.inner_text()
                    break
            except Exception:
                continue
        if not current:
            try:
                current = page.locator("body").inner_text()
            except Exception:
                pass
        if current == response_text and len(current) > 10:
            stable_count += 1
            if stable_count >= 3:  # 连续 3 秒不变 = 已完成
                break
        else:
            stable_count = 0
            response_text = current
    # 如果始终没有有效回复，最后取一次
    if not response_text or len(response_text) < 5:
        for sel in resp_selectors:
            try:
                el = page.locator(sel).last
                if el.is_visible():
                    response_text = el.inner_text()
                    break
            except Exception:
                continue
    if not response_text:
        try:
            response_text = page.locator("body").inner_text()
        except Exception:
            response_text = "(无法提取回复)"

    return {"sent": text, "response": response_text, "timestamp": time.time()}


def get_existing_page(playwright, session):
    import shutil, time
    profile_dir = session.get("profile_dir", str(Path.home() / ".web_chat_profile"))
    last_error = None
    for attempt in range(3):
        try:
            browser = playwright.chromium.launch_persistent_context(
                user_data_dir=profile_dir, headless=False, args=["--no-sandbox", "--disable-gpu"],
                viewport={"width": 1280, "height": 900},
            )
            page = browser.pages[0] if browser.pages else browser.new_page()
            if page.url == "about:blank" or session["url"] not in page.url:
                page.goto(session["url"], wait_until="domcontentloaded", timeout=30000)
            return browser, page
        except Exception as e:
            last_error = e
            sys.stderr.write(f"[Retry {attempt+1}/3] launch_persistent_context failed: {e}\n")
            # On first failure, try nuking the corrupted profile
            if attempt == 0:
                sys.stderr.write(f"[Retry] Deleting corrupted profile: {profile_dir}\n")
                try:
                    if Path(profile_dir).exists():
                        shutil.rmtree(profile_dir, ignore_errors=True)
                    Path(profile_dir).mkdir(parents=True, exist_ok=True)
                except Exception as ce:
                    sys.stderr.write(f"[Retry] Profile cleanup failed: {ce}\n")
            time.sleep(2)
    raise RuntimeError(f"launch_persistent_context failed after 3 retries: {last_error}")


# ── Actor-Critic 评审逻辑 ─────────────────────────────────────

def parse_review_json(raw_response):
    """尝试从 Critic 回复中提取 JSON 评审结果。"""
    # 直接尝试解析整个回复
    try:
        return json.loads(raw_response)
    except json.JSONDecodeError:
        pass

    # 尝试提取 JSON 块（markdown 代码块）
    for pattern in [r'```json\s*([\s\S]*?)\s*```', r'```\s*([\s\S]*?)\s*```']:
        m = re.search(pattern, raw_response)
        if m:
            try:
                return json.loads(m.group(1))
            except json.JSONDecodeError:
                continue

    # 从 "verdict" 定位，匹配最邻近的平衡括号 JSON
    verdict_idx = raw_response.find('"verdict"')
    if verdict_idx != -1:
        # 向前找到最近的 {
        start = raw_response.rfind('{', 0, verdict_idx)
        if start != -1:
            # 从 start 开始匹配平衡括号
            depth = 0
            end = -1
            for i in range(start, len(raw_response)):
                if raw_response[i] == '{':
                    depth += 1
                elif raw_response[i] == '}':
                    depth -= 1
                    if depth == 0:
                        end = i + 1
                        break
            if end != -1:
                try:
                    return json.loads(raw_response[start:end])
                except json.JSONDecodeError:
                    # 尝试修复常见截断（补闭合括号）
                    candidate = raw_response[start:end]
                    # 如果最后是逗号或冒号，尝试智能补全
                    if candidate.rstrip().endswith(','):
                        candidate = candidate.rstrip()[:-1]
                    try:
                        return json.loads(candidate + '}')
                    except json.JSONDecodeError:
                        pass

    # 无法解析，返回原始文本
    return {
        "verdict": "unknown",
        "score": None,
        "issues": [],
        "suggestions": [],
        "summary": raw_response[:500],
    }


def enable_deep_think(page):
    """在 DeepSeek Chat 页面开启深度思考模式。"""
    try:
        toggles = page.locator('.ds-toggle-button').all()
        for toggle in toggles:
            try:
                text = toggle.inner_text().strip()
                if '深度思考' in text or 'Deep Think' in text or 'DeepThink' in text:
                    cls = toggle.get_attribute('class') or ''
                    if '--selected' not in cls:
                        toggle.click()
                        time.sleep(1)
                        sys.stderr.write("[Critic] 深度思考已开启\n")
                    else:
                        sys.stderr.write("[Critic] 深度思考已处于开启状态\n")
                    return True
            except Exception:
                continue
    except Exception:
        pass
    return False


def switch_mode(page, mode="expert"):
    """切换 DeepSeek Chat 模式：fast / expert / vision。
    
    基于实测 DOM：三个 <DIV role="radio"> 横向排列在 role="radiogroup" 容器中。
    """
    mode_map = {
        "expert": "专家模式",
        "fast": "快速模式",
        "vision": "识图模式",
    }
    target_text = mode_map.get(mode, mode)
    
    try:
        radios = page.locator('[role="radio"]').all()
        for radio in radios:
            try:
                text = radio.inner_text().strip()
                if target_text in text:
                    radio.click()
                    time.sleep(1)
                    sys.stderr.write(f"[Critic] 已切换到: {target_text}\n")
                    return True
            except Exception:
                continue
    except Exception:
        pass
    return False


def do_review(content, context="", deep_think=True, mode="expert", site=None, url=None):
    """Actor-Critic 评审：将产出发给 Critic，返回结构化评审。"""
    from playwright.sync_api import sync_playwright

    if not SESSION_FILE.exists():
        return {"error": "请先运行 --init 初始化"}

    session = json.loads(SESSION_FILE.read_text(encoding="utf-8"))
    # CLI 参数覆盖 session 中的站点
    if site and site in SITE_CONFIGS and site != "custom":
        site_id = site
        session["site_id"] = site
        session["url"] = SITE_CONFIGS[site].get("url", url or session["url"])
    elif url:
        site_id, _ = get_site_config(url)
        session["url"] = url
        session["site_id"] = site_id
    else:
        site_id = session.get("site_id", "custom")
    config = SITE_CONFIGS.get(site_id, SITE_CONFIGS["custom"])

    # 构建评审消息
    if len(content) > MAX_CONTENT_CHARS:
        content = content[:MAX_CONTENT_CHARS] + f"\n\n... (截断，原长度 {len(content)} 字符)"

    prompt = CRITIC_PROMPT
    if context:
        prompt = prompt + f"\n\n上下文：{context}\n"
    if len(content) > 4000:
        prompt = prompt.replace("产出内容：", "产出内容（长文本，请重点关注关键部分）：")

    full_message = prompt + content
    sys.stderr.write(f"[Critic] 发送 {len(full_message)} 字符，等待评审...\n")

    with sync_playwright() as p:
        browser, page = get_existing_page(p, session)
        try:
            if site_id == "deepseek":
                switch_mode(page, mode)
                if deep_think:
                    enable_deep_think(page)
            result = send_message(page, config, full_message)
        finally:
            browser.close()

    if "error" in result:
        return result

    review = parse_review_json(result["response"])
    review["raw"] = result["response"]
    review["_meta"] = {
        "site": config["name"],
        "content_chars": len(content),
        "sent_chars": len(full_message),
        "timestamp": result.get("timestamp"),
    }
    return review


def loop_mode():
    from playwright.sync_api import sync_playwright

    if not SESSION_FILE.exists():
        print("[ERR] 请先运行 --init")
        sys.exit(1)

    session = json.loads(SESSION_FILE.read_text(encoding="utf-8"))
    site_id = session.get("site_id", "custom")
    config = SITE_CONFIGS.get(site_id, SITE_CONFIGS["custom"])

    print(f"[LINK] {config['name']} ({session['url']})")
    print("   输入 /quit 退出, /review 评审模式")
    print("   " + "-" * 50)

    with sync_playwright() as p:
        browser, page = get_existing_page(p, session)
        try:
            while True:
                try:
                    user_input = input("\nDeepSeek > ")
                except (EOFError, KeyboardInterrupt):
                    break

                if not user_input.strip():
                    continue
                if user_input.strip().lower() in ("/quit", "/exit", "/q"):
                    break

                result = send_message(page, config, user_input)
                if "error" in result:
                    print(f"[ERR] {result['error']}")
                    continue
                print(f"\n[WEB] ->\n{result['response']}")
                print("-" * 50)
        finally:
            browser.close()


def single_send(message):
    from playwright.sync_api import sync_playwright

    if not SESSION_FILE.exists():
        print(json.dumps({"error": "请先运行 --init"}, ensure_ascii=False))
        sys.exit(1)

    session = json.loads(SESSION_FILE.read_text(encoding="utf-8"))
    site_id = session.get("site_id", "custom")
    config = SITE_CONFIGS.get(site_id, SITE_CONFIGS["custom"])

    with sync_playwright() as p:
        browser, page = get_existing_page(p, session)
        try:
            result = send_message(page, config, message)
            print(json.dumps(result, ensure_ascii=False, indent=2))
        finally:
            browser.close()


# ── Daemon 模式：HTTP 服务器保持浏览器长驻 ────────────────────

_daemon_browser = None
_daemon_page = None
_daemon_config = None
_daemon_session = None
_daemon_playwright = None
_daemon_start_time = 0.0


def _upload_image(page, image_path):
    """上传图片到聊天页面。file input 优先，找不到则模拟拖拽丢图。"""
    if not Path(image_path).exists():
        return False, f"图片文件不存在: {image_path}"
    
    import base64
    
    # ── 策略 1：file input ──
    file_inputs = page.locator('input[type="file"]').all()
    
    if not file_inputs:
        attach_selectors = [
            'button[aria-label*="附件" i]', 'button[aria-label*="上传" i]',
            '[class*="upload-btn"]', '[class*="attach"]', 'button[class*="upload"]',
        ]
        for sel in attach_selectors:
            try:
                btn = page.locator(sel).first
                if btn.is_visible(timeout=1000):
                    btn.click()
                    time.sleep(1.0)
                    file_inputs = page.locator('input[type="file"]').all()
                    if file_inputs:
                        break
            except Exception:
                continue
    
    if file_inputs:
        try:
            file_inputs[0].set_input_files(image_path)
            sys.stderr.write(f"[图片] file input 上传: {Path(image_path).name}\n")
            _wait_for_image_preview(page)
            return True, None
        except Exception as e:
            return False, f"上传失败: {e}"
    
    # ── 策略 2：page.route 虚拟文件服务 + 拖拽/粘贴（通用，绕过参数大小限制）──
    sys.stderr.write(f"[图片] route 上传: {Path(image_path).name}\n")
    try:
        data = Path(image_path).read_bytes()
        ext = Path(image_path).suffix.lower()
        mime_map = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif", ".webp": "image/webp", ".bmp": "image/bmp"}
        mime = mime_map.get(ext, "image/png")
        filename = Path(image_path).name
        
        # 注册虚拟路由：JS fetch 时 Playwright 拦截并返回本地文件
        VIRTUAL_URL = "__local_img__"
        page.route(f"**/{VIRTUAL_URL}**", lambda route: route.fulfill(
            body=data, content_type=mime
        ))
        
        # JS：fetch 虚拟 URL → Blob → File → DataTransfer → drop
        page.evaluate("""
            async (virtualUrl, mime, filename) => {
                const resp = await fetch(virtualUrl);
                const blob = await resp.blob();
                const file = new File([blob], filename, {type: mime});
                
                const dt = new DataTransfer();
                dt.items.add(file);
                
                const target = document.querySelector('textarea') 
                    || document.querySelector('[contenteditable="true"]')
                    || document.querySelector('[class*="input"]');
                if (!target) return;
                target.focus();
                
                // 先试 paste 事件
                target.dispatchEvent(new ClipboardEvent('paste', {
                    bubbles: true, cancelable: true, clipboardData: dt
                }));
                
                // 同时也派发 drop（某些站点只认其中一种）
                ['dragenter', 'dragover'].forEach(evtName => {
                    target.dispatchEvent(new DragEvent(evtName, {
                        bubbles: true, cancelable: true, dataTransfer: dt
                    }));
                });
                target.dispatchEvent(new DragEvent('drop', {
                    bubbles: true, cancelable: true, dataTransfer: dt
                }));
            }
        """, [VIRTUAL_URL, mime, filename])
        
        # 清理路由
        try:
            page.unroute(f"**/{VIRTUAL_URL}**")
        except Exception:
            pass
        
        sys.stderr.write(f"[图片] route 完成: {filename}\n")
        _wait_for_image_preview(page)
        return True, None
    except Exception as e:
        return False, f"拖拽上传失败: {e}"


def _wait_for_image_preview(page):
    """等待图片预览/缩略图出现，最多 20 秒。"""
    loaded = False
    for _ in range(20):
        time.sleep(1)
        preview_selectors = [
            'img[src*="blob"]', '[class*="preview"] img', '[class*="thumbnail"] img',
            '[class*="upload"] img', '.ds-upload-preview img',
        ]
        for psel in preview_selectors:
            try:
                if page.locator(psel).count() > 0:
                    loaded = True
                    break
            except Exception:
                continue
        if loaded:
            break
    if loaded:
        time.sleep(1.0)
        sys.stderr.write("[图片] 预览加载完成\n")
    else:
        sys.stderr.write("[图片] 未检测到预览，继续\n")


def _send_with_image(page, config, image_path, text_prompt=""):
    """上传图片并附带文本，然后发送。"""
    # 1. 上传图片
    ok, err = _upload_image(page, image_path)
    if not ok:
        return {"error": err}
    
    # 2. 如果有文本，输入文本
    if text_prompt:
        input_el = find_and_fill_input(page, config)
        if input_el is None:
            return {"error": "找不到输入框"}
        
        # 用粘贴方式输入（文本一般较短）
        if len(text_prompt) >= PASTE_THRESHOLD:
            human_paste(page, input_el, config, text_prompt)
        else:
            human_type(page, input_el, text_prompt)
    
    # 3. 发送
    send_selectors = config["send_button"].split(", ")
    sent = False
    for sel in send_selectors:
        try:
            btn = page.locator(sel).first
            if btn.is_visible(timeout=1000) and btn.is_enabled():
                btn.click()
                sent = True
                break
        except Exception:
            continue
    if not sent:
        try:
            page.keyboard.press("Enter")
            sent = True
        except Exception:
            return {"error": "无法点击发送按钮"}
    
    sys.stderr.write("[图片] 已发送，等待回复...\n")
    
    # 4. 等待回复（重试验证：排除仅含 UI 占位文本的无效回复）
    for attempt in range(2):
        result = _wait_for_response(page, config, text_prompt or "(图片)")
        resp = result.get("response", "")
        # 如果回复很短且是占位文本，说明图片还在加载或发送失败，重试
        skip_phrases = ["使用识图模式开始对话", "深度思考", "AI 生成可能有误"]
        is_placeholder = len(resp) < 80 and any(p in resp for p in skip_phrases)
        if is_placeholder and attempt == 0:
            sys.stderr.write("[图片] 回复为占位文本，等待 3 秒后重试发送...\n")
            time.sleep(3)
            # 重试发送
            page.keyboard.press("Enter")
            continue
        return result
    
    return result


def _wait_for_response(page, config, sent_text):
    """等待回复完成并提取文本。从 send_message 中抽取的共享逻辑。"""
    wait_sel = config.get("wait_selector", "")
    wait_disappear = config.get("wait_disappear", True)
    
    if wait_sel:
        try:
            page.wait_for_selector(wait_sel, timeout=10000)
        except Exception:
            pass
    if wait_sel and wait_disappear:
        try:
            page.wait_for_selector(wait_sel, state="detached", timeout=300000)
        except Exception:
            pass
    
    # 响应稳定性检测
    resp_selectors = config["response_selector"].split(", ")
    response_text = ""
    stable_count = 0
    for _ in range(30):
        time.sleep(1)
        current = ""
        for sel in resp_selectors:
            try:
                el = page.locator(sel).last
                if el.is_visible():
                    current = el.inner_text()
                    break
            except Exception:
                continue
        if not current:
            try:
                current = page.locator("body").inner_text()
            except Exception:
                pass
        if current == response_text and len(current) > 10:
            stable_count += 1
            if stable_count >= 3:
                break
        else:
            stable_count = 0
            response_text = current
    
    if not response_text or len(response_text) < 5:
        for sel in resp_selectors:
            try:
                el = page.locator(sel).last
                if el.is_visible():
                    response_text = el.inner_text()
                    break
            except Exception:
                continue
    if not response_text:
        try:
            response_text = page.locator("body").inner_text()
        except Exception:
            response_text = "(无法提取回复)"
    
    return {"sent": sent_text, "response": response_text, "timestamp": time.time()}


def _try_recover_page():
    """尝试恢复已关闭的浏览器页面。"""
    global _daemon_browser, _daemon_page, _daemon_session, _daemon_playwright
    if _daemon_session is None:
        return False
    try:
        # 如果浏览器还活着，只需重建页面
        if _daemon_browser and _daemon_browser.contexts:
            ctx = _daemon_browser.contexts[0]
            pages = ctx.pages
            if pages:
                _daemon_page = pages[0]
            else:
                _daemon_page = ctx.new_page()
            _daemon_page.goto(_daemon_session["url"], wait_until="domcontentloaded", timeout=30000)
            sys.stderr.write(f"[Daemon] 页面已恢复 (复用已有浏览器)\n")
            return True
        
        # 浏览器死了，需要完全重建：先清理旧实例，再启动新实例
        if _daemon_browser:
            try:
                _daemon_browser.close()
            except Exception:
                pass
            _daemon_browser = None
        if _daemon_playwright:
            try:
                _daemon_playwright.stop()
            except Exception:
                pass
            _daemon_playwright = None
        
        from playwright.sync_api import sync_playwright
        _daemon_playwright = sync_playwright().start()
        _daemon_browser, _daemon_page = get_existing_page(_daemon_playwright, _daemon_session)
        site_id = _daemon_session.get("site_id", "custom")
        if site_id == "deepseek":
            try:
                switch_mode(_daemon_page, "expert")
                enable_deep_think(_daemon_page)
            except Exception:
                pass
        sys.stderr.write(f"[Daemon] 浏览器已完全重建\n")
        return True
    except Exception as e:
        sys.stderr.write(f"[Daemon] 浏览器恢复失败: {e}\n")
        return False

def daemon_send_message(message):
    """在 daemon 持有的浏览器中发送消息。"""
    global _daemon_page, _daemon_config
    sys.stderr.write(f"[Daemon] send_message: _daemon_page={_daemon_page}, is_closed={_daemon_page.is_closed() if _daemon_page else 'None'}\n")
    if _daemon_page is None or _daemon_page.is_closed():
        sys.stderr.write(f"[Daemon] send_message: page None/closed, trying recovery...\n")
        if not _try_recover_page():
            return {"error": "浏览器未就绪，请稍后再试"}
    try:
        result = send_message(_daemon_page, _daemon_config, message)
        return result
    except Exception as e:
        sys.stderr.write(f"[Daemon] send_message 异常: {e}\n")
        return {"error": f"发送失败: {e}"}


def daemon_review_image(image_path, context="", mode="vision"):
    """在 daemon 浏览器中：切识图模式 → 上传图片 → 发送识图请求。"""
    global _daemon_page, _daemon_config, _daemon_session
    if _daemon_page is None or _daemon_page.is_closed():
        if not _try_recover_page():
            return {"error": "浏览器未就绪，请稍后再试"}
    
    # 切识图模式（仅 DeepSeek 有此功能，豆包忽略）
    try:
        switch_mode(_daemon_page, mode)
    except Exception:
        pass
    
    # 识图 prompt：直接让模型看图片内容
    full_prompt = "请仔细查看这张图片，描述你看到的所有内容。如果图片包含文本、数据、图表、错误信息等，请逐一解读。"
    if context:
        full_prompt = f"{context}\n\n{full_prompt}"
    
    sys.stderr.write(f"[识图] 上传图片: {image_path}\n")
    result = _send_with_image(_daemon_page, _daemon_config, image_path, full_prompt)
    
    if "error" in result:
        return result
    
    review = parse_review_json(result["response"])
    review["raw"] = result["response"]
    review["_meta"] = {
        "site": _daemon_config["name"],
        "image": image_path,
        "timestamp": result.get("timestamp"),
    }
    return review


def daemon_do_review(content, context="", deep_think=True, mode="expert"):
    """在 daemon 持有的浏览器中执行评审。"""
    global _daemon_page, _daemon_config, _daemon_session
    if _daemon_page is None or _daemon_page.is_closed():
        if not _try_recover_page():
            return {"error": "浏览器未就绪，请稍后再试"}
    
    site_id = _daemon_session.get("site_id", "custom")
    
    if len(content) > MAX_CONTENT_CHARS:
        content = content[:MAX_CONTENT_CHARS] + f"\n\n... (截断，原长度 {len(content)} 字符)"
    
    prompt = CRITIC_PROMPT
    if context:
        prompt = prompt + f"\n\n上下文：{context}\n"
    
    full_message = prompt + content
    sys.stderr.write(f"[Critic] 发送 {len(full_message)} 字符，等待评审...\n")
    
    try:
        if site_id == "deepseek":
            switch_mode(_daemon_page, mode)
            if deep_think:
                enable_deep_think(_daemon_page)
    except Exception:
        pass
    
    result = send_message(_daemon_page, _daemon_config, full_message)
    
    if "error" in result:
        return result
    
    review = parse_review_json(result["response"])
    review["raw"] = result["response"]
    review["_meta"] = {
        "site": _daemon_config["name"],
        "content_chars": len(content),
        "sent_chars": len(full_message),
        "timestamp": result.get("timestamp"),
    }
    return review


# ── Browser Agent 操作函数 ─────────────────────────────────────

SCREENSHOT_DIR = Path.home() / ".web_chat_screenshots"

def _ensure_page():
    """确保浏览器页面可用，否则尝试恢复。返回 (page, config) 或 (None, None)。"""
    global _daemon_page, _daemon_config
    if _daemon_page is None or _daemon_page.is_closed():
        if not _try_recover_page():
            return None, None
    return _daemon_page, _daemon_config


def daemon_screenshot(full_page=False, quality=80):
    """截图并保存到本地，返回文件路径。"""
    page, _ = _ensure_page()
    if page is None:
        return {"error": "浏览器未就绪"}
    try:
        SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
        ts = time.strftime("%Y%m%d_%H%M%S")
        filename = f"shot_{ts}.jpg"
        filepath = SCREENSHOT_DIR / filename
        page.screenshot(path=str(filepath), full_page=full_page, quality=quality, type="jpeg")
        sys.stderr.write(f"[Agent] 截图已保存: {filepath}\n")
        return {
            "status": "ok",
            "path": str(filepath),
            "filename": filename,
            "url": page.url,
            "title": page.title(),
        }
    except Exception as e:
        return {"error": f"截图失败: {e}"}


def daemon_navigate(url, wait_until="domcontentloaded", timeout=30000):
    """导航到指定 URL。"""
    page, config = _ensure_page()
    if page is None:
        return {"error": "浏览器未就绪"}
    try:
        page.goto(url, wait_until=wait_until, timeout=timeout)
        sys.stderr.write(f"[Agent] 导航到: {url}\n")
        return {
            "status": "ok",
            "url": page.url,
            "title": page.title(),
        }
    except Exception as e:
        return {"error": f"导航失败: {e}"}


def daemon_click(selector=None, text=None, index=0, timeout=5000):
    """点击元素：支持 CSS selector 或文本内容匹配。"""
    page, _ = _ensure_page()
    if page is None:
        return {"error": "浏览器未就绪"}
    try:
        if selector:
            el = page.locator(selector).nth(index)
        elif text:
            el = page.get_by_text(text, exact=False).first
        else:
            return {"error": "需要 selector 或 text 参数"}
        
        if not el.is_visible(timeout=timeout):
            return {"error": f"元素不可见: {selector or text}"}
        
        tag = el.evaluate("el => el.tagName")
        el_text = el.inner_text()[:80] if el else ""
        el.click()
        sys.stderr.write(f"[Agent] 点击: <{tag}> {el_text}\n")
        time.sleep(0.5)  # 等页面响应
        return {
            "status": "ok",
            "clicked": f"<{tag}> {el_text}",
            "url": page.url,
            "title": page.title(),
        }
    except Exception as e:
        return {"error": f"点击失败: {e}"}


def daemon_scroll(direction="down", amount=300):
    """滚动页面。"""
    page, _ = _ensure_page()
    if page is None:
        return {"error": "浏览器未就绪"}
    try:
        if direction == "down":
            page.evaluate(f"window.scrollBy(0, {amount})")
        elif direction == "up":
            page.evaluate(f"window.scrollBy(0, -{amount})")
        elif direction == "bottom":
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        elif direction == "top":
            page.evaluate("window.scrollTo(0, 0)")
        else:
            return {"error": f"不支持的方向: {direction} (支持: up/down/top/bottom)"}
        scroll_y = page.evaluate("window.scrollY")
        sys.stderr.write(f"[Agent] 滚动 {direction} → scrollY={scroll_y}\n")
        return {"status": "ok", "direction": direction, "scrollY": scroll_y}
    except Exception as e:
        return {"error": f"滚动失败: {e}"}


def daemon_read(selector=None):
    """提取页面可见文本。可指定 selector 只提取某元素。"""
    page, _ = _ensure_page()
    if page is None:
        return {"error": "浏览器未就绪"}
    try:
        if selector:
            el = page.locator(selector).first
            if not el.is_visible():
                return {"error": f"元素不可见: {selector}"}
            text = el.inner_text()
            tag = el.evaluate("el => el.tagName")
        else:
            text = page.locator("body").inner_text()
            tag = "body"
        
        # 截断超长文本
        max_chars = 10000
        truncated = len(text) > max_chars
        if truncated:
            text = text[:max_chars] + f"\n\n... (截断，原长度 {len(text)} 字符)"
        
        sys.stderr.write(f"[Agent] 读取 <{tag}>: {len(text)} 字符\n")
        return {
            "status": "ok",
            "tag": tag,
            "text": text,
            "truncated": truncated,
            "total_chars": len(text) if not truncated else max_chars,
            "url": page.url,
            "title": page.title(),
            "scrollY": page.evaluate("window.scrollY"),
            "scrollHeight": page.evaluate("document.body.scrollHeight"),
        }
    except Exception as e:
        return {"error": f"读取失败: {e}"}


def daemon_type_text(selector, text, human_like=True):
    """在指定元素中输入文本。human_like=True 时使用人类化逐字打字。"""
    page, config = _ensure_page()
    if page is None:
        return {"error": "浏览器未就绪"}
    try:
        el = page.locator(selector).first
        if not el.is_visible():
            return {"error": f"元素不可见: {selector}"}
        el.click()
        el.fill("")  # 清空
        
        if human_like and len(text) < PASTE_THRESHOLD:
            human_type(page, el, text)
        else:
            human_paste(page, el, config or SITE_CONFIGS["custom"], text)
        
        sys.stderr.write(f"[Agent] 输入到 <{selector}>: {len(text)} 字符\n")
        return {
            "status": "ok",
            "selector": selector,
            "length": len(text),
            "method": "human_type" if (human_like and len(text) < PASTE_THRESHOLD) else "human_paste",
        }
    except Exception as e:
        return {"error": f"输入失败: {e}"}


def daemon_execute(js_code):
    """在页面中执行 JavaScript 并返回结果。"""
    page, _ = _ensure_page()
    if page is None:
        return {"error": "浏览器未就绪"}
    try:
        result = page.evaluate(js_code)
        sys.stderr.write(f"[Agent] JS 执行完成\n")
        return {
            "status": "ok",
            "result": result,
        }
    except Exception as e:
        return {"error": f"JS 执行失败: {e}"}


class DaemonHandler(http.server.BaseHTTPRequestHandler):
    """处理来自 CLI 的 HTTP 请求。"""
    
    def _json_reply(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", len(body))
        self.end_headers()
        self.wfile.write(body)
    
    def do_GET(self):
        if self.path == "/status":
            global _daemon_session
            self._json_reply({
                "status": "ok",
                "site": _daemon_config.get("name", "unknown") if _daemon_config else "unknown",
                "url": _daemon_session.get("url", "") if _daemon_session else "",
            })
        elif self.path == "/health":
            self._json_reply({
                "status": "ok",
                "uptime": time.time() - _daemon_start_time if "_daemon_start_time" in globals() else -1,
                "browser_alive": not (_daemon_page is None or _daemon_page.is_closed()) if "_daemon_page" in globals() else False,
            })
        else:
            self._json_reply({"error": "not found"}, 404)
    
    def do_POST(self):
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length) if content_length else b"{}"
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            self._json_reply({"error": "invalid JSON"}, 400)
            return
        
        if self.path == "/send":
            message = data.get("message", "")
            if not message:
                self._json_reply({"error": "缺少 message 字段"}, 400)
                return
            try:
                result = daemon_send_message(message)
                self._json_reply(result)
            except Exception as e:
                sys.stderr.write(f"[Daemon] /send 异常: {e}\n")
                self._json_reply({"error": str(e)}, 500)
        
        elif self.path == "/review":
            content = data.get("content", "")
            if not content:
                self._json_reply({"error": "缺少 content 字段"}, 400)
                return
            
            context = data.get("context", "")
            mode = data.get("mode", "expert")
            bypass = data.get("cache_bypass", False)
            
            # 缓存查询
            if not bypass:
                key = _cache_key(content, context, mode)
                if key in _review_cache:
                    cached = _review_cache[key]
                    cached["_source"] = "cache"
                    self._json_reply(cached)
                    return
            
            result = daemon_do_review(
                content,
                context=context,
                deep_think=data.get("deep_think", True),
                mode=mode,
            )
            
            # 写入缓存
            if "error" not in result:
                key = _cache_key(content, context, mode)
                _review_cache[key] = result
                result["_source"] = "fresh"
            else:
                result["_source"] = "error"
            
            self._json_reply(result)
        
        elif self.path == "/review-image":
            image = data.get("image", "")
            if not image:
                self._json_reply({"error": "缺少 image 字段"}, 400)
                return
            result = daemon_review_image(
                image,
                context=data.get("context", ""),
                mode=data.get("mode", "vision"),
            )
            self._json_reply(result)
        
        elif self.path == "/shutdown":
            self._json_reply({"status": "shutting down"})
            threading.Thread(target=self.server.shutdown, daemon=True).start()
        
        elif self.path == "/cache/clear":
            scope = data.get("scope", "all")
            if scope == "all":
                _review_cache.clear()
                self._json_reply({"status": "ok", "cleared": "all", "note": "所有缓存已清除"})
            else:
                self._json_reply({"error": f"不支持的 scope: {scope}"}, 400)
        
        # ── Browser Agent 端点 ──
        elif self.path == "/screenshot":
            full_page = data.get("full_page", False)
            quality = data.get("quality", 80)
            result = daemon_screenshot(full_page=full_page, quality=quality)
            self._json_reply(result)
        
        elif self.path == "/navigate":
            url = data.get("url", "")
            if not url:
                self._json_reply({"error": "缺少 url 字段"}, 400)
                return
            result = daemon_navigate(
                url,
                wait_until=data.get("wait_until", "domcontentloaded"),
                timeout=data.get("timeout", 30000),
            )
            self._json_reply(result)
        
        elif self.path == "/click":
            if not data.get("selector") and not data.get("text"):
                self._json_reply({"error": "需要 selector 或 text 参数"}, 400)
                return
            result = daemon_click(
                selector=data.get("selector"),
                text=data.get("text"),
                index=data.get("index", 0),
                timeout=data.get("timeout", 5000),
            )
            self._json_reply(result)
        
        elif self.path == "/scroll":
            result = daemon_scroll(
                direction=data.get("direction", "down"),
                amount=data.get("amount", 300),
            )
            self._json_reply(result)
        
        elif self.path == "/read":
            result = daemon_read(selector=data.get("selector"))
            self._json_reply(result)
        
        elif self.path == "/type":
            selector = data.get("selector", "")
            text = data.get("text", "")
            if not selector or not text:
                self._json_reply({"error": "需要 selector 和 text 参数"}, 400)
                return
            result = daemon_type_text(
                selector,
                text,
                human_like=data.get("human_like", True),
            )
            self._json_reply(result)
        
        elif self.path == "/execute":
            js_code = data.get("code", "")
            if not js_code:
                self._json_reply({"error": "缺少 code 字段"}, 400)
                return
            result = daemon_execute(js_code)
            self._json_reply(result)
        
        else:
            self._json_reply({"error": "not found"}, 404)
    
    def log_message(self, format, *args):
        sys.stderr.write(f"[Daemon] {args[0]}\n")


def start_daemon(port=DAEMON_PORT, site=None, url=None):
    """启动 HTTP daemon，保持浏览器长驻。"""
    from playwright.sync_api import sync_playwright
    
    global _daemon_browser, _daemon_page, _daemon_config, _daemon_session, _daemon_start_time
    
    _daemon_start_time = time.time()
    
    # ── 端口冲突检测 ──
    alive, info = _check_daemon(port)
    if alive:
        browser_ok = info.get("browser_alive", False)
        uptime = info.get("uptime", -1)
        if browser_ok:
            print(f"[Daemon] 端口 {port} 已有运行中的 daemon (浏览器正常, 已运行 {uptime:.0f}s)", flush=True)
            print(f"[Daemon] 无需重复启动，直接使用现有 daemon 即可", flush=True)
            sys.exit(0)
        else:
            sys.stderr.write(f"[Daemon] 端口 {port} 上有残留 daemon (浏览器已死), 需要重启\n")
            sys.stderr.write(f"[Daemon] 请先手动终止该进程后重试\n")
            sys.exit(1)
    
    if not SESSION_FILE.exists():
        print(json.dumps({"error": "请先运行 --init 初始化"}, ensure_ascii=False))
        sys.exit(1)
    
    _daemon_session = json.loads(SESSION_FILE.read_text(encoding="utf-8"))
    
    if site and site in SITE_CONFIGS and site != "custom":
        _daemon_session["site_id"] = site
        _daemon_session["url"] = SITE_CONFIGS[site].get("url", _daemon_session["url"])
    elif url:
        site_id, _ = get_site_config(url)
        _daemon_session["site_id"] = site_id
        _daemon_session["url"] = url
    
    site_id = _daemon_session.get("site_id", "custom")
    _daemon_config = SITE_CONFIGS.get(site_id, SITE_CONFIGS["custom"])
    
    # 先初始化浏览器（必须在主线程，Playwright greenlet 有线程亲和性）
    _init_ok = False
    for attempt in range(5):
        try:
            _daemon_playwright = sync_playwright().start()
            _daemon_browser, _daemon_page = get_existing_page(_daemon_playwright, _daemon_session)
            if site_id == "deepseek":
                try:
                    switch_mode(_daemon_page, "expert")
                    enable_deep_think(_daemon_page)
                except Exception:
                    pass
            print(f"[Daemon] 浏览器已就绪 — {_daemon_config['name']} ({_daemon_session['url']})", flush=True)
            _init_ok = True
            break
        except Exception as e:
            sys.stderr.write(f"[Daemon] 浏览器初始化失败(尝试{attempt+1}/5): {e}\n")
            try:
                if _daemon_playwright:
                    _daemon_playwright.stop()
                    _daemon_playwright = None
            except Exception:
                pass
            time.sleep(5)
    if not _init_ok:
        sys.stderr.write(f"[Daemon] 浏览器初始化失败(已重试5次)，HTTP 服务仍可用但浏览器操作不可用\n")
    
    # 启动 HTTP 服务（单线程，与浏览器共享主线程 greenlet 上下文）
    server = http.server.HTTPServer(("127.0.0.1", port), DaemonHandler)
    print(f"[Daemon] HTTP 服务: http://127.0.0.1:{port}", flush=True)
    print(f"[Daemon] Actor-Critic: POST /send | /review | /review-image | /cache/clear", flush=True)
    print(f"[Daemon] Browser Agent: POST /navigate | /screenshot | /read | /click | /scroll | /type | /execute", flush=True)
    print(f"[Daemon] 管理: GET /status | /health | POST /shutdown", flush=True)
    
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[Daemon] 正在关闭...", flush=True)
    finally:
        server.server_close()
        try:
            _daemon_browser.close()
        except Exception:
            pass
        try:
            if _daemon_playwright:
                _daemon_playwright.stop()
        except Exception:
            pass


def _call_daemon(endpoint, data, port=None):
    """向 daemon 发送 HTTP 请求。返回 (ok, result)。
    port=None 时使用默认 DAEMON_PORT，否则使用指定端口。"""
    if port is None:
        port = DAEMON_PORT
    import urllib.request
    try:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/{endpoint}",
            data=body,
            headers={"Content-Type": "application/json; charset=utf-8"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=300) as resp:
            return True, json.loads(resp.read().decode("utf-8"))
    except urllib.request.URLError:
        return False, {"error": f"无法连接 daemon (端口 {port})，请先运行 --serve"}
    except Exception as e:
        return False, {"error": str(e)}


def _check_daemon(port=None):
    """检测 daemon 是否存活。返回 (alive: bool, info: dict)。"""
    if port is None:
        port = DAEMON_PORT
    import urllib.request
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{port}/health")
        with urllib.request.urlopen(req, timeout=5) as resp:
            return True, json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        return False, {"error": str(e), "port": port}


def _with_cache(key, compute_fn, force_refresh=False, level="L1"):
    """缓存装饰器：singleflight合并 + 分级key + 命中率统计。"""
    global _cache_stats
    _cache_stats["total"] += 1
    # 缓存命中快速路径（含滑动 TTL）
    if not force_refresh and key in _review_cache:
        # 硬上限检查
        if key in _cache_birth and (time.time() - _cache_birth[key]) > CACHE_MAX_AGE:
            del _review_cache[key]
            _cache_birth.pop(key, None)
        else:
            # 滑动 TTL：命中时 pop + reinsert 重置过期计时
            value = _review_cache.pop(key)
            _review_cache[key] = value
            _cache_stats[f"hit_{level.lower()}"] += 1
            return _review_cache[key], True
    # singleflight：同 key 并发只跑一次 compute_fn
    with _inflight_lock:
        if key not in _inflight:
            _inflight[key] = threading.Event()
            is_leader = True
        else:
            is_leader = False
            event = _inflight[key]
    if not is_leader:
        event.wait()
        if key in _review_cache:
            _cache_stats[f"hit_{level.lower()}"] += 1
            return _review_cache[key], True
    # leader 执行
    try:
        _cache_stats["miss"] += 1
        result = compute_fn()
        _review_cache[key] = result
        _cache_birth[key] = time.time()
        _mark_cache_dirty()
        _maybe_save_cache()
        return result, False
    finally:
        if is_leader:
            with _inflight_lock:
                event = _inflight.pop(key, None)
                if event:
                    event.set()


# ── CLI ──────────────────────────────────────────────────────
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="网页大模型对话桥接 v5 — Actor-Critic + Browser Agent")
    parser.add_argument("--init", action="store_true", help="初始化浏览器会话")
    parser.add_argument("--url", default="https://chat.deepseek.com", help="聊天页面 URL")
    parser.add_argument("--site", help="站点标识 (deepseek/openai/claude/kimi/tongyi/doubao)")

    # Actor-Critic 评审
    parser.add_argument("--review", help="评审一段文本")
    parser.add_argument("--review-file", help="从文件读取产出并评审")
    parser.add_argument("--review-image", help="上传图片到识图模式并评审（需 DeepSeek Chat）")
    parser.add_argument("--context", default="", help="评审附加上下文说明")
    parser.add_argument("--deep", action="store_true", default=True, help="开启深度思考 (默认)")
    parser.add_argument("--no-deep", action="store_true", help="关闭深度思考")
    parser.add_argument("--mode", default="expert", choices=["fast", "expert", "vision"], help="模式切换 (默认 expert)")
    parser.add_argument("--no-cache", action="store_true", help="跳过评审缓存，强制重新请求")

    # 通用
    parser.add_argument("--send", help="发送单条消息")
    parser.add_argument("--loop", action="store_true", help="持续对话模式")
    parser.add_argument("--serve", action="store_true", help="启动 daemon 模式（浏览器长驻）")
    parser.add_argument("--port", type=int, default=DAEMON_PORT, help=f"Daemon 端口 (默认 {DAEMON_PORT})，也用于 --review/--send 连接")
    parser.add_argument("--cache-file", default=str(CACHE_FILE), help=f"缓存持久化文件 (默认 {CACHE_FILE})")
    parser.add_argument("--cache-info", action="store_true", help="显示缓存统计信息后退出")

    # ── Browser Agent CLI ──
    parser.add_argument("--screenshot", action="store_true", help="截取当前页面并保存")
    parser.add_argument("--full-page", action="store_true", help="--screenshot 时截取整页")
    parser.add_argument("--navigate", help="导航到指定 URL")
    parser.add_argument("--click", help="点击元素 (CSS selector)")
    parser.add_argument("--click-text", help="点击包含指定文本的元素")
    parser.add_argument("--scroll", help="滚动方向 (up/down/top/bottom)")
    parser.add_argument("--scroll-amount", type=int, default=300, help="滚动像素量 (默认 300)")
    parser.add_argument("--read", action="store_true", help="提取页面可见文本")
    parser.add_argument("--read-selector", help="--read 时只提取指定 CSS selector 的文本")
    parser.add_argument("--type", dest="type_text", help="在指定元素中输入文本（需配合 --type-selector）")
    parser.add_argument("--type-selector", help="--type 的目标元素 CSS selector")
    parser.add_argument("--execute", help="在页面中执行 JavaScript 代码")

    args = parser.parse_args()

    # 加载持久化缓存
    _load_cache()
    atexit.register(_flush_cache)

    # --cache-info：显示统计后退出
    if args.cache_info:
        total = _cache_stats["total"]
        hit_rate = f"{(_cache_stats['hit_l1'] + _cache_stats['hit_l2']) / total * 100:.1f}%" if total else "N/A"
        print(json.dumps({
            "cache_size": len(_review_cache),
            "cache_maxsize": CACHE_MAXSIZE,
            "cache_ttl_seconds": CACHE_TTL,
            "cache_file": args.cache_file,
            "stats": _cache_stats,
            "hit_rate": hit_rate,
        }, ensure_ascii=False, indent=2))
        sys.exit(0)

    if args.serve:
        start_daemon(port=args.port, site=args.site, url=args.url)
    
    elif args.init:
        site_id = args.site
        if site_id is None:
            site_id, _ = get_site_config(args.url)
        init_browser(args.url, site_id)
    
    elif args.review or args.review_file:
        content = args.review or ""
        if args.review_file:
            try:
                content = Path(args.review_file).read_text(encoding="utf-8")
                sys.stderr.write(f"[Actor] 读取文件: {args.review_file} ({len(content)} 字符)\n")
            except Exception as e:
                print(json.dumps({"error": f"无法读取文件: {e}"}, ensure_ascii=False))
                sys.exit(1)
        
        # L1 精确 key（content+context+mode）
        l1_key = _cache_key(content, args.context, args.mode)
        # L2 宽松 key（仅 content，忽略 context/mode 差异）
        l2_key = _cache_key(content, normalize=True)  # context="" mode=default

        def _do_review_daemon():
            ok, result = _call_daemon("review", {
                "content": content,
                "context": args.context,
                "deep_think": not args.no_deep,
                "mode": args.mode,
            }, port=args.port)
            if ok:
                return result
            sys.stderr.write(f"[Fallback] {result.get('error', 'daemon 不可用')}，使用直接模式\n")
            return do_review(content, args.context, deep_think=not args.no_deep, mode=args.mode, site=args.site, url=args.url if args.url != "https://chat.deepseek.com" else None)

        # L1 精确匹配
        result, cache_hit = _with_cache(l1_key, _do_review_daemon, force_refresh=args.no_cache, level="L1")
        if cache_hit:
            sys.stderr.write(f"[Cache] L1命中 (精确)\n")
        else:
            # L1 miss → L2 宽松回退（仅 content 匹配）
            result2, cache_hit2 = _with_cache(l2_key, _do_review_daemon, force_refresh=args.no_cache, level="L2")
            if cache_hit2 and result2 != result:
                sys.stderr.write(f"[Cache] L2命中 (content-only, context/mode 差异已被忽略)\n")
                result = result2
        print(json.dumps(result, ensure_ascii=False, indent=2))
    
    elif args.review_image:
        image_path = args.review_image
        if not Path(image_path).exists():
            print(json.dumps({"error": f"图片不存在: {image_path}"}, ensure_ascii=False))
            sys.exit(1)
        # 优先通过 daemon
        ok, result = _call_daemon("review-image", {
            "image": str(Path(image_path).resolve()),
            "context": args.context,
            "mode": "vision",
        }, port=args.port)
        if ok:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            sys.stderr.write(f"[Fallback] {result.get('error', 'daemon 不可用')}\n")
            sys.stderr.write("图片评审需要 daemon 模式，请先运行 --serve\n")
    
    elif args.loop:
        loop_mode()
    
    elif args.send:
        # 优先通过 daemon
        ok, result = _call_daemon("send", {"message": args.send}, port=args.port)
        if ok:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            sys.stderr.write(f"[Fallback] {result.get('error', 'daemon 不可用')}，使用直接模式\n")
            single_send(args.send)
    
    # ── Browser Agent CLI ──
    elif args.screenshot:
        ok, result = _call_daemon("screenshot", {
            "full_page": args.full_page,
            "quality": 80,
        }, port=args.port)
        if ok:
            print(json.dumps(result, ensure_ascii=False, indent=2))
            if result.get("path"):
                print(f"\n📸 截图: {result['path']}")
        else:
            print(json.dumps(result, ensure_ascii=False, indent=2))
    
    elif args.navigate:
        ok, result = _call_daemon("navigate", {"url": args.navigate}, port=args.port)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    
    elif args.click or args.click_text:
        ok, result = _call_daemon("click", {
            "selector": args.click,
            "text": args.click_text,
        }, port=args.port)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    
    elif args.scroll:
        ok, result = _call_daemon("scroll", {
            "direction": args.scroll,
            "amount": args.scroll_amount,
        }, port=args.port)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    
    elif args.read:
        ok, result = _call_daemon("read", {
            "selector": args.read_selector,
        }, port=args.port)
        if ok and result.get("text"):
            print(result["text"])
        else:
            print(json.dumps(result, ensure_ascii=False, indent=2))
    
    elif args.type_text:
        if not args.type_selector:
            print(json.dumps({"error": "需要 --type-selector 指定目标元素"}, ensure_ascii=False))
            sys.exit(1)
        ok, result = _call_daemon("type", {
            "selector": args.type_selector,
            "text": args.type_text,
            "human_like": True,
        }, port=args.port)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    
    elif args.execute:
        ok, result = _call_daemon("execute", {"code": args.execute}, port=args.port)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    
    else:
        parser.print_help()