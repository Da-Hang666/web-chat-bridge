"""
web-chat-bridge MCP — 浏览器控制层

从 v3 提取的核心 Playwright 交互逻辑：
- init_browser — 初始化浏览器会话
- get_existing_page — 复用已有持久化浏览器上下文
- human_type — 逐字打字（短文本）
- human_paste — 模拟粘贴（长文本）
- send_message — 发送消息并等待回复
- _wait_for_response — 等待模型回复完成（稳定性检测）
- _try_recover_page — 页面崩溃自动恢复
"""

import io
import json
import random
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

from config import (
    SESSION_FILE, SITE_CONFIGS,
    HUMAN_MIN_DELAY, HUMAN_MAX_DELAY,
    HUMAN_PAUSE_MIN, HUMAN_PAUSE_MAX,
    HUMAN_CHUNK_MIN, HUMAN_CHUNK_MAX,
    PUNCTUATION_PAUSE, PASTE_THRESHOLD,
)
from chat_adapters import get_site_config, find_and_fill_input, enable_deep_think, switch_mode


def init_browser(url: str, site_id: str = None):
    """初始化浏览器会话（用户手动登录）。"""
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
        try:
            page.bring_to_front()
        except Exception:
            pass

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

        try:
            while browser.pages:
                time.sleep(1)
        except Exception:
            pass
        try:
            browser.close()
        except Exception:
            pass


def connect_via_cdp(port: int = 9222):
    """通过 Chrome DevTools Protocol 连接已有浏览器。
    
    智能选页原则：
    1) 遍历所有 context 的所有 pages，找第一个非空白/非 chrome 内部页面
    2) 如果都是空白/新标签页，新建一个空白 page
    3) 不在此处导航——由 caller（daemon.py）负责导航到目标 URL
    """
    from playwright.sync_api import sync_playwright
    playwright = sync_playwright().start()
    browser = playwright.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
    
    # 找第一个非空白、非 chrome 内部页面
    for context in browser.contexts:
        for page in context.pages:
            if page.url and not page.url.startswith("chrome-extension://") and page.url != "about:blank":
                return playwright, browser, page
    
    # 全是空白/新标签页 → 新建一个 page
    context = browser.contexts[0] if browser.contexts else browser.new_context()
    page = context.new_page()
    return playwright, browser, page


def find_free_cdp_port(start: int = 9222, max_attempts: int = 10):
    """在 start 到 start+max_attempts-1 范围内找空闲 TCP 端口。"""
    for port in range(start, start + max_attempts):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            s.bind(('127.0.0.1', port))
            s.close()
            return port
        except OSError:
            continue
    return None


def launch_edge_with_cdp(port: int = 9222, browser_path: str = None) -> bool:
    """启动 Edge 并开启 CDP 调试端口。返回是否成功。"""
    if browser_path:
        paths = [browser_path]
    else:
        paths = [
            r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
            r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        ]
    for path in paths:
        if Path(path).exists():
            subprocess.Popen(
                [path, f"--remote-debugging-port={port}", "--no-first-run",
                 "--no-default-browser-check", "--new-window"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            return True
    return False


def wait_for_cdp(port: int, timeout: int = 30) -> bool:
    """等待 CDP 端口就绪，带进度提示。返回是否超时。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            s = socket.create_connection(('127.0.0.1', port), timeout=2)
            s.close()
            return True
        except (OSError, ConnectionRefusedError):
            time.sleep(1)
    return False


def _detect_edge_running() -> bool:
    """检测 Edge 进程是否已在运行。"""
    try:
        result = subprocess.run(
            ['tasklist', '/fi', 'IMAGENAME eq msedge.exe'],
            capture_output=True, text=True, timeout=5,
        )
        return 'msedge.exe' in result.stdout
    except Exception:
        return False


def _detect_cdp_port_in_use(port: int = 9222) -> bool:
    """检测指定 CDP 端口是否已被占用（通过 netstat）。"""
    try:
        result = subprocess.run(
            ['netstat', '-ano', '|', 'findstr', f':{port}'],
            capture_output=True, text=True, timeout=5, shell=True,
        )
        return f':{port}' in result.stdout and 'LISTENING' in result.stdout
    except Exception:
        return False


def get_existing_page(playwright, session):
    """复用持久化浏览器上下文（3 次重试 + profile 自修复）。"""
    profile_dir = session.get("profile_dir", str(Path.home() / ".web_chat_profile"))
    last_error = None
    for attempt in range(3):
        try:
            browser = playwright.chromium.launch_persistent_context(
                user_data_dir=profile_dir, headless=False,
                args=["--no-sandbox", "--disable-gpu"],
                viewport={"width": 1280, "height": 900},
            )
            page = browser.pages[0] if browser.pages else browser.new_page()
            if page.url == "about:blank" or session["url"] not in page.url:
                page.goto(session["url"], wait_until="domcontentloaded", timeout=30000)
            return browser, page
        except Exception as e:
            last_error = e
            sys.stderr.write(f"[Retry {attempt+1}/3] launch_persistent_context failed: {e}\n")
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


def human_type(page, input_el, text: str):
    """模拟人类打字：可变延迟、段落间歇、标点停顿。
    用 page.keyboard.press() 逐字输入，兼容 React/Vue 受控组件的 onChange 监听。
    """
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
            try:
                page.keyboard.insert_text(char)
            except Exception:
                pass

        delay = random.uniform(HUMAN_MIN_DELAY, HUMAN_MAX_DELAY)
        if char in '，。！？；：、—…""''）》》《' or char in ',.!?;:':
            delay += PUNCTUATION_PAUSE
        if i >= next_pause_at:
            time.sleep(random.uniform(HUMAN_PAUSE_MIN, HUMAN_PAUSE_MAX))
            next_pause_at = i + random.randint(HUMAN_CHUNK_MIN, HUMAN_CHUNK_MAX)
        time.sleep(delay)

    time.sleep(random.uniform(0.3, 0.8))
    sys.stderr.write(f"[输入] {total} 字符逐字输入完成\n")


def human_paste(page, input_el, config, text: str):
    """模拟人类粘贴：使用 document.execCommand('insertText') 避免浏览器权限问题。"""
    try:
        input_el.click()
        time.sleep(random.uniform(0.3, 0.6))
    except Exception:
        pass
    
    try:
        first_sel = config["input_selector"].split(", ")[0]
        page.evaluate("""
            (sel, text) => {
                const el = document.querySelector(sel);
                if (!el) return;
                el.focus();
                document.execCommand('selectAll', false, null);
                document.execCommand('insertText', false, text);
            }
        """, [first_sel, text])
    except Exception:
        try:
            page.keyboard.insert_text(text)
        except Exception:
            pass
    
    time.sleep(random.uniform(0.5, 1.0))
    sys.stderr.write(f"[输入] {len(text)} 字符粘贴完成\n")


def send_message(page, config, text: str):
    """在指定的浏览器页面中发送消息并等待模型回复。"""
    input_el = find_and_fill_input(page, config)
    if input_el is None:
        return {"error": "找不到输入框"}

    pre_delay = random.uniform(1.0, 3.0)
    sys.stderr.write(f"[等待] 发送前等待 {pre_delay:.1f} 秒...\n")
    time.sleep(pre_delay)

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

    # 响应稳定性检测：连续 3 秒文字不变才判定完成
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

    ai_done = "内容由 AI 生成" in response_text or "AI 生成" in response_text
    return {"sent": text, "response": response_text, "timestamp": time.time(), "ai_done": ai_done}


def _wait_for_response(page, config, sent_text: str):
    """等待模型回复完成并提取文本（共享逻辑）。"""
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
    
    ai_done = "内容由 AI 生成" in response_text or "AI 生成" in response_text
    return {"sent": sent_text, "response": response_text, "timestamp": time.time(), "ai_done": ai_done}


def _try_recover_page(daemon_browser, daemon_page, daemon_session, daemon_playwright):
    """尝试恢复已关闭的浏览器页面。"""
    if daemon_session is None:
        return False, daemon_browser, daemon_page, daemon_playwright
    try:
        if daemon_browser and daemon_browser.contexts:
            ctx = daemon_browser.contexts[0]
            pages = ctx.pages
            if pages:
                daemon_page = pages[0]
            else:
                daemon_page = ctx.new_page()
            daemon_page.goto(daemon_session["url"], wait_until="domcontentloaded", timeout=30000)
            sys.stderr.write(f"[Daemon] 页面已恢复 (复用已有浏览器)\n")
            return True, daemon_browser, daemon_page, daemon_playwright
        
        if daemon_browser:
            try:
                daemon_browser.close()
            except Exception:
                pass
            daemon_browser = None
        if daemon_playwright:
            try:
                daemon_playwright.stop()
            except Exception:
                pass
            daemon_playwright = None
        
        from playwright.sync_api import sync_playwright
        daemon_playwright = sync_playwright().start()
        daemon_browser, daemon_page = get_existing_page(daemon_playwright, daemon_session)
        site_id = daemon_session.get("site_id", "custom")
        if site_id == "deepseek":
            try:
                switch_mode(daemon_page, "expert")
                enable_deep_think(daemon_page)
            except Exception:
                pass
        sys.stderr.write(f"[Daemon] 浏览器已完全重建\n")
        return True, daemon_browser, daemon_page, daemon_playwright
    except Exception as e:
        sys.stderr.write(f"[Daemon] 浏览器恢复失败: {e}\n")
        return False, daemon_browser, daemon_page, daemon_playwright